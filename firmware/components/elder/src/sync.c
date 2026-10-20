#include "sdkconfig.h"
#include "elder_sync.h"

#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <sys/time.h>

#include "cJSON.h"
#include "esp_chip_info.h"
#include "esp_heap_caps.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"

#include "elder_net.h"
#include "elder_reminder.h"
#include "elder_rtc.h"

static const char *TAG = "elder.sync";

#define NVS_NS       "elder_sync"
#define NVS_KEY_Q    "evtq"
#define NVS_KEY_TOK  "token"
#define NVS_KEY_EPOCH "epoch"   /* 最后一次联网校时的 UTC epoch，无 RTC 时兜底 */

/* 低于这个时刻认为时钟不可信（未校时/掉电归零）。
 * 与 reminder.c 的 ELDER_MIN_TRUSTED_EPOCH 同值同义 —— 那边是"不响"的闸门，
 * 这边是"不采信"的闸门，两处必须一致，否则会出现"时间被采信但提醒不响"的怪状态。 */
#define ELDER_TIME_MIN_TRUSTED 1577836800L   /* 2020-01-01T00:00:00Z */

#ifndef CONFIG_ELDER_SYNC_INTERVAL_SEC
#define CONFIG_ELDER_SYNC_INTERVAL_SEC 300
#endif

/* ---------------------------------------------------------------- 离线回执队列 */

typedef struct {
    uint32_t server_id;
    int64_t  due_at;
    uint8_t  state;
    uint8_t  method;
    int64_t  fired_at;
    int64_t  confirmed_at;
    char     note[32];
} evt_t;

typedef struct {
    evt_t    items[ELDER_SYNC_QUEUE_MAX];
    uint16_t head;      /* 出队位置 */
    uint16_t count;     /* 队列长度 */
    uint32_t crc32;
} evtq_t;

static struct {
    evtq_t            q;
    bool              started;
    TaskHandle_t      task;
    EventGroupHandle_t ev;
    uint32_t          interval_sec;
    time_t            last_server_time;
    char              fw_version[32];
    char              hw_model[24];
    bool              time_synced;
    /* 交互统计的异步投递队列（见 elder_sync_report_interaction 的注释）。
     * 为什么必须异步：这个函数被 SOS、服药确认、提醒超时三条路径同步调用，
     * 而它们分别跑在 elder_input(4096/原 2560)、elder_rmd(3072) 这些**小栈任务**里；
     * 函数体里是 HTTPS + TLS 握手，栈需求 10KB 量级。 */
    QueueHandle_t     report_q;
    TaskHandle_t      report_task;
} g;

/* elder_sync_token_persisted() 的锁存位：一旦确认绑过就不再开 NVS，
 * 因为该函数会被音频喂数据路径低频轮询（见函数注释）。 */
static bool s_token_known;

#define ELDER_REPORT_Q_LEN 8

typedef struct {
    uint8_t kind;
    int     duration_ms;
} report_t;

#define ELDER_SYNC_EV_NOW (1 << 0)

/* 与 reminder.c 中同款的自实现 CRC32（避免引入额外组件） */
static uint32_t crc32_simple(const void *data, size_t len)
{
    const uint8_t *p = data;
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= p[i];
        for (int k = 0; k < 8; k++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1)));
        }
    }
    return ~crc;
}

static void queue_save(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    g.q.crc32 = 0;
    g.q.crc32 = crc32_simple(&g.q, sizeof(g.q));
    nvs_set_blob(h, NVS_KEY_Q, &g.q, sizeof(g.q));
    nvs_commit(h);
    nvs_close(h);
}

static void queue_load(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return;
    size_t len = sizeof(g.q);
    if (nvs_get_blob(h, NVS_KEY_Q, &g.q, &len) == ESP_OK && len == sizeof(g.q)) {
        uint32_t saved = g.q.crc32;
        g.q.crc32 = 0;
        if (crc32_simple(&g.q, sizeof(g.q)) != saved) {
            ESP_LOGW(TAG, "回执队列校验失败，清空");
            memset(&g.q, 0, sizeof(g.q));
        } else {
            g.q.crc32 = saved;
            ESP_LOGI(TAG, "载入 %u 条待补发回执", g.q.count);
        }
    }
    nvs_close(h);
}

int elder_sync_queue_pending(void)
{
    return (int)g.q.count;
}

static bool queue_push(const evt_t *e)
{
    if (g.q.count >= ELDER_SYNC_QUEUE_MAX) {
        ESP_LOGW(TAG, "回执队列已满，丢弃最旧一条");
        g.q.head = (uint16_t)((g.q.head + 1) % ELDER_SYNC_QUEUE_MAX);
        g.q.count--;
    }
    uint16_t tail = (uint16_t)((g.q.head + g.q.count) % ELDER_SYNC_QUEUE_MAX);
    g.q.items[tail] = *e;
    g.q.count++;
    queue_save();
    return true;
}

static bool queue_pop(evt_t *out)
{
    if (!g.q.count) return false;
    *out = g.q.items[g.q.head];
    g.q.head = (uint16_t)((g.q.head + 1) % ELDER_SYNC_QUEUE_MAX);
    g.q.count--;
    return true;
}

/* ---------------------------------------------------------------- 时间 */

/**
 * 把 struct tm 当 UTC 解释，返回 epoch 秒数。
 *
 * 为什么不用 timegm()：newlib 在 -std=gnu17 下并不声明 timegm，
 * 隐式声明会被 -Werror=all 直接判错；而 mktime() 依赖本地时区、
 * 需要临时改全局 TZ，有副作用。这里用 Howard Hinnant 的
 * days-from-civil 算法，纯算术、可测试、无全局状态。
 */
