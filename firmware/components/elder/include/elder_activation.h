#ifndef ELDER_ACTIVATION_H
#define ELDER_ACTIVATION_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 设备绑定激活码（需求 6 / 7）
 * 格式：1 位字母（不区分大小写）+ 12 位纯数字，其中第 12 位为 Luhn 校验位
 * 显示时分组：A-3829-4471-0056  —— 12 位连写在 320x240 上抄错率极高，必须分组
 * 字符集剔除 I/O（易与 1/0 混淆）
 */

#define ELDER_ACT_CODE_LEN  13
#define ELDER_ACT_DISPLAY_LEN 17   /* "A-3829-4471-0056\0" */

/**
 * @brief 生成一个随机激活码（第 12 位数字为 Luhn 校验位）
 * @param out_code   至少 13 字节缓冲区（不含结尾 \0）
 */
esp_err_t elder_activation_generate(char *out_code, size_t len);

/**
 * @brief 格式化为 X-XXXX-XXXX-XXXX 便于屏显
 */
void elder_activation_format(const char *code, char *out, size_t out_len);

/**
 * @brief 校验激活码（大小写不敏感 + Luhn 校验），用于在提交前本地拦截输入错误
 */
bool elder_activation_verify(const char *code);

/**
 * @brief 当前激活码是否仍在有效期内（默认 10 分钟）
 */
bool elder_activation_valid(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_ACTIVATION_H */
