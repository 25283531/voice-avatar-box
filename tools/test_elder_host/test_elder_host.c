/* test_elder_host.c —— 断网提醒链路的宿主单元测试。
 *
 * 覆盖的断言都对应医疗事故级的失效（详见 Makefile 顶部说明）：
 *   T1  提醒时间存本地（NVS）—— 断网重启后提醒还在
 *   T2  replace_all 不抹掉本地自建 —— 一次心跳同步不该让老人的服药提醒消失
 *   T3  （已删除）断网新建 + 断网确认 —— 2026-10-20 回归上游小智，离线命令词全删
 *   T4  时钟不可信时不触发 —— 响错时间比不提醒更危险
 *   T5  本地日界不错判 —— 08:00 不是「新的一天」
 *   T6  跨天真的重置 —— 第二天提醒仍要响
 *   T7  漏服 15 分钟升级告警 —— ESCALATED 是「人要介入」的唯一信号
 *   T8  按键确认窗 30 秒超时回退 —— 按了键又不管了必须回到未确认
 *   T9  NVS CRC 校验 —— 坏数据绝不能被当成有效提醒表
 *   T10 上电首 tick 不做跨天重置 —— 否则每次上电都产生一次假「漏服」
 *   T11 重启后不得二次响铃 —— 见 test_restart_no_refire 上方的失效链推演
 *   T12 到点后很久才开机 —— 不补响、不升级，判 SKIPPED（与 T11-6 反向成对）
 *
 * 全程**没有任何网络调用**：这正是要验证的场景。
 */
#include "stubs.h"

