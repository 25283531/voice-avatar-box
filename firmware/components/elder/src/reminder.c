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

/* 低于这个时刻认为系统时钟不可信（未校时/掉电归零）。
 * 时间不可信时**绝不触发提醒** —— 响错时间的服药提醒比不提醒更危险。 */
#define ELDER_MIN_TRUSTED_EPOCH 1577836800L   /* 2020-01-01T00:00:00Z */

/* 确认方式默认值（数值与 elder_sync.h 的 elder_confirm_method_t 一致） */
#define ELDER_CFM_BUTTON_NUM 2

static struct {
    elder_rmd_table_t table;
    elder_rmd_cb_t    cb;
    void             *ctx;
    uint8_t           state[ELDER_REMINDER_MAX];
    int64_t           confirm_deadline_us[ELDER_REMINDER_MAX];
    bool              fired_today[ELDER_REMINDER_MAX];
    bool              dirty;
    int64_t           last_flush_us;
    uint8_t           last_confirm_method;
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
    if (idx < 0 || idx >= ELDER_REMINDER_MAX) return;
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

    /* 时间不可信（未校时/掉电归零）→ 暂停触发，等联网校时。
     * 依据：用户明确要求「时间不准时，离线提醒就是错的，比不提醒更危险」。 */
    if (now < (time_t)ELDER_MIN_TRUSTED_EPOCH) {
        static int64_t last_warn_us = 0;
        if (now_us() - last_warn_us > 60LL * 1000 * 1000) {
            last_warn_us = now_us();
            ESP_LOGE(TAG, "系统时钟不可信（未校时），暂停触发提醒；联网后自动校准");
        }
        return;
    }

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
            g.table.items[i].fired_due_at = 0;
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
            /* 记录本次到期时刻：后端回执靠 (reminder_id, due_at) 定位实例 */
            struct tm due_tm = tm_now;
            due_tm.tm_hour = it->hour;
            due_tm.tm_min  = it->minute;
            due_tm.tm_sec  = 0;
            it->fired_due_at = (int64_t)mktime(&due_tm);
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

void elder_reminder_tick_once(void)
{
    tick();
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

/* 两条 item 是否「同一条内容」（用于本地条目与后端条目的去重，
 * 以及运行态继承时的本地条目匹配） */
static bool same_content(const elder_rmd_item_t *a, const elder_rmd_item_t *b)
{
    return a->hour == b->hour && a->minute == b->minute &&
           a->type == b->type && a->repeat_mask == b->repeat_mask &&
           strncmp(a->label, b->label, ELDER_REMINDER_LABEL) == 0;
}

esp_err_t elder_reminder_replace_all(const elder_rmd_item_t *items, uint8_t count)
{
    if (count > ELDER_REMINDER_MAX) return ESP_ERR_INVALID_SIZE;

    /* ---- 1. 快照旧表与运行态 ---- */
    elder_rmd_item_t old[ELDER_REMINDER_MAX];
    uint8_t old_n = g.table.count > ELDER_REMINDER_MAX ? ELDER_REMINDER_MAX : g.table.count;
    memcpy(old, g.table.items, sizeof(elder_rmd_item_t) * old_n);
    uint8_t old_state[ELDER_REMINDER_MAX];
    bool    old_fired[ELDER_REMINDER_MAX];
    int64_t old_deadline[ELDER_REMINDER_MAX];
    memcpy(old_state, g.state, sizeof(old_state));
    memcpy(old_fired, g.fired_today, sizeof(old_fired));
    memcpy(old_deadline, g.confirm_deadline_us, sizeof(old_deadline));

    /* ---- 2. 挑出「设备本地自建」（server_id==0）条目 —— 后端同步绝不能抹掉它们 ----
     * 这是本函数存在的理由：断网时老人亲手建的服药提醒只存在于设备 NVS，
     * 服务端毫不知情；一次 replace_all 全量覆盖就会让它们永久消失，
     * 属于医疗事故级数据丢失。 */
    elder_rmd_item_t keep[ELDER_REMINDER_MAX];
    uint8_t keep_n = 0;
    for (uint8_t i = 0; i < old_n; i++) {
        if (old[i].server_id != 0) continue;
        /* 与后端下发内容相同的本地条目视为同一条（上传后回写 server_id 失败
         * 或响应丢失时的兜底），直接丢弃避免重复响铃 */
        bool dup = false;
        for (uint8_t j = 0; j < count; j++) {
            if (same_content(&old[i], &items[j])) { dup = true; break; }
        }
        if (dup) {
            ESP_LOGW(TAG, "本地条目与后端下发重复，按后端版本保留: %s %02d:%02d",
                     old[i].label, old[i].hour, old[i].minute);
            continue;
        }
        if (keep_n < ELDER_REMINDER_MAX) keep[keep_n++] = old[i];
    }

    /* ---- 3. 组装新表：后端下发在前，本地保留在后 ----
     * 容量溢出时优先保本地（服务端没有的数据丢了就找不回来），
     * 后端放不下的丢弃并大声告警。 */
    uint8_t srv_n = count;
    if (srv_n + keep_n > ELDER_REMINDER_MAX) {
        uint8_t room = (uint8_t)(ELDER_REMINDER_MAX - keep_n);
        ESP_LOGE(TAG, "提醒表溢出：后端 %u 条 + 本地 %u 条 > %d，丢弃 %u 条后端下发",
                 count, keep_n, ELDER_REMINDER_MAX, srv_n - room);
        srv_n = room;
    }

    /* ---- 4. 安装 + 运行态继承（按 server_id / 内容匹配旧表）---- */
    memset(g.table.items, 0, sizeof(g.table.items));
    uint8_t n = 0;
    for (uint8_t i = 0; i < srv_n; i++) {
        g.table.items[n] = items[i];
        g.table.items[n].id = n;
        g.table.items[n].label[ELDER_REMINDER_LABEL] = '\0';
        n++;
    }
    for (uint8_t i = 0; i < keep_n; i++) {
        g.table.items[n] = keep[i];
        g.table.items[n].id = n;
        g.table.items[n].label[ELDER_REMINDER_LABEL] = '\0';
        n++;
    }

    for (uint8_t i = 0; i < n; i++) {
        const elder_rmd_item_t *it = &g.table.items[i];
        int old_idx = -1;
        if (it->server_id != 0) {
            for (uint8_t j = 0; j < old_n; j++) {
                if (old[j].server_id == it->server_id) { old_idx = (int)j; break; }
            }
        } else {
            for (uint8_t j = 0; j < old_n; j++) {
                if (old[j].server_id == 0 && same_content(&old[j], it)) {
                    old_idx = (int)j;
                    break;
                }
            }
        }
        if (old_idx >= 0) {
            /* 沿用旧表同一条的运行态，避免同步打断老人当天的进度 */
            g.state[i] = old_state[old_idx];
            g.fired_today[i] = old_fired[old_idx];
            g.confirm_deadline_us[i] = old_deadline[old_idx];
        } else {
            g.state[i] = ELDER_RMD_IDLE;
            g.fired_today[i] = false;
            g.confirm_deadline_us[i] = 0;
        }
    }
    for (uint8_t i = n; i < ELDER_REMINDER_MAX; i++) {
        g.state[i] = ELDER_RMD_IDLE;
        g.fired_today[i] = false;
        g.confirm_deadline_us[i] = 0;
    }

    /* ---- 5. 位图按新状态重算 ---- */
    g.table.count = n;
    g.table.done_today = 0;
    g.table.pending_today = 0;
    for (uint8_t i = 0; i < n; i++) {
        if (g.state[i] == ELDER_RMD_DONE) {
            g.table.done_today |= (1u << i);
        } else if (g.state[i] == ELDER_RMD_PENDING || g.state[i] == ELDER_RMD_ESCALATED ||
                   g.state[i] == ELDER_RMD_CONFIRM_WIN) {
            g.table.pending_today |= (1u << i);
        }
    }
    /* ---- 6. 自检：本地自建条目一条都不许丢 ----
     * 这是本函数最容易写错、后果最重的一步。老人断网时亲手建的服药提醒
     * 只存在于这台设备，一次全量覆盖就会让它们**永久消失**（没有任何副本），
     * 老人从此再也收不到那条服药提醒 —— 医疗事故级数据丢失。
     * 下面的断言把「不该发生」变成「当场发现」，宁可刷屏报错也不能静默丢。 */
    for (uint8_t k = 0; k < keep_n; k++) {
        bool survived = false;
        for (uint8_t i = 0; i < n; i++) {
            if (same_content(&g.table.items[i], &keep[k])) { survived = true; break; }
        }
        if (!survived) {
            ESP_LOGE(TAG, "严重：本地自建提醒在合并中丢失 '%s' %02d:%02d —— "
                          "服药提醒丢失属医疗事故级数据丢失，请立即上报",
                     keep[k].label, keep[k].hour, keep[k].minute);
        }
    }

    g.dirty = true;
    ESP_LOGI(TAG, "提醒表已合并：后端 %u 条 + 本地 %u 条 = %u 条", srv_n, keep_n, n);
    return elder_reminder_flush();
}

int elder_reminder_find_by_server_id(uint32_t server_id)
{
    if (!server_id) return -1;
    for (int i = 0; i < g.table.count && i < ELDER_REMINDER_MAX; i++) {
        if (g.table.items[i].server_id == server_id) return i;
    }
    return -1;
}

time_t elder_reminder_get_due_at(uint8_t id)
{
    if (id >= g.table.count) return 0;
    return (time_t)g.table.items[id].fired_due_at;
}

esp_err_t elder_reminder_list(const elder_rmd_table_t **out)
{
    *out = &g.table;
    return ESP_OK;
}

esp_err_t elder_reminder_confirm(uint8_t id)
{
    return elder_reminder_confirm_ex(id, ELDER_CFM_BUTTON_NUM);
}

esp_err_t elder_reminder_confirm_ex(uint8_t id, uint8_t confirm_method)
{
    if (id >= g.table.count) return ESP_ERR_NOT_FOUND;
    /* 先记方式再改状态：set_state() 会同步回调 elder_sync_on_reminder_state，
     * 那时 sync 层要能读到正确的确认方式。 */
    g.last_confirm_method = confirm_method;
    g.confirm_deadline_us[id] = 0;
    set_state(id, ELDER_RMD_DONE);
    elder_reminder_flush();
    return ESP_OK;
}

uint8_t elder_reminder_last_confirm_method(void)
{
    return g.last_confirm_method;
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