static time_t elder_utc_epoch(const struct tm *tm)
{
    int y = tm->tm_year + 1900;
    int m = tm->tm_mon + 1;
    if (m <= 2) {
        y -= 1;
        m += 12;
    }
    const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = (unsigned)(y - era * 400);                 // [0, 399]
    const unsigned doy = (unsigned)((153 * (m - 3) + 2) / 5 + tm->tm_mday - 1); // [0, 365]
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;     // [0, 146096]
    const int64_t days = (int64_t)era * 146097 + (int64_t)doe - 719468;

    return (time_t)(days * 86400 + tm->tm_hour * 3600 + tm->tm_min * 60 + tm->tm_sec);
}

/* ---------------------------------------------------------------- 时间基准
 *
 * 设备没有电池供电的 RTC 时，`settimeofday` 的结果在**完全断电后丢失**
 * （ESP32 的 RTC 域只扛软复位/看门狗复位，扛不住拔电）。
 * 于是掉电重启 + 无网 = 时钟停在 1970 → reminder.c 的可信时间守卫主动暂停
 * → 当天所有提醒一条不响，而屏幕一切正常，老人和子女都察觉不到。
 *
 * 这里做两件事：
 *   1) 每次联网校时成功后，把 epoch 落 NVS（近似兜底）；
 *   2) 开机时按 RTC → NVS 的顺序恢复时钟（见 elder_time_restore_boot）。
 */

static void epoch_save(time_t t)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_i64(h, NVS_KEY_EPOCH, (int64_t)t);
    nvs_commit(h);
    nvs_close(h);
}

#if CONFIG_ELDER_TIME_NVS_FALLBACK
static time_t epoch_load(void)
{
    nvs_handle_t h;
    int64_t v = 0;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return 0;
    if (nvs_get_i64(h, NVS_KEY_EPOCH, &v) != ESP_OK) v = 0;
    nvs_close(h);
    return (time_t)v;
}
#endif

/*
 * 开机恢复时钟。必须在 reminder 引擎开始 tick 之前调用。
 *
 * 三级降级：
 *   1) DS3231 且 OSF=0   → 采信（精确）
 *   2) NVS 里的上次校时  → 采信但**标为近似**（关机时长未知，会偏慢）
 *   3) 都没有            → 什么都不做，让 reminder.c 的守卫暂停并屏显提示
 *
 * ⚠️ 第 2 级是"宁可偏差也别静默"的取舍，不是精确方案：断电 3 天再开机，
 *    时间就慢 3 天，日提醒的星期判断会错。所以它必须配合"联网后立刻校正"，
 *    并且**不能**用于任何对外宣称时间的场景。
 */
esp_err_t elder_time_restore_boot(void)
{
    time_t t = 0;

    esp_err_t err = elder_rtc_read(&t);
    if (err == ESP_OK && t >= (time_t)ELDER_TIME_MIN_TRUSTED) {
        struct timeval tv = { .tv_sec = t, .tv_usec = 0 };
        settimeofday(&tv, NULL);
        g.time_synced = true;
        ESP_LOGI(TAG, "开机时钟来源: DS3231（%d）", (int)t);
        return ESP_OK;
    }
    if (err == ESP_OK) {
        ESP_LOGW(TAG, "RTC 读数早于 2020，判为不可信，转 NVS 兜底");
    }

#if CONFIG_ELDER_TIME_NVS_FALLBACK
    time_t saved = epoch_load();
    if (saved >= (time_t)ELDER_TIME_MIN_TRUSTED) {
        struct timeval tv = { .tv_sec = saved, .tv_usec = 0 };
        settimeofday(&tv, NULL);
        /* 刻意不置 g.time_synced：这是近似值，不是"已校时" */
        ESP_LOGW(TAG, "开机时钟来源: NVS 近似值（关机时长未知，可能偏慢），"
                      "联网后立即校正");
        return ESP_OK;
    }
#endif

    ESP_LOGW(TAG, "无可用时间源：时钟停在 1970，提醒将暂停直到联网校时");
    return ESP_ERR_NOT_FOUND;
}

/*
 * 后端 /api/device/sync 返回的 server_time 是**带偏移的 ISO8601**
 * （Pydantic 序列化 aware datetime，如 "2026-10-15T09:30:00+08:00"）。
 * settimeofday() 需要的是 UTC epoch，因此必须先减掉偏移。
 * 同时把 TZ 固定成 UTC+8，与后端 TZ=Asia/Shanghai 保持一致。
 */
