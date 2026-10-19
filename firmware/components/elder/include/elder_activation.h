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
 * 格式：**7 位纯数字** = 6 位随机数字 + 1 位 Luhn 校验位
 * 显示时分组：123-4567 —— 320x240 上必须一行大字，抄错率最低
 *
 * ⚠️ 为什么从「1 位字母 + 12 位数字」退成全数字（2026-10-20 用户拍板）
 *   内置音效表（xiaozhi/main/assets/lang_config.h）只有 OGG_0..OGG_9，
 *   **没有字母音**；上游 Application::ShowActivationCode() 逐字符播报，
 *   遇到字母静默跳过 ⇒ 老人只听得到 12 位数字，照输必被 APP 拒掉。
 *   全数字后 7 位都能念完，屏显也只需要一行。
 *
 * ⚠️ 代价：空间从 26e11 降到 1e6（校验位由前 6 位决定，不增加熵）。
 *   安全性由服务端承担：绑定接口限速 + 单码失败上限 + 码与 MAC 强绑定，
 *   见 server/app/ratelimit.py::bind_gate 与 models.ActivationCode。
 *   **算法必须与 server/app/security.py 逐位一致**，改一处必须改两处。
 */
#define ELDER_ACT_CODE_LEN  7
#define ELDER_ACT_DISPLAY_LEN 9    /* "123-4567\0" */

/**
 * @brief 生成一个随机激活码（末位为 Luhn 校验位）
 * @param out_code   至少 ELDER_ACT_CODE_LEN 字节缓冲区（不含结尾 \0）
 */
esp_err_t elder_activation_generate(char *out_code, size_t len);

/**
 * @brief 格式化为 123-4567 便于屏显
 */
void elder_activation_format(const char *code, char *out, size_t out_len);

/**
 * @brief 校验激活码（7 位纯数字 + Luhn 校验），用于在提交前本地拦截输入错误
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
