/*
 * elder_rmd_sched 主机单测（2026-10-23）。
 *
 * 为什么要有它：这次修的是「服务端设成一次性的提醒，设备每天都响」——
 * 根因是设备端完全忽略 schedule_kind，只按 repeat_mask 判到期，而
 * sync.c 在"没有 weekdays"时把 mask 强制成 0x7F（每天）。于是
 * ONCE / MONTHLY / INTERVAL_DAYS 三种规则**全部静默退化成每天**。
 *
 * 这类 bug 的特点是「不崩、不报错、只是行为错」，靠真机肉眼看很难发现
 * （要等到第二天、第三天才看出"它怎么又响了"）。所以判定逻辑被拆成
 * 零依赖的纯函数，在 CI 上秒级跑完。
 *
 * 编译运行（CI 的 host-test job 用的就是这条）：
 *   gcc -std=c11 -Wall -Werror -Ifirmware/components/elder/include \
 *       -o /tmp/t tools/test_rmd_sched.c \
 *              firmware/components/elder/src/elder_rmd_sched.c
 *   /tmp/t
 *
 * ⚠️ 本文件**不许** include 任何 ESP-IDF 头文件。
 */

#include <stdio.h>
#include <string.h>

#include "elder_rmd_sched.h"

static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, msg)                                                      \
    do {                                                                      \
        if (cond) {                                                           \
            g_pass++;                                                         \
        } else {                                                              \
            g_fail++;                                                         \
            printf("  FAIL  %s   (%s:%d)\n", (msg), __FILE__, __LINE__);      \
        }                                                                     \
    } while (0)

/* ---- 测试用的小工具（都是被测函数之上的薄封装，不重复实现判定逻辑） ---- */

static int wday_of(int y, int m, int d)
{
    /* 1970-01-01 是周四（tm_wday=4）。 */
    int w = (elder_rmd_ordinal_of(y, m, d) + 4) % 7;
    if (w < 0) w += 7;
    return w;
}

static int ord_of_ymd(int ymd)
{
    return elder_rmd_ordinal_of(ymd / 10000, (ymd / 100) % 100, ymd % 100);
}

static int wd_of_ymd(int ymd)
{
    return wday_of(ymd / 10000, (ymd / 100) % 100, ymd % 100);
}

/* 按 YYYYMMDD 调用被测函数（ordinal / wday 都从 ymd 推，避免手抄出错） */
static bool due_on(const elder_rmd_when_t *w, int ymd)
{
    return elder_rmd_due_on(w, ymd, ord_of_ymd(ymd), wd_of_ymd(ymd));
}

