#ifndef ELDER_RGB_H
#define ELDER_RGB_H

#include <stdint.h>
#include <stdbool.h>   /* elder_rgb_button_pending(bool) 的形参类型 */
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * RGB 灯效优先级仲裁器
 *
 * 灯带共 9 颗**受控**（见 elder_board.h 的 ELDER_RGB_LEDS；物理上是 20 颗，
 * #10~#20 每帧显式写 0 保持熄灭，见 ELDER_RGB_CHAIN）：
 *   #1       按键指示灯（独立状态机，见本文件下半部分）
 *   #2 ~ #9  对话/提醒灯效（本文件上半部分）
 *
 * 两路**完全分开渲染**：提醒在 #2~#9 上闪，不会把按键反馈盖掉；
 * 反过来按键呼吸也不会污染提醒的颜色语义。优先级只作用于 #2~#9。
 */

typedef enum {
    ELDER_RGB_PRIO_IDLE      = 0,  /* 呼吸/待机 */
    ELDER_RGB_PRIO_EMOTION   = 10, /* 对话状态（可被提醒打断） */
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
    ELDER_FX_RAINBOW,   /* 整条灯带同步变色（旧的"五彩"） */
    ELDER_FX_FLOW       /* **低速流水五彩**：彩虹沿灯带逐颗流动 */
} elder_rgb_fx_t;

/* ---------------------------------------------------------------- 对话四态
 *
 * 2026-10-27 用户重新定义的灯效（原话见 docs/决策记录.md D59）。
 * 四个状态**互斥**，共用 ELDER_RGB_PRIO_EMOTION 一个槽位 —— 切换即替换，
 * 不会留下过期槽位把新状态顶掉。
 *
 * 为什么亮度要单独传、而不是把颜色预先调暗：
 *   呼吸和流水都是**动态**算出来的（呼吸会从 0 涨到峰值、流水的色相一直在
 *   变），颜色值预先乘 0.3 之后再叠加动态亮度，峰值就变成 0.3×255×动态值，
 *   实测峰值只有约 9% —— 看起来像灯坏了。所以亮度必须是"算完之后统一乘"。
 */
typedef enum {
    ELDER_RGB_UI_IDLE = 0,   /* 待机：天蓝 #87CEFA，30% 亮度，呼吸 */
    ELDER_RGB_UI_LISTENING,  /* 唤醒后收听：深天蓝 #00BFFF，55%，常亮 */
                             /* （原 #0000FF/100%，2026-10-27 用户实报刺眼） */
    ELDER_RGB_UI_CONNECTING, /* 数据上传/等回复：五彩流水，80% */
    ELDER_RGB_UI_SPEAKING    /* TTS 输出：深橙 #FF8C00，100%，常亮 */
} elder_rgb_ui_t;

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
 * @brief 同上，但可以指定亮度等级。
 * @param level 0~255（255 = 100%）。**不是** 0~100，别按百分数传。
 */
esp_err_t elder_rgb_request_ex(elder_rgb_prio_t prio, elder_rgb_fx_t fx,
                               uint32_t color, uint8_t level, uint32_t ttl_ms);

/**
 * @brief 释放之前申请的灯效，回落到次高优先级的请求
 */
void elder_rgb_release(elder_rgb_prio_t prio);

/**
 * @brief 设置对话四态灯效（推荐入口，语义见 elder_rgb_ui_t）
 */
void elder_rgb_set_ui_state(elder_rgb_ui_t st);

/**
 * @brief 设置对话情绪（内部映射到 ELDER_RGB_PRIO_EMOTION）
 *
 * NEUTRAL/LISTENING/SPEAKING 三个会转成对应的 UI 四态；
 * HAPPY/SAD/ANGRY 保留原来的颜色，100% 常亮。
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
 * 2026-10-27 用户原话（重新定义，覆盖 10-26 的"暖琥珀呼吸"版本）：
 *
 *   「第一颗LED一般情况下不亮，只有需要按键时才亮。当有待确认的日程提醒
 *     （服药）时，为深橙色 #FF8C00 高亮（100%）呼吸灯效。当用户按下确认
 *     按键时，从浅绿色 #90EE90 向深绿色 #006400 转变，转变时长为按钮定义
 *     的需要按键时长。确认完成时以深蓝绿色 #00CED1 闪烁三次结束。如果按键
 *     中途松开，没有完成确认，则以深粉红色 #FF1493 闪烁5次结束。」
 *
 * 四个状态（互斥）：
 *   ① 熄灭      —— 没有待确认提醒，也没在按。**这是常态**，夜里不发光。
 *   ② 橙色呼吸  —— 有待确认提醒。作用是"吸引注意"：老人听到提醒后要知道
 *                  该按哪个键，灯亮着的那颗就是。
 *   ③ 绿→深绿   —— 按住期间。渐变本身是**进度条**：老人能看出"还差多少"，
 *                  比一个突然亮起的"完成"更不容易半途松手。
 *   ④ 结果闪烁  —— 深蓝绿 3 次 = 确认成功；深粉红 5 次 = 中途松开没成。
 *
 * 为什么"没待确认提醒时按下什么都不亮"（而不是给个"按到了"的反馈）：
 *   本按键只做确认。没有可确认的东西时给正反馈，等于告诉老人"办成了"，
 *   而实际什么都没发生 —— 这是最坏的一类欺骗。安静地不亮才是诚实的。
 *
 * ⚠️ 渐变时长必须与 input.c 的长按判定**同源**：两边都用
 *    elder_board.h 的 ELDER_BTN_CONFIRM_HOLD_MS。各写一份的话，只要有人
 *    改了一处，就会出现"灯已经过渡到最深绿、按键却还没算确认"（或反过来）
 *    —— 灯在骗人，比没有灯更糟。
 */

/** 按下（不管当时有没有可确认的提醒；没有就保持熄灭） */
void elder_rgb_button_press(void);

/** 松开。仍在渐变中 ⇒ 判定为"中途松开没确认"，粉红闪 5 次。
 *  已经出过结果（confirmed 或已闪烁）⇒ 忽略，不会重复反馈。 */
void elder_rgb_button_release(void);

/** 确认完成 ⇒ 深蓝绿闪 3 次 */
void elder_rgb_button_confirmed(void);

/** 是否存在待确认的提醒 ⇒ #1 橙色呼吸 / 熄灭。
 *  由 reminder.c 在提醒状态集合变化时调用，是"吸引注意"态的唯一真源。 */
void elder_rgb_button_pending(bool pending);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_RGB_H */