#include "elder_reminder.h"
#include "elder_ui.h"
#include "elder_sync.h"   /* ELDER_CFM_VOICE：T13 要断言确认方式不被写成 BUTTON */

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
    stub_fx_reset();                     /* 只统计本次 tick 的副作用 */
    elder_reminder_tick_once();
    const elder_rmd_table_t *t = tbl();
    check("时钟不可信时不触发任何提醒",
          t && t->pending_today == 0 && t->done_today == 0,
          "响错时间的服药提醒比不提醒更危险");

    /* 2026-10-21 补的两条：原来的实现**只打一条 ESP_LOGE**，
     * 而老人看到的是**一切正常的待机屏** —— 当天一条都不会响却无人知晓。
     * 所以「不响」必须配「让人看见」，且不能靠提示音（此刻多半离线，
     * 突兀的响声只会吓人一跳）。 */
    check("★未校时必须在屏幕上说明（光打日志等于没说）",
          stub_notice_count() >= 1,
          det("屏显 %d 次 —— 老人以为设备一切正常，实际当天所有提醒都不会响",
              stub_notice_count()));
    check("未校时提示只屏显、不发声",
          stub_tone_count() == 0,
          det("提示音 %d 次 —— 离线状态下突兀的响声只会吓人", stub_tone_count()));

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
 * 它驱动 elder_sync_on_reminder_state() 上报 ELDER_EV_MISSED（子女端告警）。
 * ⚠️ 需求 8（2026-10-22）之后它**不再点灯**：老人眼前的红灯统一由
 * `elder_rgb_reminder_nag_start()` 按「闪 10s / 每 10min / 2 小时后停」负责，
 * 两套判据同时点灯会让「2 小时后不再亮灯」永远不成立。
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
          g_last_st[0] == ELDER_RMD_PENDING
              && stub_rgb_nag_count() == 1,
          det("状态=%s，闪灯 nag=%d escalate=%d", state_name(g_last_st[0]),
              stub_rgb_nag_count(), stub_rgb_escalate_count()));

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
    /* 需求 8（2026-10-22 用户拍板）**覆盖**了原来的「升级就快速闪红」：
     * ESCALATED 刻意**不动灯**，交给 nag 按自己的「闪 10s / 每 10min / 2 小时后
     * 不再亮」节奏走。两套点灯判据同时存在会互相打架 —— 升级一旦发生，
     * 「2 小时后不再亮灯」就永远不成立。
     * 所以这条断言从「升级必须点亮红灯」改成「升级不得额外点灯」：
     * 它仍然钉着"红灯告警这件事由谁负责"，只是责任人从 escalate 换成了 nag。 */
    check("升级为 ESCALATED 时不动灯（点灯交给 nag，见需求 8）",
          stub_rgb_escalate_count() == 0 && stub_rgb_nag_count() == 1,
          det("escalate=%d nag=%d —— ESCALATED 额外点灯会让"
              "「2 小时后不再亮灯」永远不成立",
              stub_rgb_escalate_count(), stub_rgb_nag_count()));

    /* 升级态是「持续告警」，不是一次性事件：再过 10 分钟仍应是 ESCALATED，
     * 且不得回落成 PENDING（回落 = 红灯熄灭 = 又变成没人管）。 */
    stub_time_advance(10 * 60);      /* 08:25 */
    elder_reminder_tick_once();
    check("08:25 仍保持 ESCALATED（告警持续，不回落、也不重复点灯）",
          g_last_st[0] == ELDER_RMD_ESCALATED
              && stub_rgb_escalate_count() == 0 && stub_rgb_nag_count() == 1,
          det("状态=%s，escalate=%d nag=%d（回落=灯灭没人管；"
              "反复点灯=2 小时后仍亮）",
              state_name(g_last_st[0]), stub_rgb_escalate_count(),
              stub_rgb_nag_count()));

    /* 升级态下 done_today 绝不能被置位 —— 否则子女端看到的是「已服药」，
     * 与刚发出的 MISSED 告警自相矛盾，等于把漏服悄悄抹掉。 */
    const elder_rmd_table_t *t = tbl();
    check("升级态不得被记成「已完成」",
          t && (t->done_today & 1u) == 0 && (t->pending_today & 1u) != 0,
          det("%s（done 有位 = 子女端以为已服药，漏服被悄悄抹掉）", dump_tbl(t)));
    check("漏服期间仍算「有未完成的服药」（红灯告警必须还在进行中）",
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
    check("回退到 PENDING 时闪灯重新开始（nag_start）",
          stub_rgb_nag_count() == 2,
          det("nag 累计 %d 次（应为 2：触发 1 次 + 回退 1 次）",
              stub_rgb_nag_count()));
    check("回退绝不能被记成已完成",
          (tbl()->done_today & 1u) == 0,
          det("%s（超时回退若被写成 DONE = 长按了一下就凭空记一次服药）",
              dump_tbl(tbl())));
    check("回退后仍算「有未完成的服药」",
          elder_reminder_has_pending_medicine(),
          "has_pending_medicine()=false -> 红灯熄灭，漏服无人知晓");

    /* 计时器必须被清掉：否则下一次 tick 会拿一个早已过期的 deadline 反复回退 */
    int nag_before = stub_rgb_nag_count();
    stub_time_advance(5);
    elder_reminder_tick_once();
    check("deadline 已清零，不会反复触发回退",
          stub_rgb_nag_count() == nag_before && g_last_st[0] == ELDER_RMD_PENDING,
          det("nag %d -> %d，状态=%s",
              nag_before, stub_rgb_nag_count(), state_name(g_last_st[0])));

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

    /* 重新下发的这条**刻意定在 10:00**，不用原来的 08:00：
     * 本用例要测的是「坏表被丢弃后引擎还能不能正常触发」，不是「过期补响」。
     * 2026-10-21 起，过期超过 ELDER_RMD_STALE_MIN（60 分钟）按设计**不补响**
     * （判 SKIPPED，见 T12）。此时钟已在 09:00，若仍用 08:00，这里测到的
     * 就会是过期跳过逻辑，而不是「引擎有没有被坏表卡死」——
     * 断言会红，但红的原因跟本用例要守的东西无关。 */
    elder_rmd_item_t re = mk_med(10, 0, "早上的药", 0);
    check("重新下发提醒后引擎恢复正常",
          elder_reminder_add(&re) == ESP_OK && tbl()->count == 1,
          det("%s", dump_tbl(tbl())));
    stub_time_advance(3600);         /* 10:00 到点 */
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

/* =================================================================== */
/* T11：重启后不得二次响铃（当天已响过 / 已确认服药的事实必须持久化）    */
/* =================================================================== */
/* 这是本仓最贵的一条失效链，因为它**每天都在发生**且**没有任何告警**：
 *
 *   08:00  触发          -> set_state(PENDING)，152 行 done_today &= ~bit
 *   08:05  老人确认服药  -> set_state(DONE)，   148 行 done_today |= bit
 *          （elder_reminder_confirm_ex 里立刻 flush，done_today=0x0001 落盘）
 *   ---- 同一天 09:00 断电重启（掉电/看门狗/OTA/弱电，老人设备掉电率不低）----
 *   load() 读回表，done_today=0x0001 确实还在表里
 *   但 state[] 靠 C 静态零初始化 = 全 IDLE，fired_today[] 同样全 false
 *   （修复前这两个数组**从不落盘**）
 *   重启后首个 tick：
 *     203 行 !false && IDLE && now_min(540) >= due(480) => true
 *       -> 二次 set_state(PENDING) -> done_today &= ~bit => 0x0001 变 0x0000
 *       -> 222 行 (540-480)=60 >= 15 => **同一个 tick 内**推成 ESCALATED
 *
 * 后果：子女端收到假「漏服」MISSED 告警；老人已吃过药却被要求再吃一次。
 * 重复服用降压/降糖/抗凝药有实际危险，这是医疗事故级的。
 *
 * 关键点：触发条件是 `now_min >= due`，**不是**「已确认过才不响」。
 * 所以修复前只要当天重启时已过服药时刻，**连今天本来还没到点响过的提醒**
 * 也会在开机瞬间补响一遍，然后立刻升级。本组用例把这个更宽的面也钉住。 */

/* T11-1：08:00 响 → 08:05 确认服药 → 同日 09:00 重启 → 绝不二次响铃 */
static void test_restart_no_refire(void)
{
    printf("\n[T11-1] 当天已确认服药后重启：不得二次响铃、不得升级告警\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    check("新增 08:00 服药提醒", elder_reminder_add(&m) == ESP_OK, NULL);

    /* 到点触发 */
    stub_time_advance(2 * 3600);     /* 08:00 */
    elder_reminder_tick_once();
    check("08:00 正常触发为 PENDING",
          g_last_st[0] == ELDER_RMD_PENDING,
          det("状态=%s（08:00 没响 = 到点触发被破坏）", state_name(g_last_st[0])));

    /* 老人长按确认：吃药 */
    stub_time_advance(5 * 60);       /* 08:05 */
    check("老人确认服药成功",
          elder_reminder_confirm_ex(0, 2) == ESP_OK, NULL);
    const elder_rmd_table_t *t = tbl();
    check("确认后 done_today 有位且已落盘",
          t && (t->done_today & 1u) != 0,
          det("%s", dump_tbl(t)));
    check("确认后当天响过的位也被记下",
          t && (t->fired_today & 1u) != 0,
          det("fired_today=0x%04x —— 这个位不落盘，重启后必然二次响铃",
              t ? t->fired_today : 0xFFFF));

    /* ---- 同一天 09:00 断电重启 ---- */
    stub_time_advance(55 * 60);      /* 09:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);

    uint16_t done_before = tbl()->done_today;
    check("重启后服药记录仍在（先确认没丢表）",
          (tbl()->done_today & 1u) != 0, det("%s", dump_tbl(tbl())));

    /* 重启后的第一个 tick —— 二次响铃就发生在这一行 */
    elder_reminder_tick_once();
    t = tbl();

    check("★重启后不得二次响铃（不得重新进入 PENDING）",
          saw(0, ELDER_RMD_PENDING) == 0,
          det("重启后又发生了 %d 次 PENDING —— 老人已吃过药却被要求再吃一次",
              saw(0, ELDER_RMD_PENDING)));
    check("★重启后不得升级成 ESCALATED（漏服 15 分钟的告警是假的）",
          saw(0, ELDER_RMD_ESCALATED) == 0,
          det("重启后发生了 %d 次 ESCALATED —— 子女端会收到假「漏服」告警",
              saw(0, ELDER_RMD_ESCALATED)));
    check("★elder_rgb_med_escalate() 不得被调用",
          stub_rgb_escalate_count() == 0,
          det("红灯升级被点亮 %d 次（老人已服药，红灯不该再亮）",
              stub_rgb_escalate_count()));
    check("★重启后 done_today 不得被清空（否则子女端看到假「漏服」）",
          (t->done_today & 1u) != 0,
          det("done_today 0x%04x -> 0x%04x —— 已服药记录被抹掉 = 假漏服告警",
              done_before, t->done_today));
    check("★重启后不得把提醒拉回待办",
          (t->pending_today & 1u) == 0,
          det("pending_today=0x%04x —— 已服药者被要求再吃一次",
              t->pending_today));
    check("重启后也不该重复点亮待确认红灯",
          stub_rgb_pending_count() == 0,
          det("med_pending 被点亮 %d 次", stub_rgb_pending_count()));

    /* ---- 反向：跨到第二天必须重新响，否则就修成了「永不响」 ---- */
    stub_time_set(1772661600L + 86400 + 2 * 3600);   /* 次日 08:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_tick_once();
    check("反向：跨天之后 08:00 仍会正常重新响",
          saw(0, ELDER_RMD_PENDING) == 1,
          det("次日发生了 %d 次 PENDING（应为 1）—— 跨天后永远不响 = 提醒彻底失效",
              saw(0, ELDER_RMD_PENDING)));
}

/* T11-2：07:00 重启（提醒 08:00 才到点）→ 不得提前响，到点后仍要正常响 */
static void test_restart_before_due(void)
{
    printf("\n[T11-2] 未到点就重启：不得提前响，到点后仍会正常响\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    elder_reminder_add(&m);

    /* 先跑一次 tick 让引擎认下今天（与真实场景一致：设备一直在跑） */
    stub_time_advance(3600);         /* 07:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_tick_once();
    check("07:00 尚未到点，不应触发",
          saw(0, ELDER_RMD_PENDING) == 0,
          det("07:00 就触发了 —— 到点前响铃比不响更危险"));

    /* 07:00 断电重启 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);
    elder_reminder_tick_once();
    check("★重启后不得提前响（提醒 08:00 才到点）",
          saw(0, ELDER_RMD_PENDING) == 0 && stub_rgb_pending_count() == 0,
          det("重启后 %d 次 PENDING、红灯 %d 次 —— 未到点就响 = 老人被无效打扰",
              saw(0, ELDER_RMD_PENDING), stub_rgb_pending_count()));
    check("未到点重启后不得把这条记成已完成",
          (tbl()->done_today & 1u) == 0,
          det("%s", dump_tbl(tbl())));

    /* 到点后必须照常响 —— 修复不能变成「重启一次就再也不响」 */
    stub_time_advance(3600);         /* 08:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_tick_once();
    check("★08:00 到点后仍会正常触发（没被修成永不响）",
          saw(0, ELDER_RMD_PENDING) == 1,
          det("08:00 的 PENDING 次数 = %d（应为 1）—— 漏响 = 老人漏服药且无人知晓",
              saw(0, ELDER_RMD_PENDING)));
    check("到点触发后 done_today 仍为空（响不等于吃了）",
          (tbl()->done_today & 1u) == 0, det("%s", dump_tbl(tbl())));

    /* 15 分钟后仍要升级 —— 确认「响过」没有把升级路径也一起堵掉 */
    stub_time_advance(16 * 60);
    rec_reset();
    elder_reminder_tick_once();
    check("响过后 15 分钟仍会升级为 ESCALATED（漏服告警路径没被堵）",
          saw(0, ELDER_RMD_ESCALATED) >= 1,
          det("ESCALATED 次数 = %d —— 堵掉升级 = 漏服无人知晓",
              saw(0, ELDER_RMD_ESCALATED)));
}

/* T11-3：跨天之后重启 → 新的一天要正常触发 */
static void test_restart_across_day(void)
{
    printf("\n[T11-3] 跨过日界后重启：新的一天必须正常触发\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    elder_reminder_add(&m);

    /* 当天 08:00 响、08:05 确认吃药 */
    stub_time_advance(2 * 3600);
    elder_reminder_tick_once();
    stub_time_advance(5 * 60);
    elder_reminder_confirm_ex(0, 2);
    check("首日确认服药成功", (tbl()->done_today & 1u) != 0,
          det("%s", dump_tbl(tbl())));

    /* 次日 07:00 重启（跨过日界）—— fired_today 若没随跨天重置清掉，
     * 新一天这条提醒就会永远不再响（比二次响铃更难发现） */
    stub_time_set(1772661600L + 86400 + 3600);   /* 次日 07:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);
    elder_reminder_tick_once();
    check("跨天重启后首日记录被正确重置",
          (tbl()->done_today & 1u) == 0,
          det("%s —— 跨天不重置 = 第二天提醒被当成已完成，从此再不响",
              dump_tbl(tbl())));
    check("跨天重启后 07:00 不得提前响",
          saw(0, ELDER_RMD_PENDING) == 0, NULL);

    /* 次日 08:00 必须响 */
    stub_time_advance(3600);
    rec_reset();
    stub_fx_reset();
    elder_reminder_tick_once();
    check("★次日 08:00 正常触发（新的一天响铃未被 fired_today 误锁）",
          saw(0, ELDER_RMD_PENDING) == 1,
          det("次日 PENDING 次数 = %d（应为 1）—— 跨天后被 fired_today 锁死 = 提醒彻底静默",
              saw(0, ELDER_RMD_PENDING)));

    /* 当天重启一次，确认「跨天 + 重启」叠加也不锁死 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);   /* 次日 08:05 重启 */
    elder_reminder_tick_once();
    check("★跨天当天再重启也不得二次响铃",
          saw(0, ELDER_RMD_PENDING) == 0 && (tbl()->done_today & 1u) == 0,
          det("PENDING %d 次、done=0x%04x",
              saw(0, ELDER_RMD_PENDING), tbl()->done_today));
    check("跨天当天重启后仍处于待确认（红灯保持，15 分钟后会升级）",
          (tbl()->pending_today & 1u) != 0,
          det("pending_today=0x%04x —— 红灯熄灭 = 漏服无人知晓",
              tbl()->pending_today));

    /* ---- 最阴的一格：跨天重置跑完之后**再**重启 ----
     * 跨天重置会把运行态的 fired_today[] 数组清成 false，但若落盘的位图
     * 漏清（只改了数组、没改 table.fired_today），那么「重置 → 上电」这个
     * 顺序下：RAM 数组是干净的，可落盘那份仍带着昨天的「响过」位。
     * 下次 init() 从位图恢复出 fired=true + state=IDLE —— 这个组合
     * 在语义上是「今天没响过但已经被标记成响过」，于是
     * `!fired_today[i]` 永远为假，这条提醒**今天再也不会响**。
     *
     * 这个失效方向与「二次响铃」相反：老人当天彻底收不到这条服药提醒，
     * 而且没有任何红灯、没有告警 —— 比二次响铃更难被发现。
     * 所以必须专门走一遍「跨天重置 → 落盘 → 重启 → 到点」这个顺序。 */
    stub_time_set(1772661600L + 2 * 86400 + 2 * 3600);   /* 第 3 天 08:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_tick_once();          /* 响一次，建立当天的 fired 位 */
    elder_reminder_flush();              /* 确保位图真的落盘了 */
    check("前置：第 3 天已响过并落盘",
          (tbl()->fired_today & 1u) != 0,
          det("fired_today=0x%04x", tbl()->fired_today));

    /* 跨到第 4 天 07:00（**未到点**）→ 跨天重置在未到点时就会跑，
     * 这正是「位图被清掉」的时机 */
    stub_time_set(1772661600L + 3 * 86400 + 3600);       /* 第 4 天 07:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_tick_once();
    check("前置：第 4 天跨天重置已执行",
          (tbl()->fired_today & 1u) == 0,
          det("fired_today=0x%04x —— 跨天若没清位图，本组就验不到那一格",
              tbl()->fired_today));
    elder_reminder_flush();

    /* 就在这个状态下重启 —— 位图若没被清，落盘那份仍带着「响过」 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);   /* 第 4 天 07:00 重启 */
    elder_reminder_tick_once();
    check("跨天重启后 07:00 不得提前响",
          saw(0, ELDER_RMD_PENDING) == 0, NULL);

    stub_time_advance(3600);             /* 第 4 天 08:00 到点 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_tick_once();
    check("★跨天重置后重启：当天到点必须仍能响（不得被昨天的 fired 位锁死）",
          saw(0, ELDER_RMD_PENDING) == 1,
          det("第 4 天 PENDING 次数 = %d（应为 1）—— 跨天漏清位图 = "
              "提醒当天彻底静默，无红灯无告警", saw(0, ELDER_RMD_PENDING)));
}

/* T11-4：老设备升级 —— 旧格式 blob 一条提醒都不许丢 */
static void test_legacy_blob_upgrade(void)
{
    printf("\n[T11-4] 老设备升级：旧格式 blob 的提醒与服药记录一条都不许丢\n");

    /* ⚠️ 先把时钟钉住，再构造 blob：下面要用 stub_time_now() 算
     * last_day_ordinal，而受控时钟是**跨用例保留**的（不像 NVS 那样
     * 每个用例重置）。顺序反了就会拿到上一个用例留下的时刻，
     * 算出的日序号对不上，升级后第一个 tick 会被误判成「新的一天」
     * 而清掉全部当日记录 —— 那会让本组用例测的根本不是升级兼容。 */
    stub_time_set(1772661600L + 3 * 3600);   /* 09:00：已过 08:00 */

    /* ---- 构造一份「旧固件写下的」blob ----
     * 直接手写 V1 布局而不是拿新结构去凑长度：这样才有「设备上存着的
     * 确实是老格式」的意义。crc32 必须按 V1 的尺寸算 —— 这正是老固件
     * 的做法，也是本条用例要验的核心（CRC 覆盖范围随 sizeof 变化）。 */
    uint8_t raw[sizeof(elder_rmd_table_v1_t)];
    memset(raw, 0, sizeof(raw));
    elder_rmd_table_v1_t v1;
    memset(&v1, 0, sizeof(v1));

    v1.items[0] = mk_med(8, 0, "早上的药", 101);
    v1.items[1] = mk_med(20, 0, "晚上的药", 102);
    v1.count = 2;
    /* 首日已经吃过早上的药，晚上那顿响过但还没确认 */
    v1.done_today    = 0x0001;
    v1.pending_today = 0x0002;
    /* 与当前 tick 同一个本地日序号：让升级后第一个 tick 不误判跨天，
     * 这样观察到的现象才是「升级本身有没有搞坏数据」而非日界效应。 */
    {
        time_t now = stub_time_now();
        struct tm tm_now;
        localtime_r(&now, &tm_now);
        v1.last_day_ordinal = tm_now.tm_year * 366 + tm_now.tm_yday;
    }
    /* 老固件的 crc32 就是按 sizeof(V1) 对全结构算的 */
    {
        uint32_t c = 0xFFFFFFFF;
        const uint8_t *p = (const uint8_t *)&v1;
        for (size_t i = 0; i < sizeof(v1); i++) {
            c ^= p[i];
            for (int k = 0; k < 8; k++)
                c = (c >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(c & 1)));
        }
        v1.crc32 = ~c;
    }
    memcpy(raw, &v1, sizeof(v1));

    /* ---- 前置条件：确认我们构造的确实是「更小的旧格式」 ---- */
    check("前置：旧格式尺寸确实小于当前格式（迁移路径存在）",
          sizeof(elder_rmd_table_v1_t) < sizeof(elder_rmd_table_t),
          det("V1=%zu 字节, V2=%zu 字节", sizeof(elder_rmd_table_v1_t),
              sizeof(elder_rmd_table_t)));

    stub_nvs_init();
    stub_nvs_write_raw(0, raw, sizeof(v1));
    check("前置：NVS 里存的是旧尺寸 blob",
          stub_nvs_blob_len(0) == sizeof(v1),
          det("已写入 %zu 字节", stub_nvs_blob_len(0)));

    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);   /* ← 模拟升级后第一次上电 */

    const elder_rmd_table_t *t = tbl();
    check("★升级后两条提醒一条都不丢",
          t && t->count == 2 && find_idx(t, 8, 0, "早上的药") >= 0 &&
                             find_idx(t, 20, 0, "晚上的药") >= 0,
          det("count=%u —— 老用户升级后服药提醒消失 = 医疗事故级数据丢失；%s",
              t ? t->count : 0, dump_tbl(t)));
    check("★升级后 server_id 也保住（否则会与后端下发的那条重复）",
          t && t->items[0].server_id == 101 && t->items[1].server_id == 102,
          det("server_id = %u / %u", t ? t->items[0].server_id : 0,
              t && t->count > 1 ? t->items[1].server_id : 0));
    check("★升级后已完成的服药记录不丢（done_today 原样保住）",
          t && t->done_today == 0x0001,
          det("done_today = 0x%04x（应为 0x0001）—— 老人已吃过药，"
              "记录丢了就是一次假「漏服」告警", t ? t->done_today : 0xFFFF));
    check("★升级后未确认的待办记录不丢（pending_today 原样保住）",
          t && t->pending_today == 0x0002,
          det("pending_today = 0x%04x（应为 0x0002）—— 红灯熄灭 = 漏服无人知晓",
              t ? t->pending_today : 0xFFFF));
    check("★升级后 last_day_ordinal 不丢（否则当天被当成新的一天全清）",
          t && t->last_day_ordinal == v1.last_day_ordinal,
          det("last_day_ordinal = %d（应为 %d）", t ? t->last_day_ordinal : -1,
              v1.last_day_ordinal));
    check("★升级后 CRC 校验没有被结构变化误判（数据不是被判成坏表丢掉的）",
          t && t->count == 2,
          det("count=%u —— 若为 0 说明旧 CRC 按 V2 尺寸重算不匹配，"
              "整张表被当成坏数据丢弃", t ? t->count : 0));
    check("升级后已自动改写成新格式（下次重启走快路径）",
          stub_nvs_blob_len(0) == sizeof(elder_rmd_table_t),
          det("落盘 %zu 字节（应为 %zu）—— 没改写的话每次重启都要重走迁移",
              stub_nvs_blob_len(0), sizeof(elder_rmd_table_t)));
    check("升级后 fired_today 由当日位图反推出来（两条都响过）",
          t && t->fired_today == 0x0003,
          det("fired_today = 0x%04x（应为 0x0003 = done|pending）",
              t ? t->fired_today : 0xFFFF));

    /* ---- 升级后的第一个 tick：已完成的绝不能被拉回待办 ---- */
    elder_reminder_tick_once();
    t = tbl();
    check("★升级后首个 tick：已服的那条不得被拉回待办",
          (t->pending_today & 1u) == 0,
          det("pending_today = 0x%04x —— 老人已吃过药却被要求再吃一次",
              t->pending_today));
    check("★升级后首个 tick：已服的不得升级为 ESCALATED",
          saw(0, ELDER_RMD_ESCALATED) == 0 && stub_rgb_escalate_count() == 0,
          det("ESCALATED %d 次、红灯升级 %d 次", saw(0, ELDER_RMD_ESCALATED),
              stub_rgb_escalate_count()));
    check("升级后首个 tick：20:00 那条维持待确认（响过未确认 = 红灯保持）",
          (t->pending_today & 2u) != 0 || (t->done_today & 2u) == 0,
          det("pending_today=0x%04x done_today=0x%04x", t->pending_today,
              t->done_today));

    /* ---- 反向：迁移之后，新的一天提醒仍要正常响 ---- */
    stub_time_set(1772661600L + 86400 + 3600);   /* 次日 07:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);            /* 再上电一次（此时已是 V2） */
    elder_reminder_tick_once();
    check("反向：迁移后跨天 + 重启，次日 07:00 不得提前响",
          saw(0, ELDER_RMD_PENDING) == 0,
          det("次日 07:00 触发 %d 次", saw(0, ELDER_RMD_PENDING)));
    stub_time_advance(3600);                      /* 次日 08:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_tick_once();
    check("★反向：迁移后新的一天 08:00 照常触发（迁移没把提醒锁死）",
          saw(0, ELDER_RMD_PENDING) == 1,
          det("次日 08:00 的 PENDING 次数 = %d（应为 1）", saw(0, ELDER_RMD_PENDING)));
    check("反向：迁移后跨天重置生效（新的一天 done 应清零）",
          (tbl()->done_today & 1u) == 0,
          det("%s", dump_tbl(tbl())));

    /* ---- 坏 CRC 的旧表仍然必须被拒（迁移不得成为绕过校验的后门） ---- */
    stub_nvs_init();
    memcpy(raw, &v1, sizeof(v1));
    raw[sizeof(v1) - 1] ^= 0xFF;      /* 破坏 crc32 */
    stub_nvs_write_raw(0, raw, sizeof(v1));
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);
    check("CRC 损坏的旧表仍被拒（迁移路径没有变成校验后门）",
          tbl()->count == 0,
          det("count=%u —— 坏数据被当成了有效提醒表 = 老人照着乱码时间吃药",
              tbl()->count));
    check("坏旧表被丢弃后不得凭空触发任何提醒",
          saw(0, ELDER_RMD_PENDING) == 0 && stub_rgb_escalate_count() == 0,
          det("PENDING %d 次、红灯升级 %d 次", saw(0, ELDER_RMD_PENDING),
              stub_rgb_escalate_count()));
}

/* T11-5：replace_all 之后 fired_today 位图必须跟着新下标重算 */
static void test_replace_all_recomputes_fired(void)
{
    printf("\n[T11-5] 后端同步（replace_all）后：响过位图必须跟着新下标重算\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    /* 两条后端提醒：早上的药（idx 0）、晚上的药（idx 1） */
    elder_rmd_item_t morning = mk_med(8, 0, "早上的药", 101);
    elder_rmd_item_t evening = mk_med(20, 0, "晚上的药", 102);
    elder_reminder_replace_all((const elder_rmd_item_t[]){morning, evening}, 2);

    /* 08:00 触发 + 确认服药 */
    stub_time_advance(2 * 3600);
    rec_reset();
    elder_reminder_tick_once();
    elder_reminder_confirm_ex(0, 2);
    check("前置：08:00 触发并确认服药",
          (tbl()->done_today & 1u) != 0 && (tbl()->fired_today & 1u) != 0,
          det("%s fired=0x%04x", dump_tbl(tbl()), tbl()->fired_today));
    check("前置：20:00 那条今天还没响（fired_today 只有 bit0）",
          (tbl()->fired_today & 2u) == 0,
          det("fired_today=0x%04x", tbl()->fired_today));

    /* ---- 关键：后端把两条**顺序调换**后同步下来 ----
     * replace_all 按 server_id 匹配旧条目、把运行态搬到新下标上，
     * 于是「早上的药」从 idx0 挪到 idx1。若位图不跟着重算，
     * 落盘那份 fired_today 仍把 bit0 标成响过 —— 而 bit0 现在是
     * 「晚上的药」。后果有二，方向都致命：
     *   a) 早上的药（新 idx1）的「响过」位丢失 -> 重启后二次响铃；
     *   b) 晚上的药（新 idx0）被误标成响过 -> 今天再也不会响。 */
    elder_rmd_item_t swapped[2] = { evening, morning };
    check("后端下发调换顺序的两条提醒",
          elder_reminder_replace_all(swapped, 2) == ESP_OK, NULL);
    const elder_rmd_table_t *t = tbl();
    check("★同步后顺序确实调换了（新 idx0 应是晚上的药）",
          t && t->count == 2 && t->items[0].server_id == 102 &&
                               t->items[1].server_id == 101,
          det("新 idx0 server_id=%u、新 idx1 server_id=%u",
              t && t->count > 0 ? t->items[0].server_id : 0,
              t && t->count > 1 ? t->items[1].server_id : 0));
    check("★同步后 fired_today 位图必须跟着新下标重算（bit1 响过、bit0 没响）",
          (t->fired_today & 2u) != 0 && (t->fired_today & 1u) == 0,
          det("fired_today=0x%04x（应为 0x0002）—— bit 没跟着挪："
              "早上的药会二次响铃，而晚上的药今天再也不会响",
              t ? t->fired_today : 0xFFFF));
    check("★同步后 done_today 同样跟着重算",
          (t->done_today & 2u) != 0 && (t->done_today & 1u) == 0,
          det("done_today=0x%04x（应为 0x0002）", t ? t->done_today : 0xFFFF));

    /* ---- 落盘后重启：两个方向的后果都要钉住 ---- */
    stub_time_advance(3600);         /* 09:00：已过 08:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);
    elder_reminder_tick_once();
    t = tbl();
    check("★同步+重启后：早上的药（idx1）不得二次响铃",
          saw(1, ELDER_RMD_PENDING) == 0,
          det("idx1 的 PENDING 次数 = %d —— 已服用的药被要求再吃一次",
              saw(1, ELDER_RMD_PENDING)));
    check("★同步+重启后：已服记录仍在",
          (t->done_today & 2u) != 0, det("%s", dump_tbl(t)));
    check("★同步+重启后：不得升级告警",
          stub_rgb_escalate_count() == 0,
          det("红灯升级 %d 次", stub_rgb_escalate_count()));

    /* 晚上的药 20:00 必须照常响（没被误标成「响过」而静默漏掉） */
    stub_time_set(1772661600L + 14 * 3600);   /* 20:00 */
    rec_reset();
    stub_fx_reset();
    elder_reminder_tick_once();
    check("★同步+重启后：晚上的药 20:00 仍会正常响",
          saw(0, ELDER_RMD_PENDING) == 1,
          det("idx0 的 PENDING 次数 = %d（应为 1）—— 晚上那顿药被静默漏掉",
              saw(0, ELDER_RMD_PENDING)));
}

/* T11-6：整点前一直关机的设备，开机后必须补响（别修成「永不响」） */
static void test_restart_catchup_still_rings(void)
{
    printf("\n[T11-6] 到点时设备是关机的：开机后必须补响（漏服告警不能丢）\n");
    /* 这条守的是修复的**另一半方向**。
     *
     * 触发条件是 `!fired_today && state==IDLE && now_min>=due`，其中
     * `now_min >= due` 这一条**必须保留**：若把它也一并收紧成
     * 「只在 now_min == due 的那一分钟响」，那么「到点时设备恰好断电/
     * 看门狗复位/OTA 重启」的老人将在当天**永远收不到**这条服药提醒，
     * 子女端也永远收不到漏服告警 —— 从「重复服药」变成「彻底漏服」，
     * 两者都是医疗事故级，只是方向相反。
     *
     * 场景：老人设备在 07:00 到 09:00 之间断电（老人没理由知道/去充电），
     * 09:00 插上电开机。早上的药还没吃，这条提醒今天响都没响过。 */
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    elder_reminder_add(&m);
    check("前置：08:00 提醒已下发", tbl()->count == 1, det("%s", dump_tbl(tbl())));

    /* 设备一直关到 09:00 才上电 —— 全程没有任何 tick */
    stub_time_set(1772661600L + 3 * 3600);
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);       /* 09:00 开机 */
    elder_reminder_tick_once();              /* 上电后第一个 tick */

    check("★到点时关机的设备，开机后必须补响这条服药提醒",
          saw(0, ELDER_RMD_PENDING) == 1,
          det("开机后 PENDING 次数 = %d（应为 1）—— 漏服药且三方都以为没事，"
              "这是比重复服药更隐蔽的事故", saw(0, ELDER_RMD_PENDING)));
    check("补响后闪灯开始（nag_start），家人看得见",
          stub_rgb_nag_count() >= 1,
          det("nag %d 次 —— 补响了却没灯 = 老人不知道该吃药",
              stub_rgb_nag_count()));
    check("补响后被记为待办（子女端能看到这条未完成）",
          (tbl()->pending_today & 1u) != 0,
          det("%s —— 红灯不亮 = 漏服无人知晓", dump_tbl(tbl())));
    check("补响不得被误记成已服用",
          (tbl()->done_today & 1u) == 0,
          det("%s（响不等于吃）", dump_tbl(tbl())));

    /* 2026-10-21 修正升级的计时基准：必须**从响铃算起**，不是从计划时刻算起。
     *
     * 旧判据是 `(now_min - due) >= 15`。本场景（计划 08:00、开机 09:00）
     * 让它一上电就成立，于是 PENDING 刚设上、下一行就被推成 ESCALATED ——
     * 子女端在设备刚说出「该吃药了」的**同一秒**收到「老人漏服」告警。
     * 没人能在一秒内确认服药，这是构造性的假告警（产品侧认定为最坏失败模式）。
     *
     * 「补响不等于豁免升级」这条**意图原样保留**：响铃满 15 分钟仍无人确认，
     * 照样升级。改的只是计时起点，不是要不要升级。 */
    check("★补响的同一 tick 内不得直接判漏服（刚说完就报漏服 = 假告警）",
          saw(0, ELDER_RMD_ESCALATED) == 0,
          det("ESCALATED %d 次 —— 老人还没有任何机会确认就被判漏服",
              saw(0, ELDER_RMD_ESCALATED)));

    stub_time_advance(15 * 60);      /* 响铃后满 15 分钟，仍无人确认 */
    elder_reminder_tick_once();
    check("补响后 15 分钟未确认必须升级为 ESCALATED（补响不等于豁免升级）",
          saw(0, ELDER_RMD_ESCALATED) >= 1,
          det("ESCALATED %d 次 —— 补响后免于升级 = 漏服药静默通过",
              saw(0, ELDER_RMD_ESCALATED)));
    /* 子女端的 MISSED 告警走 elder_sync_on_reminder_state()，**不走红灯**：
     * 需求 8 之后 ESCALATED 不再额外点灯（见 T7 的同款说明）。
     * 这条断言因此改成「升级不得额外点灯」—— 它钉的仍是
     * "升级时到底有没有多亮一次灯"，只是期望值从 1 变成 0。 */
    check("升级不额外点灯（灯由 nag 统一负责，见需求 8）",
          stub_rgb_escalate_count() == 0,
          det("红灯升级 %d 次（额外点灯会让 2 小时后仍亮）",
              stub_rgb_escalate_count()));

    /* 补响只发生一次：再 tick 若干次不得重复响 */
    int pend_after = saw(0, ELDER_RMD_PENDING);
    stub_time_advance(60);
    rec_reset();
    elder_reminder_tick_once();
    stub_time_advance(60);
    elder_reminder_tick_once();
    check("补响之后不得反复重新触发（同一个 tick 只响一次）",
          saw(0, ELDER_RMD_PENDING) == 0,
          det("后续 tick 又触发了 %d 次 —— 反复响铃会持续骚扰老人",
              saw(0, ELDER_RMD_PENDING)));
    (void)pend_after;
}

/* =================================================================== */
/* T12：到点后很久才开机 —— 不补响，也不谎报漏服（2026-10-21 新增）    */
/* =================================================================== */
/* 与 T11-6 是**一对反向断言**，中间那条线是 ELDER_RMD_STALE_MIN（60 分钟）：
 *   T11-6  晚 60 分钟开机 —— 必须补响（别修成「永不响」）；
 *   T12    晚 6 小时开机  —— 必须不响（别修成「六小时后叫人吃降压药」）。
 * 只写一边就会把修复做偏。
 *
 * 为什么「晚 6 小时」必须不响：老人 14:00 才听到「该吃早上的药了」，
 * 补服后与次日晨药间隔过近，低血压风险大于少提醒一次。
 *
 * 为什么「不许升级」：旧实现在这种情况下 now_min>=due 与 now_min-due>=15
 * 在**同一个 tick** 里同时成立，PENDING 刚设上就被推成 ESCALATED，
 * 子女端收到「老人漏服」告警 —— 而事实只是设备没开机。这是假告警，
 * 也是这类产品最坏的失败模式之一。
 *
 * 为什么「要报 SKIPPED」：既不响也不报 = 静默吞掉一次提醒，同样不可接受。
 * SKIPPED 在服务端（EventState.SKIPPED=4）已被排除在「待服药」之外，
 * 子女端看到的是「这次没提醒」，而不是「漏服」。 */
static void test_stale_skip_no_false_missed(void)
{
    printf("\n[T12] 到点后很久才开机：不补响、不升级，判 SKIPPED\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00，可信时钟 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "早上的药", 0);
    elder_reminder_add(&m);
    check("前置：08:00 提醒已下发", tbl()->count == 1, det("%s", dump_tbl(tbl())));

    /* 07:00 断电，14:00 才上电 —— 全程没有任何 tick */
    stub_time_set(1772661600L + 8 * 3600);
    rec_reset();
    stub_fx_reset();
    elder_reminder_init(rec_cb, NULL);       /* 14:00 开机 */
    elder_reminder_tick_once();

    check("★过期 6 小时不得补响（补服会与次日晨药过近）",
          saw(0, ELDER_RMD_PENDING) == 0,
          det("PENDING %d 次 —— 六小时后叫老人吃降压药，比漏一次更危险",
              saw(0, ELDER_RMD_PENDING)));
    check("过期项不得升级为 ESCALATED（不许谎报漏服）",
          saw(0, ELDER_RMD_ESCALATED) == 0,
          det("ESCALATED %d 次 —— 子女端会收到「老人漏服」，而事实只是没开机",
              saw(0, ELDER_RMD_ESCALATED)));
    check("过期项必须判 SKIPPED（让子女端知道「这次没提醒」而非「漏服」）",
          saw(0, ELDER_RMD_SKIPPED) >= 1,
          det("SKIPPED %d 次 —— 既不响也不报 = 静默吞掉一次提醒",
              saw(0, ELDER_RMD_SKIPPED)));
    check("过期项不得点亮红灯待确认",
          (tbl()->pending_today & 1u) == 0, det("%s", dump_tbl(tbl())));
    check("过期项不得被记成已服用",
          (tbl()->done_today & 1u) == 0,
          det("%s（跳过不等于吃了）", dump_tbl(tbl())));

    /* 跳过只判定一次：后续 tick 不得反复上报 */
    stub_time_advance(60);
    rec_reset();
    elder_reminder_tick_once();
    check("过期判定只发生一次（不得每个 tick 都重报）",
          saw(0, ELDER_RMD_SKIPPED) == 0,
          det("后续 tick 又判了 %d 次", saw(0, ELDER_RMD_SKIPPED)));

    /* 跳过是「当天作废」，不是「从此不再提醒」—— 第二天必须照常响。
     * 这条守的是把修复做过头的方向：fired_today 位若不清，老人从此失声。 */
    stub_time_set(1772661600L + 24 * 3600);   /* 次日 06:00 */
    elder_reminder_tick_once();
    stub_time_advance(2 * 3600);              /* 次日 08:00 */
    rec_reset();
    elder_reminder_tick_once();
    check("★跳过只作废当天，次日 08:00 必须照常响",
          saw(0, ELDER_RMD_PENDING) >= 1,
          det("PENDING %d 次 —— 一次跳过变成永久失声，是更坏的事故",
              saw(0, ELDER_RMD_PENDING)));
}

/* ---- T13：语音确认（服务端 MCP 工具 → elder_confirm_pending_medicine）----
 *
 * 这条链路在 2026-10-21 之前是**断的**：elder_reminder_confirm() 只有一个
 * 调用点，在按键回调里；按键迁到 GPIO11 后原型期没接线，离线命令词也已
 * 随上游回归删除 ⇒ 提醒响完没有任何确认手段 ⇒ 15 分钟后升级 ESCALATED ⇒
 * 回执 ELDER_EV_MISSED ⇒ 子女端每条提醒都收到「老人漏服」的假告警。
 *
 * 宿主测试能覆盖的是**状态机这一半**：确认必须落 DONE、此后不得再升级、
 * 确认方式必须是 VOICE 而不是 BUTTON（否则子女端分不清「老人自己按的」
 * 还是「跟小智说的」）。MCP 传输那半边（tools/call → mcp_server 的 lambda）
 * 在 xiaozhi 的 C++ 里，宿主环境编不了，由真机验收清单覆盖。
 */
static void test_voice_confirm_no_false_missed(void)
{
    printf("\n[T13] 语音确认：落 DONE、记 VOICE 来源、此后不得再报漏服\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00，可信时钟 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "降压药", 0);
    elder_reminder_add(&m);
    stub_time_advance(2 * 3600);     /* 08:00 到点 */
    rec_reset();
    elder_reminder_tick_once();
    check("前置：08:00 已响（PENDING）",
          saw(0, ELDER_RMD_PENDING) >= 1,
          det("PENDING %d 次", saw(0, ELDER_RMD_PENDING)));

    /* 老人对小智说「我吃过了」→ 服务端 tools/call → 固件走这里 */
    check("语音确认返回 OK",
          elder_reminder_confirm_ex(0, ELDER_CFM_VOICE) == ESP_OK, NULL);
    check("★确认方式必须是 VOICE（不能记成 BUTTON）",
          elder_reminder_last_confirm_method() == ELDER_CFM_VOICE,
          det("记成 %u —— 子女端会误判成「老人自己按的」",
              (unsigned)elder_reminder_last_confirm_method()));
    check("确认后必须落 DONE 且清掉红灯待确认",
          saw(0, ELDER_RMD_DONE) >= 1 && (tbl()->pending_today & 1u) == 0,
          det("%s", dump_tbl(tbl())));
    check("确认后必须记成今天已服用",
          (tbl()->done_today & 1u) != 0, det("%s", dump_tbl(tbl())));

    /* 最关键的一条：确认过之后，15 分钟的升级扫描不得再判漏服 */
    stub_time_advance(20 * 60);
    rec_reset();
    elder_reminder_tick_once();
    check("★确认后再过 20 分钟不得升级 ESCALATED",
          saw(0, ELDER_RMD_ESCALATED) == 0,
          det("ESCALATED %d 次 —— 老人明明确认过，家属仍收到「漏服」",
              saw(0, ELDER_RMD_ESCALATED)));

    /* 固件侧 elder_confirm_pending_medicine() 用 FirstUndoneMedicine() 守卫，
     * 正常不会传越界 id；这里钉住底层契约：越界 id 必须被拒，且不得把
     * 任何一条提醒的状态搅乱。 */
    check("确认越界 id 必须返回 NOT_FOUND（不得静默成功）",
          elder_reminder_confirm_ex(5, ELDER_CFM_VOICE) == ESP_ERR_NOT_FOUND,
          NULL);
    check("越界确认不得污染已有提醒的完成位",
          (tbl()->done_today & 1u) != 0, det("%s", dump_tbl(tbl())));
}

/* ---- T14：晚开机（但没到过期阈值）不得一响就判漏服 ----
 *
 * 场景：08:00 的药，设备 08:50 才开机（路由器断了 50 分钟、或老人早上
 * 出门没带设备）。设备这时**要补响** —— 这是 T11-6 守的方向。
 * 但如果升级计时拿**计划时刻 due** 当基准，`now_min - due = 50 >= 15`
 * 会在**响铃的同一个 tick** 里直接判 ESCALATED：老人刚听到「该吃药了」，
 * 家属手机上已经弹出「漏服」，老人连 15 分钟的确认时间都没有。
 *
 * 修复把基准换成**响铃时刻** pending_since_us。本测试专门钉它：
 * 把 reminder.c 的 overdue 判据改回 `(now_min - due) >= 15`，这里必须变红。
 *
 * 为什么 T11-6 和 T12 都抓不到这个回归（当初正是因此漏掉）：
 *   T11-6 在 due 整点触发，now_min-due == 0，新旧判据都不升级；
 *   T12  走的是 SKIPPED 分支，根本到不了升级逻辑。
 */
static void test_late_boot_no_instant_escalate(void)
{
    printf("\n[T14] 晚开机 50 分钟：要补响，但不得一响就判漏服\n");
    stub_nvs_init();
    stub_time_set(1772661600L);      /* 06:00 */
    rec_reset();
    elder_reminder_init(rec_cb, NULL);

    elder_rmd_item_t m = mk_med(8, 0, "降压药", 0);
    elder_reminder_add(&m);

    stub_time_set(1772661600L + 2 * 3600 + 50 * 60);   /* 08:50 开机 */
    rec_reset();
    elder_reminder_tick_once();
    check("晚开机 50 分钟仍要补响（否则老人彻底漏服）",
          saw(0, ELDER_RMD_PENDING) >= 1,
          det("PENDING %d 次 —— 修成「不补响」是另一个方向的漏服事故",
              saw(0, ELDER_RMD_PENDING)));
    check("★响铃同一个 tick 内不得判 ESCALATED",
          saw(0, ELDER_RMD_ESCALATED) == 0,
          det("ESCALATED %d 次 —— 老人刚听到提醒，家属已收到「漏服」",
              saw(0, ELDER_RMD_ESCALATED)));

    /* 15 分钟窗口要从**响铃**起算，不是从 08:00 起算 */
    stub_time_advance(14 * 60);
    rec_reset();
    elder_reminder_tick_once();
    check("响铃后 14 分钟仍未升级（窗口没走完）",
          saw(0, ELDER_RMD_ESCALATED) == 0,
          det("ESCALATED %d 次 —— 提前升级等于缩短老人确认时间",
              saw(0, ELDER_RMD_ESCALATED)));

    stub_time_advance(2 * 60);
    rec_reset();
    elder_reminder_tick_once();
    check("★响铃后满 15 分钟必须升级（该报警时不能哑）",
          saw(0, ELDER_RMD_ESCALATED) >= 1,
          det("ESCALATED %d 次 —— 一直不升级则「人要介入」的信号彻底失效",
              saw(0, ELDER_RMD_ESCALATED)));
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
    test_untrusted_clock_guard();
    test_local_day_boundary();
    test_next_day_fires_again();
    test_escalate_after_15min();
    test_confirm_window_timeout();
    test_nvs_crc_rejects_corrupt();
    test_first_tick_day_guard();
    test_restart_no_refire();
    test_restart_before_due();
    test_restart_across_day();
    test_legacy_blob_upgrade();
    test_replace_all_recomputes_fired();
    test_restart_catchup_still_rings();
    test_stale_skip_no_false_missed();
    test_voice_confirm_no_false_missed();
    test_late_boot_no_instant_escalate();

    printf("\n=== %d 通过 / %d 失败 ===\n", g_pass, g_fail);
    return g_fail ? 1 : 0;
}