int main(void)
{
    elder_rmd_when_t w;

    /* ================= 0. 日期工具 ================= */
    printf("[0] ymd / ordinal 工具\n");
    CHECK(elder_rmd_ymd_of(2026, 10, 23) == 20261023, "ymd_of 正常值");
    CHECK(elder_rmd_ymd_of(2026, 13, 1) == 0, "ymd_of 月份越界 -> 0");
    CHECK(elder_rmd_ymd_of(2026, 0, 1) == 0, "ymd_of 月份 0 -> 0");
    CHECK(elder_rmd_ymd_of(2026, 1, 32) == 0, "ymd_of 日越界 -> 0");

    CHECK(elder_rmd_ordinal_of(1970, 1, 1) == 0, "1970-01-01 序号为 0");
    CHECK(elder_rmd_ordinal_of(1970, 1, 2) == 1, "1970-01-02 序号为 1");
    /* 闰年连续性：2028-02-28 -> 02-29 -> 03-01 必须各差 1 天。
     * 这条专门钉死"把每年都当 366 天"的实现 —— 那种实现在 2 月底会跳。 */
    {
        int a = elder_rmd_ordinal_of(2028, 2, 28);
        int b = elder_rmd_ordinal_of(2028, 2, 29);
        int c = elder_rmd_ordinal_of(2028, 3, 1);
        CHECK(b - a == 1, "2028-02-28 -> 02-29 相差 1 天");
        CHECK(c - b == 1, "2028-02-29 -> 03-01 相差 1 天");
    }
    CHECK(elder_rmd_ordinal_of(2027, 1, 1) - elder_rmd_ordinal_of(2026, 1, 1) == 365,
          "2026 平年跨年差 365 天");
    CHECK(elder_rmd_ordinal_of(2029, 1, 1) - elder_rmd_ordinal_of(2028, 1, 1) == 366,
          "2028 闰年跨年差 366 天");

    /* 星期换算自检：2024-01-01 是周一（tm_wday=1） */
    CHECK(wday_of(2024, 1, 1) == 1, "wday_of(2024-01-01) == 1(周一)");

    /* ================= 1. ONCE ================= */
    printf("[1] ONCE 一次性\n");
    memset(&w, 0, sizeof(w));
    w.kind = ELDER_RMD_KIND_ONCE;
    w.once_ymd = 20261023;
    CHECK(!due_on(&w, 20261022), "ONCE 早一天 -> false");
    CHECK(due_on(&w, 20261023), "ONCE 当天 -> true");
    CHECK(!due_on(&w, 20261024), "ONCE 晚一天 -> false");
    CHECK(!due_on(&w, 20271023), "ONCE 次年同一天 -> false（不能变成每年）");

    /* 缺日期：**按每天处理**，不是"不到期"。
     * 历史上 APP 编辑页的 kindOptions 与 ScheduleKind 枚举错位一格，
     * 用户选"每天"会存成 kind=0(ONCE) 且 once_date 为空 —— 存量里有一批
     * 这样的规则。判成"不到期"等于让它们从此永不触发：老人收不到提醒，
     * 而屏幕一切正常、没人会发现。宁可多响。 */
    w.once_ymd = 0;
    CHECK(due_on(&w, 20261023), "ONCE 缺 once_date -> true（按每天兜底，绝不静默不响）");
    CHECK(due_on(&w, 20280229), "ONCE 缺 once_date 换一天仍 true");

    /* ================= 2. DAILY ================= */
    printf("[2] DAILY 每天\n");
    memset(&w, 0, sizeof(w));
    w.kind = ELDER_RMD_KIND_DAILY;
    CHECK(due_on(&w, 20261023), "DAILY 任意日 true");
    CHECK(due_on(&w, 20280229), "DAILY 闰日 true");
    CHECK(due_on(&w, 20270101), "DAILY 元旦 true");
    /* DAILY 不看 mask：mask=0 也照样每天 */
    w.repeat_mask = 0;
    CHECK(due_on(&w, 20261023), "DAILY mask=0 仍 true");

    /* ================= 3. WEEKLY ================= */
    printf("[3] WEEKLY 每周\n");
    memset(&w, 0, sizeof(w));
    w.kind = ELDER_RMD_KIND_WEEKLY;
    /* 2026-10-26 是周一（tm_wday=1） */
    CHECK(wd_of_ymd(20261026) == 1, "2026-10-26 是周一");
    w.repeat_mask = (uint8_t)(1u << 1);          /* 周一 */
    CHECK(due_on(&w, 20261026), "WEEKLY 命中周一 -> true");
    CHECK(!due_on(&w, 20261027), "WEEKLY 周二 -> false");
    /* bit0 = 周日：2026-10-25 是周日 */
    CHECK(wd_of_ymd(20261025) == 0, "2026-10-25 是周日");
    w.repeat_mask = (uint8_t)(1u << 0);
    CHECK(due_on(&w, 20261025), "WEEKLY bit0 命中周日 -> true");
    CHECK(!due_on(&w, 20261026), "WEEKLY bit0 不命中周一 -> false");
    /* mask=0（服务端没给 weekdays）：WEEKLY 语义下就是哪天都不响，
     * 绝不能像旧代码那样被强行改成"每天" */
    w.repeat_mask = 0;
    CHECK(!due_on(&w, 20261026), "WEEKLY mask=0 -> 哪天都不响");

    /* ================= 4. MONTHLY ================= */
    printf("[4] MONTHLY 每月\n");
    memset(&w, 0, sizeof(w));
    w.kind = ELDER_RMD_KIND_MONTHLY;
    w.month_mask = (1u << (1 - 1)) | (1u << (31 - 1));   /* 1 号 + 31 号 */
    CHECK(due_on(&w, 20260101), "MONTHLY 1 号 -> true");
    CHECK(due_on(&w, 20260131), "MONTHLY 31 号 -> true");
    CHECK(!due_on(&w, 20260115), "MONTHLY 15 号未列出 -> false");
    CHECK(!due_on(&w, 20260230), "MONTHLY 不存在的 2/30 不崩且 false");
    CHECK(due_on(&w, 20261231), "MONTHLY 12/31 命中（跨月仍按日号）");
    /* 空 mask（服务端没给 month_days）：不响 */
    w.month_mask = 0;
    CHECK(!due_on(&w, 20260101), "MONTHLY 空 mask -> false");

    /* ================= 5. INTERVAL_DAYS ================= */
    printf("[5] INTERVAL_DAYS 隔 N 天\n");
    memset(&w, 0, sizeof(w));
    w.kind = ELDER_RMD_KIND_INTERVAL_DAYS;
    w.interval_days = 1;
    CHECK(due_on(&w, 20261023), "INTERVAL N=1 每天都 true");
    CHECK(due_on(&w, 20261024), "INTERVAL N=1 次日也 true");
    w.interval_days = 0;
    CHECK(due_on(&w, 20261023), "INTERVAL N=0（异常值）按每天 true");

    w.interval_days = 3;
    w.anchor_ordinal = (int32_t)elder_rmd_ordinal_of(2026, 12, 30);
    CHECK(due_on(&w, 20261230), "INTERVAL N=3 anchor 当天 -> true");
    CHECK(!due_on(&w, 20261231), "INTERVAL N=3 +1 天 -> false");
    CHECK(!due_on(&w, 20270101), "INTERVAL N=3 +2 天 -> false");
    /* ⚠️ 核心用例：跨年相位不漂移。
     * anchor=2026-12-30，真实相差 3 天 ⇒ 2027-01-02 必须命中。
     * 用 reminder.c 里那个 `tm_year*366 + tm_yday` 的假日号会算出别的余数，
     * 于是"隔三天"的提醒在元旦后整体错位 —— 这条断言就是那个坑的钉子。 */
    CHECK(due_on(&w, 20270102), "INTERVAL N=3 跨年 +3 天 -> true（钉死 tm_year*366 假日号）");
    CHECK(due_on(&w, 20270105), "INTERVAL N=3 跨年 +6 天 -> true");
    /* 今天早于 anchor（负差值）：C 的 % 返回负余数，必须归一化 */
    CHECK(due_on(&w, 20261227), "INTERVAL N=3 anchor 之前 3 天 -> true（负数取模要归一）");
    CHECK(!due_on(&w, 20261228), "INTERVAL N=3 anchor 之前 2 天 -> false");

    /* ================= 6. start_date / end_date ================= */
    printf("[6] 生效区间\n");
    memset(&w, 0, sizeof(w));
    w.kind = ELDER_RMD_KIND_DAILY;
    w.start_ymd = 20261020;
    w.end_ymd   = 20261025;
    CHECK(!due_on(&w, 20261019), "start 前一天 -> false");
    CHECK(due_on(&w, 20261020), "== start -> true");
    CHECK(due_on(&w, 20261025), "== end -> true");
    CHECK(!due_on(&w, 20261026), "end 后一天 -> false");
    /* 只有 start */
    w.end_ymd = 0;
    CHECK(due_on(&w, 20301020), "只设 start，远期仍 true");
    CHECK(!due_on(&w, 20261019), "只设 start，之前 false");
    /* 只有 end */
    w.start_ymd = 0;
    w.end_ymd = 20261025;
    CHECK(due_on(&w, 19990101), "只设 end，很早也 true");
    CHECK(!due_on(&w, 20261026), "只设 end，之后 false");
    /* 区间与 kind 叠加：ONCE 落在区间外照样 false */
    memset(&w, 0, sizeof(w));
    w.kind = ELDER_RMD_KIND_ONCE;
    w.once_ymd = 20261023;
    w.start_ymd = 20261024;
    CHECK(!due_on(&w, 20261023), "ONCE 早于 start -> false");

    /* ================= 7. 未知 kind ================= */
    printf("[7] 未知 kind\n");
    memset(&w, 0, sizeof(w));
    w.kind = 9;
    CHECK(due_on(&w, 20261023), "未知 kind=9 按每天 -> true（宁可多响也不能静默不响）");
    w.kind = 255;
    CHECK(due_on(&w, 20261023), "未知 kind=255 按每天 -> true");

    /* ================= 8. NULL 防御 ================= */
    printf("[8] NULL 防御\n");
    CHECK(elder_rmd_due_on(NULL, 20261023, ord_of_ymd(20261023), wd_of_ymd(20261023)),
          "w == NULL -> true（按每天，绝不静默不响）");

    /* ================= 汇总 ================= */
    printf("\n----------------------------------------\n");
    printf("通过 %d，失败 %d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
