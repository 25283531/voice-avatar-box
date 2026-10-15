#ifndef ELDER_NET_H
#define ELDER_NET_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * HTTP 客户端薄封装（需求：离线兜底 + 不能死等网络）
 *
 * 设计要点：
 *  - 所有请求都带**硬超时**（CONFIG_ELDER_HTTP_TIMEOUT_MS，默认 8s），
 *    DNS 失败 / TCP 无响应 / 服务端慢都会在该时间内返回，绝不无限阻塞。
 *  - 调用方用 elder_net_backoff_next_ms() 取「下一次重试应等待多久」，
 *    连续失败按 5s → 10s → 20s → 40s → 80s → 300s 退避，成功即清零。
 *  - elder_net_is_online() 是本地维护的**软状态**（最近一次请求是否成功），
 *    不依赖 Wi-Fi 事件，因此「Wi-Fi 已连但后端不可达」也能正确判为离线，
 *    这正是离线兜底提示音的触发依据。
 */

#define ELDER_NET_URL_MAX    160
#define ELDER_NET_TOKEN_MAX  256

/** HTTP 响应体缓冲区上限（/api/device/sync 全量快照约 20KB 足够） */
#define ELDER_NET_RESP_MAX   (24 * 1024)

typedef struct {
    char base_url[ELDER_NET_URL_MAX];     /* 形如 http://192.168.1.10:8000，末尾无 / */
    char token[ELDER_NET_TOKEN_MAX];      /* 设备 JWT；为空则不发 Authorization 头 */
} elder_net_cfg_t;

/**
 * @brief 初始化网络模块（只是保存配置，不发起连接）
 */
esp_err_t elder_net_init(const elder_net_cfg_t *cfg);

/**
 * @brief 运行时更新配置（例如激活完成后写入 token）
 */
esp_err_t elder_net_set_base_url(const char *url);
esp_err_t elder_net_set_token(const char *token);
void elder_net_get_cfg(elder_net_cfg_t *out);

/** 是否已有 token（决定能否调用需要鉴权的接口） */
bool elder_net_has_token(void);

/**
 * @brief 最近一次 HTTP 交互是否成功 —— 「在线」的软判据
 */
bool elder_net_is_online(void);

/** 距上一次成功请求过去了多少毫秒（用于心跳周期自适应） */
int64_t elder_net_ms_since_last_ok(void);

/**
 * @brief 发起一次 POST（JSON），硬超时保护。
 *
 * @param path     以 / 开头，例如 "/api/device/sync?fw_version=1.0"
 * @param body     请求体；可为 NULL（空 body）
 * @param resp     响应缓冲区；可为 NULL（不关心响应）
 * @param resp_len 入参为缓冲区大小，出参为实际写入长度（不含结尾 \0）
 * @param status   出参：HTTP 状态码（可为 NULL）
 * @return ESP_OK 表示**收到 2xx 响应**；
 *         ESP_ERR_TIMEOUT / ESP_FAIL 等表示网络层失败（调用方应退避重试）
 */
esp_err_t elder_net_post(const char *path, const char *body,
                         char *resp, size_t *resp_len, int *status);

/** GET 便捷封装（健康检查用） */
esp_err_t elder_net_get(const char *path, char *resp, size_t *resp_len, int *status);

/**
 * @brief 退避计时器：连续失败时返回下一次应等待的毫秒数，成功时清零。
 * @param success 本次请求是否成功
 * @return 建议等待毫秒数（仅在网络层失败时递增；HTTP 4xx 不递增，属业务错误）
 */
uint32_t elder_net_backoff_next_ms(bool success);

/** 当前连续失败次数（0 表示网络健康） */
uint32_t elder_net_fail_streak(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_NET_H */
