#ifndef ELDER_INPUT_H
#define ELDER_INPUT_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 服药/提醒确认输入：这个按键**只干两件事**（2026-10-20 用户拍板）
 *   1) 服药确认（主用途）
 *        · 提醒响铃期间（ELDER_UI_REMINDER）短按        → 直接确认「已服药」
 *        · 长按 >=3s 后松开 → 30s 窗口内再短按           → 确认（不在响铃时也能用）
 *   2) 没有待确认提醒时，短按复用为「开始/结束对话」
 *
 * ⚠️ SOS（长按 8 秒求助）**已删除**。原因不是功能不好，而是它在这台设备上
 *    有真实危害：长按路径同步跑联网上报，2560 字节的输入任务栈被撑爆
 *    （`***ERROR*** A stack overflow in task elder_input`），设备直接重启，
 *    求助反而发不出去；而聊天机器人场景里「按着说话」比「长按求救」常用得多。
 *
 * 设计约束：立创板 BOOT(GPIO0) 上电 3s 内长按会进 USB 下载模式，
 * 因此使能时间由 ELDER_KEY_BOOT_GUARD_MS 控制，由本模块内部延迟使能。
 * 另外 GPIO0 会被板上自动下载电路拉低（USB 无主机时），
 * 所以本模块只认「先松开、再按下」的边沿，长时间低电平判为卡住并忽略。
 */

typedef enum {
    ELDER_KEY_EV_NONE = 0,
    ELDER_KEY_EV_SHORT,        /* 单击 */
    ELDER_KEY_EV_LONG_HOLD,    /* 长按达标（仍按住） */
    ELDER_KEY_EV_RELEASE       /* 长按后松开 */
} elder_key_event_t;

typedef void (*elder_key_cb_t)(elder_key_event_t ev, void *ctx);

/**
 * @brief 初始化按键检测（内部起一个 FreeRTOS 任务，含消抖）
 * @param cb   事件回调（在任务上下文调用，不要阻塞）
 * @param ctx  透传上下文
 */
esp_err_t elder_input_init(elder_key_cb_t cb, void *ctx);

/**
 * @brief 启动 30 秒确认窗口（由提醒触发方在 ELDER_KEY_EV_RELEASE 后调用）
 * @return true 表示窗口已打开
 */
bool elder_input_open_confirm_window(void);

/**
 * @brief 查询确认窗口是否仍有效
 */
bool elder_input_confirm_window_active(void);

/**
 * @brief 确认完成或取消，关闭窗口
 */
void elder_input_close_confirm_window(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_INPUT_H */
