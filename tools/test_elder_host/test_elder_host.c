/* test_elder_host.c —— 断网提醒链路的宿主单元测试。
 *
 * 覆盖的断言都对应医疗事故级的失效（详见 Makefile 顶部说明）：
 *   T1  提醒时间存本地（NVS）—— 断网重启后提醒还在
 *   T2  replace_all 不抹掉本地自建 —— 一次心跳同步不该让老人的服药提醒消失
 *   T3  断网新建 + 断网确认 + 确认方式正确回传
 *   T4  时钟不可信时不触发 —— 响错时间比不提醒更危险
 *   T5  本地日界不错判 —— 08:00 不是「新的一天」
 *   T6  跨天真的重置 —— 第二天提醒仍要响
 *   T7  漏服 15 分钟升级告警 —— ESCALATED 是「人要介入」的唯一信号
 *   T8  按键确认窗 30 秒超时回退 —— 按了键又不管了必须回到未确认
 *   T9  NVS CRC 校验 —— 坏数据绝不能被当成有效提醒表
 *   T10 上电首 tick 不做跨天重置 —— 否则每次上电都产生一次假「漏服」
 *
 * 全程**没有任何网络调用**：这正是要验证的场景。
 */
#include "stubs.h"

#include "elder_reminder.h"
#include "elder_cmd.h"
#include "elder_cmd_words.h"
#include "elder_ui.h"

#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* reminder.c 内部的 now_us() 用它；这里直接用桩的 esp_timer。 */

static int g_fail;
static int g_pass;

static void check(const char *name, int ok, const char *detail)
{
    if (ok) {
        g_pass++;
        printf("  [PASS] %s%s%s\n", name, detail ? "  -> " : "", detail ? detail : "");
    } else {
        g_fail++;
        printf("  [FAIL] %s%s%s\n", name, detail ? "  -> " : "", detail ? detail : "");
    }
}

/* ---- 断言详情格式化 ----
 * 原先若干断言的 detail 写的是字面量（如"实际 4 条"），与实测值无关：
 * 变异测试里它红的时候打印的仍是"实际 4 条"而实际是 2 条，
 * 失败信息会把排查带偏。下面两个函数保证 detail 永远反映真实值。 */
static char g_det[512];
static char g_det2[512];

static const char *det(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_det, sizeof(g_det), fmt, ap);
    va_end(ap);
    return g_det;
}

/* 把整张表摊平成一行，用于给「某条提醒在不在」这类断言补上下文 */
static const char *dump_tbl(const elder_rmd_table_t *t)
{
    if (!t) { snprintf(g_det2, sizeof(g_det2), "tbl() 返回 NULL"); return g_det2; }
    int n = snprintf(g_det2, sizeof(g_det2),
                     "count=%u done=0x%04x pending=0x%04x last_day_ord=%d items=[",
                     t->count, t->done_today, t->pending_today, t->last_day_ordinal);
    if (n < 0) n = 0;
    for (uint8_t i = 0; i < t->count && i < ELDER_REMINDER_MAX; i++) {
        int w = snprintf(g_det2 + n, sizeof(g_det2) - (size_t)n, "%s%u:%02u\"%s\"",
                         i ? " " : "", t->items[i].hour, t->items[i].minute, t->items[i].label);
        if (w < 0) break;
        n += w;
        if ((size_t)n >= sizeof(g_det2) - 4) { n = (int)sizeof(g_det2) - 4; break; }
    }
    snprintf(g_det2 + n, sizeof(g_det2) - (size_t)n, "]");
    return g_det2;
}

/* ---- 状态机转移记录 ----
 * ESCALATED / CONFIRM_WIN 这两个状态在 elder_rmd_table_t 里**没有任何字段**
 * （表里只有 done_today / pending_today 两个位图），所以想钉死它们只能
 * 观察 set_state() 的回调。这不是"为了测试而加钩子"：固件里
 * elder_sync_on_reminder_state 正是挂在同一个回调上，
 * 「升级成 ESCALATED -> 上报 ELDER_EV_MISSED（子女端收到漏服告警）」
 * 这条链的唯一入口就是它。 */
static elder_rmd_state_t g_last_st[ELDER_REMINDER_MAX];
static uint8_t g_trans_id[128];
static uint8_t g_trans_st[128];
static int    g_trans_n;

static void rec_cb(const elder_rmd_item_t *item, elder_rmd_state_t st, void *ctx)
{
    (void)ctx;
    if (!item || item->id >= ELDER_REMINDER_MAX) return;
    g_last_st[item->id] = st;
    if (g_trans_n < (int)(sizeof(g_trans_id) / sizeof(g_trans_id[0]))) {
        g_trans_id[g_trans_n] = item->id;
        g_trans_st[g_trans_n] = (uint8_t)st;
        g_trans_n++;
    }
}

static void rec_reset(void)
{
    memset(g_last_st, 0, sizeof(g_last_st));
    memset(g_trans_id, 0, sizeof(g_trans_id));
    memset(g_trans_st, 0, sizeof(g_trans_st));
    g_trans_n = 0;
}

/* 某条提醒累计发生过几次「进入某状态」 */
static int saw(uint8_t id, elder_rmd_state_t st)
{
    int n = 0;
    for (int i = 0; i < g_trans_n; i++)
        if (g_trans_id[i] == id && g_trans_st[i] == (uint8_t)st) n++;
    return n;
}

static const char *state_name(uint8_t st)
{
    switch ((elder_rmd_state_t)st) {
    case ELDER_RMD_IDLE:        return "IDLE";
    case ELDER_RMD_PENDING:     return "PENDING";
    case ELDER_RMD_CONFIRM_WIN: return "CONFIRM_WIN";
    case ELDER_RMD_DONE:        return "DONE";
    case ELDER_RMD_ESCALATED:   return "ESCALATED";
    default:                    return "?";
    }
}

