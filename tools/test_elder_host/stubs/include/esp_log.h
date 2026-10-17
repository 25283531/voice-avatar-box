/* esp_log.h —— 宿主测试桩：把固件的日志打到 stdout，
 * 失败用例排查时能直接看到 reminder.c 为什么拒绝合并。 */
#ifndef STUB_ESP_LOG_H
#define STUB_ESP_LOG_H

#include <stdio.h>

#define ESP_LOGE(tag, fmt, ...) fprintf(stderr, "E %s: " fmt "\n", (tag), ##__VA_ARGS__)
#define ESP_LOGW(tag, fmt, ...) fprintf(stderr, "W %s: " fmt "\n", (tag), ##__VA_ARGS__)
#define ESP_LOGI(tag, fmt, ...) fprintf(stdout, "I %s: " fmt "\n", (tag), ##__VA_ARGS__)
#define ESP_LOGD(tag, fmt, ...) do {} while (0)
#define ESP_LOGV(tag, fmt, ...) do {} while (0)

#endif