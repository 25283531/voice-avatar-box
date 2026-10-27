#ifndef ELDER_RMD_SCHED_H
#define ELDER_RMD_SCHED_H

/*
 * 提醒排期判定 —— **纯函数，零依赖**。
 *
 * 为什么单独拆一个文件（2026-10-23）：
 *   设备端一直只按 repeat_mask 判到期，完全忽略 schedule_kind，而 sync.c
 *   在"没有 weekdays"时把 mask 强制写成 0x7F（每天）。净效果是后端
 *   ScheduleKind 的五种里 ONCE / MONTHLY / INTERVAL_DAYS **全部静默退化成
 *   "每天"**：用户设一次性的喝水提醒，设备天天响。要修就得把判定逻辑从
 *   esp_err / freertos / gpio 里摘出来，才能在主机上用 gcc 直接跑单测
 *   （见 tools/test_rmd_sched.c 与 CI 的 host-test job）。
 *
 * ⚠️ 本文件及其 .c **只许 include <stdint.h> / <stdbool.h>**。
 *    一旦引入 esp_log.h，主机测试就编不过，"能单测"这个唯一价值就没了。
 *    需要打日志的地方走 elder_rmd_set_warn_hook() 注入。
 */

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 与后端 ScheduleKind 数值一一对应（server/app/models.py 的 ScheduleKind） */
#define ELDER_RMD_KIND_ONCE          0
#define ELDER_RMD_KIND_DAILY         1
#define ELDER_RMD_KIND_WEEKLY        2
#define ELDER_RMD_KIND_MONTHLY       3
#define ELDER_RMD_KIND_INTERVAL_DAYS 4

/**
 * @brief 一条提醒的「什么时候该响」。
 *
 * 与 elder_rmd_table_t.when[] 同下标存放（见 elder_reminder.h 的 V3 说明）。
 * 老表（V1/V2）升级上来时整个数组清零 = kind=0(ONCE) 且 once_ymd=0？
 * **不是** —— 迁移代码会显式把 kind 置成 DAILY，见 reminder.c 的 migrate。
 * 这里的零值语义只用于"设备本地新建且没填排期"的兜底，由调用方负责。
 */
typedef struct {
    uint8_t  kind;            /* 0=ONCE 1=DAILY 2=WEEKLY 3=MONTHLY 4=INTERVAL_DAYS */
    uint8_t  repeat_mask;     /* WEEKLY：bit0=周日 .. bit6=周六（与 tm_wday 一致） */
    uint32_t once_ymd;        /* ONCE 的 YYYYMMDD；0 = 未指定（永不到期 + 告警一次） */
    uint32_t month_mask;      /* MONTHLY：bit(d-1) 表示每月 d 号 */
    uint16_t interval_days;   /* INTERVAL_DAYS 的 N（<=1 视为每天） */
    int32_t  anchor_ordinal;  /* INTERVAL_DAYS 的基准日序号（elder_rmd_ordinal_of 的返回值） */
    int32_t  start_ymd;       /* 生效起始 YYYYMMDD；0 = 不限 */
    int32_t  end_ymd;         /* 生效截止 YYYYMMDD；0 = 不限 */
} elder_rmd_when_t;

/** 告警回调：上层（reminder.c）注入 ESP_LOGW，本文件不依赖 esp_log。 */
typedef void (*elder_rmd_warn_fn)(const char *msg);
void elder_rmd_set_warn_hook(elder_rmd_warn_fn fn);

/**
 * @brief 今天该不该响。
 *
 * @param w              排期；NULL 视为 DAILY（防御，正常不会传 NULL）
 * @param today_ymd      今天的 YYYYMMDD
 * @param today_ordinal  今天的**真实**日序号（elder_rmd_ordinal_of）
 * @param today_wday     今天星期几，0=周日 .. 6=周六（= struct tm 的 tm_wday）
 *
 * 判定顺序是**先区间、后重复**：start/end 是外层约束，任何 kind 都要过。
 */
bool elder_rmd_due_on(const elder_rmd_when_t *w, int today_ymd,
                      int today_ordinal, int today_wday);

/** @brief YYYYMMDD。参数越界时返回 0（调用方按"无日期"处理）。 */
int elder_rmd_ymd_of(int year, int mon, int mday);

/**
 * @brief days-from-civil：1970-01-01 记为 0 的**真实**天数。
 *
 * ⚠️ 必须用这个，**不能**复用 reminder.c 里那个 `tm_year*366 + tm_yday`：
 * 后者把每年都当 366 天，拿它做 `% N` 会跨年漂移 —— 比如 anchor 在
 * 2026-12-30、N=3，真实相差 3 天（2027-01-02）应该命中，用假序号会算出
 * 别余数，于是"隔三天"的提醒在跨年后整体错位一天。
 * 算法是 Howard Hinnant 的 days_from_civil（纯整数、无查表、无时区）。
 */
int elder_rmd_ordinal_of(int year, int mon, int mday);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_RMD_SCHED_H */
