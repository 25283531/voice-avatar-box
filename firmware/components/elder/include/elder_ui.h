#ifndef ELDER_UI_H
#define ELDER_UI_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 形象（EAF 序列帧）与提示音的**抽象层**。
 *
 * elder 组件本身是纯 C / 纯 ESP-IDF 的，不认识 xiaozhi 的 Display / Application，
 * 也就不认识 esp_emote_gfx。真正的播放动作由上层（main/elder_bridge.cc）
 * 通过 elder_ui_set_hooks() 注入。好处：
 *   - 组件可脱离 xiaozhi 单独编译（firmware/ 下的自测工程就是这么用的）
 *   - 换屏幕 / 换形象引擎只需替换 bridge，业务代码不动
 *
 * EAF 素材来自 esp_emote_gfx（managed component，由 IDF 组件管理器拉取），
 * 立创板可用的情绪名见 main/boards/lichuang-dev/emote.json：
 *   happy / laughing / funny / loving / embarrassed / confident / delicious /
 *   sad / crying / sleepy / silly / angry / surprised / shocked / thinking /
 *   winking / relaxed / confused / neutral / idle
 */

typedef enum {
    ELDER_UI_IDLE = 0,      /* 待机：neutral（不循环，静止） */
    ELDER_UI_LISTENING,     /* 聆听：thinking */
    ELDER_UI_SPEAKING,      /* 说话：happy */
    ELDER_UI_REMINDER,      /* 提醒触发：shocked（醒目） */
    ELDER_UI_CONFIRMED,     /* 已确认：happy */
    ELDER_UI_MISSED,        /* 超时未确认：angry */
    ELDER_UI_ALARM,         /* 求助/告警：angry */
    ELDER_UI_OFFLINE,       /* 离线：sleepy */
    ELDER_UI_ACTIVATING     /* 待绑定：winking */
} elder_ui_state_t;

/* 本地提示音（离线兜底用，全部来自固件内置 ogg，无需联网） */
typedef enum {
    ELDER_TONE_NONE = 0,
    ELDER_TONE_POPUP,        /* 通用弹窗提示 —— 提醒触发 */
    ELDER_TONE_EXCLAMATION,  /* 感叹 —— 升级告警 */
    ELDER_TONE_SUCCESS,      /* 成功 —— 确认完成 */
    ELDER_TONE_VIBRATION,    /* 震动音 —— 求助 */
    ELDER_TONE_LOW_BATTERY,
    /* 提醒专用音乐（约 4.9 秒，固件内置固定资源，见
     * tools/make_reminder_chime.py）。与 POPUP 那种"叮咚"分开的原因：
     * 提醒要先响一段音乐把老人叫过来，再念事项内容，两声必须能分别控制。 */
    ELDER_TONE_REMINDER
} elder_tone_t;

typedef struct {
    /* 切换 EAF 形象；name 为情绪名（见上表）。可为 NULL（无屏设备） */
    void (*set_emotion)(const char *name, void *ctx);

    /* 屏显一条通知；emotion 可为 NULL 表示沿用当前形象。可为 NULL */
    void (*notify)(const char *text, const char *emotion, void *ctx);

    /* 播放本地提示音；离线时必须仍然可用。可为 NULL */
    void (*play_tone)(elder_tone_t tone, void *ctx);

    /* 联网时的语音播报（TTS）；离线实现应直接忽略或降级为提示音。可为 NULL */
    void (*speak)(const char *text, void *ctx);

    /* 查询当前是否联网（离线时 speak 不可用，应改用 play_tone）。可为 NULL */
    bool (*is_online)(void *ctx);

    /* 全屏显示一条提醒内容（需求 8：图片不再显示，用较大字体全屏显示）。
     * text 为任务内容（如"该吃降压药了"），由实现方决定字号与排版。
     * 可为 NULL —— 无屏设备上是空操作，业务代码不必分支。 */
    void (*show_reminder)(const char *text, void *ctx);

    /* 收掉全屏提醒，恢复正常界面。可为 NULL */
    void (*hide_reminder)(void *ctx);

    void *ctx;
} elder_ui_hooks_t;

/**
 * @brief 注入播放实现。可在任意时刻调用，未注入的钩子被安全忽略。
 */
