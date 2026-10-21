#ifndef ELDER_SYNC_H
#define ELDER_SYNC_H

#include <stdint.h>
#include <stdbool.h>
#include <time.h>
#include "esp_err.h"

#include "elder_reminder.h"
#include "elder_ui.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 与业务后端（FastAPI, server/）的提醒同步。
 *
 * 对齐的接口（见 server/app/routers/device_api.py）：
 *   POST /api/device/sync?fw_version=<ver>&hw_model=<chip>
 *        Bearer <device_token>
 *        → { server_time, device{...}, avatar{...},
 *            reminders:[ReminderOut], upcoming:[EventOut],
 *            providers:[...], ws_url, ota_url, sync_interval_sec }
 *   POST /api/device/events                  Bearer <device_token>
 *        ← { reminder_id, due_at, state, confirm_method, fired_at, confirmed_at, note }
 *   POST /api/device/interactions            Bearer <device_token>
 *        ← { kind, ts, duration_ms, meta }
 *
 * 状态枚举（server/app/models.py）：
 *   EventState  PENDING=0 FIRED=1 CONFIRMED=2 MISSED=3 SKIPPED=4
 *   ConfirmMethod NONE=0 VOICE=1 BUTTON=2 APP=3 AUTO=4
 *   InteractionKind WAKE=0 DIALOG=1 REMINDER_CONFIRM=2 REMINDER_MISS=3 SOS=4 TOUCH=5
 *
 * 离线策略（核心需求）：
 *   1. 拉取失败 → 本地 NVS 中的提醒表继续按 RTC 触发，功能不受影响；
 *   2. 断网期间产生的回执写入**持久化队列**（NVS），联网后逐条补发；
 *   3. 网络请求全部走 elder_net 的硬超时 + 指数退避，绝不死等。
 */

#define ELDER_SYNC_QUEUE_MAX  32

typedef enum {
    ELDER_EV_PENDING = 0,
    ELDER_EV_FIRED = 1,
    ELDER_EV_CONFIRMED = 2,
    ELDER_EV_MISSED = 3,
    ELDER_EV_SKIPPED = 4
} elder_event_state_t;

typedef enum {
    ELDER_CFM_NONE = 0,
    ELDER_CFM_VOICE = 1,
    ELDER_CFM_BUTTON = 2,
    ELDER_CFM_APP = 3,
    ELDER_CFM_AUTO = 4
} elder_confirm_method_t;

typedef enum {
    ELDER_IT_WAKE = 0,
    ELDER_IT_DIALOG = 1,
    ELDER_IT_REMINDER_CONFIRM = 2,
    ELDER_IT_REMINDER_MISS = 3,
    ELDER_IT_SOS = 4,
    ELDER_IT_TOUCH = 5
} elder_interaction_kind_t;

typedef struct {
    const char *base_url;      /* 形如 http://192.168.1.10:8000 */
    const char *token;         /* device JWT；NULL 表示尚未激活 */
    const char *fw_version;
    const char *hw_model;      /* 硬件型号（如 "ESP32-S3"），上报给服务端设备表的
                                  「硬件型号」列；NULL = 自动探测（esp_chip_info，
                                  见 sync.c 的 chip_model_name） */
    uint32_t    interval_sec;  /* 心跳周期；服务端下发后以服务端为准 */
} elder_sync_cfg_t;

/**
 * @brief 初始化并启动同步任务（任务内部自管退避，离线不阻塞任何业务）
 */
esp_err_t elder_sync_init(const elder_sync_cfg_t *cfg);
esp_err_t elder_sync_start(void);

/** 运行时更新令牌（激活完成后调用） */
esp_err_t elder_sync_set_token(const char *token);

/**
 * @brief 设备是否已绑定（NVS 里持久化过 device token）。
 *
 * ⚠️ 别用 elder_net_has_token() 代替：那个读的是**内存里**的 token，
 *    而开机顺序是 app.Start()（拉起 AudioService/WakeWord）→ 2 秒后
 *    elder_bridge_start()（才 elder_sync_init 并恢复 token）。
 *    在 WakeWord 初始化那一刻问 elder_net_has_token()，**已绑定的设备也会
 *    回答 false**，于是 MultiNet 被误判为「未激活设备才不需要」而永远不起。
 *
 * 本函数直接读 NVS，与初始化顺序无关；结果锁存（一旦 true 就一直 true），
 * 所以可以在音频喂数据的路径上周期性调用，不需要每次都开 NVS。
 */
