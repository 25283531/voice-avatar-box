/* freertos/task.h —— 宿主测试桩。
 *
 * 关键设计：xTaskCreatePinnedToCore **不真的启动任务**。
 * reminder_task() 是死循环，真跑起来测试就永远收不到控制权；
 * 测试改为手动调用 elder_reminder_tick_once() 驱动状态机。
 * 这与固件行为等价：固件里 tick() 由 1s 周期任务调用。 */
#ifndef STUB_FREERTOS_TASK_H
#define STUB_FREERTOS_TASK_H

#include "freertos/FreeRTOS.h"

/* 收到任务函数指针后**丢弃**，不启动 —— 避免死循环抢占测试控制权 */
BaseType_t stub_task_create(void (*fn)(void *));
void vTaskDelay(TickType_t ticks);

#define xTaskCreatePinnedToCore(fn, ...) stub_task_create(fn)
#define xTaskCreate(fn, ...)             stub_task_create(fn)

#endif