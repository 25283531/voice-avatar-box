#include "sdkconfig.h"
#include "elder_net.h"

#include <string.h>
#include <stdlib.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_http_client.h"

static const char *TAG = "elder.net";

/* 退避阶梯：5s / 10s / 20s / 40s / 80s / 160s / 300s(封顶) */
static const uint32_t BACKOFF_STEPS[] = {5000, 10000, 20000, 40000, 80000, 160000, 300000};
#define BACKOFF_STEPS_N (sizeof(BACKOFF_STEPS) / sizeof(BACKOFF_STEPS[0]))

#ifndef CONFIG_ELDER_HTTP_TIMEOUT_MS
#define CONFIG_ELDER_HTTP_TIMEOUT_MS 8000
#endif

static struct {
    elder_net_cfg_t cfg;
    bool            inited;
    bool            online;          /* 软状态：最近一次请求是否成功 */
    int64_t         last_ok_us;      /* 最近一次成功时刻 */
    uint32_t        fail_streak;     /* 连续失败次数 */
} g;

/* ---------------------------------------------------------------- 基础 */

esp_err_t elder_net_init(const elder_net_cfg_t *cfg)
{
    if (cfg) g.cfg = *cfg;
    g.cfg.base_url[ELDER_NET_URL_MAX - 1] = '\0';
    g.cfg.token[ELDER_NET_TOKEN_MAX - 1] = '\0';
    g.inited = true;
    ESP_LOGI(TAG, "base_url=%s token=%s", g.cfg.base_url,
             g.cfg.token[0] ? "***" : "(empty)");
    return ESP_OK;
}

esp_err_t elder_net_set_base_url(const char *url)
{
    if (!url) return ESP_ERR_INVALID_ARG;
    snprintf(g.cfg.base_url, sizeof(g.cfg.base_url), "%s", url);
    return ESP_OK;
}

esp_err_t elder_net_set_token(const char *token)
{
    snprintf(g.cfg.token, sizeof(g.cfg.token), "%s", token ? token : "");
    return ESP_OK;
}

void elder_net_get_cfg(elder_net_cfg_t *out)
{
    if (out) *out = g.cfg;
}

bool elder_net_has_token(void)
{
    return g.cfg.token[0] != '\0';
}

bool elder_net_is_online(void)
{
    return g.online;
}

int64_t elder_net_ms_since_last_ok(void)
{
    if (!g.last_ok_us) return INT64_MAX;
    return (esp_timer_get_time() - g.last_ok_us) / 1000;
}

uint32_t elder_net_fail_streak(void)
{
    return g.fail_streak;
}

uint32_t elder_net_backoff_next_ms(bool success)
{
    if (success) {
        g.fail_streak = 0;
        return 0;
    }
    uint32_t idx = g.fail_streak;
    if (idx >= BACKOFF_STEPS_N) idx = BACKOFF_STEPS_N - 1;
    g.fail_streak++;
    return BACKOFF_STEPS[idx];
}

/* ---------------------------------------------------------------- HTTP */

/* 拼出完整 URL：base_url + path（path 必须以 / 开头） */
static esp_err_t build_url(const char *path, char *out, size_t out_len)
{
    if (!g.cfg.base_url[0]) return ESP_ERR_INVALID_STATE;
    /* 去掉 base_url 结尾多余的 / */
    size_t n = strlen(g.cfg.base_url);
    while (n > 0 && g.cfg.base_url[n - 1] == '/') n--;
    int w = snprintf(out, out_len, "%.*s%s", (int)n, g.cfg.base_url, path);
    if (w < 0 || (size_t)w >= out_len) return ESP_ERR_INVALID_SIZE;
    return ESP_OK;
}

