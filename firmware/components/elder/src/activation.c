#include "elder_activation.h"

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"

#include <string.h>

static const char *TAG = "elder.act";

#define VALID_MS (10 * 60 * 1000LL)   /* 10 分钟有效 */

static char  s_code[ELDER_ACT_CODE_LEN + 1];
static int64_t s_deadline_us = 0;

/*
 * 计算 Luhn 校验位。**完整码长 = n + 1**（校验位自己占第 1 位，不加倍）。
 *
 * ⚠️ 这里的位序必须与 server/app/security.py::luhn_check_digit() 逐位一致：
 *   自右往左数，偶数位加倍（>9 减 9）。两边算出来不一致的症状是
 *   「设备屏上的码在 APP 里永远校验失败」，而且两边各自的自测都能过 ——
 *   历史真坏过一次，所以两处注释互相指认。
 *
 * ⚠️ 校验位由前 6 位完全决定，**不增加任何密钥空间**，它只防「抄错一位」。
 */
static int luhn_check_digit(const char *body, int n)
{
    int sum = 0;
    for (int i = 0; i < n; i++) {
        int d = body[i] - '0';
        /* body 的 index i 在完整码里是自右往左第 (n + 1 - i) 位 */
        if (((n + 1 - i) % 2) == 0) {
            d *= 2;
            if (d > 9) d -= 9;
        }
        sum += d;
    }
    return (10 - (sum % 10)) % 10;
}

esp_err_t elder_activation_generate(char *out_code, size_t len)
{
    if (!out_code || len < ELDER_ACT_CODE_LEN) return ESP_ERR_INVALID_SIZE;

    /* 6 位随机数字 + 1 位校验位。esp_random() 是硬件 TRNG，
     * 每取一位都重新取随机数 —— 不要图省事拿一次随机数取模切 6 段，
     * 那样 6 位之间有相关性（且 2^32 不是 10 的整数倍，低位有偏）。 */
    for (int i = 0; i < ELDER_ACT_CODE_LEN - 1; i++) {
        s_code[i] = (char)('0' + (esp_random() % 10));
    }
    s_code[ELDER_ACT_CODE_LEN - 1] =
        (char)('0' + luhn_check_digit(s_code, ELDER_ACT_CODE_LEN - 1));
    s_code[ELDER_ACT_CODE_LEN] = '\0';

    s_deadline_us = esp_timer_get_time() + VALID_MS * 1000LL;

    memcpy(out_code, s_code, ELDER_ACT_CODE_LEN);
    /* 缓冲区还有富余就补一个 \0，避免调用方直接当字符串用时越界 */
    if (len > ELDER_ACT_CODE_LEN) out_code[ELDER_ACT_CODE_LEN] = '\0';
    ESP_LOGI(TAG, "new activation code generated (valid 10min)");
    return ESP_OK;
}

void elder_activation_format(const char *code, char *out, size_t out_len)
{
    if (!out || out_len < 1) return;
    /* ★ 先把输出置空，且放在**所有早退之前**：任何一条守卫命中时，调用方
     * `snprintf(..., "%s", out)` 读到的是空串，而不是栈上未初始化内存。
     * 这一行比长度守卫更重要 —— 少了它，早退就变成"随机乱码"这种更难查的故障。 */
    out[0] = '\0';

    /* ⚠️ 判据里的 7 / 9 是**字面量，刻意不引用 header 宏**：半合并
     * （header 与 .c 版本不一致）时，错的那一半恰好就是 header ——
     * 拿错的值去校验自己必然失效。
     * 本实现只写 out[0..8]（"123-4567" + '\0'），所以 9 字节下限是对的。
     * ⚠️ 注意：旧 13 位实现写满 17 字节且自身无守卫，那种半合并**无法**
     * 靠这里挡住，只能靠 merge 之后的 `diff -rq` 闸门（见 CI）。 */
    if (!code || strlen(code) != 7) return;
    if (out_len < 9) return;
    /* code: 7 digits -> "123-4567" */
    out[0] = code[0];
    out[1] = code[1];
    out[2] = code[2];
    out[3] = '-';
    out[4] = code[3];
    out[5] = code[4];
    out[6] = code[5];
    out[7] = code[6];
    out[8] = '\0';
}

bool elder_activation_verify(const char *code)
{
    if (!code) return false;
    if (strlen(code) != ELDER_ACT_CODE_LEN) return false;

    for (int i = 0; i < ELDER_ACT_CODE_LEN; i++) {
        if (code[i] < '0' || code[i] > '9') return false;
    }
    return (code[ELDER_ACT_CODE_LEN - 1] - '0') ==
           luhn_check_digit(code, ELDER_ACT_CODE_LEN - 1);
}

bool elder_activation_valid(void)
{
    return s_deadline_us && (esp_timer_get_time() < s_deadline_us);
}
