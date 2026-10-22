#ifndef ELDER_RGB_H
#define ELDER_RGB_H

#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * RGB 灯效优先级仲裁器
 * 需求 11（服药未确认长亮红灯 / 完成后绿灯）与需求 12（灯效随对话情绪）
 * 会争抢同一颗灯，必须定优先级，否则现场表现不可预测。
 */

typedef enum {
    ELDER_RGB_PRIO_IDLE      = 0,  /* 呼吸/待机 */
    ELDER_RGB_PRIO_EMOTION   = 10, /* 对话情绪（可被提醒打断） */
    ELDER_RGB_PRIO_REMINDER  = 50, /* 日程提醒中 */
    ELDER_RGB_PRIO_MED_PEND  = 90, /* 服药未确认 —— 除升级外最高 */
    ELDER_RGB_PRIO_ALARM     = 100 /* 求助/告警 */
} elder_rgb_prio_t;

typedef enum {
    ELDER_EMO_NEUTRAL = 0,
    ELDER_EMO_HAPPY,
    ELDER_EMO_SAD,
    ELDER_EMO_ANGRY,
    ELDER_EMO_LISTENING,
    ELDER_EMO_SPEAKING
} elder_emotion_t;

typedef enum {
    ELDER_FX_SOLID = 0,
    ELDER_FX_BREATH,
    ELDER_FX_BLINK_FAST,
    ELDER_FX_RAINBOW
} elder_rgb_fx_t;

/**
 * @brief 初始化 RMT + WS2812 灯带，并启动灯效任务
 */
esp_err_t elder_rgb_init(void);

/**
 * @brief 请求某种灯效。优先级低的请求不会抢占高的。
 * @param prio   优先级，见 elder_rgb_prio_t
 * @param fx     灯效
 * @param color  0xRRGGBB
 * @param ttl_ms 持续时间；0 表示常驻直到被释放
 */
esp_err_t elder_rgb_request(elder_rgb_prio_t prio, elder_rgb_fx_t fx,
                            uint32_t color, uint32_t ttl_ms);

/**
 * @brief 释放之前申请的灯效，回落到次高优先级的请求
 */
void elder_rgb_release(elder_rgb_prio_t prio);

/**
 * @brief 设置对话情绪（内部映射到 ELDER_RGB_PRIO_EMOTION）
 */
void elder_rgb_set_emotion(elder_emotion_t emo);

/* 需求 11 的语义封装 */
void elder_rgb_med_pending(void);   /* 长亮红 */
void elder_rgb_med_done(void);      /* 长亮绿（默认 60s 后回落） */
void elder_rgb_med_escalate(void);  /* 快速闪烁红 */

/* ------------------------------------------------- 需求 8：提醒的闪灯节奏
 *
 * 用户 2026-10-22 原话：「定时任务提醒时，应该红灯闪烁（闪烁10秒，
 * 10分钟后再闪烁10秒，2小时内以此重复。2小时后不再亮灯。）」
 *
 * 为什么节奏放在**灯效模块内部**而不是让上层每 10 分钟调一次 request：
 *   1. 上层要自己做这件事就必须再起一个任务或定时器 —— 本仓已经因为
 *      "回调跑在 esp_timer 任务里、栈只有 3.5KB" 出过随机 LoadProhibited
 *      （见 elder_bridge.cc 的 ScheduleAnnounce 注释），能不加任务就不加；
 *   2. rgb_task 本来就每 30ms 醒一次，判断"现在是不是该闪"是零成本的；
 *   3. 节奏只有一份真源。上层若另算一遍，"2 小时后关闭"就有两个判据，
 *      迟早分叉成"灯还闪、界面已经没了"。
 *
 * 语义：
 *   * nag_start() 从**调用时刻**起算 2 小时，每 10 分钟的前 10 秒红灯闪；
 *   * 2 小时到点后灯自然熄灭（不需要上层记得来关）；
 *   * 期间若老人确认完成（med_done）或出现更高优先级的灯效
 *     （ELDER_RGB_PRIO_MED_PEND 及以上，例如求助告警），nag 让位；
 *   * nag_stop() 立即停（确认完成时调）。
 */
void elder_rgb_reminder_nag_start(void);
void elder_rgb_reminder_nag_stop(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_RGB_H */
