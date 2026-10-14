#include "elder_activation.h"

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"

#include <string.h>
#include <ctype.h>

static const char *TAG = "elder.act";

#define VALID_MS (10 * 60 * 1000LL)   /* 10 分钟有效 */

static char  s_code[ELDER_ACT_CODE_LEN + 1];
static int64_t s_deadline_us = 0;

/* 字母表：剔除 I / O */
static const char LETTERS[] = "ABCDEFGHJKLMNPQRSTUVWXYZ";

static int luhn_check_digit(const char *digits12)
{
    /* 对前 11 位数字计算，返回第 12 位校验值 */
    int sum = 0;
    for (int i = 0; i < 11; i++) {
        int d = digits12[i] - '0';
        /* 从右往左，第 12 位是校验位，所以第 11 位（index 10）权重为 2 */
        if (((11 - i) % 2) == 0) {   /* 偶数位（自右数第 2、4…）加倍 */
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

    uint32_t r = esp_random();
    s_code[0] = LETTERS[r % (sizeof(LETTERS) - 1)];

    for (int i = 1; i <= 11; i++) {
        uint32_t v = esp_random();
        s_code[i] = (char)('0' + (v % 10));
    }
    s_code[12] = (char)('0' + luhn_check_digit(s_code + 1));
    s_code[ELDER_ACT_CODE_LEN] = '\0';

    s_deadline_us = esp_timer_get_time() + VALID_MS * 1000LL;

    memcpy(out_code, s_code, ELDER_ACT_CODE_LEN);
    ESP_LOGI(TAG, "new activation code generated (valid 10min)");
    return ESP_OK;
}

void elder_activation_format(const char *code, char *out, size_t out_len)
{
    if (!code || !out || out_len < ELDER_ACT_DISPLAY_LEN) return;
    /* code: A + 12 digits */
    out[0] = (char)toupper((unsigned char)code[0]);
    out[1] = '-';
    for (int g = 0; g < 3; g++) {
        for (int k = 0; k < 4; k++) {
            out[2 + g * 5 + k] = code[1 + g * 4 + k];
        }
        if (g < 2) out[2 + g * 5 + 4] = '-';
    }
    out[16] = '\0';
}

bool elder_activation_verify(const char *code)
{
    if (!code) return false;
    if (strlen(code) != ELDER_ACT_CODE_LEN) return false;

    char c0 = (char)toupper((unsigned char)code[0]);
    if (c0 < 'A' || c0 > 'Z') return false;
    if (c0 == 'I' || c0 == 'O') return false;

    for (int i = 1; i < ELDER_ACT_CODE_LEN; i++) {
        if (code[i] < '0' || code[i] > '9') return false;
    }
    return (code[12] - '0') == luhn_check_digit(code + 1);
}

bool elder_activation_valid(void)
{
    return s_deadline_us && (esp_timer_get_time() < s_deadline_us);
}
