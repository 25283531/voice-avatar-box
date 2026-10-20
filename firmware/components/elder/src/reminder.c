#include "elder_reminder.h"
#include "elder_rgb.h"
#include "elder_ui.h"

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

/* 过期阈值（分钟）：到点时设备已经离线/断电超过这么久，就**不再补响**，
 * 直接判 SKIPPED。两条理由，任何一条都足以定这个阈值：
 *   ① 安全：晚上 8 点才播「该吃降压药了」，老人补服后与次日晨药间隔过近，
 *      低血压的风险比"少提醒一次"大得多；
 *   ② 正确性：不挡的话，校时后的**第一个 tick** 里 `now_min >= due` 与
 *      `now_min - due >= 15` 同时成立 —— PENDING 刚设上就被推成 ESCALATED，
 *      子女端收到「漏服」告警，而事实只是设备没开机。
 * 取 60 分钟：比 15 分钟升级阈值宽，能容纳「重启/短暂断网后几分钟内补响」；
 * 又短到不会把半天前的提醒翻出来。 */
#define ELDER_RMD_STALE_MIN     60

/* 确认方式默认值（数值与 elder_sync.h 的 elder_confirm_method_t 一致） */
#define ELDER_CFM_BUTTON_NUM 2

static struct {
    elder_rmd_table_t table;
    elder_rmd_cb_t    cb;
    void             *ctx;
    uint8_t           state[ELDER_REMINDER_MAX];
    int64_t           confirm_deadline_us[ELDER_REMINDER_MAX];
    /* 本轮「已响铃」的时刻（RAM）。升级判定用它而不是 due：
     * 「15 分钟未确认」的语义必须是**从响铃算起**，不能从计划时刻算起，
     * 否则设备晚开机 20 分钟就会在响铃的同一个 tick 里被判漏服。 */
    int64_t           pending_since_us[ELDER_REMINDER_MAX];
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

/* 本地日序号 —— **必须与提醒调度本身用同一个时区**。
 *
 * 曾经的写法是 `(int)(t / 86400)`，那是 **UTC 日**。而同一文件里判断
 * 「几点该响」「今天星期几」全部走 localtime_r()（本地 CST-8）。
 * 两者时区不一致的代价是实打实的：CST-8 下 UTC 日界落在**本地 08:00**，
 * 于是每天早上 8 点 tick() 都会误判为「跨天」→
 *   done_today/pending_today 被清空（子女端看到假「漏服」并误告警）
 *   + fired_today 与状态机归零 → 同一条已确认的提醒**二次响铃**
 *     并立刻升级成 ESCALATED（老人已服药却被要求再吃一次）。
 * 服药类重复服用有实际危险，所以这里按本地 tm 折算日序号。
 *
 * tm_year*366 + tm_yday 单调递增即可；闰年多出来的那个槽位不影响单调性，
 * 而本函数只用于「和上次比是否变了」，不需要真实日历日号。
 * 改成以本地时间为准后，老设备 NVS 里存的是旧的 UTC 日号，
 * 升级后第一次 tick 会多触发一次跨天重置 —— 一次性、且方向是
 * 「重新响一次」，远小于原本「每天 08:00 都响一次」。 */
static int day_ordinal(const struct tm *tm)
{
    return tm->tm_year * 366 + tm->tm_yday;
}

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

/* 从 blob 里按 V1（老格式）布局解读。之所以必须用**独立的 V1 类型**而不是
 * 「按新结构取前几个字段」，是因为 crc32 的覆盖范围随 sizeof 变化：
 * 老固件是按 sizeof(V1) 算出来的，拿 sizeof(V2) 去重算必然不匹配。
 * 所以校验与迁移都只在 V1 的字节数上进行。 */
static bool table_v1_crc_ok(const uint8_t *raw, size_t len)
{
    if (len != sizeof(elder_rmd_table_v1_t)) return false;
    /* 不能直接在 raw 上改 crc32 字段再算（raw 指向 g.table 的缓冲区时可写，
     * 但依赖可写性太脆弱）；拷一份出来算，语义更清楚。 */
    elder_rmd_table_v1_t v1;
    memcpy(&v1, raw, sizeof(v1));
    uint32_t saved = v1.crc32;
    v1.crc32 = 0;
    bool ok = (crc32_simple(&v1, sizeof(v1)) == saved);
    v1.crc32 = saved;
    return ok;
}

/* 把按 V1 解读出来的内容装进运行态，并就地升级成 V2。
 * 老表的 items / count / done_today / pending_today / last_day_ordinal
 * **逐字段原样保留** —— 这些正是老人已有的提醒与已确认记录，
 * 迁移过程中任何一处丢失都等于提醒或服药记录消失。
 * fired_today 从 done_today|pending_today 反推：这俩位图非零即证明
 * 「今天响过」，用它们把重启后二次响铃堵住，同时不制造「响都没响
 * 却说响过」的假抑制。 */
static void migrate_v1_to_v2(const elder_rmd_table_v1_t *v1)
{
    memcpy(g.table.items, v1->items, sizeof(g.table.items));
    g.table.count            = v1->count;
    g.table.done_today       = v1->done_today;
    g.table.pending_today    = v1->pending_today;
    g.table.last_day_ordinal = v1->last_day_ordinal;
    g.table.crc32            = v1->crc32;

    /* 「今天响过」在 V1 里没有独立记录，只能从两个当日位图反推。
     * done_today 的位=老人已确认吃过药；pending_today 的位=响过还没确认。
     * 两者合起来正好覆盖「今天响过」的全体，且不会误判没到点的提醒。 */
    g.table.fired_today = (uint16_t)(v1->done_today | v1->pending_today);
    g.table.fmt_ver     = ELDER_RMD_FMT_V2;

    ESP_LOGW(TAG, "迁移旧格式提醒表(V1) -> V2：%u 条，已完成 0x%04x / 待办 0x%04x / "
                  "响过 0x%04x 全部保留", g.table.count,
             g.table.done_today, g.table.pending_today, g.table.fired_today);
}

static esp_err_t load(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return ESP_ERR_NOT_FOUND;

    /* 按 V2 尺寸读。V1 是 V2 的逐字节前缀（有编译期断言），所以
     * 老 blob 一定读得下，且读进来的前 sizeof(V1) 字节就是合法的 V1。 */
    size_t len = sizeof(g.table);
    esp_err_t err = nvs_get_blob(h, NVS_KEY, &g.table, &len);
    nvs_close(h);
    if (err != ESP_OK) return ESP_ERR_NOT_FOUND;

    /* ---- 版本判定 ----
     * 先按长度分流，再校验各自那一段字节的 CRC。
     * 为什么不能只看 fmt_ver：新 blob 写进来之前，V1/V2 的前 sizeof(V1)
     * 字节完全相同，fmt_ver 在结构体末尾、读的时候根本还不知道它的值
     * （nvs_get_blob 只告诉我们读到了多少字节）。长度是唯一可靠的信号。 */
    if (len == sizeof(elder_rmd_table_t)) {
        if (g.table.fmt_ver != ELDER_RMD_FMT_V2) {
            ESP_LOGE(TAG, "未知提醒表版本 %u，按无数据处理", g.table.fmt_ver);
            memset(&g.table, 0, sizeof(g.table));
            return ESP_ERR_NOT_FOUND;
        }
        uint32_t saved = g.table.crc32;
        g.table.crc32 = 0;
        if (crc32_simple(&g.table, sizeof(g.table)) != saved) {
            ESP_LOGE(TAG, "NVS checksum mismatch, reset table");
            memset(&g.table, 0, sizeof(g.table));
            return ESP_ERR_INVALID_CRC;
        }
        g.table.crc32 = saved;
    } else if (len == sizeof(elder_rmd_table_v1_t)) {
        /* ---- 老设备升级路径 ----
         * 这里**绝不能**返回 NOT_FOUND：那会让 init() memset 掉整张表，
         * 老用户升级固件后老人的服药提醒全部消失（医疗事故级）。
         * CRC 必须按 V1 的尺寸重算 —— 老固件就是按 sizeof(V1) 算的。 */
        if (!table_v1_crc_ok((const uint8_t *)&g.table, len)) {
            ESP_LOGE(TAG, "旧格式提醒表 CRC 校验失败，reset table");
            memset(&g.table, 0, sizeof(g.table));
            return ESP_ERR_INVALID_CRC;
        }
        elder_rmd_table_v1_t v1;
        memcpy(&v1, &g.table, sizeof(v1));
        memset(&g.table, 0, sizeof(g.table));
        migrate_v1_to_v2(&v1);
        /* 迁移必须立刻落盘，否则下次重启又读到 V1 来一遍。 */
        g.dirty = true;
        elder_reminder_flush();
    } else {
        ESP_LOGE(TAG, "提醒表长度异常（%u 字节，既非 V1 也非 V2），按无数据处理",
                 (unsigned)len);
        memset(&g.table, 0, sizeof(g.table));
        return ESP_ERR_NOT_FOUND;
    }

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
             st == ELDER_RMD_ESCALATED ? "ESCALATED" :
             st == ELDER_RMD_SKIPPED ? "SKIPPED" : "IDLE");

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
    } else if (st == ELDER_RMD_SKIPPED) {
        /* 过期作废：既不算完成（不能给绿灯），也不算待办（不能再提醒） */
        g.table.pending_today &= ~(1u << idx);
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
        /* 光打日志等于没说：老人看到的是**一切正常的待机屏**，而当天所有
         * 提醒一条都不会响。所以必须屏显告知。30 分钟一次，够醒目又不刷屏；
         * 只屏显不发声 —— 此刻多半离线，突兀的提示音只会吓人。 */
        static int64_t last_notice_us = -1;   /* -1 = 本次上电还没提示过 */
        if (last_notice_us < 0 || now_us() - last_notice_us > 30LL * 60 * 1000 * 1000) {
            last_notice_us = now_us();
            elder_ui_notice("时间未校准，请连网", "confused");
        }
        return;
    }

    struct tm tm_now;
    localtime_r(&now, &tm_now);

    /* 跨天重置。日序号取自上面已算好的 tm_now（本地时区），
     * 不可退回用 time()/86400 —— 那是 UTC 日，会让 CST-8 下每天 08:00
     * 误判跨天，把已确认的服药记录清空并让提醒二次响铃。详见 day_ordinal()。 */
    int ord = day_ordinal(&tm_now);
    if (g.table.last_day_ordinal != 0 && ord != g.table.last_day_ordinal) {
        ESP_LOGI(TAG, "new day: reset daily state");
        g.table.done_today = 0;
        g.table.pending_today = 0;
        /* 「今天响过」也是**当日**事实，跨天必须一起清。
         * 漏清的后果：新一天的第一次 tick 里，每条已经响过的提醒都会
         * 因为 fired_today 那一位还留着而**当天再也不响** —— 老人从此
         * 收不到当天任何一条服药提醒。方向与「二次响铃」相反，但同样致命。 */
        g.table.fired_today = 0;
        for (int i = 0; i < ELDER_REMINDER_MAX; i++) {
            g.state[i] = ELDER_RMD_IDLE;
            g.fired_today[i] = false;
            g.confirm_deadline_us[i] = 0;
            g.pending_since_us[i] = 0;
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

        /* 触发。三道闸门（缺一道就会在「当天重启」后二次响铃）：
         *   1. !fired_today[i] —— 今天还没响过。这条位图现在**落盘**了，
         *      所以「响过」这个事实在重启后依然成立。
         *   2. state[i]==IDLE —— 没有正在进行/已完成的流程。
         *   3. now_min >= due —— 到点了。
         * 只有这三条同时成立才触发。若只保留第 3 条（本条修复之前的实际状态，
         * 因为 fired_today 从不落盘），则**当天任意时刻重启、只要已过服药
         * 时刻就会补响一遍**：连「本来今天还没到点响过」的提醒也会在开机
         * 瞬间响一次，随后被升级分支立刻推成 ESCALATED。 */
        if (!g.fired_today[i] && g.state[i] == ELDER_RMD_IDLE && now_min >= due) {
            g.fired_today[i] = true;
            g.table.fired_today |= (uint16_t)(1u << i);
            /* 记录本次到期时刻：后端回执靠 (reminder_id, due_at) 定位实例 */
            struct tm due_tm = tm_now;
            due_tm.tm_hour = it->hour;
            due_tm.tm_min  = it->minute;
            due_tm.tm_sec  = 0;
            it->fired_due_at = (int64_t)mktime(&due_tm);

            int late_min = now_min - due;
            if (late_min > ELDER_RMD_STALE_MIN) {
                /* 过期太久：设备在到点前后处于离线/断电状态。
                 * **不补响、不升级**，只报 SKIPPED 让子女端知道「这次没提醒」，
                 * 而不是收到一条「漏服」告警。详见 ELDER_RMD_STALE_MIN 注释。 */
                ESP_LOGW(TAG, "[%s] 已过期 %d 分钟（设备离线/断电期间错过），"
                              "不补响，上报 SKIPPED", it->label, late_min);
                set_state(i, ELDER_RMD_SKIPPED);
            } else {
                g.pending_since_us[i] = now_us();
                set_state(i, ELDER_RMD_PENDING);
            }
        }

        /* 确认窗口超时 → 升级 */
        if (g.state[i] == ELDER_RMD_CONFIRM_WIN && g.confirm_deadline_us[i] &&
            now_us() > g.confirm_deadline_us[i]) {
            g.confirm_deadline_us[i] = 0;
            set_state(i, ELDER_RMD_PENDING);   /* 回到未确认，红灯保持 */
        }

        /* PENDING 超过 15 分钟未确认 → 升级告警。
         * 计时基准是**响铃时刻**（pending_since_us），不是计划时刻 due ——
         * 用 due 的话，设备晚开机 20 分钟就会在响铃的同一个 tick 里被判漏服，
         * 子女端收到假告警。pending_since_us 是 RAM 值，重启后为 0；那时退回
         * due 基准：能走到这里说明这条当天确实响过（fired_today 位已落盘），
         * 判漏服是合理的。 */
        if (g.state[i] == ELDER_RMD_PENDING) {
            bool overdue = g.pending_since_us[i]
                ? (now_us() - g.pending_since_us[i]) >= (15LL * 60 * 1000 * 1000)
                : (now_min - due) >= 15;
            if (overdue) set_state(i, ELDER_RMD_ESCALATED);
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

/* 从落盘的「今日位图」把运行态恢复出来。
 *
 * 为什么必须恢复：state[] 与 fired_today[] 只活在 RAM 里，重启后靠 C 静态
 * 零初始化变成「全 IDLE + 全没响过」。而 tick() 的触发条件是
 *   `!fired_today[i] && state[i]==IDLE && now_min >= due`
 * 于是当天任意时刻重启，只要已过服药时刻，这条提醒就会二次响铃，
 * 并在同一个 tick 里被 `(now_min-due)>=15` 直接推成 ESCALATED：
 *   done_today 里那一位在 set_state(PENDING) 时被 `&= ~bit` 清掉，
 *   子女端看到假「漏服」并收到 MISSED 告警，老人已吃过药却被要求再吃一次。
 *
 * 恢复规则（保守：只从**确实落过盘**的事实推断，不凭空造状态）：
 *   done_today  有位 -> DONE     （已确认服药，今天不该再响）
 *   pending_today 有位 -> PENDING（响过但未确认，保持红灯等老人）
 *   fired_today 有位 -> 至少已经响过，绝不二次触发
 * 其余保持 IDLE。
 *
 * 注意这里**不调用 set_state()**：那会重放副作用（打灯、回调 sync 上报回执），
 * 把「重启」本身变成一次「又一次触发了所有提醒」，正是本函数要消灭的故障。
 * 直接写 g.state[]，并顺手把位图对齐（fired_today 必须覆盖 done/pending：
 * 任何 done/pending 的条目按定义都响过）。 */
static void restore_daily_state(void)
{
    uint16_t before = g.table.fired_today;
    for (int i = 0; i < ELDER_REMINDER_MAX; i++) {
        bool done    = (g.table.done_today    & (1u << i)) != 0;
        bool pending = (g.table.pending_today & (1u << i)) != 0;
        bool fired   = (g.table.fired_today   & (1u << i)) != 0;

        /* 反向也补上：done/pending 的条目必然响过，即便 fired_today 那一位
         * 丢了（V1 迁移、或历史数据不一致），也不能让它们二次响铃。 */
        if (done || pending) fired = true;
        if (fired) g.table.fired_today |= (uint16_t)(1u << i);

        g.fired_today[i] = fired;
        g.confirm_deadline_us[i] = 0;   /* 30s 确认窗跨重启无意义 */

        if (done) {
            g.state[i] = ELDER_RMD_DONE;
        } else if (pending) {
            g.state[i] = ELDER_RMD_PENDING;
        } else {
            g.state[i] = ELDER_RMD_IDLE;
        }
    }
    /* 补齐过的位（done/pending 反推出 fired）必须落盘，否则下一次重启
     * 又要靠同样的推断走一遍；更重要的是别让它在 V1 迁移那种
     * fired_today 缺失的场景里反复靠猜。 */
    if (g.table.fired_today != before) g.dirty = true;
}

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
    /* 盖上版本号。任何**本固件写出**的表都必须是 V2：新建表与迁移表
     * 走的是同一条路，load() 里靠 fmt_ver 判定读取布局，所以漏盖的后果
     * 是「下一次重启把这张表当成未知版本丢弃」—— 即刚加的提醒白加。
     * 放在 load() 之后统一盖，覆盖「无数据新建」与「迁移成功」两种情况。 */
    g.table.fmt_ver = ELDER_RMD_FMT_V2;
    /* 恢复当天运行态（旧表无数据时这里是全 IDLE，行为与修复前一致） */
    restore_daily_state();

    /* 栈 8192 而不是 3072：2026-10-20 线上实测
     *   ***ERROR*** A stack overflow in task elder_rmd has been detected.
     * 溢出路径是 set_state() 的同步回调链：
     *   tick() -> set_state() -> OnReminder() -> elder_ui_announce()
     *     -> OnSpeak() -> Application::SendMcpMessage()
     * 最后一环是 C++ std::string 拼 JSON 再走 WebSocket 发送，
     * 单这一段的栈需求就超过 3KB，再加上 set_state 里的 ESP_LOGx 与
     * nvs_set_blob，3072 必崩。
     *
     * ⚠️ 崩溃会形成**稳定循环**（这是最狠的地方）：崩 -> 重启 ->
     * restore_daily_state() 把 pending_today 恢复成 PENDING -> 下一轮 tick
     * 里 (now_min-due)>=15 又推成 ESCALATED -> 再次回调播报 -> 再崩。
     * 线上表现为设备每 ~18 秒重启一次、语音每次播到一半就断
     * （用户原话：「话没有说完整」「发音卡卡顿顿」）。
     *
     * 取 8192 的依据：同工程里 elder_report 12288、elder_sync 6144，
     * 这条链路要跑 UI+TTS+WS，比 sync 深，取两者之间偏上。 */
    xTaskCreatePinnedToCore(reminder_task, "elder_rmd", 8192, NULL, 5, &g.task, 1);
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
    /* fired_today 位图必须跟着重算，否则「今天响过」这个事实会与
     * items[] 的新下标脱节：同步一次之后，旧下标 i 的那一位还在，
     * 新下标 j 的那一位空着 —— 结果要么某条响过的提醒当天再也不响，
     * 要么某条没响过的被误判成响过而静默漏掉。 */
    g.table.fired_today = 0;
    for (uint8_t i = 0; i < n; i++) {
        if (g.state[i] == ELDER_RMD_DONE) {
            g.table.done_today |= (1u << i);
        } else if (g.state[i] == ELDER_RMD_PENDING || g.state[i] == ELDER_RMD_ESCALATED ||
                   g.state[i] == ELDER_RMD_CONFIRM_WIN) {
            g.table.pending_today |= (1u << i);
        }
        if (g.fired_today[i]) g.table.fired_today |= (1u << i);
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
