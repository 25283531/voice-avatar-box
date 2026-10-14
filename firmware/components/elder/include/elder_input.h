#ifndef ELDER_INPUT_H
#define ELDER_INPUT_H

#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 服药/提醒确认输入：单键三段式手势
 *   长按 >=3s  → 设备提示「松开后，30 秒内再短按一次确认」
 *   松开       → 进入 30s 确认窗口
 *   窗口内短按 → 确认完成
 *   窗口超时   → 回到未确认态（红灯保持）
 *
 * 设计约束：立创板 BOOT(GPIO0) 上电 3s 内长按会进 USB 下载模式，
 * 因此使能时间由 ELDER_KEY_BOOT_GUARD_MS 控制，由本模块内部延迟使能。
 */

typedef enum {
    ELDER_KEY_EV_NONE = 0,
    ELDER_KEY_EV_SHORT,        /* 单击 */
    ELDER_KEY_EV_LONG_HOLD,    /* 长按达标（仍按住） */
    ELDER_KEY_EV_RELEASE,      /* 长按后松开 */
    ELDER_KEY_EV_SOS           /* 长按 >=8s：求助（与确认手势区分） */
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
