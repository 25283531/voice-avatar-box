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
 *   POST /api/device/sync?fw_version=<ver>   Bearer <device_token>
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
    uint32_t    interval_sec;  /* 心跳周期；服务端下发后以服务端为准 */
} elder_sync_cfg_t;

/**
 * @brief 初始化并启动同步任务（任务内部自管退避，离线不阻塞任何业务）
 */
esp_err_t elder_sync_init(const elder_sync_cfg_t *cfg);
esp_err_t elder_sync_start(void);

/** 运行时更新令牌（激活完成后调用） */
esp_err_t elder_sync_set_token(const char *token);

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
 * @brief 用后端下发的 server_time（ISO8601，带 +08:00 偏移）校准本地时钟。
 *        同时把 TZ 固定为 Asia/Shanghai，保证与后端规则一致。
 */
esp_err_t elder_time_apply_server_time(const char *iso);

/** 是否已至少校准过一次（未校准时提醒仍走本地 RTC，可能不准） */
bool elder_time_synced(void);

/** 把 time_t 格式化为 ISO8601 本地字符串（+08:00），用于上报 due_at */
void elder_time_iso_local(time_t t, char *out, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_SYNC_H */
