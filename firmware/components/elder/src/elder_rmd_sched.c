/*
 * 提醒排期判定 —— 纯函数实现，只依赖 stdint/stdbool。
 * 单测见 tools/test_rmd_sched.c（CI 的 host-test job 用 gcc 直接编）。
 */

#include "elder_rmd_sched.h"

/* 告警钩子：本文件不许 include esp_log.h（否则主机测试编不过），
 * 由 reminder.c 在 init 时注入 ESP_LOGW。未注入时静默。
 * 初值写 0 而不是 NULL：NULL 要 <stddef.h>，而本文件刻意只依赖
 * stdint/stdbool（函数指针的"空指针常量"用 0 是标准做法）。 */
static elder_rmd_warn_fn s_warn = 0;

/* 「只告警一次」的标志。放在文件作用域而不是每次调用局部，是因为
 * elder_rmd_due_on 会被 tick 每秒调 16 次，同一个配置错误刷屏等于没告警。 */
static bool s_warned_once_no_date = false;
static bool s_warned_unknown_kind = false;

void elder_rmd_set_warn_hook(elder_rmd_warn_fn fn)
{
    s_warn = fn;
}

static void warn_once(bool *flag, const char *msg)
{
    if (*flag) return;
    *flag = true;
    if (s_warn) s_warn(msg);
}

int elder_rmd_ymd_of(int year, int mon, int mday)
{
    if (year < 1900 || year > 2999) return 0;
    if (mon < 1 || mon > 12) return 0;
    if (mday < 1 || mday > 31) return 0;
    return year * 10000 + mon * 100 + mday;
}

int elder_rmd_ordinal_of(int year, int mon, int mday)
{
    /* Howard Hinnant, "chrono-Compatible Low-Level Date Algorithms":
     * days_from_civil。1970-01-01 → 0，之前为负。
     * 纯整数运算，不查表、不看时区、不依赖 time.h —— 主机与设备结果一致。 */
    int y = year;
    unsigned m = (unsigned)mon;
    unsigned d = (unsigned)mday;
    y -= (m <= 2);
    int era = (y >= 0 ? y : y - 399) / 400;
    unsigned yoe = (unsigned)(y - era * 400);                          /* [0, 399] */
    unsigned doy = (153u * (m + (m > 2 ? (unsigned)-3 : 9u)) + 2u) / 5u + d - 1u; /* [0, 365] */
    unsigned doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;           /* [0, 146096] */
    return (int)(era * 146097 + (int)doe - 719468);
}

bool elder_rmd_due_on(const elder_rmd_when_t *w, int today_ymd,
                      int today_ordinal, int today_wday)
{
    /* 防御：没有排期信息时按"每天"处理。宁可多响也不能静默不响 ——
     * 漏掉一条服药提醒的后果远大于多响一次。 */
    if (!w) return true;

    /* ---- 1. 生效区间是外层约束，任何 kind 都先过这一关 ---- */
    if (w->start_ymd != 0 && today_ymd < w->start_ymd) return false;
    if (w->end_ymd   != 0 && today_ymd > w->end_ymd)   return false;

    /* ---- 2. 按 kind 分派 ---- */
    switch (w->kind) {
    case ELDER_RMD_KIND_ONCE:
        if (w->once_ymd == 0) {
            /* 一次性规则却没给日期：这是服务端/解析出了问题。
             * 不猜"那就今天吧"——猜错会让它今天响一次然后永远不响，
             * 比一直不响更难排查。判为不到期并告警一次。 */
            warn_once(&s_warned_once_no_date,
                      "一次性提醒缺 once_date，判为不到期（请检查服务端下发）");
            return false;
        }
        return (uint32_t)today_ymd == w->once_ymd;

    case ELDER_RMD_KIND_DAILY:
        return true;

    case ELDER_RMD_KIND_WEEKLY:
        if (today_wday < 0 || today_wday > 6) return false;
        return (w->repeat_mask & (uint8_t)(1u << today_wday)) != 0;

    case ELDER_RMD_KIND_MONTHLY: {
        int mday = today_ymd % 100;   /* YYYYMMDD 的后两位就是"日" */
        if (mday < 1 || mday > 31) return false;
        return (w->month_mask & (1u << (mday - 1))) != 0;
    }

    case ELDER_RMD_KIND_INTERVAL_DAYS: {
        if (w->interval_days <= 1) return true;   /* N<=1 等价每天 */
        int n = (int)w->interval_days;
        int d = today_ordinal - (int)w->anchor_ordinal;
        /* ⚠️ C 的 % 对负数返回负余数。今天早于 anchor 时 d<0，
         * 不修正的话 (d % n) == 0 只在 d 是 n 的负整数倍时成立，
         * 相位整体错位。先归一到 [0, n)。 */
        int r = d % n;
        if (r < 0) r += n;
        return r == 0;
    }

    default:
        /* 未知 kind：**按 DAILY 处理**，保持"以前能响的现在还响"。
         * 静默不响是最坏的降级方向（老人收不到服药提醒），
         * 所以这里宁可多响，并把配置问题暴露在日志里。 */
        warn_once(&s_warned_unknown_kind,
                  "未知 schedule_kind，按每天处理（请检查服务端下发）");
        return true;
    }
}