/* 统一的请求执行器 */
static esp_err_t perform(esp_http_client_method_t method, const char *path,
                         const char *body, char *resp, size_t *resp_len, int *status)
{
    if (!g.inited || !g.cfg.base_url[0]) {
        ESP_LOGW(TAG, "未配置后端地址，跳过请求");
        return ESP_ERR_INVALID_STATE;
    }

    char url[ELDER_NET_URL_MAX + 160];
    esp_err_t err = build_url(path, url, sizeof(url));
    if (err != ESP_OK) return err;

    size_t cap = (resp && resp_len) ? *resp_len : 0;
    if (resp_len) *resp_len = 0;
    if (status) *status = 0;

    esp_http_client_config_t cfg = {
        .url = url,
        .method = method,
        .timeout_ms = CONFIG_ELDER_HTTP_TIMEOUT_MS,   /* 硬超时：DNS+连接+收发全程 */
        .disable_auto_redirect = false,
        .max_redirection_count = 2,
        /* 不要 keep-alive：老人设备长时间离线，连接池反而增加出错面 */
        .keep_alive_enable = false,
        .buffer_size = 2048,
    };

    esp_http_client_handle_t cli = esp_http_client_init(&cfg);
    if (!cli) return ESP_ERR_NO_MEM;

    esp_http_client_set_header(cli, "Content-Type", "application/json");
    esp_http_client_set_header(cli, "Accept", "application/json");
    if (g.cfg.token[0]) {
        char auth[ELDER_NET_TOKEN_MAX + 16];
        snprintf(auth, sizeof(auth), "Bearer %s", g.cfg.token);
        esp_http_client_set_header(cli, "Authorization", auth);
    }

    esp_err_t res = ESP_FAIL;
    if (body) {
        esp_http_client_set_post_field(cli, body, (int)strlen(body));
    }

    int64_t t0 = esp_timer_get_time();
    err = esp_http_client_perform(cli);
    int64_t cost_ms = (esp_timer_get_time() - t0) / 1000;

    if (err != ESP_OK) {
        /* 网络层失败：DNS/连接/超时都在这里 */
        ESP_LOGW(TAG, "%s %s 失败: %s (%lldms)", method == HTTP_METHOD_POST ? "POST" : "GET",
                 path, esp_err_to_name(err), cost_ms);
        res = (err == ESP_ERR_HTTP_CONNECT || err == ESP_ERR_HTTP_CONNECTING)
              ? ESP_ERR_TIMEOUT : err;
        goto done;
    }

    int code = esp_http_client_get_status_code(cli);
    if (status) *status = code;

    int64_t total = esp_http_client_get_content_length(cli);

    if (code >= 200 && code < 300) {
        if (resp && cap > 0) {
            /* 分段读取，最多读 cap-1 字节，保证结尾有 \0 */
            int read = 0, remain = (int)(cap - 1);
            while (remain > 0) {
                int r = esp_http_client_read(cli, resp + read, remain);
                if (r <= 0) break;
                read += r;
                remain -= r;
            }
            resp[read] = '\0';
            if (resp_len) *resp_len = (size_t)read;
            if (total > 0 && (int64_t)read < total) {
                ESP_LOGW(TAG, "响应被截断: %d/%lld 字节（缓冲 %u）", read, total, (unsigned)cap);
            }
        }
        g.online = true;
        g.last_ok_us = esp_timer_get_time();
        ESP_LOGD(TAG, "%s -> %d (%d bytes, %lldms)", path, code,
                 resp_len ? (int)*resp_len : 0, cost_ms);
        res = ESP_OK;
    } else {
        /* 业务层错误：不算网络失败，不触发退避阶梯 */
        ESP_LOGW(TAG, "%s -> HTTP %d (%lldms)", path, code, cost_ms);
        res = ESP_ERR_INVALID_RESPONSE;
        if (code >= 500) {
            /* 服务端故障按网络问题处理，稍后重试 */
            res = ESP_FAIL;
        }
    }

done:
    esp_http_client_cleanup(cli);
    return res;
}

esp_err_t elder_net_post(const char *path, const char *body,
                         char *resp, size_t *resp_len, int *status)
{
    return perform(HTTP_METHOD_POST, path, body, resp, resp_len, status);
}

esp_err_t elder_net_get(const char *path, char *resp, size_t *resp_len, int *status)
{
    return perform(HTTP_METHOD_GET, path, NULL, resp, resp_len, status);
}