esp_err_t elder_time_apply_server_time(const char *iso)
{
    if (!iso || strlen(iso) < 19) return ESP_ERR_INVALID_ARG;

    int Y, M, D, h, m, s;
    if (sscanf(iso, "%d-%d-%dT%d:%d:%d", &Y, &M, &D, &h, &m, &s) != 6) {
        /* 兼容 "YYYY-MM-DD HH:MM:SS" 这种空格分隔 */
        if (sscanf(iso, "%d-%d-%d %d:%d:%d", &Y, &M, &D, &h, &m, &s) != 6) {
            return ESP_ERR_INVALID_ARG;
        }
    }

    /* 解析尾部时区偏移：+08:00 / +0800 / Z / 无偏移（按本地处理） */
    int off_min = 0;
    const char *p = strpbrk(iso, "+-Zz");
    if (p && p > iso + 10) {          /* 避免匹配到日期里的 '-' */
        if (*p == 'Z' || *p == 'z') {
            off_min = 0;
        } else {
            int sign = (*p == '-') ? -1 : 1;
            int oh = 0, om = 0;
            if (sscanf(p + 1, "%d:%d", &oh, &om) >= 1) {
                off_min = sign * (oh * 60 + om);
            }
        }
    } else {
        off_min = 8 * 60;             /* 无偏移：按后端默认 Asia/Shanghai 解释 */
    }

    struct tm tmv = {0};
    tmv.tm_year = Y - 1900;
    tmv.tm_mon  = M - 1;
    tmv.tm_mday = D;
    tmv.tm_hour = h;
    tmv.tm_min  = m;
    tmv.tm_sec  = s;

    /* 把 tm 当 UTC 解释 → epoch。
     * 不用 timegm()：newlib 在 -std=gnu17 下不声明它（隐式声明会被 -Werror=all 拦下），
     * 这里用 days-from-civil 算法自行计算，纯算术、无全局时区副作用。 */
    /* 变量语义必须写清（2026-10-20 真机日志踩过）：
     *   wall_epoch = 把后端给的「墙上时间」当 UTC 解释出的 epoch（≈ 真 UTC + 8h）
     *   utc_epoch  = 真正写进 RTC 的 UTC epoch
     * 曾经把 wall_epoch 当「本地时间」再按 UTC+8 格式化打日志 ⇒ 日志比真实
     * 时间**快 8 小时**（真机打 16:59，实际 08:59），排查时差点去改服务端时钟。
     * 而 settimeofday 用的是 utc_epoch ⇒ **只有日志错、时钟没错**。 */
    time_t wall_epoch = elder_utc_epoch(&tmv);
    time_t utc_epoch = wall_epoch - off_min * 60;

    struct timeval tv = { .tv_sec = utc_epoch, .tv_usec = 0 };
    settimeofday(&tv, NULL);

    /* POSIX TZ：CST-8 = UTC+8，无夏令时 */
    setenv("TZ", "CST-8", 1);
    tzset();

    g.time_synced = true;
    g.last_server_time = utc_epoch;   /* 语义是 UTC epoch，不是墙上时间 */

    /* 校时结果必须**落两处**：
     *   - NVS：无 RTC 时掉电重启的近似兜底；
     *   - RTC：有 DS3231 时下次掉电可精确恢复（写失败只告警，不影响本次校时）。 */
    epoch_save(utc_epoch);
    if (elder_rtc_present()) {
        esp_err_t rtc_err = elder_rtc_write(utc_epoch);
        if (rtc_err != ESP_OK) {
            ESP_LOGW(TAG, "回写 RTC 失败: %s（本次校时仍生效）", esp_err_to_name(rtc_err));
        }
    }

    char buf[32];
    elder_time_iso_local(utc_epoch, buf, sizeof(buf));
    /* %lld 在 nano-printf（CONFIG_LIBC_NEWLIB_NANO_FORMAT=y）下会打成字面量 */
    ESP_LOGI(TAG, "校时完成: 服务器 %s (UTC epoch %d)", buf, (int)utc_epoch);
    return ESP_OK;
}

bool elder_time_synced(void)
{
    return g.time_synced;
}

void elder_time_iso_local(time_t t, char *out, size_t len)
{
    struct tm tmv;
    localtime_r(&t, &tmv);
    /* 固定按 UTC+8 输出，与后端 Asia/Shanghai 一致（不依赖 gmtoff 可移植性） */
    snprintf(out, len, "%04d-%02d-%02dT%02d:%02d:%02d+08:00",
             tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
             tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
}

time_t elder_sync_last_server_time(void)
{
    return g.last_server_time;
}

/* ---------------------------------------------------------------- JSON 解析 */

/*
 * 后端 ReminderOut 关键字段：
 *   id, title, speak_text, rtype, schedule_kind,
 *   times_of_day:["08:00","20:00"], weekdays:[1..7]（1=周一）,
 *   month_days, interval_days, once_date, start_date, end_date,
 *   dose, require_confirm, repeat_interval_min, repeat_times,
 *   escalate_after_min, confirm_methods, enabled
 *
 * 映射约定（重要，两处易错点）：
 *  1) 星期位序：后端 weekdays 用 1=周一..7=周日；
 *     固件 repeat_mask 用 bit0=周日..bit6=周六（与 struct tm 的 tm_wday 一致）。
 *     因此 mask_bit = (w == 7) ? 0 : w        —— 即 bit = w % 7。
 *  2) 一天多次：后端 times_of_day 可有多项，固件一条 item 只表示一个时刻，
 *     因此按 times_of_day 展开成多条 item，共用同一个 server_id。
 */
static uint8_t weekdays_to_mask(cJSON *arr)
{
    uint8_t mask = 0;
    if (!cJSON_IsArray(arr)) return 0;
    cJSON *it = NULL;
    cJSON_ArrayForEach(it, arr) {
        if (!cJSON_IsNumber(it)) continue;
        int w = it->valueint;            /* 1=周一 .. 7=周日 */
        if (w < 1 || w > 7) continue;
        mask |= (uint8_t)(1u << (w % 7)); /* 7(周日) -> bit0；1(周一) -> bit1 */
    }
    return mask;
}

static void parse_reminders(cJSON *root, elder_rmd_item_t *out, uint8_t *out_count)
{
    uint8_t n = 0;
    if (!cJSON_IsArray(root)) return;

    cJSON *r = NULL;
    cJSON_ArrayForEach(r, root) {
        cJSON *jid = cJSON_GetObjectItem(r, "id");
        cJSON *jtitle = cJSON_GetObjectItem(r, "title");
        cJSON *jenabled = cJSON_GetObjectItem(r, "enabled");
        cJSON *jrtype = cJSON_GetObjectItem(r, "rtype");
        cJSON *jkind = cJSON_GetObjectItem(r, "schedule_kind");
        cJSON *jtimes = cJSON_GetObjectItem(r, "times_of_day");
        cJSON *jweek = cJSON_GetObjectItem(r, "weekdays");

        if (!cJSON_IsNumber(jid)) continue;
        if (cJSON_IsBool(jenabled) && !cJSON_IsTrue(jenabled)) continue;

        uint32_t sid = (uint32_t)jid->valuedouble;
        uint8_t rtype = cJSON_IsNumber(jrtype) ? (uint8_t)jrtype->valueint : 4;
        uint8_t kind = cJSON_IsNumber(jkind) ? (uint8_t)jkind->valueint : 1;
        uint8_t mask = weekdays_to_mask(jweek);

        /* 无 weekdays（每天 / 一次性）→ 全周掩码，tick 里靠 repeat_mask==0 判一次性，
           这里统一用 0x7F 表示「每天」 */
        if (mask == 0) mask = 0x7F;

        const char *title = cJSON_IsString(jtitle) ? jtitle->valuestring : "提醒";

        /* 展开 times_of_day */
        if (cJSON_IsArray(jtimes) && cJSON_GetArraySize(jtimes) > 0) {
            cJSON *t = NULL;
            cJSON_ArrayForEach(t, jtimes) {
                if (n >= ELDER_REMINDER_MAX) break;
                if (!cJSON_IsString(t)) continue;
                int hh = 0, mm = 0;
                if (sscanf(t->valuestring, "%d:%d", &hh, &mm) != 2) continue;

                elder_rmd_item_t *it = &out[n];
                memset(it, 0, sizeof(*it));
                it->id = n;
                it->hour = (uint8_t)hh;
                it->minute = (uint8_t)mm;
                /* 后端 rtype: 0=服药 其余按日程；固件 type: 0=日程 1=服药 */
                it->type = (rtype == 0) ? ELDER_RMD_TYPE_MEDICINE : ELDER_RMD_TYPE_SCHEDULE;
                it->rtype = rtype;
                it->schedule_kind = kind;
                it->repeat_mask = mask;
                it->enabled = true;
                it->server_id = sid;
                snprintf(it->label, sizeof(it->label), "%s", title);
                n++;
            }
        } else {
            /* 没有具体时刻的规则（理论上不会出现）跳过 */
            continue;
        }
        if (n >= ELDER_REMINDER_MAX) break;
    }
    *out_count = n;
}

/* ---------------------------------------------------------------- 上报 */

static esp_err_t post_event(const evt_t *e)
{
    char due[40], fired[40], conf[40];
    elder_time_iso_local((time_t)e->due_at, due, sizeof(due));
    fired[0] = conf[0] = '\0';

    cJSON *root = cJSON_CreateObject();
    cJSON_AddNumberToObject(root, "reminder_id", (double)e->server_id);
    cJSON_AddStringToObject(root, "due_at", due);
    cJSON_AddNumberToObject(root, "state", (double)e->state);
    cJSON_AddNumberToObject(root, "confirm_method", (double)e->method);
    if (e->fired_at) {
        elder_time_iso_local((time_t)e->fired_at, fired, sizeof(fired));
        cJSON_AddStringToObject(root, "fired_at", fired);
    }
    if (e->confirmed_at) {
        elder_time_iso_local((time_t)e->confirmed_at, conf, sizeof(conf));
        cJSON_AddStringToObject(root, "confirmed_at", conf);
    }
    if (e->note[0]) cJSON_AddStringToObject(root, "note", e->note);

    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) return ESP_ERR_NO_MEM;

    esp_err_t err = elder_net_post("/api/device/events", body, NULL, NULL, NULL);
    cJSON_free(body);
    return err;
}

