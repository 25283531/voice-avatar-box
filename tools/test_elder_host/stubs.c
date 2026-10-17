/* stubs.c —— 让 reminder.c / elder_cmd.c 能在宿主机上编译运行的桩。
 *
 * reminder.c 的真实依赖：NVS 持久化、esp_timer 计时、FreeRTOS 任务、
 * RGB 指示灯。这里全部替换为「内存实现」，语义与固件一致到足以验证
 * 断网逻辑 —— 尤其 NVS 必须能跨「重启」保留数据，否则 T1 测的是假东西。
 *
 * 这是一份**桩**，不是模拟器：不模拟并发、不模拟掉电时序，
 * 只保证单线程下的状态语义可测。
 */
#include "stubs.h"

#include "nvs.h"        /* nvs_handle_t —— 与 reminder.c 用的是同一份替身 */
#include "elder_ui.h"   /* elder_tone_t —— elder_ui_* 桩的形参类型 */

#include <stdio.h>
#include <string.h>

/* ---------------- NVS（内存实现，跨"重启"保留） ---------------- */

static uint8_t s_nvs[NVS_NS_MAX][NVS_BLOB_MAX];
static size_t  s_nvs_len[NVS_NS_MAX];
static int     s_nvs_ready;

/* 「上电」：清空持久化数据并置就绪。
 * 刻意**同时清空**数据 —— 各测试用例之间必须互相独立，
 * 否则上一个用例写进 NVS 的提醒表会被下一个用例的
 * elder_reminder_init() 读回来，断言在「表非空」这类地方莫名其妙地过/挂。 */
void stub_nvs_init(void)
{
    memset(s_nvs, 0, sizeof(s_nvs));
    memset(s_nvs_len, 0, sizeof(s_nvs_len));
    s_nvs_ready = 1;
}

void stub_nvs_reset(void)
{
    memset(s_nvs, 0, sizeof(s_nvs));
    memset(s_nvs_len, 0, sizeof(s_nvs_len));
}

int stub_nvs_open(const char *ns, int mode, nvs_handle_t *out)
{
    (void)mode;   /* 本测试只关心读写语义，不区分读写模式 */
    if (!s_nvs_ready) return -1;
    int idx = (strcmp(ns, "elder_rmd") == 0) ? 0 : 1;
    *out = (nvs_handle_t)idx;
    return 0;
}

int stub_nvs_get_blob(nvs_handle_t h, const char *key, void *buf, size_t *len)
{
    (void)key;
    int i = (int)h;
    if (i < 0 || i >= NVS_NS_MAX) return -1;
    if (s_nvs_len[i] == 0) return -1;      /* 模拟「键不存在」 */
    if (*len < s_nvs_len[i]) return -1;
    memcpy(buf, s_nvs[i], s_nvs_len[i]);
    *len = s_nvs_len[i];
    return 0;
}

int stub_nvs_set_blob(nvs_handle_t h, const char *key, const void *buf, size_t len)
{
    (void)key;
    int i = (int)h;
    if (i < 0 || i >= NVS_NS_MAX) return -1;
    if (len > NVS_BLOB_MAX) return -1;
    memcpy(s_nvs[i], buf, len);
    s_nvs_len[i] = len;
    return 0;
}

int stub_nvs_commit(nvs_handle_t h) { (void)h; return 0; }
void stub_nvs_close(nvs_handle_t h) { (void)h; }

void stub_nvs_corrupt(int ns_index, size_t offset, uint8_t xor_mask)
{
    if (ns_index < 0 || ns_index >= NVS_NS_MAX) return;
    if (offset >= s_nvs_len[ns_index]) return;
    s_nvs[ns_index][offset] ^= xor_mask;
}

void stub_nvs_write_raw(int ns_index, const void *buf, size_t len)
{
    if (ns_index < 0 || ns_index >= NVS_NS_MAX) return;
    if (len == 0 || len > NVS_BLOB_MAX) return;
    memcpy(s_nvs[ns_index], buf, len);
    s_nvs_len[ns_index] = len;
}

