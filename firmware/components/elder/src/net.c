#include "sdkconfig.h"
#include "elder_net.h"

#include <string.h>
#include <stdlib.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"

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

/* ---- 响应体收集：必须走事件回调，不能 perform() 之后再 read ----
 *
 * ⚠️ 这是 2026-10-20 真机（COM10）钉死的第三个缺陷，只有一行日志可循：
 *      W elder.net: 响应被截断: 0/… 字节（缓冲 2403）
 *      E elder.sync: 响应不是合法 JSON（前 120 字节）:
 *   而服务端明明回了 200 + 合法 JSON。
 *
 * 原因在 IDF v5.5.1 的实现里：esp_http_client_perform() 内部会
 *      while (client->response->data_process < client->response->content_length)
 *          esp_http_client_get_data(client);
 * 也就是**整个响应体在 perform 里就被读完丢弃**（本该交给
 * HTTP_EVENT_ON_DATA 回调），之后 esp_http_client_read() 一律返回 0。
 * ⇒ 「连得上、状态码 200、读不到内容」。
 *
 * 所以：响应体只能在事件回调里收。谁把它改回 perform 后再 read，谁的
 * 同步就会回到「静默拿到空串」——比崩溃更难查，别改。 */
typedef struct {
    char  *buf;
    size_t cap;        /* 含结尾 \0 的容量 */
    size_t len;
    bool   truncated;
} resp_sink_t;

static esp_err_t on_http_event(esp_http_client_event_t *ev)
{
    if (ev->event_id != HTTP_EVENT_ON_DATA) return ESP_OK;
    resp_sink_t *s = (resp_sink_t *)ev->user_data;
    if (!s || !s->buf || s->cap == 0 || ev->data_len <= 0) return ESP_OK;
    size_t room = s->cap - 1 - s->len;          /* 留 1 字节给结尾 \0 */
    size_t n = (size_t)ev->data_len < room ? (size_t)ev->data_len : room;
    if (n > 0) {
        memcpy(s->buf + s->len, ev->data, n);
        s->len += n;
    }
    if (n < (size_t)ev->data_len) s->truncated = true;
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

    resp_sink_t sink = {.buf = resp, .cap = cap, .len = 0, .truncated = false};

    esp_http_client_config_t cfg = {
        .url = url,
        .method = method,
        .timeout_ms = CONFIG_ELDER_HTTP_TIMEOUT_MS,   /* 硬超时：DNS+连接+收发全程 */
        .disable_auto_redirect = false,
        .max_redirection_count = 2,
        /* 不要 keep-alive：老人设备长时间离线，连接池反而增加出错面 */
        .keep_alive_enable = false,
        .buffer_size = 2048,
        /* 响应体从这里收（见 resp_sink_t 上方的长注释） */
        .event_handler = on_http_event,
        .user_data = &sink,
        /* 🔴 必须挂证书包（2026-10-20，真机 COM10 实证）：
         * 后端是 https://152.136.230.132，本结构体若不填任何 cert 相关字段，
         * esp-tls 会在建连前就报
         *   "No server verification option set in esp_tls_cfg_t structure"
         *   → ESP_ERR_MBEDTLS_SSL_SETUP_FAILED → HTTP_CLIENT 连接失败
         * ⇒ **所有 /api/device/sync 心跳永久失败**，设备恒离线，而 OTA 走
         * 另一条 HttpClient（esp-ml307 的 esp_ssl.cc:35 已挂 crt_bundle）
         * 一切正常 —— 这正是「能连上能说话、后台永远离线」的最后一环。
         * 不要改成 skip_cert：那等于对 IP 直连放手，MAC 即身份的场景下不可接受。 */
        .crt_bundle_attach = esp_crt_bundle_attach,
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
        /* ⚠️ 这里不能用 %lld：本工程 CONFIG_LIBC_NEWLIB_NANO_FORMAT=y，
         * nano-printf 不支持 64 位长度修饰符，真机打出来的是字面量
         * 「(ldms)」——2026-10-20 排查时这行日志差点被当成正常输出忽略。 */
        ESP_LOGW(TAG, "%s %s 失败: %s (%dms)", method == HTTP_METHOD_POST ? "POST" : "GET",
                 path, esp_err_to_name(err), (int)cost_ms);
        res = (err == ESP_ERR_HTTP_CONNECT || err == ESP_ERR_HTTP_CONNECTING)
              ? ESP_ERR_TIMEOUT : err;
        goto done;
    }

    int code = esp_http_client_get_status_code(cli);
    if (status) *status = code;

    if (code >= 200 && code < 300) {
        /* 响应体已经在 on_http_event 里收进 sink（perform 之后 read 恒为 0） */
        if (resp && cap > 0) {
            resp[sink.len] = '\0';
            if (resp_len) *resp_len = sink.len;
            if (sink.truncated) {
                ESP_LOGW(TAG, "%s 响应超出缓冲（%u 已收，最多 %u），已截断",
                         path, (unsigned)sink.len, (unsigned)(cap - 1));
            }
        }
        g.online = true;
        g.last_ok_us = esp_timer_get_time();
        ESP_LOGD(TAG, "%s -> %d (%u bytes, %dms)", path, code,
                 (unsigned)sink.len, (int)cost_ms);
        res = ESP_OK;
    } else {
        /* 业务层错误：不算网络失败，不触发退避阶梯 */
        ESP_LOGW(TAG, "%s -> HTTP %d (%dms)", path, code, (int)cost_ms);
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