/* 把队列里能发的都发掉；失败即停止（保持顺序），等待下次 */
static void flush_queue(void)
{
    int sent = 0;
    while (g.q.count) {
        evt_t e;
        if (!queue_pop(&e)) break;
        if (post_event(&e) == ESP_OK) {
            sent++;
        } else {
            /* 放回队首，下次再试（幂等：后端按 (reminder_id,due_at) 定位，重复上报安全） */
            g.q.head = (uint16_t)((g.q.head + ELDER_SYNC_QUEUE_MAX - 1) % ELDER_SYNC_QUEUE_MAX);
            g.q.count++;
            break;
        }
    }
    if (sent) {
        queue_save();
        ESP_LOGI(TAG, "补发 %d 条离线回执，剩余 %u", sent, g.q.count);
    }
}

esp_err_t elder_sync_enqueue_event(uint32_t server_id, time_t due_at,
                                   elder_event_state_t state,
                                   elder_confirm_method_t method,
                                   const char *note)
{
    evt_t e = {0};
    e.server_id = server_id;
    e.due_at = (int64_t)due_at;
    e.state = (uint8_t)state;
    e.method = (uint8_t)method;
    if (state == ELDER_EV_FIRED || state == ELDER_EV_MISSED) e.fired_at = (int64_t)time(NULL);
    if (state == ELDER_EV_CONFIRMED) e.confirmed_at = (int64_t)time(NULL);
    if (note) snprintf(e.note, sizeof(e.note), "%s", note);

    ESP_LOGI(TAG, "回执入队: rid=%lu state=%d method=%d", (unsigned long)server_id,
             (int)state, (int)method);
    queue_push(&e);

    /* 在线则顺手立即发一次，不必等心跳 */
    if (elder_net_is_online()) flush_queue();
    return ESP_OK;
}

void elder_sync_on_reminder_state(const elder_rmd_item_t *item, elder_rmd_state_t st)
{
    if (!item) return;

    uint32_t sid = item->server_id;
    time_t due = (time_t)item->fired_due_at;
    if (due == 0) due = time(NULL);

    switch (st) {
    case ELDER_RMD_PENDING:
        elder_sync_enqueue_event(sid, due, ELDER_EV_FIRED, ELDER_CFM_NONE, "fired");
        break;
    case ELDER_RMD_CONFIRM_WIN:
        /* 只是打开了确认窗口，不上报 */
        break;
    case ELDER_RMD_DONE:
        /* 确认方式由 elder_reminder_confirm_ex() 暂存：
         * 按键路径传 BUTTON，断网语音确认（「吃过了」）传 VOICE。
         * 这里若写死 BUTTON，离线补发的回执会把语音确认误报成按键，
         * 子女端看到的依从率数据就不可信了。 */
        elder_sync_enqueue_event(sid, due, ELDER_EV_CONFIRMED,
                                 (elder_confirm_method_t)elder_reminder_last_confirm_method(),
                                 "confirmed");
        elder_sync_report_interaction(ELDER_IT_REMINDER_CONFIRM, 0);
        break;
    case ELDER_RMD_ESCALATED:
        elder_sync_enqueue_event(sid, due, ELDER_EV_MISSED, ELDER_CFM_NONE, "timeout");
        elder_sync_report_interaction(ELDER_IT_REMINDER_MISS, 0);
        break;
    case ELDER_RMD_SKIPPED:
        /* 到点时设备已离线/断电太久，本条作废、没补响。
         * 报 SKIPPED 而不是 MISSED：子女端看到的是「这次没提醒」，
         * 而不是「老人漏服」—— 后者是假告警，也是这类产品最坏的失败模式。
         * 服务端 home.py 已把 EventState.SKIPPED 排除在「待服药」之外。 */
        elder_sync_enqueue_event(sid, due, ELDER_EV_SKIPPED, ELDER_CFM_NONE, "stale");
        break;
    default:
        break;
    }
}

