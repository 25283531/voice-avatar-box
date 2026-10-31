#include "elder_reminder.h"
#include "elder_rgb.h"
#include "elder_ui.h"
/* 确认方式（ELDER_CFM_VOICE / ELDER_CFM_BUTTON …）的**唯一真源**在
 * elder_sync.h 的 elder_confirm_method_t。这里原来有一份本地
 * `#define ELDER_CFM_BUTTON_NUM 2` 兜着，理由是"不想让提醒引擎依赖同步层"，
 * 代价是同一个枚举两处定义 —— 2026-10-27 加「语音确认=半置信」时，
 * 本地那份没有 VOICE，编译才发现。直接 include 更省事：elder_sync.h 本身
 * 已经 include elder_reminder.h，头文件保护能挡住循环。 */
#include "elder_sync.h"

#include "nvs.h"
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
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

/* 半置信的「复核窗口」（2026-10-28 用户定义）。
 *
 * 语义：老人用语音说了「我吃过了」⇒ 进 HALF_CONFIRMED，设备提示他长按按钮
 * 复核。这个窗口内按了 = 全置信（DONE）；窗口过了还没按 = **跳过这一次**
 * （SKIPPED）—— 不再闪灯、不再橙色呼吸、不再升级漏服，服务端那边保持
 * 已经上报的「语音确认」标记不变（子女端网页/APP 看得见）。
 *
 * 为什么是 10 分钟：
 *   ① 老人可能正在倒水、找药，手也不一定利索，给 1~2 分钟等于逼他手忙脚乱；
 *   ② 正好与 nag 的「每 10 分钟闪 10 秒」对齐 —— 窗口末尾那一次闪灯就是
 *      最后一次提醒，闪完不按就安静下来，不需要额外定时器。
 *
 * 为什么必须有个上限、不能一直催：语音确认本身就是**半置信**。老人不按
 * 按钮，说明他要么不方便、要么压根没听清设备在说什么。无限期橙色呼吸 +
 * 每 10 分钟闪一次，夜里会一直折腾他 —— 而这条提醒在服务端已经标成
 * 「语音确认」，子女端看得见，不需要靠设备继续骚扰来保证知情。
 *
 * ⚠️ 跳过**不等于完成**：done_today 绝不置位，否则「半置信」这个设计
 *    就白做了（语音误识别会被当成真吃了药）。 */
#define ELDER_HALF_REVIEW_MS    (10u * 60u * 1000u)

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
    /* 进入 HALF_CONFIRMED 的时刻（RAM）。复核窗口从这个时刻起算，
     * 见 ELDER_HALF_REVIEW_MS。
     *
     * 为什么只放 RAM、不落盘：状态机本身（state[]）就是 RAM 的，重启后
     * 全部回 IDLE；而 fired_today 是落盘的，所以重启后这条提醒不会重响、
     * 也就不会再有"复核窗口"。落盘一个和 state[] 生命周期不一致的时刻，
     * 只会在跨重启时给出错误的窗口起点。 */
    int64_t           half_since_us[ELDER_REMINDER_MAX];
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

/* 今天该不该响。判定**全部委托**给 elder_rmd_sched.c 的纯函数。
 *
 * 原先这里只有两行：
 *     if (it->repeat_mask == 0) return true;   // 注释写"一次性"
 *     return (it->repeat_mask & weekday_mask_from_tm(tm)) != 0;
 * 两个问题叠在一起就成了用户实报的 bug「设成一次性的喝水提醒每天响」：
 *   1) `mask==0` 返回的是 **true（永远到期）**，注释"一次性"是错的；
 *   2) sync.c 在"没有 weekdays"时把 mask 强制写成 0x7F（每天），
 *      一次性规则连 0 都拿不到。
 * 现在 kind 决定语义、mask 只做 WEEKLY 的位图，两条都不再靠猜。 */
