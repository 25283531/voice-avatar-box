/* esp_heap_caps.h —— 宿主测试桩。
 *
 * reminder.c 的 elder_reminder_replace_all() 把「旧表快照区」放在堆上
 * （见该函数上方的注释：7 份定长数组合计 2.8KB，压在 6KB 的同步任务栈上
 * 会在后端下发 6 条提醒时溢出重启）。宿主测试里没有 ESP-IDF 堆，
 * 这里直接转发到 libc malloc/free，并**记账**：
 *
 *   stub_heap_alloc_count()  累计分配次数
 *   stub_heap_live_count()   当前未释放的块数
 *
 * 记账是为了让「忘记 free」变成一条能断言的失败，而不是一个只在真机上
 * 跑几小时后才显形的内存泄漏。测试在收尾处断言 live == 0。
 */
#ifndef STUB_ESP_HEAP_CAPS_H
#define STUB_ESP_HEAP_CAPS_H

#include <stddef.h>

#define MALLOC_CAP_8BIT   0x00000004
#define MALLOC_CAP_SPIRAM 0x00000400
#define MALLOC_CAP_DEFAULT 0

void *heap_caps_malloc(size_t size, unsigned caps);
void  heap_caps_free(void *ptr);

/* 记账查询（仅宿主测试用，固件里没有这两个符号） */
int  stub_heap_alloc_count(void);
int  stub_heap_live_count(void);
void stub_heap_reset(void);

#endif
