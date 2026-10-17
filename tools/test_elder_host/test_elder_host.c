/* test_elder_host.c —— 断网提醒链路的宿主单元测试。
 *
 * 覆盖的三条断言都对应医疗事故级的失效（详见 Makefile 顶部说明）：
 *   T1  提醒时间存本地（NVS）—— 断网重启后提醒还在
 *   T2  replace_all 不抹掉本地自建 —— 一次心跳同步不该让老人的服药提醒消失
 *   T3  断网新建 + 断网确认 + 确认方式正确回传
 *
 * 全程**没有任何网络调用**：这正是要验证的场景。
 */
#include "stubs.h"

#include "elder_reminder.h"
#include "elder_cmd.h"
#include "elder_cmd_words.h"
#include "elder_ui.h"

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
          t ? "实际 4 条" : NULL);
    check("后端条目在", t && find_idx(t, 9, 30, "降压药") >= 0, NULL);
    check("本地「早上的药」未被抹掉", t && find_idx(t, 8, 0, "早上的药") >= 0, NULL);
    check("本地「晚上的药」未被抹掉", t && find_idx(t, 20, 0, "晚上的药") >= 0, NULL);

    /* 最狠的一遍：服务端下发空表（孩子把 App 里的提醒全删了）。
     * 老人在断网时建的提醒此时只剩这一份拷贝，绝不能跟着消失。 */
    check("后端下发 0 条时也不丢本地",
          elder_reminder_replace_all(srv, 0) == ESP_OK, NULL);
    t = tbl();
    check("空表下发后本地 2 条仍在", t && t->count == 2,
          t ? "count=2" : "count=0 —— 本地提醒被抹掉了");
    check("两条内容都对",
          t && find_idx(t, 8, 0, "早上的药") >= 0 && find_idx(t, 20, 0, "晚上的药") >= 0,
          NULL);

    /* 容量溢出：本地必须优先活下来（服务端数据可再拉，本地丢了找不回） */
    elder_rmd_item_t many[16];
    for (int i = 0; i < 16; i++) {
        many[i] = mk_med((uint8_t)(i % 24), 0, "服务端提醒", (uint32_t)(200 + i));
    }
    elder_reminder_replace_all(many, 16);
    t = tbl();
    check("溢出时本地两条仍活", t && find_idx(t, 8, 0, "早上的药") >= 0
                             && find_idx(t, 20, 0, "晚上的药") >= 0, NULL);
    check("表不超过 16 条上限", t && t->count <= ELDER_REMINDER_MAX,
          t ? "count<=16" : NULL);
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
          t && find_idx(t, 8, 0, "早上的药") >= 0, NULL);
    check("新建的是 server_id==0 的本地提醒",
          t && t->items[find_idx(t, 8, 0, "早上的药")].server_id == 0, NULL);

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
    check("已建 20:00 晚上的药", t && find_idx(t, 20, 0, "晚上的药") >= 0, NULL);

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

    printf("\n=== %d 通过 / %d 失败 ===\n", g_pass, g_fail);
    return g_fail ? 1 : 0;
}