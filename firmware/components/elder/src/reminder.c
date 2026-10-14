#include "elder_reminder.h"
#include "elder_rgb.h"

#include "nvs.h"
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <string.h>
#include <time.h>

static const char *TAG = "elder.reminder";
#define NVS_NS   "elder_rmd"
#define NVS_KEY  "table"

static struct {
    elder_rmd_table_t table;
    elder_rmd_cb_t    cb;
    void             *ctx;
    uint8_t           state[ELDER_REMINDER_MAX];
    int64_t           confirm_deadline_us[ELDER_REMINDER_MAX];
    bool              fired_today[ELDER_REMINDER_MAX];
    bool              dirty;
    int64_t           last_flush_us;
    TaskHandle_t      task;
} g;

static int64_t now_us(void);

/* 简易 CRC32（避免为校验引入额外组件） */
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

static int day_ordinal(time_t t) { return (int)(t / 86400); }

static int weekday_mask_from_tm(const struct tm *tm)
{
    /* tm_wday: 0=周日 .. 6=周六 */
    return 1 << tm->tm_wday;
}

static bool item_due_today(const elder_rmd_item_t *it, const struct tm *tm)
{
    if (!it->enabled) return false;
    if (it->repeat_mask == 0) return true;              /* 一次性 */
    return (it->repeat_mask & weekday_mask_from_tm(tm)) != 0;
}

/* ---------------- 持久化 ---------------- */

static esp_err_t load(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return ESP_ERR_NOT_FOUND;

    size_t len = sizeof(g.table);
    esp_err_t err = nvs_get_blob(h, NVS_KEY, &g.table, &len);
    nvs_close(h);
    if (err != ESP_OK || len != sizeof(g.table)) return ESP_ERR_NOT_FOUND;

    uint32_t saved = g.table.crc32;
    g.table.crc32 = 0;
    if (crc32_simple(&g.table, sizeof(g.table)) != saved) {
        ESP_LOGE(TAG, "NVS checksum mismatch, reset table");
        memset(&g.table, 0, sizeof(g.table));
        return ESP_ERR_INVALID_CRC;
    }
    g.table.crc32 = saved;
    ESP_LOGI(TAG, "loaded %u reminder(s)", g.table.count);
    return ESP_OK;
}

esp_err_t elder_reminder_flush(void)
{
    nvs_handle_t h;
    ESP_ERROR_CHECK_WITHOUT_ABORT(nvs_open(NVS_NS, NVS_READWRITE, &h));
    g.table.crc32 = 0;
    g.table.crc32 = crc32_simple(&g.table, sizeof(g.table));
    esp_err_t err = nvs_set_blob(h, NVS_KEY, &g.table, sizeof(g.table));
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    g.dirty = false;
    g.last_flush_us = now_us();
    return err;
}

/* ---------------- 状态机 ---------------- */

static void set_state(int idx, elder_rmd_state_t st)
{
    if (g.state[idx] == (uint8_t)st) return;
    g.state[idx] = (uint8_t)st;
    ESP_LOGI(TAG, "[%s] %s -> %s", g.table.items[idx].label,
             g.table.items[idx].type == ELDER_RMD_TYPE_MEDICINE ? "med" : "sched",
             st == ELDER_RMD_PENDING ? "PENDING" :
             st == ELDER_RMD_CONFIRM_WIN ? "CONFIRM_WIN" :
             st == ELDER_RMD_DONE ? "DONE" :
             st == ELDER_RMD_ESCALATED ? "ESCALATED" : "IDLE");

    /* 需求 11：服药未完成 → 红灯长亮；完成 → 绿灯 */
    if (g.table.items[idx].type == ELDER_RMD_TYPE_MEDICINE) {
        if (st == ELDER_RMD_DONE)        elder_rgb_med_done();
        else if (st == ELDER_RMD_PENDING) elder_rgb_med_pending();
        else if (st == ELDER_RMD_ESCALATED) elder_rgb_med_escalate();
    }

    if (st == ELDER_RMD_DONE) {
        g.table.done_today |= (1u << idx);
        g.table.pending_today &= ~(1u << idx);
    } else if (st == ELDER_RMD_PENDING || st == ELDER_RMD_ESCALATED) {
        g.table.pending_today |= (1u << idx);
        g.table.done_today &= ~(1u << idx);
    }
    g.dirty = true;
    if (g.cb) g.cb(&g.table.items[idx], st, g.ctx);
}

static void tick(void)
{
    time_t now = time(NULL);
    struct tm tm_now;
    localtime_r(&now, &tm_now);

    /* 跨天重置 */
    int ord = day_ordinal(now);
    if (g.table.last_day_ordinal != 0 && ord != g.table.last_day_ordinal) {
        ESP_LOGI(TAG, "new day: reset daily state");
        g.table.done_today = 0;
        g.table.pending_today = 0;
        for (int i = 0; i < ELDER_REMINDER_MAX; i++) {
            g.state[i] = ELDER_RMD_IDLE;
            g.fired_today[i] = false;
            g.confirm_deadline_us[i] = 0;
        }
        g.dirty = true;
    }
    g.table.last_day_ordinal = ord;

    int now_min = tm_now.tm_hour * 60 + tm_now.tm_min;

    for (int i = 0; i < g.table.count && i < ELDER_REMINDER_MAX; i++) {
        elder_rmd_item_t *it = &g.table.items[i];
        if (!item_due_today(it, &tm_now)) continue;

        int due = it->hour * 60 + it->minute;

        /* 触发 */
        if (!g.fired_today[i] && g.state[i] == ELDER_RMD_IDLE && now_min >= due) {
            g.fired_today[i] = true;
            set_state(i, ELDER_RMD_PENDING);
        }

        /* 确认窗口超时 → 升级 */
        if (g.state[i] == ELDER_RMD_CONFIRM_WIN && g.confirm_deadline_us[i] &&
            now_us() > g.confirm_deadline_us[i]) {
            g.confirm_deadline_us[i] = 0;
            set_state(i, ELDER_RMD_PENDING);   /* 回到未确认，红灯保持 */
        }

        /* PENDING 超过 15 分钟未确认 → 升级告警 */
        if (g.state[i] == ELDER_RMD_PENDING && (now_min - due) >= 15) {
            set_state(i, ELDER_RMD_ESCALATED);
        }
    }

    /* NVS 去抖：脏数据最多 30s 落一次盘，降低 Flash 磨损 */
    if (g.dirty && (now_us() - g.last_flush_us) > 30LL * 1000 * 1000) {
        elder_reminder_flush();
    }
}

