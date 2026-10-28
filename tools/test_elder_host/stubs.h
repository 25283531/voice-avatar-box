/* stubs.h —— 宿主测试的受控环境入口。 */
#ifndef STUBS_H
#define STUBS_H

#include <stdint.h>
#include <time.h>

/* 本头文件自己的原型就用到了 ESP-IDF 的类型（esp_err_t / BaseType_t /
 * TickType_t），所以必须把它们的宿主替身一起带上。
 * 漏掉这一步的后果很隐蔽：test_elder_host.c 的**第一个** #include 就是本文件，
 * 此时 esp_err.h 与 freertos 下的两个头都还没被包含，于是 BaseType_t 之类
 * 全部未定义，编译直接失败 —— 而这个 Makefile 从未在 CI 上跑过，所以没人发现过。 */
#include "esp_err.h"
#include "freertos/task.h"   /* 连带提供 freertos/FreeRTOS.h 里的 BaseType_t */

#define NVS_NS_MAX  2
#define NVS_BLOB_MAX 4096

/* NVS：初始化一次（模拟上电），reset 清空（模拟换 Flash） */
void stub_nvs_init(void);
void stub_nvs_reset(void);

/* 把已落盘 blob 里的某个字节翻转（异或 mask），长度不变。
 * 唯一的用途是让 reminder.c 的 CRC 校验分支**可达**：正常流程写进去的
 * 永远 CRC 自洽，不刻意写坏就读不到「NVS 校验」这条路径 —— 而它恰恰是
 * 断网设备最该守住的一条（把坏数据当成有效提醒表 = 老人照着乱码时间吃药）。*/
void stub_nvs_corrupt(int ns_index, size_t offset, uint8_t xor_mask);

/* 直接往 NVS 塞一个**指定长度**的 blob（绕过 elder_rminder_flush）。
 * 唯一用途是模拟「设备上已经存着旧固件写下的 blob」—— 升级兼容路径
 * 只有这样才测得到：正常流程写进去的永远是当前格式。 */
void stub_nvs_write_raw(int ns_index, const void *buf, size_t len);

/* 读回落盘的 blob 长度（判断「固件有没有把自己的表改写成新格式」）；
 * 返回 0 表示该 namespace 里没有 blob。 */
size_t stub_nvs_blob_len(int ns_index);

/* 受控时钟：所有 time() 与 esp_timer 都由它推导 */
void stub_time_set(time_t epoch);
void stub_time_advance(int64_t sec);
time_t stub_time_now(void);

/* 副作用计数器 */
int  stub_tone_count(void);
int  stub_rgb_count(void);
/* 三种灯分开计数。合并成一个总数时，「红灯升级」与「红灯待确认」
 * 无法区分 —— 而「漏服 15 分钟后升级成红灯告警」正是必须钉死的医疗级行为。*/
int  stub_rgb_pending_count(void);
int  stub_rgb_escalate_count(void);
int  stub_rgb_done_count(void);
/* 漏服后的闪烁提醒次数（elder_rgb_reminder_nag_start）。 */
int  stub_rgb_nag_count(void);
int  stub_rgb_nag_stop_count(void);
/* 「只屏显不发声」的通知次数（elder_ui_notice）。与 tone 分开计：
 * 未校时提示必须**看得见但不响**，两者混在一起就测不出这条区分。 */
int  stub_notice_count(void);
void stub_fx_reset(void);

/* 任务创建被丢弃（不启动死循环） */
BaseType_t stub_task_create(void (*fn)(void *));
void vTaskDelay(TickType_t ticks);

/* esp_err_to_name 的桩实现 */
const char *esp_err_to_name(esp_err_t code);

#endif