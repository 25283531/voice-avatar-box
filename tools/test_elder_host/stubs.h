/* stubs.h —— 宿主测试的受控环境入口。 */
#ifndef STUBS_H
#define STUBS_H

#include <stdint.h>
#include <time.h>

#define NVS_NS_MAX  2
#define NVS_BLOB_MAX 4096

/* NVS：初始化一次（模拟上电），reset 清空（模拟换 Flash） */
void stub_nvs_init(void);
void stub_nvs_reset(void);

/* 受控时钟：所有 time() 与 esp_timer 都由它推导 */
void stub_time_set(time_t epoch);
void stub_time_advance(int64_t sec);
time_t stub_time_now(void);

/* 副作用计数器 */
int  stub_tone_count(void);
int  stub_rgb_count(void);
void stub_fx_reset(void);

/* 任务创建被丢弃（不启动死循环） */
BaseType_t stub_task_create(void (*fn)(void *));
void vTaskDelay(TickType_t ticks);

/* esp_err_to_name 的桩实现 */
const char *esp_err_to_name(esp_err_t code);

#endif