static void reminder_task(void *arg)
{
    while (1) {
        tick();
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static int64_t now_us(void) { return esp_timer_get_time(); }

/* ---------------- 对外接口 ---------------- */

esp_err_t elder_reminder_init(elder_rmd_cb_t cb, void *ctx)
{
    memset(&g, 0, sizeof(g));
    g.cb = cb;
    g.ctx = ctx;
    g.last_flush_us = now_us();

    if (load() != ESP_OK) {
        memset(&g.table, 0, sizeof(g.table));
        ESP_LOGW(TAG, "no saved table, starting empty");
    }
    for (int i = 0; i < ELDER_REMINDER_MAX; i++) g.state[i] = ELDER_RMD_IDLE;

    xTaskCreatePinnedToCore(reminder_task, "elder_rmd", 3072, NULL, 5, &g.task, 1);
    ESP_LOGI(TAG, "reminder engine ready");
    return ESP_OK;
}

esp_err_t elder_reminder_add(const elder_rmd_item_t *item)
{
    if (g.table.count >= ELDER_REMINDER_MAX) return ESP_ERR_NO_MEM;
    int idx = g.table.count;
    memcpy(&g.table.items[idx], item, sizeof(elder_rmd_item_t));
    g.table.items[idx].id = (uint8_t)idx;
    g.table.items[idx].label[ELDER_REMINDER_LABEL] = '\0';
    g.table.count++;
    g.dirty = true;
    elder_reminder_flush();
    return ESP_OK;
}

esp_err_t elder_reminder_del(uint8_t id)
{
    if (id >= g.table.count) return ESP_ERR_NOT_FOUND;
    for (int i = id; i < (int)g.table.count - 1; i++) {
        memcpy(&g.table.items[i], &g.table.items[i + 1], sizeof(elder_rmd_item_t));
        g.table.items[i].id = (uint8_t)i;
    }
    g.table.count--;
    g.dirty = true;
    elder_reminder_flush();
    return ESP_OK;
}

esp_err_t elder_reminder_set_enabled(uint8_t id, bool enabled)
{
    if (id >= g.table.count) return ESP_ERR_NOT_FOUND;
    g.table.items[id].enabled = enabled;
    g.dirty = true;
    return ESP_OK;
}

esp_err_t elder_reminder_list(const elder_rmd_table_t **out)
{
    *out = &g.table;
    return ESP_OK;
}

esp_err_t elder_reminder_confirm(uint8_t id)
{
    if (id >= g.table.count) return ESP_ERR_NOT_FOUND;
    g.confirm_deadline_us[id] = 0;
    set_state(id, ELDER_RMD_DONE);
    elder_reminder_flush();
    return ESP_OK;
}

void elder_reminder_arm_confirm(uint8_t id)
{
    if (id >= g.table.count) return;
    if (g.state[id] != ELDER_RMD_PENDING && g.state[id] != ELDER_RMD_ESCALATED) return;
    set_state(id, ELDER_RMD_CONFIRM_WIN);
    g.confirm_deadline_us[id] = now_us() + 30LL * 1000 * 1000;
}

bool elder_reminder_has_pending_medicine(void)
{
    for (int i = 0; i < g.table.count && i < ELDER_REMINDER_MAX; i++) {
        if (g.table.items[i].type != ELDER_RMD_TYPE_MEDICINE) continue;
        if (!g.table.items[i].enabled) continue;
        if (g.state[i] == ELDER_RMD_PENDING ||
            g.state[i] == ELDER_RMD_CONFIRM_WIN ||
            g.state[i] == ELDER_RMD_ESCALATED) {
            return true;
        }
    }
    return false;
}

int32_t elder_reminder_next_in_sec(void)
{
    time_t now = time(NULL);
    struct tm tm_now;
    localtime_r(&now, &tm_now);
    int now_min = tm_now.tm_hour * 60 + tm_now.tm_min;
    int best = -1;

    for (int i = 0; i < g.table.count && i < ELDER_REMINDER_MAX; i++) {
        elder_rmd_item_t *it = &g.table.items[i];
        if (!item_due_today(it, &tm_now)) continue;
        if (g.state[i] != ELDER_RMD_IDLE) continue;
        int due = it->hour * 60 + it->minute;
        int delta = due - now_min;
        if (delta < 0) delta += 24 * 60;
        if (best < 0 || delta < best) best = delta;
    }
    return best < 0 ? -1 : (int32_t)best * 60;
}
