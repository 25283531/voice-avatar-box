#ifndef ELDER_MN_H
#define ELDER_MN_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 离线命令词的**异步执行通道**（esp-sr MultiNet → elder_cmd 分发器）。
 *
 * 为什么要这一层（别当成多余抽象）：
 *   MultiNet 的识别回调跑在音频任务里，而 elder_cmd_dispatch() 会写 NVS
 *   （毫秒级）。直接在回调里同步执行，表现为「老人说完话设备卡一下」，
 *   极端情况丢音频帧。所以这里用有界队列把「听」和「做」拆到两个任务：
 *   音频任务只 memcpy + 入队（微秒级），独立任务再同步执行。
 *
 * 队列满时**丢弃**并返回 false，不阻塞音频任务 ——
 * 宁可漏识别（老人按键兜底，见 elder_input.c），也不能让音频卡顿。
 */

/**
 * @brief 启动命令词执行任务（幂等，重复调用直接返回 ESP_OK）。
 *        由 elder_bridge_start() 调用。
 */
esp_err_t elder_mn_init(void);

/**
 * @brief 投递一条识别到的指令（**非阻塞**，可在音频任务里调用）。
 * @return true=已入队；false=未入队（未初始化 / 参数非法 / 队列已满）。
 */
bool elder_mn_post(const char *phrase);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_MN_H */