/* 真正发 HTTP 的那一段（阻塞，只能在 report_task 里跑）。 */
static void report_interaction_blocking(elder_interaction_kind_t kind, int duration_ms)
{
    if (!elder_net_is_online()) {
        ESP_LOGD(TAG, "离线，交互统计 kind=%d 暂不上报", (int)kind);
        return;
    }
    cJSON *root = cJSON_CreateObject();
    cJSON_AddNumberToObject(root, "kind", (double)kind);
    cJSON_AddNumberToObject(root, "duration_ms", (double)duration_ms);
    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) return;
    elder_net_post("/api/device/interactions", body, NULL, NULL, NULL);
    cJSON_free(body);
}

/* 交互上报 worker：唯一允许跑 HTTPS 的地方，栈 12KB（TLS 握手够用）。 */
static void report_worker(void *)
{
    report_t r;
    while (xQueueReceive(g.report_q, &r, portMAX_DELAY) == pdTRUE) {
        report_interaction_blocking((elder_interaction_kind_t)r.kind, r.duration_ms);
    }
}

esp_err_t elder_sync_report_interaction(elder_interaction_kind_t kind, int duration_ms)
{
    /* 🔴 只入队，**绝不在这里发 HTTP**（2026-10-20 真机实证）。
     *
     * 这个函数有三个调用点，全部跑在小栈任务里：
     *   elder_bridge.cc  SOS          → elder_input 任务（input.c，原 2560 字节）
     *   sync.c           服药确认 DONE → elder_rmd 任务（reminder.c，3072 字节）
     *   sync.c           提醒超时     → 同上
     * 而函数体是 elder_net_post()（HTTPS + TLS 握手，栈需求 10KB 量级）。
     *
     * 串口原文（长按 8 秒触发 SOS 后立刻）：
     *   E (21813) elder.bridge: SOS：本地告警 + 联网后上报
     *   ***ERROR*** A stack overflow in task elder_input has been detected.
     *   rst:0xc (RTC_SW_CPU_RST)
     * ⇒ **老人按下 SOS，设备重启，求助根本没发出去**；服药确认那条路径同理
     *   （而且它比 SOS 更常触发）。
     *
     * 所以这里改成投队列入队（非阻塞、几十字节栈），由 report_task 慢慢发。
     * 顺带修掉"输入任务被 HTTP 阻塞到 8 秒超时"这个第二毛病。
     *
     * 返回 ESP_OK 只代表"已排队"，不代表已送达 —— 调用方本来就都不检查返回值；
     * 离线时由 worker 丢弃并打 DEBUG 日志，与旧行为一致。 */
    if (!g.report_q) return ESP_ERR_INVALID_STATE;
    report_t r = { .kind = (uint8_t)kind, .duration_ms = duration_ms };
    if (xQueueSend(g.report_q, &r, 0) != pdTRUE) {
        /* 队列满就丢并留日志：宁可少一条统计，也不能阻塞按键/提醒任务 */
        ESP_LOGW(TAG, "交互上报队列满，丢弃 kind=%d", (int)kind);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

/* ---------------------------------------------------------------- 本地自建提醒上报 */

/* 稳定的幂等键：设备侧不落库，仅用于服务端查重与回写定位。
 * 只取「类型 + 时刻 + 标题」—— 变了就当成新提醒，不会错合并。 */
static void local_client_key(const elder_rmd_item_t *it, char *out, size_t len)
{
    snprintf(out, len, "%d-%02d%02d-%s", (int)it->type, it->hour, it->minute, it->label);
}

/*
 * 把本地 server_id==0 的提醒上报服务端，回写服务端分配的 id。
 *
 * 为什么必须做：老人断网时亲手建的服药提醒只存在于设备 NVS。若不上报，
 * 这些提醒在子女端永远不可见 —— 老人以为服药记录同步给子女了，实际没有，
 * 出事时没人知道药到底吃没吃。
 *
 * 为什么放在 replace_all **之前**：replace_all 会用后端下发的表做全量合并。
 * 本地自建条目虽然会被保住（见 reminder.c），但只要还没拿到 server_id，
 * 它们对服务端而言就是「不存在的规则」，APP 侧看不到、事件也无法对账。
 * 先上报拿到 id，再 replace_all，才能让本地条目与服务端规则真正对上。
 *
 * 失败处理：整个函数失败即放弃本次回写，下次心跳重试。此时 replace_all
 * 仍会保住这些条目（server_id 仍为 0），所以不会丢数据，只是暂时不同步。
 */
static bool upload_local_reminders(void)
{
    const elder_rmd_table_t *tbl = NULL;
    if (elder_reminder_list(&tbl) != ESP_OK || !tbl) return false;

    int n_local = 0;
    for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
        if (tbl->items[i].server_id == 0) n_local++;
    }
    if (n_local == 0) return true;

    /* 请求体放到堆上，不占栈：同步任务栈只有 6KB（见 elder_sync_start 的
     * xTaskCreatePinnedToCore），这里再压 1.4KB 局部数组有溢出风险。
     * 上限按「16 条 × 每条约 90 字节」留足。 */
    #define LOCAL_RPT_BODY_MAX (2 * 1024)
    char *body = heap_caps_malloc(LOCAL_RPT_BODY_MAX, MALLOC_CAP_8BIT);
    if (!body) return false;
    int off = snprintf(body, LOCAL_RPT_BODY_MAX, "[");
    for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
        const elder_rmd_item_t *it = &tbl->items[i];
        if (it->server_id != 0) continue;

        char key[80];
        local_client_key(it, key, sizeof(key));

        char tod[8];
        snprintf(tod, sizeof(tod), "%02d:%02d", it->hour, it->minute);

        /* 固件 repeat_mask 是 bit0=周日..bit6=周六，后端是 1=周一..7=周日，
         * 与 weekdays_to_mask() 的转换互逆：w%7 == bit。 */
        char wd[16] = "";
        int wlen = 0;
        for (int b = 0; b < 7; b++) {
            if (it->repeat_mask & (1u << b)) {
                wlen += snprintf(wd + wlen, sizeof(wd) - (size_t)wlen,
                                 "%s%d", wlen ? "," : "", b == 0 ? 7 : b);
            }
        }

        off += snprintf(body + off, LOCAL_RPT_BODY_MAX - (size_t)off,
                        "%s{\"client_key\":\"%s\",\"title\":\"%s\",\"rtype\":%d,"
                        "\"schedule_kind\":%d,\"times_of_day\":[\"%s\"],"
                        "\"weekdays\":[%s],\"confirm_methods\":[\"voice\",\"button\"],"
                        "\"enabled\":true}",
                        off > 1 ? "," : "", key, it->label, (int)it->rtype,
                        (int)it->schedule_kind, tod, wd);
        if (off < 0 || (size_t)off >= LOCAL_RPT_BODY_MAX) {
            ESP_LOGW(TAG, "本地上报体超长（%u 条），本次放弃，下个心跳重试", n_local);
            heap_caps_free(body);
            return false;
        }
    }
    off += snprintf(body + off, LOCAL_RPT_BODY_MAX - (size_t)off, "]");

    char *resp = heap_caps_malloc(ELDER_NET_RESP_MAX, MALLOC_CAP_8BIT);
    if (!resp) {
        heap_caps_free(body);
        return false;
    }
    size_t len = ELDER_NET_RESP_MAX;
    esp_err_t err = elder_net_post("/api/device/reminders/local", body, resp, &len, NULL);
    heap_caps_free(body);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "本地上报失败（%s），下次心跳重试；提醒仍在本地响铃不受影响",
                 esp_err_to_name(err));
        heap_caps_free(resp);
        return false;
    }

    cJSON *root = cJSON_ParseWithLength(resp, len);
    heap_caps_free(resp);
    if (!root) {
        ESP_LOGW(TAG, "本地上报响应非 JSON（可能响应丢失，下次重试靠幂等去重）");
        return false;
    }

    cJSON *acks = cJSON_GetObjectItem(root, "acks");
    int applied = 0;
    cJSON *a = NULL;
    cJSON_ArrayForEach(a, acks) {
        cJSON *jk = cJSON_GetObjectItem(a, "client_key");
        cJSON *jid = cJSON_GetObjectItem(a, "reminder_id");
        if (!cJSON_IsString(jk) || !cJSON_IsNumber(jid)) continue;

        char key[80];
        snprintf(key, sizeof(key), "%s", jk->valuestring);
        /* 按 client_key 回写：先重新取表（期间可能被 replace_all 动过） */
        if (elder_reminder_list(&tbl) != ESP_OK || !tbl) break;
        for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
            elder_rmd_item_t *it = &tbl->items[i];
            if (it->server_id != 0) continue;
            char mine[80];
            local_client_key(it, mine, sizeof(mine));
            if (strcmp(mine, key) != 0) continue;
            it->server_id = (uint32_t)jid->valueint;
            applied++;
            ESP_LOGI(TAG, "本地提醒已同步: '%s' %02d:%02d -> rid=%u",
                     it->label, it->hour, it->minute, (unsigned)it->server_id);
            break;
        }
    }
    cJSON_Delete(root);

    if (applied) {
        elder_reminder_flush();
        ESP_LOGI(TAG, "本地上报完成：%u 条取得服务端 id", (unsigned)applied);
    }
    return applied > 0;
}