/* 造一条服药提醒 */
static elder_rmd_item_t mk_med(uint8_t hour, uint8_t minute, const char *label,
                               uint32_t server_id)
{
    elder_rmd_item_t it;
    memset(&it, 0, sizeof(it));
    it.hour = hour;
    it.minute = minute;
    it.type = ELDER_RMD_TYPE_MEDICINE;
    it.rtype = 0;            /* 后端 ReminderType.MEDICINE */
    it.schedule_kind = 1;    /* DAILY */
    it.repeat_mask = 0x7F;   /* 每天 */
    it.enabled = true;
    it.server_id = server_id;
    snprintf(it.label, sizeof(it.label), "%s", label);
    return it;
}

static const elder_rmd_table_t *tbl(void)
{
    const elder_rmd_table_t *t = NULL;
    elder_reminder_list(&t);
    return t;
}

/* 在表里按「时刻+标题」找一条，返回索引或 -1 */
static int find_idx(const elder_rmd_table_t *t, uint8_t h, uint8_t m, const char *label)
{
    for (uint8_t i = 0; i < t->count; i++) {
        if (t->items[i].hour == h && t->items[i].minute == m &&
            strcmp(t->items[i].label, label) == 0) return i;
    }
    return -1;
}

/* =================================================================== */
/* T1：提醒时间存本地 —— 断网重启后还在                                   */
/* =================================================================== */
static void test_local_persistence(void)
{
    printf("\n[T1] 提醒时间存储在设备本地（NVS）\n");
    stub_nvs_init();
    stub_time_set(1772665200L);   /* 2026-03-05 07:00 +08:00，可信时钟 */
    elder_reminder_init(NULL, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    check("新增本地服药提醒", elder_reminder_add(&m) == ESP_OK, NULL);

    const elder_rmd_table_t *t = tbl();
    check("表里有 1 条", t && t->count == 1, NULL);
    check("时间是 08:00", t && t->items[0].hour == 8 && t->items[0].minute == 0, NULL);
    check("server_id == 0（本地自建）", t && t->items[0].server_id == 0, NULL);

    /* 模拟「断网 + 断电重启」：同一块 Flash，重新 init 引擎 */
    elder_reminder_init(NULL, NULL);
    t = tbl();
    check("重启后提醒仍在（时间存本地）",
          t && t->count == 1 && t->items[0].hour == 8 && t->items[0].minute == 0,
          t ? "count=1, 08:00" : "count=0 —— 数据丢了");

    /* 到点应触发（断网也必须响） */
    stub_time_advance(3600);      /* 08:00 到了 */
    elder_reminder_tick_once();
    t = tbl();
    check("到点自动触发（PENDING）",
          t && (t->pending_today & 1u), t ? "pending_today 已置位" : NULL);
}

/* =================================================================== */
/* T2：replace_all 绝不能抹掉本地自建                                     */
/* =================================================================== */
static void test_replace_all_keeps_local(void)
{
    printf("\n[T2] elder_reminder_replace_all 不抹掉设备本地自建的提醒\n");
    stub_nvs_init();
    stub_time_set(1772665200L);
    elder_reminder_init(NULL, NULL);

    /* 老人断网时亲手建了两条（server_id == 0） */
    elder_rmd_item_t local1 = mk_med(8, 0, "早上的药", 0);
    elder_rmd_item_t local2 = mk_med(20, 0, "晚上的药", 0);
    elder_reminder_add(&local1);
    elder_reminder_add(&local2);

    /* 服务端此时下发两条自己的规则 */
    elder_rmd_item_t srv[2];
    srv[0] = mk_med(9, 30, "降压药", 101);
    srv[1] = mk_med(21, 0, "钙片", 102);

    check("replace_all 返回成功", elder_reminder_replace_all(srv, 2) == ESP_OK, NULL);

    const elder_rmd_table_t *t = tbl();
    check("后端 2 条 + 本地 2 条 = 4 条", t && t->count == 4,
          det("实际 count=%u（后端 2 + 本地 2），%s", t ? t->count : 0, dump_tbl(t)));
    check("后端条目在", t && find_idx(t, 9, 30, "降压药") >= 0,
          det("09:30「降压药」idx=%d，%s", t ? find_idx(t, 9, 30, "降压药") : -1, dump_tbl(t)));
    check("本地「早上的药」未被抹掉", t && find_idx(t, 8, 0, "早上的药") >= 0,
          det("08:00「早上的药」idx=%d，%s", t ? find_idx(t, 8, 0, "早上的药") : -1, dump_tbl(t)));
    check("本地「晚上的药」未被抹掉", t && find_idx(t, 20, 0, "晚上的药") >= 0,
          det("20:00「晚上的药」idx=%d，%s", t ? find_idx(t, 20, 0, "晚上的药") : -1, dump_tbl(t)));

    /* 最狠的一遍：服务端下发空表（孩子把 App 里的提醒全删了）。
     * 老人在断网时建的提醒此时只剩这一份拷贝，绝不能跟着消失。 */
    check("后端下发 0 条时也不丢本地",
          elder_reminder_replace_all(srv, 0) == ESP_OK, NULL);
    t = tbl();
    check("空表下发后本地 2 条仍在", t && t->count == 2,
          det("实际 count=%u，%s", t ? t->count : 0, dump_tbl(t)));
    check("两条内容都对",
          t && find_idx(t, 8, 0, "早上的药") >= 0 && find_idx(t, 20, 0, "晚上的药") >= 0,
          det("早上的药 idx=%d，晚上的药 idx=%d，%s",
              t ? find_idx(t, 8, 0, "早上的药") : -1,
              t ? find_idx(t, 20, 0, "晚上的药") : -1, dump_tbl(t)));

    /* 容量溢出：本地必须优先活下来（服务端数据可再拉，本地丢了找不回） */
    elder_rmd_item_t many[16];
    for (int i = 0; i < 16; i++) {
        many[i] = mk_med((uint8_t)(i % 24), 0, "服务端提醒", (uint32_t)(200 + i));
    }
    elder_reminder_replace_all(many, 16);
    t = tbl();
    check("溢出时本地两条仍活", t && find_idx(t, 8, 0, "早上的药") >= 0
                             && find_idx(t, 20, 0, "晚上的药") >= 0,
          det("早上的药 idx=%d，晚上的药 idx=%d，%s",
              t ? find_idx(t, 8, 0, "早上的药") : -1,
              t ? find_idx(t, 20, 0, "晚上的药") : -1, dump_tbl(t)));
    check("表不超过 16 条上限", t && t->count <= ELDER_REMINDER_MAX,
          det("实际 count=%u（上限 %d），%s",
              t ? t->count : 0, ELDER_REMINDER_MAX, dump_tbl(t)));
}

/* =================================================================== */
/* T3：断网新建 + 断网确认（命令词分发器）                                 */
/* =================================================================== */
static void test_offline_command_dispatch(void)
{
    printf("\n[T3] 断网新建服药提醒 + 断网语音确认\n");
    stub_nvs_init();
    stub_time_set(1772665200L);
    elder_reminder_init(NULL, NULL);

    /* --- 词表自检：不能有重复短语（重复会让 MultiNet 注册冲突） --- */
    int n = elder_cmd_word_count();
    check("词表非空", n > 0, NULL);
    int dup = 0;
    for (int i = 0; i < n; i++) {
        for (int j = i + 1; j < n; j++) {
            const elder_cmd_word_t *a = elder_cmd_word_at(i);
            const elder_cmd_word_t *b = elder_cmd_word_at(j);
            if (a && b && strcmp(a->phrase, b->phrase) == 0) dup++;
        }
    }
    check("词表无重复短语", dup == 0, NULL);

    /* --- 词表必须保守：不能有删除/改期类命令 ---
     * 误识别删除一条服药提醒不可逆，所以只允许 create/confirm/query。 */
    int destructive = 0;
    for (int i = 0; i < n; i++) {
        const elder_cmd_word_t *w = elder_cmd_word_at(i);
        if (!w) continue;
        if (w->action != ELDER_CMD_CREATE_MORNING && w->action != ELDER_CMD_CREATE_NOON &&
            w->action != ELDER_CMD_CREATE_EVENING && w->action != ELDER_CMD_CREATE_ALL &&
            w->action != ELDER_CMD_CONFIRM &&
            w->action != ELDER_CMD_QUERY_NEXT && w->action != ELDER_CMD_QUERY_TODAY) {
            destructive++;
        }
    }
    check("词表不含删除/改期类危险命令", destructive == 0, NULL);

    /* --- 断网新建：命令词 -> 本地提醒（server_id=0） --- */
    elder_cmd_result_t r;
    check("识别「新建早上吃药提醒」",
          elder_cmd_dispatch("新建早上吃药提醒", &r) == ESP_OK && r.recognized, r.feedback);
    const elder_rmd_table_t *t = tbl();
    check("已写入 08:00 早上的药",
          t && find_idx(t, 8, 0, "早上的药") >= 0,
          det("08:00「早上的药」idx=%d，%s", t ? find_idx(t, 8, 0, "早上的药") : -1, dump_tbl(t)));
    check("新建的是 server_id==0 的本地提醒",
          t && t->items[find_idx(t, 8, 0, "早上的药")].server_id == 0,
          det("server_id=%u（0=设备本地自建），%s",
              t ? t->items[find_idx(t, 8, 0, "早上的药")].server_id : 0xFFFFFFFF, dump_tbl(t)));

    /* 重复说同一句不应建出第二条（老人会连着说两遍） */
    int before = tbl()->count;
    elder_cmd_dispatch("新建早上吃药提醒", &r);
    check("重复新建不产生第二条", tbl()->count == before,
          "count 未变（已存在时明确告知）");

    /* --- 断网确认：到点触发 -> 说「吃过了」-> 完成 --- */
    stub_time_advance(3600);      /* 08:00 到点 */
    elder_reminder_tick_once();
    t = tbl();
    int idx = find_idx(t, 8, 0, "早上的药");
    check("08:00 已触发待确认", t && idx >= 0 && (t->done_today & (1u << idx)) == 0, NULL);

    check("识别「吃过了」",
          elder_cmd_dispatch("吃过了", &r) == ESP_OK && r.recognized, r.feedback);
    t = tbl();
    check("确认后标记为已完成",
          t && idx >= 0 && (t->done_today & (1u << idx)) != 0, r.feedback);

    /* 确认方式必须是「语音」（1），不能被误报成按键（2）——
     * 子女端靠它算依从率，误报会让数据不可信。 */
    check("确认方式记为语音(VOICE=1)",
          elder_reminder_last_confirm_method() == 1,
          "实际 method 应为 1");

    /* --- 没响过的提醒不能被「吃过了」误确认 ---
     * 老人一天里可能说好几遍「吃过了」：第一遍确认 08:00 那条，
     * 后面几遍如果去确认一条从没响过的提醒，就等于凭空记了一次服药完成，
     * 子女端看到的是「按时服药」，实际老人压根没到点。这属于假绿，
     * 必须由 find_confirmable() 挡住。 */
    elder_cmd_dispatch("新建晚上吃药提醒", &r);
    t = tbl();
    check("已建 20:00 晚上的药", t && find_idx(t, 20, 0, "晚上的药") >= 0,
          det("20:00「晚上的药」idx=%d，%s", t ? find_idx(t, 20, 0, "晚上的药") : -1, dump_tbl(t)));

    elder_cmd_dispatch("吃过了", &r);      /* 再说一遍，不应确认 20:00 那条 */
    t = tbl();
    int idx2 = find_idx(t, 20, 0, "晚上的药");
    check("今天没响过的提醒不被误确认",
          t && idx2 >= 0 && (t->done_today & (1u << idx2)) == 0,
          "20:00 未到点，不应被记为已完成");
    check("重复说「吃过了」被明确拒绝而非误确认",
          strstr(r.feedback, "没有需要确认") != NULL, r.feedback);

    /* --- 未识别的语句必须老实说没听懂，不能瞎猜（宁可走按键） --- */
    check("无关语句不被误命中",
          elder_cmd_dispatch("今天天气怎么样", &r) != ESP_OK && !r.recognized, NULL);

    /* --- 查询类 --- */
    check("查询下一个提醒", elder_cmd_dispatch("下一个提醒", &r) == ESP_OK, r.feedback);
    check("查询今天的提醒", elder_cmd_dispatch("今天的提醒", &r) == ESP_OK, r.feedback);
}

/* =================================================================== */
/* T4：时钟不可信时不得触发提醒（响错时间比不提醒更危险）               */
/* =================================================================== */
static void test_untrusted_clock_guard(void)
{
    printf("\n[T4] 系统时钟不可信（未校时/掉电归零）时暂停触发\n");
    stub_nvs_init();
    /* 1970 附近 = 掉电后未校时的典型值 */
    stub_time_set(1000L);
    elder_reminder_init(NULL, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    elder_reminder_add(&m);

    /* 推进 100 天，时间仍然不可信 */
    stub_time_advance(100LL * 86400);
    elder_reminder_tick_once();
    const elder_rmd_table_t *t = tbl();
    check("时钟不可信时不触发任何提醒",
          t && t->pending_today == 0 && t->done_today == 0,
          "响错时间的服药提醒比不提醒更危险");

    /* 校时之后恢复正常：受控时钟回到 07:00（可信），再推进到 08:00 */
    stub_time_set(1772665200L);
    elder_reminder_tick_once();          /* 先让引擎认下这一天 */
    stub_time_advance(3600);             /* 08:00 到点 */
    elder_reminder_tick_once();
    t = tbl();
    check("校时后提醒恢复触发", t && (t->pending_today & 1u) != 0, NULL);
    check("跨过的 100 天未造成误触发",
          t && t->done_today == 0, "未确认不应记为已完成");
}

/* =================================================================== */
/* T5：本地日边界 —— 08:00 不是「新的一天」                            */
/* =================================================================== */
/* reminder.c 里提醒的「几点」「星期几」全部取自 localtime_r()（本地时区
 * CST-8），但 tick() 判断「是否跨天」用的 day_ordinal() 却是
 * time/86400 —— 那是 **UTC 日**。CST-8 下 UTC 日界落在本地 08:00，
 * 于是每天早上 8 点都会被当成新的一天：done_today / pending_today 被清空、
 * fired_today 与状态机归零，随后因为 now_min >= due 而**再次触发**。
 *
 * 后果是医疗事故级的，且两个方向都错：
 *   1. 老人 07:00 已服药并确认，记录在 08:00 被抹掉
 *      → 子女端看到「漏服」，触发不该有的告警与催促；
 *   2. 同一条提醒在 08:00 二次响铃，要老人再吃一次
 *      → 已服药者被要求重复服药（降压药/降糖药重复服用有实际危险）。
 *
 * 本组断言必须跑在真实时刻上（06:00 -> 07:00 -> 08:00 本地），
 * 不能靠推进受控时钟糊弄过去：只有让本地时间真的跨过 08:00，
 * UTC 日界才会翻转，缺陷才会暴露。 */
static void test_local_day_boundary(void)
{
    printf("\n[T5] 本地日边界：08:00 不得被当成「新的一天」清掉服药记录\n");
    stub_nvs_init();
    stub_time_set(1772661600L);   /* 2026-03-05 06:00 +08:00，可信时钟 */
    elder_reminder_init(NULL, NULL);

    elder_rmd_item_t m = mk_med(7, 0, "早上的药", 0);
    elder_reminder_add(&m);

    /* 06:00：未到点，不该触发 */
    elder_reminder_tick_once();
    const elder_rmd_table_t *t = tbl();
    check("未到点不触发", t && t->pending_today == 0, NULL);

    /* 07:00：到点触发 */
    stub_time_advance(3600);
    elder_reminder_tick_once();
    t = tbl();
    check("07:00 到点触发", t && (t->pending_today & 1u) != 0, NULL);

    /* 老人确认服药完成 */
    check("确认 07:00 那条", elder_reminder_confirm(0) == ESP_OK, NULL);
    t = tbl();
    check("07:00 已记为完成", t && (t->done_today & 1u) != 0, NULL);

    /* 08:00：本地仍是同一天（UTC 恰好跨了日界，正是缺陷触发窗口） */
    stub_time_advance(3600);
    elder_reminder_tick_once();
    t = tbl();
    check("08:00 不清掉当天已完成的服药记录",
          t && (t->done_today & 1u) != 0,
          "记录被清空 -> 子女端会看到假「漏服」并误告警");
    check("已确认的提醒不在 08:00 二次响铃",
          t && (t->pending_today & 1u) == 0,
          "老人已服药却被要求再吃一次 -> 重复服药有实际危险");
}

/* =================================================================== */
/* T6：跨天必须真的重置 —— T5 的必要反向                              */
/* =================================================================== */
/* T5 守的是「同一天内不许误重置」，本组守的是它的反面：「跨天必须重置」。
 * 两者缺一不可，只写 T5 会留下一半的漏洞：
 * T5 只在**同一天**内推进时钟，所以若 day_ordinal() 恒定返回同一个值
 * （或恒为 0，被 `last_day_ordinal != 0` 的守卫吃掉），
 * 跨天重置就永远不会发生 —— 而 T5 全程观察不到，因为那天里没跨天。
 *
 * 那个缺陷的后果同样是医疗事故级：服药提醒每天都要响，
 * 一旦跨天不再重置，fired_today 永远为真，
 * 老人第二天开始**再也收不到任何服药提醒**，且没有任何报错。
 * 「测试全绿但提醒再也不响」是最坏的失效形态，所以必须钉死。 */
static void test_next_day_fires_again(void)
{
    printf("\n[T6] 跨天重置生效：第二天的服药提醒仍会照常响\n");
    stub_nvs_init();

    /* 2026-03-05 06:00 +08:00（周四） */
    stub_time_set(1772661600L);
    elder_reminder_init(NULL, NULL);

    elder_rmd_item_t m = mk_med(7, 0, "早上的药", 0);
    elder_reminder_add(&m);

    /* ---- 第一天：07:00 响 + 确认完成 ---- */
    stub_time_advance(3600);              /* 第一天 07:00 */
    elder_reminder_tick_once();
    const elder_rmd_table_t *t = tbl();
    check("第一天 07:00 触发", t && (t->pending_today & 1u) != 0, NULL);
    elder_reminder_confirm(0);
    t = tbl();
    check("第一天已确认完成", t && (t->done_today & 1u) != 0, NULL);

    /* ---- 跨到第二天 06:00：昨天的完成状态必须清掉 ---- */
    stub_time_set(1772661600L + 86400);   /* 2026-03-06 06:00 +08:00 */
    elder_reminder_tick_once();
    t = tbl();
    check("跨天后昨日完成状态被清空",
          t && (t->done_today & 1u) == 0,
          "跨天不重置 -> 第二天提醒被当成已完成，从此再也不响");

    /* ---- 关键：第二天 07:00 必须再次响铃 ---- */
    stub_time_advance(3600);              /* 第二天 07:00 */
    elder_reminder_tick_once();
    t = tbl();
    check("第二天 07:00 再次触发",
          t && (t->pending_today & 1u) != 0,
          "每天都要吃的药，第二天没响 = 静默漏服药，且无任何报错");
}

/* =================================================================== */
/* T7：漏服 15 分钟未确认 → 升级 ESCALATED（红灯告警）                    */
/* =================================================================== */
/* reminder.c:222 `if (state == PENDING && (now_min - due) >= 15)`。
 *
 * 为什么这是医疗级的一条：ESCALATED 是**「漏服了、人要介入」的唯一信号**。
 * 它同时驱动两件事：
 *   1. elder_rgb_med_escalate() —— 老人眼前的红灯；
 *   2. elder_sync_on_reminder_state() 上报 ELDER_EV_MISSED —— 子女端收到告警。
 * 这一行静默失效的后果比「二次响铃」更隐蔽也更危险：老人漏服药，
 * 设备一声不吭，子女端也永远收不到告警 —— 两边都以为一切正常。
 *
 * 注意断言必须钉「15 分钟」这个**具体阈值**（14 分钟不许升级），
 * 而不是笼统地「最终会升级」：把 >=15 写成 >=5 同样能让「最终升级」通过，
 * 但那意味着老人刚点完药 5 分钟就被判漏服、子女端收到假告警。 */
static void test_escalate_after_15min(void)
{
    printf("\n[T7] 漏服 15 分钟未确认必须升级为 ESCALATED（红灯 + 上报子女端）\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 2026-03-05 06:00 +08:00 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    check("新增 08:00 服药提醒", elder_reminder_add(&m) == ESP_OK, NULL);

    stub_time_advance(2 * 3600);     /* 08:00 到点 */
    stub_fx_reset();
    elder_reminder_tick_once();
    check("08:00 到点触发为 PENDING",
          g_last_st[0] == ELDER_RMD_PENDING && stub_rgb_pending_count() == 1,
          det("状态=%s，红灯 pending=%d escalate=%d", state_name(g_last_st[0]),
              stub_rgb_pending_count(), stub_rgb_escalate_count()));

    /* 14 分钟：还没到阈值，绝不能提前升级。
     * 提前升级 = 老人刚把药放进嘴里就收到子女端「漏服」告警，
     * 后果是老人被反复催促、误以为药没吃（真实存在的老年依从性事故）。 */
    stub_time_advance(14 * 60);      /* 08:14 */
    elder_reminder_tick_once();
    check("08:14（未满 15 分钟）不得升级",
          g_last_st[0] == ELDER_RMD_PENDING && saw(0, ELDER_RMD_ESCALATED) == 0,
          det("状态=%s，已进入 ESCALATED %d 次（阈值被改小 -> 假漏服告警）",
              state_name(g_last_st[0]), saw(0, ELDER_RMD_ESCALATED)));

    /* 第 15 分钟：必须升级 */
    stub_time_advance(60);           /* 08:15 */
    elder_reminder_tick_once();
    check("08:15（满 15 分钟未确认）升级为 ESCALATED",
          g_last_st[0] == ELDER_RMD_ESCALATED,
          det("状态=%s —— 升级失效 = 老人漏服药无人知晓（设备不响、子女端不告警）",
              state_name(g_last_st[0])));
    check("升级必须真的点亮红灯（elder_rgb_med_escalate）",
          stub_rgb_escalate_count() == 1,
          det("escalate 红灯亮了 %d 次（老人眼前的告警）", stub_rgb_escalate_count()));

    /* 升级态是「持续告警」，不是一次性事件：再过 10 分钟仍应是 ESCALATED，
     * 且不得回落成 PENDING（回落 = 红灯熄灭 = 又变成没人管）。 */
    stub_time_advance(10 * 60);      /* 08:25 */
    elder_reminder_tick_once();
    check("08:25 仍保持 ESCALATED（告警持续，不回落）",
          g_last_st[0] == ELDER_RMD_ESCALATED && stub_rgb_escalate_count() == 1,
          det("状态=%s，escalate 红灯累计 %d 次（回落或反复重闪都是错的行为）",
              state_name(g_last_st[0]), stub_rgb_escalate_count()));

    /* 升级态下 done_today 绝不能被置位 —— 否则子女端看到的是「已服药」，
     * 与刚发出的 MISSED 告警自相矛盾，等于把漏服悄悄抹掉。 */
    const elder_rmd_table_t *t = tbl();
    check("升级态不得被记成「已完成」",
          t && (t->done_today & 1u) == 0 && (t->pending_today & 1u) != 0,
          det("%s（done 有位 = 子女端以为已服药，漏服被悄悄抹掉）", dump_tbl(t)));
    check("漏服期间仍算「有未完成的服药」（红灯必须保持长亮）",
          elder_reminder_has_pending_medicine(),
          "has_pending_medicine()=false -> 红灯熄灭，没人知道药没吃");

    /* 反向：确认过就不该再升级（防「无论确认与否都升级」） */
    check("确认后完成", elder_reminder_confirm(0) == ESP_OK, NULL);
    t = tbl();
    check("确认后记为已完成", t && (t->done_today & 1u) != 0,
          det("%s", dump_tbl(t)));
    stub_time_advance(60);           /* 08:26 */
    elder_reminder_tick_once();
    check("已确认的提醒不再升级",
          g_last_st[0] == ELDER_RMD_DONE && saw(0, ELDER_RMD_ESCALATED) == 1,
          det("状态=%s，ESCALATED 累计 %d 次（确认后仍升级 = 已服药却被判漏服）",
              state_name(g_last_st[0]), saw(0, ELDER_RMD_ESCALATED)));
}

/* =================================================================== */
/* T8：按键确认窗 30 秒内没短按 → 回退到 PENDING                        */
/* =================================================================== */
/* reminder.c:215-219。elder_reminder_arm_confirm()（按键长按）把状态推进
 * CONFIRM_WIN 并起 30 秒计时；超时后必须回退到 PENDING。
 *
 * 老人「长按了但没短按」的实际含义是：他走到药盒前、按了按钮，然后走开了
 * （接电话、被打扰、干脆忘了）。此时设备**不能**认为他在确认流程里，
 * 更不能认为已服药 —— 必须回到「未确认」并保持红灯，
 * 于是 15 分钟后照常升级成 ESCALATED，子女端最终会收到漏服告警。
 * 这正是这条分支的医疗价值。
 *
 * 去掉它之后的失效形态是「静默卡死」：状态永远停在 CONFIRM_WIN，
 * 红灯不升级、子女端收不到 MISSED，老人漏服药而三方都以为没事。
 * 比「提前升级」更难被发现，所以必须钉死。 */
static void test_confirm_window_timeout(void)
{
    printf("\n[T8] 按键确认窗 30 秒超时必须回退到未确认态\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    elder_reminder_add(&m);

    stub_time_advance(2 * 3600);     /* 08:00 到点 */
    stub_fx_reset();
    elder_reminder_tick_once();
    check("08:00 触发为 PENDING", g_last_st[0] == ELDER_RMD_PENDING, NULL);

    /* 模拟老人长按（elder_bridge.cc 的按键路径就是这么调的） */
    elder_reminder_arm_confirm(0);
    check("长按后进入确认窗口 CONFIRM_WIN",
          g_last_st[0] == ELDER_RMD_CONFIRM_WIN,
          det("状态=%s（按键长按未打开确认窗 = 老人按键确认路径直接失效）",
              state_name(g_last_st[0])));
    check("打开确认窗不等于已服药",
          (tbl()->done_today & 1u) == 0,
          det("%s（done 有位 = 按了一下就记成服药）", dump_tbl(tbl())));

    /* 29 秒：窗口内不得提前回退 —— 否则老人稍慢一点按短按就被拒 */
    stub_time_advance(29);
    elder_reminder_tick_once();
    check("29 秒时仍在确认窗口内（不得提前回退）",
          g_last_st[0] == ELDER_RMD_CONFIRM_WIN,
          det("状态=%s —— 窗口被设得过短，老人按键确认会被判失败",
              state_name(g_last_st[0])));

    /* 跨过 30 秒：必须回退 */
    stub_time_advance(2);            /* 共 31 秒 */
    elder_reminder_tick_once();
    check("超过 30 秒未短按 → 回退为 PENDING",
          g_last_st[0] == ELDER_RMD_PENDING,
          det("状态=%s —— 卡在 CONFIRM_WIN = 红灯永不升级、子女端永远收不到漏服告警",
              state_name(g_last_st[0])));
    check("回退到 PENDING 时红灯重新点亮（med_pending）",
          stub_rgb_pending_count() == 2,
          det("pending 红灯累计 %d 次（应为 2：触发 1 次 + 回退 1 次）",
              stub_rgb_pending_count()));
    check("回退绝不能被记成已完成",
          (tbl()->done_today & 1u) == 0,
          det("%s（超时回退若被写成 DONE = 长按了一下就凭空记一次服药）",
              dump_tbl(tbl())));
    check("回退后仍算「有未完成的服药」",
          elder_reminder_has_pending_medicine(),
          "has_pending_medicine()=false -> 红灯熄灭，漏服无人知晓");

    /* 计时器必须被清掉：否则下一次 tick 会拿一个早已过期的 deadline 反复回退 */
    int pend_before = stub_rgb_pending_count();
    stub_time_advance(5);
    elder_reminder_tick_once();
    check("deadline 已清零，不会反复触发回退",
          stub_rgb_pending_count() == pend_before && g_last_st[0] == ELDER_RMD_PENDING,
          det("pending 红灯 %d -> %d，状态=%s",
              pend_before, stub_rgb_pending_count(), state_name(g_last_st[0])));

    /* 回退之后必须还能重新 arm：老人返回来再按一次是有意义的动作。
     * 判据必须用「是否真的发生了第二次进入 CONFIRM_WIN 的转移」，
     * 而不是只看最终状态 —— 回退没发生时状态本来就停在 CONFIRM_WIN，
     * 只看最终状态的话这条断言在坏实现上也会绿。 */
    int cwin_before = saw(0, ELDER_RMD_CONFIRM_WIN);
    elder_reminder_arm_confirm(0);
    check("回退后仍可再次打开确认窗（按键路径未被卡死）",
          saw(0, ELDER_RMD_CONFIRM_WIN) == cwin_before + 1,
          det("CONFIRM_WIN 转移次数 %d -> %d —— arm_confirm() 只接受 PENDING/ESCALATED，"
              "状态卡在 CONFIRM_WIN 就直接 return，老人再也开不了确认窗",
              cwin_before, saw(0, ELDER_RMD_CONFIRM_WIN)));

    /* 窗口内正常短按必须能确认成功 —— 回退逻辑不得反过来把正常按键也吃掉 */
    check("窗口内短按确认成功",
          elder_reminder_confirm_ex(0, 2) == ESP_OK &&
          (tbl()->done_today & 1u) != 0,
          det("状态=%s，%s", state_name(g_last_st[0]), dump_tbl(tbl())));
}

/* =================================================================== */
/* T9：NVS CRC 校验 —— 坏数据绝不能被当成有效提醒表                      */
/* =================================================================== */
/* reminder.c:100-106：读出 NVS 后重算 CRC，不匹配则整表清空并返回
 * ESP_ERR_INVALID_CRC（随后 init() 走「no saved table, starting empty」）。
 *
 * 为什么是医疗级：断网设备最怕的不是「提醒丢了」，而是**读出坏数据还当成
 * 有效的提醒表继续用** —— 那意味着老人照着乱码出来的时间吃药
 * （比如时刻被翻成 03:00，凌晨三点把人叫醒吃降压药）。
 * 整表丢弃 + 重新同步是可恢复的降级；照着坏数据响是不可逆的伤害。
 *
 * 这一条此前一行断言都没碰过：stub_nvs_init() 之后写入的永远是 CRC 自洽的
 * 数据，校验分支根本不可达。stub_nvs_corrupt() 就是为了把它变成可达 ——
 * 模拟 NVS 位翻转 / 掉电写坏（这是真实故障，不是人为刁难）。 */
static void test_nvs_crc_rejects_corrupt(void)
{
    printf("\n[T9] NVS 读出坏数据必须被 CRC 拦下（不得当成有效提醒表）\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t a = mk_med(8, 0, "早上的药", 0);
    elder_rmd_item_t b = mk_med(20, 0, "晚上的药", 0);
    check("写入两条服药提醒",
          elder_reminder_add(&a) == ESP_OK && elder_reminder_add(&b) == ESP_OK, NULL);

    /* ---- 对照组：数据完好时必须读得回来 ----
     * 没有这一段，「重启后表空了」既可能是 CRC 正确拦下了坏数据，
     * 也可能是 NVS 桩根本没在工作 —— 两种情况这组断言都会绿，
     * 那就是假绿。 */
    elder_reminder_init(rec_cb, NULL);
    const elder_rmd_table_t *t = tbl();
    check("对照组：数据完好时重启后两条提醒都在",
          t && t->count == 2 && find_idx(t, 8, 0, "早上的药") >= 0
                             && find_idx(t, 20, 0, "晚上的药") >= 0,
          det("%s", dump_tbl(t)));

    /* ---- 现在写坏一个字节：把 items[0].hour 从 8 翻成别的值 ----
     * offset 用 offsetof 从结构体定义推导，不写死魔数：
     * 结构体布局一变（比如加字段），写死的偏移会悄悄指到别的字节上，
     * 测试仍然「绿」，但验的根本不是这一条。 */
    size_t items_off = offsetof(elder_rmd_table_t, items);
    size_t hour_off  = items_off + offsetof(elder_rmd_item_t, hour);
    size_t table_len = sizeof(elder_rmd_table_t);
    check("损坏偏移落在提醒表范围内（前置条件）",
          items_off == 0 && hour_off > 0 && hour_off < table_len,
          det("items@%zu, items[0].hour@%zu, 表长 %zu 字节", items_off, hour_off, table_len));
    stub_nvs_corrupt(0, hour_off, 0xFF);
    printf("  [INFO] 已翻转 NVS 第 %zu 字节（items[0].hour: 8 -> %u）\n",
           hour_off, (unsigned)(8u ^ 0xFFu));

    rec_reset();
    elder_reminder_init(rec_cb, NULL);
    t = tbl();
    check("CRC 不匹配时整表被丢弃（不得半信半疑地留一半）",
          t && t->count == 0,
          det("实际 count=%u —— 坏数据被当成了有效提醒表，%s",
              t ? t->count : 0, dump_tbl(t)));
    check("损坏后不残留任何提醒条目",
          t && find_idx(t, 8, 0, "早上的药") < 0 && find_idx(t, 20, 0, "晚上的药") < 0
             && find_idx(t, 255, 0, "早上的药") < 0,
          det("找 08:00/20:00/255:00 三种时刻，%s", dump_tbl(t)));
    check("损坏后当天的完成/待办位图一并清零",
          t && t->done_today == 0 && t->pending_today == 0,
          det("done=0x%04x pending=0x%04x —— 坏数据里的「已完成」会让子女端看到假绿",
              t ? t->done_today : 0xFFFF, t ? t->pending_today : 0xFFFF));
    check("丢弃坏表后不得凭空触发任何提醒（更不能按乱码时刻响）",
          g_trans_n == 0 && stub_rgb_escalate_count() == 0,
          det("重启后发生了 %d 次状态转移，红灯 escalate=%d", g_trans_n,
              stub_rgb_escalate_count()));

    /* ---- 丢弃之后引擎必须还能用：坏表只是「降级到空表」，
     * 不是把设备卡死。重新同步一次就应恢复正常 ---- */
    stub_time_advance(3 * 3600);     /* 09:00 */
    elder_reminder_tick_once();
    check("坏表被丢弃后 tick 不崩、也不误触发",
          g_trans_n == 0 && tbl()->count == 0,
          det("tick 后 count=%u，状态转移 %d 次", tbl()->count, g_trans_n));

    check("重新下发提醒后引擎恢复正常",
          elder_reminder_add(&a) == ESP_OK && tbl()->count == 1,
          det("%s", dump_tbl(tbl())));
    stub_time_advance(3600);         /* 10:00，已过 08:00 */
    elder_reminder_tick_once();
    check("重新下发后到点仍会触发（丢弃坏表没有把引擎卡死）",
          (tbl()->pending_today & 1u) != 0,
          det("%s", dump_tbl(tbl())));
}

/* =================================================================== */
/* T10：上电后第一个 tick 不得做跨天重置                                  */
/* =================================================================== */
/* reminder.c:180 `if (g.table.last_day_ordinal != 0 && ord != g.table.last_day_ordinal)`。
 *
 * 那个 `!= 0` 是「本设备今天还没跑过 tick」的哨兵。去掉它之后，
 * **每一次上电的第一个 tick 都会走跨天重置分支**：done_today / pending_today
 * 被清空、fired_today 与状态机归零、每条的 fired_due_at 被抹成 0。
 * 医疗后果与刚修的 UTC 时区 bug 是同一条危害链上的另一个入口 ——
 * 区别只在于触发入口从「每天 08:00」换成了「每次上电」：
 *   1. 老人已确认的服药记录被清空 -> 子女端看到假「漏服」并误告警；
 *   2. 同一条提醒因 fired_today 归零而**再次响铃**，并立刻升级 ESCALATED
 *      —— 已服药者被要求再吃一次（重复服用有实际危险）。
 *
 * T1 只验了「重启后提醒还在」，但它的重启发生在**任何 tick 之前**，
 * last_day_ordinal 仍是 0，守卫恰好把它挡住了；且 T1 重启后没有推进到
 * 到点时刻，观察不到二次响铃。所以这里必须单独构造：
 * 先让引擎跑过 tick 并把确认结果落盘（此时 last_day_ordinal 已非 0），
 * 再模拟「同一天内断电重启」—— 上电后第一个 tick 就撞上 ord != last_day_ordinal
 * 的判断之外的情形。
 *
 * 手法：add + confirm 都在**首次 tick 之前**完成（这对应真实场景里
 * 「设备刚上电、子女端远程确认」——sync 的远程确认不依赖提醒先响过），
 * 于是落盘的表里 done_today 有位而 last_day_ordinal 仍是 0。
 *
 * 这条守卫的影响半径**恰好只有 done_today 一个位**（已逐条核对跨天重置的
 * 7 行副作用：pending_today 与 items[].fired_tue_at 也被清，但它们非零的
 * 前提是「这条今天已经响过」，而响过必然意味着 tick 跑过、
 * last_day_ordinal 早已非 0 —— 在本场景下恒为 0）。所以这里不去断言那些
 * 恒为 0 的字段，那样只会写出「恒真」的假断言、让人误以为覆盖面更宽。 */
static void test_first_tick_day_guard(void)
{
    printf("\n[T10] 上电后第一个 tick 不得当成「新的一天」清掉服药记录\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    check("新增 08:00 服药提醒", elder_reminder_add(&m) == ESP_OK, NULL);

    /* 设备刚上电、tick 还没跑过，子女端就远程确认了（模拟 APP 远程确认） */
    check("上电后立刻收到确认", elder_reminder_confirm_ex(0, 3) == ESP_OK, NULL);
    const elder_rmd_table_t *t = tbl();
    check("确认已落盘", t && (t->done_today & 1u) != 0, det("%s", dump_tbl(t)));
    check("此刻 last_day_ordinal 仍为 0（本设备还没跑过 tick）",
          t && t->last_day_ordinal == 0,
          det("last_day_ordinal=%d —— 前置条件不成立，本组就验不到那条守卫",
              t ? t->last_day_ordinal : -1));

    /* 同一天 07:30 断电重启：表还在、完成记录还在 */
    stub_time_set(1772661600L + 90 * 60);
    rec_reset();
    elder_reminder_init(rec_cb, NULL);
    t = tbl();
    check("重启后提醒与完成记录都还在",
          t && t->count == 1 && (t->done_today & 1u) != 0,
          det("%s", dump_tbl(t)));

    /* 上电后的第一个 tick —— 守卫就在这一行 */
    uint16_t done_before = t ? t->done_today : 0;
    elder_reminder_tick_once();
    t = tbl();
    check("上电首个 tick 不得清掉当天已完成的服药记录",
          t && (t->done_today & 1u) != 0,
          det("done 0x%04x -> 0x%04x —— 记录被清空 = 子女端看到假「漏服」并误告警",
              done_before, t ? t->done_today : 0xFFFF));
    check("上电首个 tick 不得凭空把提醒拉回待办（=已服药者被要求再吃一次）",
          t && (t->pending_today & 1u) == 0,
          det("pending=0x%04x —— 重复服药对降压/降糖药有实际危险", t ? t->pending_today : 0xFFFF));
    check("上电首个 tick 不得重新触发已响过的提醒",
          g_trans_n == 0 || g_trans_st[0] != ELDER_RMD_PENDING,
          det("首个 tick 后第 1 次状态转移 = %s",
              g_trans_n ? state_name(g_trans_st[0]) : "无"));
    check("上电首个 tick 后引擎认下今天（last_day_ordinal 被写入）",
          t && t->last_day_ordinal != 0,
          det("last_day_ordinal=%d —— 仍是 0 的话，第二个 tick 会再做一次重置",
              t ? t->last_day_ordinal : -1));

    /* 连续两次上电：守卫必须每次都挡住，不能只挡第一次。
     * 反过来想：只验一次上电的话，「把守卫改成只对第一次生效」这种坏修法
     * 也能过 —— 而它的失效形态恰恰是「一天之内偶尔冒出一次假漏服」，
     * 最难在真机上复现。 */
    stub_time_advance(600);          /* 07:40 */
    elder_reminder_init(rec_cb, NULL);
    t = tbl();
    check("第二次上电后完成记录仍在",
          t && (t->done_today & 1u) != 0, det("%s", dump_tbl(t)));
    rec_reset();
    elder_reminder_tick_once();
    t = tbl();
    check("第二次上电的首个 tick 同样不得清掉记录",
          t && (t->done_today & 1u) != 0,
          det("%s —— 每次上电都产生一次假「漏服」", dump_tbl(t)));

    /* 反向：守卫不得把「真跨天」也一起挡掉。
     * 若有人把守卫改成 `if (0 && ...)` 或误改成永远不重置，
     * 第二天提醒就再也不会响 —— 所以这里必须补上这一段反向。 */
    stub_time_set(1772661600L + 86400 + 90 * 60);   /* 次日 07:30 */
    rec_reset();
    elder_reminder_tick_once();
    t = tbl();
    check("跨到第二天仍会真的重置（守卫没有把跨天逻辑一起挡掉）",
          t && (t->done_today & 1u) == 0,
          det("%s —— 跨天不重置 = 第二天提醒被当成已完成，从此再不响", dump_tbl(t)));
}

int main(void)
{
    /* 时区必须与固件一致（elder_bridge.cc 同样固定 CST-8）：
     * 否则 localtime_r() 算出的「几点」与提醒的 hour/minute 对不上，
     * 到点触发的断言会因为运行机器时区不同而随机失败。 */
    setenv("TZ", "CST-8", 1);
    tzset();

    printf("=== elder 断网链路宿主测试 ===\n");
    test_local_persistence();
    test_replace_all_keeps_local();
    test_offline_command_dispatch();
    test_untrusted_clock_guard();
    test_local_day_boundary();
    test_next_day_fires_again();
    test_escalate_after_15min();
    test_confirm_window_timeout();
    test_nvs_crc_rejects_corrupt();
    test_first_tick_day_guard();

    printf("\n=== %d 通过 / %d 失败 ===\n", g_pass, g_fail);
    return g_fail ? 1 : 0;
}