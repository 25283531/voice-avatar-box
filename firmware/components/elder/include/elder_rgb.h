#ifndef ELDER_RGB_H
#define ELDER_RGB_H

#include <stdint.h>
#include <stdbool.h>   /* elder_rgb_button_indicator(bool) 的形参类型 */
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

/* ---------------------------------------------- 第 1 颗灯：按键指示灯
 *
 * 灯带共 9 颗（见 elder_board.h 的 ELDER_RGB_LEDS）：#1 专做按键指示灯，
 * #2~#9 才是上面那些灯效。两路互不干扰 —— 提醒在 #2~#9 上闪，不会把
 * 按键反馈盖掉；反过来按键呼吸也不会污染提醒的颜色语义。
 *
 * 为什么按键要有独立的视觉反馈（适老化，2026-10-27 用户要求）：
 *   本机按键只做「确认日程完成」，**按下去不一定有结果**（当前没有待确认
 *   提醒时什么都不做）。老人无法从"没声音"区分「按到了但没事可确认」和
 *   「根本没按到」。给一颗常驻在按键旁边的灯做即时反馈，是把这两件事
 *   分开的唯一办法 —— 灯亮 = 设备收到了。
 *
 * 适老化取值（改动前先读，这三个数字不是随手定的）：
 *   · 颜色用**暖琥珀**：老年人晶状体黄化、蓝光透射率下降，蓝/冷白在
 *     他们眼里又暗又糊；暖色的辨识度和舒适度都更好。
 *   · 呼吸周期 **3 秒**（常规呼吸灯多用 2~2.4 秒）：明暗变化越慢，
 *     眩光与焦虑感越低，也更像"在呼吸"而不是"在闪"。
 *   · 亮度封顶 **约 40%**：夜里起夜按键时不刺眼，也不会把同床的人晃醒。
 *   · 松开后**余辉 2 秒**：一次短按只有一百多毫秒，若严格跟着"按住"的
 *     时长走，老人几乎看不到任何变化，反馈等于没有。
 */
void elder_rgb_button_indicator(bool down);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_RGB_H */