/* ---------------------------------------------------------------- 拉取 */

/* 芯片型号展示名（服务端设备表的「硬件型号」列用它）。
 *
 * esp_chip_info() 在 esp_hw_support 组件里（见 CMakeLists 的 REQUIRES）。
 * switch 只列本项目可能用到的型号；default 兜底 CONFIG_IDF_TARGET
 * （sdkconfig.h 必有该宏，形如 "esp32s3"），再兜底 "unknown"。
 * 三重兜底都不返回 NULL —— URL 拼串的 %s 吃到 NULL 是未定义行为，
 * 而「unknown」至少能让服务端与 UI 显示出一个诚实的占位。 */
static const char *chip_model_name(void)
{
    esp_chip_info_t info;
    esp_chip_info(&info);
    switch (info.model) {
        case CHIP_ESP32:   return "ESP32";
        case CHIP_ESP32S2: return "ESP32-S2";
        case CHIP_ESP32S3: return "ESP32-S3";
        case CHIP_ESP32C3: return "ESP32-C3";
        case CHIP_ESP32C2: return "ESP32-C2";
        case CHIP_ESP32C6: return "ESP32-C6";
        case CHIP_ESP32H2: return "ESP32-H2";
        case CHIP_ESP32P4: return "ESP32-P4";
        case CHIP_ESP32C5: return "ESP32-C5";
        default:
#if defined(CONFIG_IDF_TARGET)
            return CONFIG_IDF_TARGET;
#else
            return "unknown";
#endif
    }
}

