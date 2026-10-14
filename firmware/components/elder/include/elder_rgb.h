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

#ifdef __cplusplus
}
#endif

#endif /* ELDER_RGB_H */
