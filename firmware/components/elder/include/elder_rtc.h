#ifndef ELDER_RTC_H
#define ELDER_RTC_H

#include <stdbool.h>
#include <time.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * DS3231 外部时钟（温补晶振，走 I2C）。
 *
 * 为什么需要它：ESP32-S3 的 RTC 域只靠 VBAT/主电源维持，**完全断电后归零**。
 * 没有外部时钟时，拔电重启 + 无网 = 时钟停在 1970，reminder.c 的可信时间守卫
 * 会主动暂停所有提醒 —— 表现是"当天一条都不响，而屏幕一切正常"。
 *
 * 硬件位置：插在板上的 **I2C 外拓 5P 接口**（该口引出的是板载音频 I2C 总线，
 *          立创板建在 **端口 1**，SDA=1/SCL=2），地址 0x68，
 *          与 PCA9557(0x19) / FT5x06(0x38) / ES8311(0x18) 不冲突。
 *          ——注意板上另一个 5P 口只出 GPIO10/11，那是彩灯与按键用的，别插错。
 *
 * 降级策略（三级，见 elder_sync.c 的 elder_time_restore_boot）：
 *   1) RTC 存在且 OSF=0        → 采信
 *   2) RTC 不存在 / OSF=1      → NVS 里上次校时 epoch（近似）
 *   3) 两者都没有              → 什么都不做，由 reminder.c 的守卫暂停并屏显提示
 */

/**
 * @brief 初始化 DS3231。未启用（CONFIG_ELDER_RTC_ENABLE=n）或探测不到时
 *        返回 ESP_ERR_NOT_SUPPORTED / ESP_ERR_NOT_FOUND，**不打 ERROR**。
 */
esp_err_t elder_rtc_init(void);

/** @brief 是否已成功初始化（后续读写才有意义）。 */
bool elder_rtc_present(void);

/**
 * @brief 读 RTC 时间并换算成 UTC epoch。
 *
 * @param[out] out 成功时写入 epoch
 * @return ESP_OK；ESP_ERR_INVALID_STATE 表示 OSF=1（掉电停振过，读数是垃圾）；
 *         ESP_ERR_INVALID_ARG 表示参数/换算失败；其余为 I2C 错误。
 *
 * ⚠️ OSF=1 时**必须**当作"没有时间"，绝不能把垃圾读数写进系统时钟 ——
 *    那会让提醒在错误的时刻响，比不响更危险。
 */
esp_err_t elder_rtc_read(time_t *out);

/**
 * @brief 把 epoch 写回 RTC（联网校时成功后调用），并清除 OSF 标志。
 *
 * 写回的意义：下次掉电重启，时间从 RTC 恢复，而不是回到 1970。
 */
esp_err_t elder_rtc_write(time_t t);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_RTC_H */