static bool item_due_today(int idx, const struct tm *tm, int today_ymd, int today_ordinal)
{
    if (idx < 0 || idx >= ELDER_REMINDER_MAX) return false;
    const elder_rmd_item_t *it = &g.table.items[idx];
    if (!it->enabled) return false;
    return elder_rmd_due_on(&g.table.when[idx], today_ymd, today_ordinal, tm->tm_wday);
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

/* V2 布局的 CRC 校验。与 V1 同理：老固件是按 sizeof(V2) 算的，
 * 拿 sizeof(V3) 去重算必然不匹配。 */
static bool table_v2_crc_ok(const uint8_t *raw, size_t len)
{
    if (len != sizeof(elder_rmd_table_v2_t)) return false;
    elder_rmd_table_v2_t v2;
    memcpy(&v2, raw, sizeof(v2));
    uint32_t saved = v2.crc32;
    v2.crc32 = 0;
    bool ok = (crc32_simple(&v2, sizeof(v2)) == saved);
    v2.crc32 = saved;
    return ok;
}

/* 老表（V1 / V2）装进运行态的公共部分。
 *
 * items / count / done_today / pending_today / last_day_ordinal **逐字段
 * 原样保留** —— 这些正是老人已有的提醒与已确认记录，迁移过程中任何一处
 * 丢失都等于提醒或服药记录消失。
 *
 * ⚠️ when[] 必须**显式置成 DAILY**，不能只 memset 成 0：
 *    零值是 kind=0(ONCE) + once_ymd=0，而 ONCE 缺日期在 elder_rmd_due_on()
 *    里判为"不到期"（还要告警）。老用户升级后**所有**提醒会变成永不响，
 *    比原来的"天天响"更危险 —— 该响的时候不响。
 *    老固件的实际行为是"只看 repeat_mask，有 mask 按星期、没 mask 每天"，
 *    统一按 DAILY 迁移是唯一不会让已有提醒消失的选择。 */
static void adopt_legacy(const elder_rmd_item_t *items, uint8_t count,
                         uint16_t done_today, uint16_t pending_today,
                         uint16_t fired_today, int32_t last_day_ordinal)
{
    memcpy(g.table.items, items, sizeof(g.table.items));
    g.table.count            = count;
    g.table.done_today       = done_today;
    g.table.pending_today    = pending_today;
    g.table.fired_today      = fired_today;
    g.table.last_day_ordinal = last_day_ordinal;
    g.table.fmt_ver          = ELDER_RMD_FMT_V3;

    for (int i = 0; i < ELDER_REMINDER_MAX; i++) {
        g.table.when[i].kind = ELDER_RMD_KIND_DAILY;
    }
}

/* V1 → V3。
 * fired_today 从 done_today|pending_today 反推：这俩位图非零即证明
 * 「今天响过」，用它们把重启后二次响铃堵住，同时不制造「响都没响
 * 却说响过」的假抑制。 */
static void migrate_v1_to_v3(const elder_rmd_table_v1_t *v1)
{
    adopt_legacy(v1->items, v1->count, v1->done_today, v1->pending_today,
                 (uint16_t)(v1->done_today | v1->pending_today),
                 v1->last_day_ordinal);
    ESP_LOGW(TAG, "迁移旧格式提醒表(V1) -> V3：%u 条，已完成 0x%04x / 待办 0x%04x / "
                  "响过 0x%04x 全部保留", g.table.count,
             g.table.done_today, g.table.pending_today, g.table.fired_today);
}

/* V2 → V3。V2 有独立的 fired_today，直接沿用；只多一步"排期按每天"，
 * 否则 V2 里那批本来按 repeat_mask 走的提醒会全部停摆。 */
static void migrate_v2_to_v3(const elder_rmd_table_v2_t *v2)
{
    adopt_legacy(v2->items, v2->count, v2->done_today, v2->pending_today,
                 v2->fired_today, v2->last_day_ordinal);
    ESP_LOGW(TAG, "迁移提醒表(V2) -> V3：%u 条，排期字段缺失按每天处理（与升级前行为一致）",
             g.table.count);
}

static esp_err_t load(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return ESP_ERR_NOT_FOUND;

    /* 按 V3 尺寸读。V1 / V2 都是 V3 的逐字节前缀（各有编译期断言），
     * 所以老 blob 一定读得下，读进来的前 sizeof(Vn) 字节就是合法的 Vn。 */
    size_t len = sizeof(g.table);
    esp_err_t err = nvs_get_blob(h, NVS_KEY, &g.table, &len);
    nvs_close(h);
    if (err != ESP_OK) return ESP_ERR_NOT_FOUND;

    /* ---- 版本判定 ----
     * 先按长度分流，再校验各自那一段字节的 CRC。
     * 为什么不能只看 fmt_ver：V1/V2 的 fmt_ver 位置与 V3 不同（V1 压根没有），
     * 而且新 blob 写进来之前前 sizeof(V1) 字节完全相同 —— 长度是唯一可靠信号。 */
    if (len == sizeof(elder_rmd_table_t)) {
        /* ---- 当前格式（V3）---- */
        if (g.table.fmt_ver != ELDER_RMD_FMT_V3) {
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
    } else if (len == sizeof(elder_rmd_table_v2_t)) {
        /* ---- V2 升级路径 ---- */
        if (!table_v2_crc_ok((const uint8_t *)&g.table, len)) {
            ESP_LOGE(TAG, "旧格式提醒表(V2) CRC 校验失败，reset table");
            memset(&g.table, 0, sizeof(g.table));
            return ESP_ERR_INVALID_CRC;
        }
        elder_rmd_table_v2_t v2;
        memcpy(&v2, &g.table, sizeof(v2));
        memset(&g.table, 0, sizeof(g.table));
        migrate_v2_to_v3(&v2);
        /* 迁移必须立刻落盘，否则下次重启又读到 V2 来一遍。 */
        g.dirty = true;
        elder_reminder_flush();
    } else if (len == sizeof(elder_rmd_table_v1_t)) {
        /* ---- 老设备升级路径 ----
         * 这里**绝不能**返回 NOT_FOUND：那会让 init() memset 掉整张表，
         * 老用户升级固件后老人的服药提醒全部消失（医疗事故级）。
         * CRC 必须按 V1 的尺寸重算 —— 老固件就是按 sizeof(V1) 算的。 */
        if (!table_v1_crc_ok((const uint8_t *)&g.table, len)) {
            ESP_LOGE(TAG, "旧格式提醒表(V1) CRC 校验失败，reset table");
            memset(&g.table, 0, sizeof(g.table));
            return ESP_ERR_INVALID_CRC;
        }
        elder_rmd_table_v1_t v1;
        memcpy(&v1, &g.table, sizeof(v1));
        memset(&g.table, 0, sizeof(g.table));
        migrate_v1_to_v3(&v1);
        g.dirty = true;
        elder_reminder_flush();
    } else {
        ESP_LOGE(TAG, "提醒表长度异常（%u 字节，既非 V1/V2 也非 V3），按无数据处理",
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

    /* ⚠️ nvs_open 的返回值**必须**检查（2026-10-29 修）。
     *
     * 原来这里写的是 ESP_ERROR_CHECK_WITHOUT_ABORT(nvs_open(...))：
     * 它在失败时只打印、不 abort，然后**继续用未初始化的 h** 去调
     * nvs_set_blob —— 那就是拿一个随机句柄去写 NVS，轻则返回 0x1107，
     * 重则 abort 重启（2026-10-27 语音调音量重启就是同一个根因，
     * 只是发生在 xiaozhi 的 Settings 里）。
     *
     * NVS 满 / 分区异常时这条路径是**必然会走到**的，不能靠"大概不会发生"。 */
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "提醒表落盘失败：nvs_open(%s) 打不开（%s）—— "
                      "本次 %u 条提醒只在 RAM 里，重启即丢",
                 NVS_NS, esp_err_to_name(err), (unsigned)g.table.count);
        /* 保留 g.dirty，让 tick 的 30s 去抖窗口过后再试一次。
         * 这里不清 dirty 是刻意的：清掉就再也不会重试了。 */
        return err;
    }

    /* 版本号在落盘时统一写：任何一次写盘之后，NVS 里就一定是 V3 布局，
     * load() 才能按 sizeof(V3) 做 CRC 校验。 */
    g.table.fmt_ver = ELDER_RMD_FMT_V3;
    g.table.crc32 = 0;
    g.table.crc32 = crc32_simple(&g.table, sizeof(g.table));

    err = nvs_set_blob(h, NVS_KEY, &g.table, sizeof(g.table));
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);

    if (err != ESP_OK) {
        /* 这张表 1308 字节 = 约 52 个 NVS entry。扩容前可用只有 53 条，
         * 几乎是"刚好够一份"，任何别的写入都会把它挤掉。 */
        ESP_LOGE(TAG, "提醒表落盘失败（%s，需 %u 字节 / 约 %u entry）："
                      "NVS 余量不足，%u 条提醒只在 RAM 里，重启即丢",
                 esp_err_to_name(err), (unsigned)sizeof(g.table),
                 (unsigned)((sizeof(g.table) + 25u) / 26u + 1u),
                 (unsigned)g.table.count);
        /* 同上：失败不清 dirty，30s 后重试。 */
    } else {
        g.dirty = false;
    }
    g.last_flush_us = now_us();
    return err;
}

/* ---------------- 状态机 ---------------- */

/* 「还有没有等着老人确认的提醒」—— #1 按键指示灯"吸引注意"态的唯一真源。
 *
 * 为什么服药和日程都算（用户 2026-10-27 原话是「有待确认的日程提醒（服药）」）：
 *   本按键的功能就是"确认日程完成"，日程和服药走的是同一个确认入口
 *   （elder_bridge.cc 的 FirstUndoneReminder）。只判 medicine 的话，
 *   「喝水」提醒响着的时候 #1 不亮，老人根本不知道该按哪颗键 ——
 *   而那正是他最需要提示的时刻。
 *
 * 状态集合与 elder_reminder_has_pending_medicine() / first_ringing() 保持一致：
 * PENDING / CONFIRM_WIN / ESCALATED 三者都算"这一轮还没完结"，
 * 且必须 enabled（停用的提醒不发声，也就不该亮灯催人）。
 */
static bool has_pending_confirm(void)
{
    for (int i = 0; i < g.table.count && i < ELDER_REMINDER_MAX; i++) {
        if (!g.table.items[i].enabled) continue;
        uint8_t st = g.state[i];
        /* HALF_CONFIRMED（语音说过、没按按钮）也算"还要人管"：
         * #1 得继续橙色呼吸催他去按按钮。见 elder_reminder.h 的枚举注释。 */
        if (st == ELDER_RMD_PENDING || st == ELDER_RMD_CONFIRM_WIN ||
            st == ELDER_RMD_ESCALATED || st == ELDER_RMD_HALF_CONFIRMED) {
            return true;
        }
    }
    return false;
}

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
             st == ELDER_RMD_SKIPPED ? "SKIPPED" :
             st == ELDER_RMD_HALF_CONFIRMED ? "HALF(语音已记，待按键复核)" :
             st == ELDER_RMD_HALF_EXPIRED ? "HALF_EXPIRED(未复核，跳过；不上报)" : "IDLE");

    /* 需求 8（2026-10-22 用户拍板，**覆盖**原「需求 11 红灯长亮」）：
     *
     *   「定时任务提醒时，应该红灯闪烁（闪烁10秒，10分钟后再闪烁10秒，
     *     2小时内以此重复。2小时后不再亮灯。）」
     *
     * 与旧行为的差别，两条都是刻意的：
     *   1. 旧：服药未确认 → **长亮**红灯直到确认。新：每 10 分钟闪 10 秒，
     *      2 小时后**不再亮灯**。长亮在夜里尤其刺眼，而且"确认之前一直亮"
     *      会让老人分不清"这是刚响的那条"还是"昨天没确认的那条"。
     *   2. 旧：超时升级 → 一直快速闪红。新：ESCALATED **不动灯**，
     *      让 nag 按自己的 2 小时节奏走完。否则升级一旦发生，
     *      第 2 小时之后的"不再亮灯"就永远不成立（两套判据互相打架）。
     *
     * 日程提醒（非服药）同样给闪灯：用户的 8 说的是"定时任务提醒"，
     * 没把它限定在服药上。
     */
    if (st == ELDER_RMD_DONE) {
        elder_rgb_med_done();            /* 内部会先 nag_stop()，再绿 60s */
    } else if (st == ELDER_RMD_PENDING) {
        elder_rgb_reminder_nag_start();
    }

    if (st == ELDER_RMD_DONE) {
        g.table.done_today |= (1u << idx);
        g.table.pending_today &= ~(1u << idx);
    } else if (st == ELDER_RMD_PENDING || st == ELDER_RMD_ESCALATED ||
               st == ELDER_RMD_HALF_CONFIRMED) {
        /* HALF 仍然是待办：服务端/子女端看到的应该是「还没完成」，
         * 只是多了一条"老人说办完了"的记录。 */
        g.table.pending_today |= (1u << idx);
        g.table.done_today &= ~(1u << idx);
    } else if (st == ELDER_RMD_SKIPPED || st == ELDER_RMD_HALF_EXPIRED) {
        /* 作废 / 跳过：既不算完成（不能给绿灯），也不算待办（不能再提醒）。
         * HALF_EXPIRED 走这里是因为**本地语义和 SKIPPED 完全一样**，
         * 两者的区别只在上报（sync 层对 HALF_EXPIRED 什么都不发）。 */
        g.table.pending_today &= ~(1u << idx);
        g.table.done_today &= ~(1u << idx);
        /* 不再催：**只在没有别的提醒还在等确认时才停 nag**。
         * nag 是全局单例（rgb.c 里只有一个 g.nag_active），
         * 无条件停会把「B 还在响」的闪灯一起关掉。
         * 此刻 g.state[idx] 已经是 SKIPPED，has_pending_confirm() 不会
         * 把自己算进去，正好。 */
        if (!has_pending_confirm()) elder_rgb_reminder_nag_stop();
    }
    g.dirty = true;
    /* #1 指示灯的"待确认"态跟着状态集合走。放在 g.cb 之前：
     * 回调里会播提示音、切全屏提醒，那一刻灯应该已经是新的状态，
     * 否则老人会看到"屏上写着该吃药了，灯却不亮"。 */
    elder_rgb_button_pending(has_pending_confirm());
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
            g.half_since_us[i] = 0;
            g.table.items[i].fired_due_at = 0;
        }
        g.dirty = true;
        elder_rgb_button_pending(has_pending_confirm());   /* 跨天清空 ⇒ 灯也该灭 */
    }
    g.table.last_day_ordinal = ord;

    int now_min = tm_now.tm_hour * 60 + tm_now.tm_min;

    /* 排期判定需要的两个"今天"：YYYYMMDD（给 ONCE/MONTHLY 比大小）与
     * 真实日序号（给 INTERVAL_DAYS 做 % N）。**不能用上面跨天检测的
     * day_ordinal()** —— 那个是 tm_year*366 + tm_yday，把每年都当 366 天，
     * 拿它取模会跨年漂移（"隔三天"的提醒在元旦后整体错位）。
     * 跨天检测本身继续用它：那里只需要"变了没有"，不需要真实日历日号。 */
    int today_ymd = elder_rmd_ymd_of(tm_now.tm_year + 1900, tm_now.tm_mon + 1, tm_now.tm_mday);
    int today_ord = elder_rmd_ordinal_of(tm_now.tm_year + 1900, tm_now.tm_mon + 1, tm_now.tm_mday);

    for (int i = 0; i < g.table.count && i < ELDER_REMINDER_MAX; i++) {
        elder_rmd_item_t *it = &g.table.items[i];
        if (!item_due_today(i, &tm_now, today_ymd, today_ord)) continue;

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

        /* 半置信复核窗口超时 → **跳过这一次**（2026-10-28 用户定义）。
         *
         * 老人用语音说了「我吃过了」，设备提示他长按按钮复核；他一直不按，
         * 就不再闪灯、不再橙色呼吸催他，本地落 HALF_EXPIRED。
         *
         * 为什么不是 SKIPPED：SKIPPED 会经 sync 层上报 ELDER_EV_SKIPPED
         * （note 写死 "stale"），把服务端刚收到的「语音确认」覆盖成「跳过」，
         * 理由还写成"设备离线错过" —— 与事实完全不符，子女端从此看不出
         * 老人说过什么。HALF_EXPIRED 在 sync 层是**空 case，什么都不发**。
         *
         * ⚠️ 跳过 **不是完成**：done_today 绝不置位。语音是半置信，
         *    万一误识别成"吃过了"，置了完成就等于替老人谎报服药。
         *
         * 基准取 half_since_us；为 0 时退回 pending_since_us（响铃时刻，
         * 比它更早）⇒ 只会让窗口更早到期，是安全的方向。 */
        if (g.state[i] == ELDER_RMD_HALF_CONFIRMED) {
            int64_t base = g.half_since_us[i] ? g.half_since_us[i] : g.pending_since_us[i];
            if (base && (now_us() - base) >= (int64_t)ELDER_HALF_REVIEW_MS * 1000LL) {
                ESP_LOGW(TAG, "[%s] 语音确认后 %u 分钟未按键复核，跳过本次"
                              "（服务端保持「语音确认」标记，设备不再提醒）",
                         it->label, (unsigned)(ELDER_HALF_REVIEW_MS / 60000u));
                g.half_since_us[i] = 0;
                set_state(i, ELDER_RMD_HALF_EXPIRED);
            }
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

/* elder_rmd_sched 的告警出口。那个文件是纯函数、不依赖 esp_log，
 * 由 init() 用 elder_rmd_set_warn_hook() 把日志能力注入进去。 */
static void RmdSchedWarn(const char *msg)
{
    ESP_LOGW(TAG, "排期: %s", msg);
}

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

    /* 把排期模块的告警接到本组件的日志上。elder_rmd_sched.c 是纯函数、
     * 不许 include esp_log.h（否则主机单测编不过），所以只能反向注入。
     * 不注入也能工作，只是"未知 kind / 一次性缺日期"这两类配置问题
     * 不会出现在日志里。 */
    elder_rmd_set_warn_hook(RmdSchedWarn);

    if (load() != ESP_OK) {
        memset(&g.table, 0, sizeof(g.table));
        ESP_LOGW(TAG, "no saved table, starting empty");
    }
    /* 盖上版本号。任何**本固件写出**的表都必须是 V3：新建表与迁移表
     * 走的是同一条路，load() 里靠长度 + fmt_ver 判定读取布局，所以漏盖的
     * 后果是「下一次重启把这张表当成未知版本丢弃」—— 即刚加的提醒白加。
     * 放在 load() 之后统一盖，覆盖「无数据新建」与「迁移成功」两种情况。 */
    g.table.fmt_ver = ELDER_RMD_FMT_V3;
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
    /* 设备本地自建提醒没有排期来源（后端下发的走 replace_all，会带上
     * when[]），按 DAILY 处理 —— 与 2026-10-23 之前"只看 repeat_mask"
     * 的行为一致，不会因为新增字段而让本地提醒停摆。 */
    memset(&g.table.when[idx], 0, sizeof(elder_rmd_when_t));
    g.table.when[idx].kind = ELDER_RMD_KIND_DAILY;
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
        /* when[] 与 items[] 同下标，必须一起左移 —— 漏掉这一步会让
         * 删掉一条之后，后面每条都继承前一条的排期（删了"每周一"，
         * 下一条"每天"就变成每周一）。 */
        memcpy(&g.table.when[i], &g.table.when[i + 1], sizeof(elder_rmd_when_t));
        g.table.items[i].id = (uint8_t)i;
    }
    g.table.count--;
    memset(&g.table.when[g.table.count], 0, sizeof(elder_rmd_when_t));
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

/* 今天的真实日序号（本地时区），给 INTERVAL_DAYS 的相位基准兜底用。 */
static int32_t today_ordinal_now(void)
{
    time_t now = time(NULL);
    struct tm tm_now;
    localtime_r(&now, &tm_now);
    return (int32_t)elder_rmd_ordinal_of(tm_now.tm_year + 1900,
                                         tm_now.tm_mon + 1, tm_now.tm_mday);
}

/* 把一条下发的排期整理成可用的 when。
 *
 * 三件事：
 *  1) src == NULL（老调用方 / 测试）→ 按 DAILY，语义与 2026-10-23 之前一致；
 *  2) INTERVAL_DAYS 且 anchor_ordinal == 0 → 相位基准要补：服务端既没给
 *     start_date 也没给 created_at 时用"今天"。
 *     ⚠️ 必须**尽量继承旧值**：每次同步都重新取"今天"的话，相位每天跟着
 *     跳，N=3 会退化成天天响（或永远不响）—— 这是"补一个基准"这件事
 *     唯一的正确做法，不能省。
 *  3) 其余字段原样带过。 */
static elder_rmd_when_t resolve_when(const elder_rmd_when_t *src, uint32_t server_id,
                                     const elder_rmd_item_t *old,
                                     const elder_rmd_when_t *old_when, uint8_t old_n)
{
    elder_rmd_when_t w;
    if (src) {
        w = *src;
    } else {
        memset(&w, 0, sizeof(w));
        w.kind = ELDER_RMD_KIND_DAILY;
    }

    if (w.kind == ELDER_RMD_KIND_INTERVAL_DAYS && w.anchor_ordinal == 0) {
        int32_t inherited = 0;
        for (uint8_t j = 0; j < old_n; j++) {
            if (old[j].server_id != 0 && old[j].server_id == server_id &&
                old_when[j].kind == ELDER_RMD_KIND_INTERVAL_DAYS &&
                old_when[j].anchor_ordinal != 0) {
                inherited = old_when[j].anchor_ordinal;
                break;
            }
        }
        w.anchor_ordinal = inherited ? inherited : today_ordinal_now();
    }
    return w;
}

/* 合并提醒表时的「旧表快照区」—— **必须放堆上，不许压栈**。
 *
 * 为什么（2026-10-27 真机实证，代价是设备无限重启）：
 *   elder_sync 任务的栈当时只有 6144 字节（sync.c 的 xTaskCreatePinnedToCore，
 *   事后已提到 8192 —— 但**别因此把快照区改回栈上**：栈余量是给 cJSON 递归和
 *   TLS 回调留的，不该被定长数组吃掉），而下面这 7 份定长数组要同时存在：
 *       old / keep            16 × 56 = 896 × 2
 *       old_when / keep_when  16 × 28 = 448 × 2
 *       三个运行态快照        16 × (1+1+8) = 160
 *   ≈ 2.8KB。再叠上 do_sync 自己的 items[16] + when[16]（1344 字节），
 *   后端下发 6 条提醒时刚好压穿，串口原文：
 *       I (12787) elder.reminder: 提醒表已合并：后端 6 条 + 本地 0 条 = 6 条
 *       ***ERROR*** A stack overflow in task elder_sync has been detected.
 *   后果不是"少响一次"，而是**每轮同步（5 分钟）都重启**：设备永远停在
 *   开机阶段，老人收不到任何提醒。V3 新增 when[] 正是压穿它的最后一根稻草。
 *
 * 搬到堆上是结构性修法：以后 ELDER_REMINDER_MAX 或结构体字段再怎么长，
 * 都只涨堆、不再吃任务的栈 —— 不会再以"崩在别人任务里"的形态复发。 */
typedef struct {
    elder_rmd_item_t old[ELDER_REMINDER_MAX];
    elder_rmd_when_t old_when[ELDER_REMINDER_MAX];
    elder_rmd_item_t keep[ELDER_REMINDER_MAX];
    elder_rmd_when_t keep_when[ELDER_REMINDER_MAX];
    uint8_t          old_state[ELDER_REMINDER_MAX];
    bool             old_fired[ELDER_REMINDER_MAX];
    int64_t          old_deadline[ELDER_REMINDER_MAX];
    int64_t          old_due[ELDER_REMINDER_MAX];
    int64_t          old_pending[ELDER_REMINDER_MAX];
    int64_t          old_half[ELDER_REMINDER_MAX];
} rmd_merge_scratch_t;

esp_err_t elder_reminder_replace_all(const elder_rmd_item_t *items,
                                     const elder_rmd_when_t *when,
                                     uint8_t count)
{
    if (count > ELDER_REMINDER_MAX) return ESP_ERR_INVALID_SIZE;

    /* 拿不到快照区就**什么都不动**：宁可这轮不同步（5 分钟后重试），
     * 也绝不能在没有旧表快照的情况下覆盖 g.table —— 那会连带丢掉
     * 老人断网时亲手建的本地提醒（服务端没有副本，丢了就永久没了）。 */
    rmd_merge_scratch_t *sc = heap_caps_malloc(sizeof(*sc), MALLOC_CAP_8BIT);
    if (!sc) {
        ESP_LOGE(TAG, "合并提醒表时内存不足（需 %u 字节），本轮跳过同步",
                 (unsigned)sizeof(*sc));
        return ESP_ERR_NO_MEM;
    }
    elder_rmd_item_t *old = sc->old;
    elder_rmd_when_t *old_when = sc->old_when;
    elder_rmd_item_t *keep = sc->keep;
    elder_rmd_when_t *keep_when = sc->keep_when;
    uint8_t *old_state = sc->old_state;
    bool    *old_fired = sc->old_fired;
    int64_t *old_deadline = sc->old_deadline;
    int64_t *old_due = sc->old_due;
    int64_t *old_pending = sc->old_pending;
    int64_t *old_half = sc->old_half;

    /* ---- 1. 快照旧表与运行态 ---- */
    uint8_t old_n = g.table.count > ELDER_REMINDER_MAX ? ELDER_REMINDER_MAX : g.table.count;
    memcpy(old, g.table.items, sizeof(elder_rmd_item_t) * old_n);
    /* 先清零再拷：old_n 之后的元素不会被读，但保持确定性总没坏处 */
    memset(old_when, 0, sizeof(sc->old_when));
    memcpy(old_when, g.table.when, sizeof(elder_rmd_when_t) * old_n);
    memcpy(old_state, g.state, sizeof(sc->old_state));
    memcpy(old_fired, g.fired_today, sizeof(sc->old_fired));
    memcpy(old_deadline, g.confirm_deadline_us, sizeof(sc->old_deadline));
    /* fired_due_at 是"本次实例的到期时刻"，回执靠它定位服务端那一行。
     * 它住在 items[] 里，而下面第 4 步要 memset 整张表 —— 不单独快照就会
     * 跟着一起归零（R2，2026-10-31 用户实报后修）。 */
    memset(old_due, 0, sizeof(sc->old_due));
    for (uint8_t i = 0; i < old_n; i++) old_due[i] = g.table.items[i].fired_due_at;
    memcpy(old_pending, g.pending_since_us, sizeof(sc->old_pending));
    memcpy(old_half, g.half_since_us, sizeof(sc->old_half));

    /* ---- 2. 挑出「设备本地自建」（server_id==0）条目 —— 后端同步绝不能抹掉它们 ----
     * 这是本函数存在的理由：断网时老人亲手建的服药提醒只存在于设备 NVS，
     * 服务端毫不知情；一次 replace_all 全量覆盖就会让它们永久消失，
     * 属于医疗事故级数据丢失。 */
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
        if (keep_n < ELDER_REMINDER_MAX) {
            keep[keep_n] = old[i];
            /* 本地条目的排期必须跟着一起保留，否则保留下来的那条
             * 会退回 DAILY（等于把用户的"每周一次"改成"每天"）。 */
            keep_when[keep_n] = old_when[i];
            keep_n++;
        }
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
    memset(g.table.when, 0, sizeof(g.table.when));
    uint8_t n = 0;
    for (uint8_t i = 0; i < srv_n; i++) {
        g.table.items[n] = items[i];
        g.table.items[n].id = n;
        g.table.items[n].label[ELDER_REMINDER_LABEL] = '\0';
        g.table.when[n] = resolve_when(when ? &when[i] : NULL, items[i].server_id,
                                       old, old_when, old_n);
        n++;
    }
    for (uint8_t i = 0; i < keep_n; i++) {
        g.table.items[n] = keep[i];
        g.table.items[n].id = n;
        g.table.items[n].label[ELDER_REMINDER_LABEL] = '\0';
        g.table.when[n] = keep_when[i];
        n++;
    }

    /* 一条旧条目只能被一条新条目"认领"。
     *
     * 为什么需要（2026-10-29 加内容兜底时发现）：按 server_id 匹配时天然不会
     * 重复（id 唯一），但**按内容兜底**可以重复 —— 两条内容完全相同的提醒
     * （同类型、同时刻、同标签，只是 server_id 不同，例如子女在网页端重复
     * 建了两次「08:00 降压药」）会**双双认领同一条旧记录**，于是同时继承
     * 同一个运行态。若旧的那条是 DONE，两条新条目都会被标成"今天已服"——
     * 等于替老人谎报服药，医疗事故级。
     *
     * ELDER_REMINDER_MAX 是 16，用 uint32_t 位图就够（标量，不占栈数组，
     * 门禁 C13 不受影响）。 */
    uint32_t claimed = 0;

    for (uint8_t i = 0; i < n; i++) {
        const elder_rmd_item_t *it = &g.table.items[i];
        int old_idx = -1;
        if (it->server_id != 0) {
            for (uint8_t j = 0; j < old_n; j++) {
                if (claimed & (1u << j)) continue;
                if (old[j].server_id == it->server_id) { old_idx = (int)j; break; }
            }
            /* server_id 匹配不上时**按内容兜底**（2026-10-29 用户实报后加）。
             *
             * 现场：8 点的服药提醒，10 点屏幕还挂着提示，但语音/按键都
             * 确认不了。根因是这条链路 ——
             *   ① 服务端重新下发了这条提醒（重建/编辑都会换 server_id）；
             *   ② 设备按 server_id 找不到旧条目 ⇒ 当成"全新提醒"，
             *      state 重置成 IDLE、fired_today 清掉；
             *   ③ 下一个 tick 重新触发，此时 late_min 已经 > 60 分钟，
             *      被判 SKIPPED（"设备离线错过"）；
             *   ④ 引擎里没有待确认项了，但屏幕的全屏提醒是 elder_ui 自己
             *      的 4 小时计时，**还在显示** ⇒ 老人照着做，设备不理。
             *
             * 同一提醒换了个 id 就丢掉"今天已经响过、正在等确认"这个事实，
             * 在服药场景里是不可接受的。内容（类型+时刻+标签）相同的，
             * 就是同一条。 */
            if (old_idx < 0) {
                for (uint8_t j = 0; j < old_n; j++) {
                    if (claimed & (1u << j)) continue;
                    if (same_content(&old[j], it)) {
                        old_idx = (int)j;
                        ESP_LOGW(TAG, "server_id 变了（旧 %lu → 新 %lu），按内容"
                                      "认作同一条并继承运行态: %s %02d:%02d",
                                 (unsigned long)old[j].server_id,
                                 (unsigned long)it->server_id,
                                 it->label, it->hour, it->minute);
                        break;
                    }
                }
            }
        } else {
            for (uint8_t j = 0; j < old_n; j++) {
                if (claimed & (1u << j)) continue;
                if (old[j].server_id == 0 && same_content(&old[j], it)) {
                    old_idx = (int)j;
                    break;
                }
            }
        }
        if (old_idx >= 0) {
            claimed |= (1u << old_idx);   /* 这条旧条目已被认领，后面的条目不许再认 */
            /* 沿用旧表同一条的运行态，避免同步打断老人当天的进度。
             *
             * ⚠️ 这里继承的字段必须**覆盖整个"本次实例"的上下文**，
             * 漏一个就会让一次普通的心跳同步把老人当天的进度打断。
             * 2026-10-31 的 R2 就是漏了 fired_due_at 造成的：
             *   08:00 的服药提醒响过、正等确认 → 子女端改了别的提醒，
             *   触发一次 replace_all → fired_due_at 归零 → 老人/子女
             *   确认时 sync 层拿 0 兜底成"现在" → 服务端按 (rid, due)
             *   找不到 08:00 那行，add 出一条 due_at == 确认时刻的假实例。
             *   08:00 那行永远等不到确认 ⇒ 红灯一直闪，依从率全失真。 */
            g.state[i] = old_state[old_idx];
            g.fired_today[i] = old_fired[old_idx];
            g.confirm_deadline_us[i] = old_deadline[old_idx];
            g.table.items[i].fired_due_at = old_due[old_idx];
            /* 两个计时基准同样是"本次实例"的一部分：
             *   pending_since_us —— 15 分钟升级计时的起点。丢了就退回
             *     `(now_min - due) >= 15` 的兜底，晚开机/同步一次就立刻
             *     判 ESCALATED 上报 MISSED，子女端收到假"漏服"告警。
             *   half_since_us —— 半置信复核窗口的起点。丢了会退回更早的
             *     pending_since_us ⇒ 窗口立刻到期 ⇒ 语音确认被冲成
             *     HALF_EXPIRED，老人刚说过的"吃过了"白说。 */
            g.pending_since_us[i] = old_pending[old_idx];
            g.half_since_us[i] = old_half[old_idx];
        } else {
            g.state[i] = ELDER_RMD_IDLE;
            g.fired_today[i] = false;
            g.confirm_deadline_us[i] = 0;
            g.table.items[i].fired_due_at = 0;
            g.pending_since_us[i] = 0;
            g.half_since_us[i] = 0;
        }
    }
    for (uint8_t i = n; i < ELDER_REMINDER_MAX; i++) {
        g.state[i] = ELDER_RMD_IDLE;
        g.fired_today[i] = false;
        g.confirm_deadline_us[i] = 0;
        g.table.items[i].fired_due_at = 0;
        g.pending_since_us[i] = 0;
        g.half_since_us[i] = 0;
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
    /* 合并后条数/下标都变了，待确认集合必须重算 —— 否则 #1 指示灯会停在
     * 合并前的状态：提醒被后端删了灯还亮着（催老人确认一条不存在的任务），
     * 或者后端新下发的提醒在等确认、灯却不亮。 */
    elder_rgb_button_pending(has_pending_confirm());

    /* ---- 7. 同步后对账：屏幕与引擎状态必须同源 ----
     * replace_all 是**全表替换**：被后端删掉/停用的条目会整条消失，这时
     * 根本不会有 OnReminder 回调（没有状态迁移可报），而屏幕的 4 小时计时
     * 是 elder_ui 自己的、独立走的 ⇒ 老人对着一份已经不存在的提醒长按按钮，
     * 按键选不中目标（first_ringing() = -1），屏幕却还在喊「长按确认」。
     *
     * 2026-10-29 用户实报的正是这个形态：8 点服药提醒，10 点屏幕还挂着提示，
     * 语音和长按都没反应，唤醒后走服务端 MCP 才确认成功（MCP 只看服务端，
     * 不经过设备的 first_ringing()，所以"语音能用、按键不能"）。
     *
     * 前面几处修的是"为什么会脱节"，这里是**兜底对账**：只要引擎里没有
     * 待确认项，屏幕就不许留着提醒。宁可少显示一条，也不能让屏幕指着一个
     * 按不动的操作。elder_ui_hide_reminder() 自带"没在显示就直接返回"，幂等。 */
    if (!has_pending_confirm() && elder_ui_reminder_showing()) {
        elder_ui_hide_reminder();
        ESP_LOGW(TAG, "同步后引擎里没有待确认项，但全屏提醒还挂着 ⇒ 收掉"
                      "（屏幕指向了一个按不动的操作）");
    }

    ESP_LOGI(TAG, "提醒表已合并：后端 %u 条 + 本地 %u 条 = %u 条", srv_n, keep_n, n);
    esp_err_t rc = elder_reminder_flush();
    heap_caps_free(sc);
    return rc;
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
    return elder_reminder_confirm_ex(id, ELDER_CFM_BUTTON);
}

esp_err_t elder_reminder_confirm_ex(uint8_t id, uint8_t confirm_method)
{
    if (id >= g.table.count) return ESP_ERR_NOT_FOUND;
    /* 先记方式再改状态：set_state() 会同步回调 elder_sync_on_reminder_state，
     * 那时 sync 层要能读到正确的确认方式。 */
    g.last_confirm_method = confirm_method;
    g.confirm_deadline_us[id] = 0;

    /* 语音确认 = **半置信**，进 HALF 而不是 DONE（2026-10-27 用户定义）。
     *
     * 为什么不能直接置 DONE：
     *   ① 按键复核再也找不到目标 —— first_ringing() 只认待确认集合，
     *      于是"语音确认后提示按按钮、按下去却提示没有待确认的提醒"；
     *   ② 服务端只会收到一条 method=VOICE 的回执，永远升不到
     *      「语音+按键确认」（全置信）。
     * 按键/APP 确认本身就是"人在场"的证据，直接 DONE。 */
    if (confirm_method == ELDER_CFM_VOICE) {
        /* 记下复核窗口的起点。必须在 set_state 之前 —— set_state 会同步
         * 回调 sync 层，tick 也可能在别的任务上下文里跑，晚一行就可能
         * 被 tick 读到 0 而用 pending_since_us 兜底（差几十毫秒，无伤，
         * 但没必要留这种不确定性）。 */
        g.half_since_us[id] = now_us();
        set_state(id, ELDER_RMD_HALF_CONFIRMED);
    } else {
        g.half_since_us[id] = 0;
        set_state(id, ELDER_RMD_DONE);
    }
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
            g.state[i] == ELDER_RMD_ESCALATED ||
            g.state[i] == ELDER_RMD_HALF_CONFIRMED) {
            return true;
        }
    }
    return false;
}