static esp_err_t do_sync(void)
{
    static char *resp = NULL;
    if (!resp) {
        resp = heap_caps_malloc(ELDER_NET_RESP_MAX, MALLOC_CAP_8BIT);
        if (!resp) return ESP_ERR_NO_MEM;
    }

    /* 192 字节的最坏占用：固定段 37 + fw_version 31 + hw_model 23 + NUL 1
     * = 92，留一倍余量给将来再加 query 参数（如固件 build id）。
     * hw_model 为空串时退化为旧 URL 形态，老服务端照常解析。 */
    char path[192];
    snprintf(path, sizeof(path), "/api/device/sync?fw_version=%s&hw_model=%s",
             g.fw_version, g.hw_model);

    size_t len = ELDER_NET_RESP_MAX;
    int status = 0;
    esp_err_t err = elder_net_post(path, "{}", resp, &len, &status);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "同步失败（%s）；离线期间本地提醒照常工作", esp_err_to_name(err));
        return err;
    }

    cJSON *root = cJSON_ParseWithLength(resp, len);
    if (!root) {
        ESP_LOGE(TAG, "响应不是合法 JSON（前 120 字节）: %.120s", resp);
        return ESP_ERR_INVALID_RESPONSE;
    }

    /* 1) 校时 —— 提醒准点与否全靠它 */
    cJSON *st = cJSON_GetObjectItem(root, "server_time");
    if (cJSON_IsString(st)) {
        elder_time_apply_server_time(st->valuestring);
    }

    /* 2) 本地自建提醒补报（必须在 replace_all 之前：
     *    这些条目 server_id 还是 0，不先拿到 id 就无法与后端规则对上） */
    upload_local_reminders();

    /* 3) 提醒规则 */
    elder_rmd_item_t items[ELDER_REMINDER_MAX];
    uint8_t count = 0;
    parse_reminders(cJSON_GetObjectItem(root, "reminders"), items, &count);
    if (count > 0) {
        elder_reminder_replace_all(items, count);
    } else {
        ESP_LOGI(TAG, "后端没有启用中的提醒规则，保留本地表兜底");
    }

    /* 4) 心跳周期以服务端下发为准 */
    cJSON *iv = cJSON_GetObjectItem(root, "sync_interval_sec");
    if (cJSON_IsNumber(iv) && iv->valueint >= 60 && iv->valueint <= 3600) {
        g.interval_sec = (uint32_t)iv->valueint;
    }

    /* 5) 形象 / 语音地址（记录到日志，接入层在 bridge 中处理） */
    cJSON *avatar = cJSON_GetObjectItem(root, "avatar");
    if (cJSON_IsObject(avatar)) {
        cJSON *nm = cJSON_GetObjectItem(avatar, "name");
        cJSON *pack = cJSON_GetObjectItem(avatar, "eaf_pack_name");
        ESP_LOGI(TAG, "形象: %s (EAF 包: %s)",
                 cJSON_IsString(nm) ? nm->valuestring : "-",
                 cJSON_IsString(pack) ? pack->valuestring : "-");
    }
    cJSON *ws = cJSON_GetObjectItem(root, "ws_url");
    if (cJSON_IsString(ws)) ESP_LOGD(TAG, "xiaozhi ws=%s", ws->valuestring);

    cJSON_Delete(root);

    /* 联网成功 → 立刻补发离线期间攒下的回执 */
    flush_queue();
    ESP_LOGI(TAG, "同步完成，%u 条提醒，%u 秒后再次同步", count, (unsigned)g.interval_sec);
    return ESP_OK;
}

/* ---------------------------------------------------------------- 任务 */

/* OTA 绑定成功后 token 落在 xiaozhi 的 NVS 命名空间 websocket/token
 * （main/ota.cc:167 用 Settings("websocket", true) 写入；服务端在
 * voice/__init__.py:450-453 的 websocket 段里下发 {"url", "token"}），
 * 而本模块只认 elder_sync/token（文件头的 NVS_NS / NVS_KEY_TOK 定义，
 * 恢复路径在 elder_sync_init() 末尾那段，本函数不复制它）。
 * 两处不交接 ⇒ elder_net_has_token() 恒 false ⇒ 心跳永不发出 ⇒ 服务端
 * devices.online 恒 False、fw_version/hw_model 永远「未上报」，服药提醒
 * 下发与服药回执上报一并全瘫（生产事故 2026-10-20：WS 握手成功、语音
 * 会话已经在跑，管理后台却显示离线 + 硬件型号「未知」/系统版本「未上报」）。
 * 这里直接裸读同一 NVS 命名空间，不必跨到 C++ 的 Settings。
 * 只读不写：拿到就交给 elder_sync_set_token() 持久化到 elder_sync/token，
 * 之后重启走原来那条恢复路径，不新增第二处真相。 */
static void adopt_ws_token(void)
{
    nvs_handle_t h;
    if (nvs_open("websocket", NVS_READONLY, &h) != ESP_OK) return;

    char tok[ELDER_NET_TOKEN_MAX] = {0};
    size_t n = sizeof(tok);
    esp_err_t err = nvs_get_str(h, "token", tok, &n);
    nvs_close(h);

    /* 这一条不是防御性编程的摆设：设备 JWT 目前约 209 字节，紧贴 256 的
     * 上限（payload 里含 sub/device_id/ep）。真超了 nvs_get_str 会返回
     * INVALID_LENGTH，函数静默 return ⇒ 又变回「心跳永远不发」这个原始
     * 故障，而且完全没有日志可查 —— 所以这里必须留一行日志。 */
    if (err == ESP_ERR_NVS_INVALID_LENGTH) {
        ESP_LOGE(TAG, "websocket/token 超过 %d 字节，无法接管；请同步放大 ELDER_NET_TOKEN_MAX",
                 (int)ELDER_NET_TOKEN_MAX);
        return;
    }
    if (err != ESP_OK || !tok[0]) return;

    ESP_LOGI(TAG, "接管 websocket/token 作为设备令牌（绑定已完成）");
    elder_sync_set_token(tok);   /* 同时持久化到 elder_sync/token，重启后走原恢复路径 */
}