bool elder_sync_token_persisted(void);

/** 手动触发一次同步（开机 / 按键 / 提醒触发后都可调） */
esp_err_t elder_sync_request_now(void);

/** 同步任务是否已启动 */
bool elder_sync_started(void);

/** 最近一次成功同步的服务器时间戳（0 表示从未成功） */
time_t elder_sync_last_server_time(void);

/** 待补发的离线回执条数 */
int elder_sync_queue_pending(void);

/**
 * @brief 提醒状态变化时由上层转调（app_main / bridge 的 reminder 回调里调用）。
 *        负责：落队 + 尝试立即上报；离线时仅落队。
 */
void elder_sync_on_reminder_state(const elder_rmd_item_t *item, elder_rmd_state_t st);

/** 上报一次交互统计 */
esp_err_t elder_sync_report_interaction(elder_interaction_kind_t kind, int duration_ms);

/**
 * @brief 直接入队一条回执（供非 reminder 模块的调用方使用）
 */
esp_err_t elder_sync_enqueue_event(uint32_t server_id, time_t due_at,
                                   elder_event_state_t state,
                                   elder_confirm_method_t method,
                                   const char *note);

/* ---------------------------------------------------------------- 时间 */

/**
 * @brief 用后端下发的 server_time（ISO8601，带偏移）校准本地时钟。
 *
 * ⚠️ 本函数**只写时钟，不改 TZ**（2026-10-21 改）。时区的归口只有两处：
 * 开机 ``elder_time_tz_restore()``、心跳 ``elder_time_set_tz_offset()``。
 * 原先这里会硬编码 ``setenv("TZ","CST-8")``，会把服务端按设备 IP 下发的
 * 时区在下一次心跳前悄悄覆盖掉。
 */
esp_err_t elder_time_apply_server_time(const char *iso);

/**
 * @brief 设置本机时区偏移（分钟，东正西负）并落 NVS。
 *
 * 由 ``/api/device/sync`` 响应里的 ``tz_offset_minutes`` 驱动。为什么传数值
 * 而不是 IANA 名：newlib 没有 tzdata，``setenv("TZ","America/New_York")``
 * 不报错但 ``tzset()`` 会静默退回 UTC —— 比不设更坏。
 *
 * 为什么必须由服务端下发：规则里的 ``times_of_day`` 是**墙上钟点**，提醒
 * 引擎靠 ``localtime_r()`` 换成绝对时刻。本机 TZ 与规则时区不一致时，
 * "早上八点吃药"会整体错几个小时，而且**没有任何报错**。规则时区是服务端
 * 按设备 IP 判定的，固件判不出来。
 *
 * @param offset_min  偏移分钟数，合法范围 UTC-12 ~ UTC+14，越界返回
 *                    ESP_ERR_INVALID_ARG。
 * @note 固定偏移**不含夏令时规则**：有 DST 的国家在切换那天最多差 1 小时，
 *       下一次心跳（≤ sync_interval_sec，默认 300s）自愈。
 */
esp_err_t elder_time_set_tz_offset(int offset_min);

/**
 * @brief 从 NVS 恢复时区偏移（无记录则 +480 = UTC+8，与改造前逐字等价）。
 *
 * ``elder_sync_init()`` 内部已调用，OTA 校时路径（``ota.cc``）也应调用它
 * 而不是自己 ``setenv("TZ","CST-8")``。
 */
esp_err_t elder_time_tz_restore(void);

/** @brief 当前生效的时区偏移（分钟，东正西负）。 */
int elder_time_tz_offset(void);

/**
 * @brief 开机恢复时钟：DS3231 → NVS 上次校时 epoch → 放弃（等联网）。
 *
 * **必须在 elder_reminder_init() 之前调用**，否则提醒引擎会先用 1970 跑一轮
 * tick，虽然守卫会拦住，但日志里会出现误导性的"时钟不可信"告警。
 *
 * @return ESP_OK 表示时间已恢复；ESP_ERR_NOT_FOUND 表示无可用时间源。
 */
esp_err_t elder_time_restore_boot(void);

/** 是否已至少校准过一次（未校准时提醒仍走本地 RTC，可能不准） */
bool elder_time_synced(void);

/** 把 time_t 格式化为 ISO8601 本地字符串（+08:00），用于上报 due_at */
void elder_time_iso_local(time_t t, char *out, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_SYNC_H */
