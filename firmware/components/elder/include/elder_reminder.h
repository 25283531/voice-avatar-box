#ifndef ELDER_REMINDER_H
#define ELDER_REMINDER_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 离线日程 / 服药提醒引擎（需求 8 / 9 / 11）
 *
 * 设计要点：
 *  - 提醒完全离线可用：日程表存 NVS，时间来自 RTC（离线必须外挂 DS3231）
 *  - 每条提醒有独立状态机：IDLE → PENDING → (CONFIRMED | ESCALATED)
 *  - 同一天未完成则红灯长亮；确认完成转绿灯
 *  - 断网不影响提醒本身，只影响「向子女端推送」——推送会入队，联网后补发
 */

#define ELDER_REMINDER_MAX      16
#define ELDER_REMINDER_LABEL    31

typedef enum {
    ELDER_RMD_IDLE = 0,      /* 未到时间 */
    ELDER_RMD_PENDING,       /* 已触发，等待确认 */
    ELDER_RMD_CONFIRM_WIN,   /* 已长按，等待 30s 内短按 */
    ELDER_RMD_DONE,          /* 已确认完成 */
    ELDER_RMD_ESCALATED      /* 超时升级（持续告警） */
} elder_rmd_state_t;

typedef enum {
    ELDER_RMD_TYPE_SCHEDULE = 0,  /* 日程 */
    ELDER_RMD_TYPE_MEDICINE       /* 服药 */
} elder_rmd_type_t;

typedef struct {
    uint8_t  id;
    uint8_t  hour;
    uint8_t  minute;
    uint8_t  type;                       /* elder_rmd_type_t */
    uint8_t  repeat_mask;                /* bit0=周日 ... bit6=周六 */
    bool     enabled;
    char     label[ELDER_REMINDER_LABEL + 1];
} elder_rmd_item_t;

typedef struct {
    elder_rmd_item_t items[ELDER_REMINDER_MAX];
    uint8_t          count;
    /* 今日完成状态：bit i = 1 表示 items[i] 今日已完成 */
    uint16_t         done_today;
    uint16_t         pending_today;
    int32_t          last_day_ordinal;   /* 跨天检测用 */
    uint32_t         crc32;              /* NVS 完整性校验 */
} elder_rmd_table_t;

typedef void (*elder_rmd_cb_t)(const elder_rmd_item_t *item,
                               elder_rmd_state_t new_state, void *ctx);

/**
 * @brief 初始化：读 NVS → 起 tick 任务（1s 轮询）
 */
esp_err_t elder_reminder_init(elder_rmd_cb_t cb, void *ctx);

esp_err_t elder_reminder_add(const elder_rmd_item_t *item);
esp_err_t elder_reminder_del(uint8_t id);
esp_err_t elder_reminder_set_enabled(uint8_t id, bool enabled);
esp_err_t elder_reminder_list(const elder_rmd_table_t **out);

/**
 * @brief 标记某条提醒已完成（语音确认 / 按键确认 / APP 远程确认都走这里）
 */
esp_err_t elder_reminder_confirm(uint8_t id);

/**
 * @brief 打开确认窗口（按键长按后由 input 事件驱动）
 */
void elder_reminder_arm_confirm(uint8_t id);

/**
 * @brief 当前是否有「服药未完成」——决定红灯是否长亮
 */
bool elder_reminder_has_pending_medicine(void);

/**
 * @brief 获取下一条待触发提醒距现在的秒数（-1 表示无）
 */
int32_t elder_reminder_next_in_sec(void);

/**
 * @brief 强制落盘（默认写 NVS 有去抖，避免频繁擦写）
 */
esp_err_t elder_reminder_flush(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_REMINDER_H */
