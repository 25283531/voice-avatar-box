/* esp_timer.h —— 宿主测试桩。微秒时钟由受控的系统时间推导，
 * 这样 confirm 的 30s 窗口可以用「推进时钟」精确触发，不用真的等。 */
#ifndef STUB_ESP_TIMER_H
#define STUB_ESP_TIMER_H

#include <stdint.h>

int64_t stub_timer_us(void);
#define esp_timer_get_time stub_timer_us

#endif