/* 当前**正在响铃**的提醒下标；没有则 -1。
 *
 * 为什么需要它（2026-10-22 用户实报 bug）：
 *   设备上「喝水」提醒正在响铃，老人说「弄好了」，确认却记到了另一条
 *   「吃鼻炎药」上，喝水提醒依然未确认、继续被注入系统提示词。
 *   根因在 elder_bridge.cc 的 FirstUndoneReminder()：它按「服药优先」
 *   启发式选目标，循环里见到第一条 ELDER_RMD_TYPE_MEDICINE 就返回 ——
 *   正在响铃的日程（喝水）提醒永远选不中。
 *
 * 判据与 elder_reminder_has_pending_medicine() 的「响铃中」状态集合
 * 保持一致（PENDING / CONFIRM_WIN / ESCALATED 三者都算这一轮还没完结），
 * 并要求 enabled —— 停用的提醒不发声，自然也不能被确认。
 *
 * 多条同时响铃时取 fired_due_at **最大**的那条：fired_due_at 是本次
 * 触发的到期时刻（unix 秒，见 elder_rmd_item_t），最大 = 最近触发 =
 * 此刻正在发声的那条。老人说「弄好了」回应的一定是刚响的那条。 */
int elder_reminder_first_ringing(void)
{
    int best = -1;
    int64_t best_due = 0;
    for (int i = 0; i < g.table.count && i < ELDER_REMINDER_MAX; i++) {
        if (!g.table.items[i].enabled) continue;
        if (g.state[i] != ELDER_RMD_PENDING &&
            g.state[i] != ELDER_RMD_CONFIRM_WIN &&
            g.state[i] != ELDER_RMD_ESCALATED &&
            g.state[i] != ELDER_RMD_HALF_CONFIRMED) {
            continue;
        }
        /* 严格大于：fired_due_at 相等（同一分钟触发的两条）时保留下标
         * 更小的那条，行为确定，不依赖遍历顺序之外的偶然因素。 */
        if (best < 0 || g.table.items[i].fired_due_at > best_due) {
            best = (int)i;
            best_due = g.table.items[i].fired_due_at;
        }
    }
    return best;
}

int32_t elder_reminder_next_in_sec(void)
{
    time_t now = time(NULL);
    struct tm tm_now;
    localtime_r(&now, &tm_now);
    int now_min = tm_now.tm_hour * 60 + tm_now.tm_min;
    int today_ymd = elder_rmd_ymd_of(tm_now.tm_year + 1900, tm_now.tm_mon + 1, tm_now.tm_mday);
    int today_ord = elder_rmd_ordinal_of(tm_now.tm_year + 1900, tm_now.tm_mon + 1, tm_now.tm_mday);
    int best = -1;

    for (int i = 0; i < g.table.count && i < ELDER_REMINDER_MAX; i++) {
        elder_rmd_item_t *it = &g.table.items[i];
        if (!item_due_today(i, &tm_now, today_ymd, today_ord)) continue;
        if (g.state[i] != ELDER_RMD_IDLE) continue;
        int due = it->hour * 60 + it->minute;
        int delta = due - now_min;
        if (delta < 0) delta += 24 * 60;
        if (best < 0 || delta < best) best = delta;
    }
    return best < 0 ? -1 : (int32_t)best * 60;
}
