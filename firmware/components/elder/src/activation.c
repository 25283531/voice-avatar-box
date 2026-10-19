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
    if (!code || !out || out_len < ELDER_ACT_DISPLAY_LEN) return;
    /* 防御性兜底：万一 header 与 .c 版本不一致（合并脚本半途失败），
     * 这里读到的是短码，直接返回比按 7 位布局越界写安全。
     * 正常路径下服务端一定下发 7 位，这一行不会命中。 */
    if (strlen(code) < ELDER_ACT_CODE_LEN) return;
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