size_t stub_nvs_blob_len(int ns_index)
{
    if (ns_index < 0 || ns_index >= NVS_NS_MAX) return 0;
    return s_nvs_len[ns_index];
}

/* ---------------- 受控时钟 ---------------- */

/* 固定在一个「可信」的时刻：本地 2026-03-05（周四）07:00 +08:00。
 * 选它的三个理由：
 *   - 高于 reminder.c 的 ELDER_MIN_TRUSTED_EPOCH(2020-01-01)，属「已校时」；
 *   - 离 08:00 只差一小时，便于测「到点触发」；
 *   - epoch 与固件时区无关（下面用 UTC 构造），换机器跑结果一致。 */
#define FAKE_BASE 1772665200L

static time_t s_fake_now;

void stub_time_set(time_t epoch) { s_fake_now = epoch; }
void stub_time_advance(int64_t sec) { s_fake_now += (time_t)sec; }
time_t stub_time_now(void) { return s_fake_now ? s_fake_now : (time_t)FAKE_BASE; }

/* ⚠️ 必须劫持 libc 的 time()，否则 reminder.c / elder_cmd.c 里的
 *    time(NULL) 会读到真实系统时间，整套「到点触发」的断言全部失真
 *    （看起来能过，其实测的是运行测试那一秒的时刻）。
 *    做法：在本文件里直接定义 time()，链接器会用我们的定义覆盖 libc 的
 *    （共享库只在符号未定义时才会被拿来补）。
 *    注意**不能**用 -Dtime=... ：那会把 <time.h> 自身的声明也一起改名。 */
time_t time(time_t *out)
{
    time_t t = stub_time_now();
    if (out) *out = t;
    return t;
}

/* esp_timer 微秒：与受控时钟同步推进，confirm 的 30s 窗口可精确触发。
 * 固件里 esp_timer 来自 esp_timer_get_time()，桩头文件把它映射到这里。 */
int64_t stub_timer_us(void) { return (int64_t)stub_time_now() * 1000000LL; }

/* ---------------- 副作用计数（提示音 / RGB） ---------------- */

static int s_tone_count;
static int s_rgb_count;
static int s_rgb_pending;
static int s_rgb_escalate;
static int s_rgb_done;

int  stub_tone_count(void) { return s_tone_count; }
int  stub_rgb_count(void) { return s_rgb_count; }
int  stub_rgb_pending_count(void) { return s_rgb_pending; }
int  stub_rgb_escalate_count(void) { return s_rgb_escalate; }
int  stub_rgb_done_count(void) { return s_rgb_done; }
void stub_fx_reset(void)
{
    s_tone_count = 0;
    s_rgb_count = 0;
    s_rgb_pending = 0;
    s_rgb_escalate = 0;
    s_rgb_done = 0;
}

void elder_ui_play_tone(elder_tone_t tone) { (void)tone; s_tone_count++; }
void elder_ui_announce(const char *t, elder_tone_t tone) { (void)t; (void)tone; s_tone_count++; }

void elder_rgb_med_done(void) { s_rgb_count++; s_rgb_done++; }
void elder_rgb_med_pending(void) { s_rgb_count++; s_rgb_pending++; }
void elder_rgb_med_escalate(void) { s_rgb_count++; s_rgb_escalate++; }

/* ---------------- FreeRTOS 任务（丢弃，不启动死循环） ---------------- */

BaseType_t stub_task_create(void (*fn)(void *))
{
    (void)fn;   /* 故意不调用：reminder_task 是 while(1)，会吞掉整个测试 */
    return pdPASS;
}

void vTaskDelay(TickType_t ticks) { (void)ticks; }

const char *esp_err_to_name(esp_err_t code)
{
    switch (code) {
    case ESP_OK: return "ESP_OK";
    case ESP_ERR_NOT_FOUND: return "ESP_ERR_NOT_FOUND";
    case ESP_ERR_NO_MEM: return "ESP_ERR_NO_MEM";
    case ESP_ERR_INVALID_ARG: return "ESP_ERR_INVALID_ARG";
    case ESP_ERR_INVALID_SIZE: return "ESP_ERR_INVALID_SIZE";
    default: return "ESP_ERR(?)";
    }
}