static void sync_task(void *arg)
{
    while (1) {
        uint32_t wait_ms = g.interval_sec * 1000;

        /* token 是**运行期**才出现的：OTA 绑定成功那一刻才写进 websocket/token。
         * 所以每轮都试一次接管，不能让「启动时没有 token」一直读到重启。 */
        if (!elder_net_has_token()) {
            adopt_ws_token();
        }

        /* 无 token：只能走激活流程，不做无效重试 */
        if (!elder_net_has_token()) {
            /* 未绑定：最多 15 秒再看一眼。绑定是运行时事件，不能按 300 秒
             * 周期死等 —— 用户扫完码指望半分钟内看到「在线」。 */
            if (wait_ms > 15000) wait_ms = 15000;
            ESP_LOGW(TAG, "尚未绑定（无 device_token），仅本地提醒可用；%u ms 后重试",
                     (unsigned)wait_ms);
        } else {
            esp_err_t err = do_sync();
            if (err != ESP_OK) {
                uint32_t backoff = elder_net_backoff_next_ms(false);
                /* 失败时用退避值覆盖默认周期 */
                wait_ms = backoff ? backoff : wait_ms;
            } else {
                elder_net_backoff_next_ms(true);
            }
        }

        /* 期间可被 elder_sync_request_now() 唤醒；也保证离线时不会死等 */
        xEventGroupWaitBits(g.ev, ELDER_SYNC_EV_NOW, pdTRUE, pdFALSE,
                            pdMS_TO_TICKS(wait_ms));
        elder_ui_tick();
    }
}

esp_err_t elder_sync_init(const elder_sync_cfg_t *cfg)
{
    memset(&g, 0, sizeof(g));

    elder_net_cfg_t ncfg = {0};
    if (cfg && cfg->base_url) snprintf(ncfg.base_url, sizeof(ncfg.base_url), "%s", cfg->base_url);
    else snprintf(ncfg.base_url, sizeof(ncfg.base_url), "%s", CONFIG_ELDER_SERVER_URL);

    if (cfg && cfg->token) snprintf(ncfg.token, sizeof(ncfg.token), "%s", cfg->token);
    else snprintf(ncfg.token, sizeof(ncfg.token), "%s", CONFIG_ELDER_SERVER_TOKEN);

    elder_net_init(&ncfg);

    snprintf(g.fw_version, sizeof(g.fw_version), "%s",
             (cfg && cfg->fw_version) ? cfg->fw_version : "elder-1.0.0");
    /* 硬件型号：cfg 显式给了就用（上层Override 场景），否则自动探测。
     * 与 fw_version 同一形态：cfg 优先、默认值兜底。 */
    snprintf(g.hw_model, sizeof(g.hw_model), "%s",
             (cfg && cfg->hw_model) ? cfg->hw_model : chip_model_name());
    g.interval_sec = (cfg && cfg->interval_sec) ? cfg->interval_sec
                                                : CONFIG_ELDER_SYNC_INTERVAL_SEC;

    queue_load();

    /* 持久化过的 token 优先（NVS 里可能已保存激活结果） */
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        char tok[ELDER_NET_TOKEN_MAX];
        size_t n = sizeof(tok);
        if (nvs_get_str(h, NVS_KEY_TOK, tok, &n) == ESP_OK && tok[0]) {
            elder_net_set_token(tok);
            ESP_LOGI(TAG, "已恢复保存的设备令牌");
        }
        nvs_close(h);
    }

    g.ev = xEventGroupCreate();
    return ESP_OK;
}

esp_err_t elder_sync_set_token(const char *token)
{
    esp_err_t err = elder_net_set_token(token);
    if (err != ESP_OK) return err;

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_str(h, NVS_KEY_TOK, token ? token : "");
        nvs_commit(h);
        nvs_close(h);
    }
    if (token && token[0]) {
        s_token_known = true;   /* 让 elder_sync_token_persisted() 立刻承认 */
    }
    ESP_LOGI(TAG, "设备令牌已更新并保存");
    elder_sync_request_now();
    return ESP_OK;
}

bool elder_sync_token_persisted(void)
{
    /* 锁存：一旦确认绑过就不再开 NVS。本函数会被唤醒引擎的音频任务（如 AfeWakeWord::Feed()）
     * 周期性调用（音频每帧一次的低频抽样），nvs_get_str 每次几十微秒，
     * 不能当热路径用。
     * 反向（token 被清空 = 解绑）只在后台重置设备时发生，那种场景本来就要
     * 重启，锁存不会带来问题。 */
    if (s_token_known) return true;

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return false;
    char tok[ELDER_NET_TOKEN_MAX];
    size_t n = sizeof(tok);
    bool ok = (nvs_get_str(h, NVS_KEY_TOK, tok, &n) == ESP_OK) && tok[0] != '\0';
    nvs_close(h);
    if (ok) s_token_known = true;
    return ok;
}

esp_err_t elder_sync_request_now(void)
{
    if (!g.ev) return ESP_ERR_INVALID_STATE;
    xEventGroupSetBits(g.ev, ELDER_SYNC_EV_NOW);
    return ESP_OK;
}

esp_err_t elder_sync_start(void)
{
    if (g.started) return ESP_OK;
    if (!g.ev && !(g.ev = xEventGroupCreate())) return ESP_ERR_NO_MEM;

    /* 交互上报队列 + worker（见 elder_sync_report_interaction 的注释）。
     * 必须在 elder_net_start 之前就绪 —— SOS 可能在联网完成前被按下，
     * 那时入队会失败并丢一条统计，但**设备不会崩**，这是有意的取舍。 */
    if (!g.report_q && !(g.report_q = xQueueCreate(ELDER_REPORT_Q_LEN, sizeof(report_t)))) {
        return ESP_ERR_NO_MEM;
    }
    if (!g.report_task &&
        xTaskCreatePinnedToCore(report_worker, "elder_report", 12288, NULL, 3, &g.report_task, 1) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    /* 栈 6KB：cJSON 解析 + 24KB 响应缓冲（分配在堆） */
    if (xTaskCreatePinnedToCore(sync_task, "elder_sync", 6144, NULL, 4, &g.task, 1) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    g.started = true;
    elder_net_cfg_t cfgbuf;
    elder_net_get_cfg(&cfgbuf);
    ESP_LOGI(TAG, "同步任务已启动（周期 %u s，后端 %s）",
             (unsigned)g.interval_sec, cfgbuf.base_url);
    return ESP_OK;
}

bool elder_sync_started(void)
{
    return g.started;
}