void elder_ui_set_hooks(const elder_ui_hooks_t *hooks);

/**
 * @brief 切换形象状态。内部会去重（相同状态不重复下发），
 *        并在计时器到期后自动回落到 IDLE，避免一直停在「提醒」表情。
 * @param st        目标状态
 * @param hold_ms   保持时长；0 表示常驻直到下一次显式切换
 */
esp_err_t elder_ui_set_state(elder_ui_state_t st, uint32_t hold_ms);

/** 当前状态 */
elder_ui_state_t elder_ui_get_state(void);

/** 状态 → 情绪名（供上层调试 / 直接调用 SetEmotion 用） */
const char *elder_ui_emotion_name(elder_ui_state_t st);

/** 状态 → 对应提示音 */
elder_tone_t elder_ui_tone_of(elder_ui_state_t st);

/**
 * @brief 播报一条提醒：在线走 TTS，离线自动降级为本地提示音 + 屏显。
 *        这是「离线兜底」的对外统一入口。
 */
void elder_ui_announce(const char *text, elder_tone_t tone);

/**
 * @brief 只**念**（TTS），不刷屏显。
 *
 * 为什么单独开一个入口（2026-10-22 需求）：
 * 提醒流程是「先响固定音乐 4.9s，再念事项内容」。announce() 会先调 notify
 * 刷一次屏显 —— 而刷屏显要拿 LVGL 锁、要重排整屏，实测卡掉 1.5 秒，
 * 正好卡在音乐结束到 TTS 开始之间，于是老人听到的间隔被撑到 2 秒以上
 * （用户原话「有 1 秒的间隔就够了」）。
 *
 * 提醒场景下屏显早就由 elder_ui_show_reminder() 做过了，这里再刷一次纯属
 * 重复劳动 —— 所以把「念」和「显示」拆开，音乐结束那一刻直接发 TTS。
 */
void elder_ui_speak(const char *text);

/** 直接播放本地提示音（离线兜底） */
void elder_ui_play_tone(elder_tone_t tone);

/**
 * @brief 只屏显一条通知，**不发声**（不发 TTS、不播提示音）。
 *
 * 与 elder_ui_announce 的区别：announce 是「有事要告诉老人，得让他听见」，
 * notice 是「状态提示，安静地说」。典型用途：时钟未校准 —— 这时设备可能
 * 完全离线，播提示音会吓人一跳，但必须让老人/子女知道「今天不会提醒」。
 *
 * @param text     显示文本；为空则忽略
 * @param emotion  可选形象名（见文件头列表），NULL 表示沿用当前形象
 */
void elder_ui_notice(const char *text, const char *emotion);

/**
 * @brief 内部计时器 tick；由本组件自有任务驱动，外部无需调用。
 */
void elder_ui_tick(void);

/* ------------------------------------------------- 需求 8：全屏提醒
 *
 * 用户 2026-10-22 原话：「屏幕的图片不再显示，用较大的字体在屏幕上全屏
 * 显示任务内容。在用户没有进行语音确认任务完成，或者按键确认任务完成，
 * 就持续显示任务内容。持续时间 4 个小时。」
 *
 * 三层分工（**别把计时往上层搬**）：
 *   * 上层（elder_bridge）只在「到点」调一次 show，「确认完成」调一次 hide；
 *   * 4 小时到点由本组件的 elder_ui_tick() 负责收掉 —— 让调用方自己记得
 *     "4 小时后关掉"，就必然会有忘记关的那一次（手机屏保同理）；
 *   * 具体怎么画（字号、是否隐藏图片）是 hooks.show_reminder 的事，
 *     组件本身不认识 LVGL。
 */

/**
 * @brief 全屏显示一条提醒，并开始 4 小时倒计时。
 *
 * 重复调用是安全的：内容会被替换、倒计时**重新**开始 4 小时
 * （同一条提醒每 10 分钟还会闪灯闪 2 小时，但那不发这条）。
 */
esp_err_t elder_ui_show_reminder(const char *text);

/** 收掉全屏提醒（确认完成 / 按键确认 / 4 小时到点都会走这里） */
void elder_ui_hide_reminder(void);

/** 当前是否正在全屏显示提醒（上层据此判断"这一下短按该不该当成确认"） */
bool elder_ui_reminder_showing(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_UI_H */
