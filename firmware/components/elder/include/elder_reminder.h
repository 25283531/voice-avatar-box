#ifndef ELDER_REMINDER_H
#define ELDER_REMINDER_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <time.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 离线日程 / 服药提醒引擎（需求 8 / 9 / 11）
 *
 * 设计要点：
 *  - 提醒完全离线可用：日程表存 NVS，时间来自 RTC（离线必须外挂 DS3231）
 *  - 每条提醒有独立状态机：IDLE → PENDING → (CONFIRMED | ESCALATED)
 *  - 同一天未完成则红灯长亮；确认完成转绿灯
 *  - 断网不影响提醒本身，只影响「向子女端推送」——推送会入队，联网后补发
 */

#define ELDER_REMINDER_MAX      16
#define ELDER_REMINDER_LABEL    31

typedef enum {
    ELDER_RMD_IDLE = 0,      /* 未到时间 */
    ELDER_RMD_PENDING,       /* 已触发，等待确认 */
    ELDER_RMD_CONFIRM_WIN,   /* 已长按，等待 30s 内短按 */
    ELDER_RMD_DONE,          /* 已确认完成 */
    ELDER_RMD_ESCALATED,     /* 超时升级（持续告警） */
    /* 过期作废：到点时设备已离线/断电太久（> ELDER_RMD_STALE_MIN 分钟），
     * 不再补响。语义对齐服务端 EventState.SKIPPED(4)——「不算待服药，
     * 也不算漏服」。加这个状态是为了堵住「校时后第一个 tick 就谎报漏服」。 */
    ELDER_RMD_SKIPPED
} elder_rmd_state_t;

typedef enum {
    ELDER_RMD_TYPE_SCHEDULE = 0,  /* 日程 */
    ELDER_RMD_TYPE_MEDICINE       /* 服药 */
} elder_rmd_type_t;

typedef struct {
    uint8_t  id;
    uint8_t  hour;
    uint8_t  minute;
    uint8_t  type;                       /* elder_rmd_type_t：0=日程 1=服药 */
    uint8_t  repeat_mask;                /* bit0=周日 ... bit6=周六（注意与后端差异，见 sync.c） */
    bool     enabled;
    char     label[ELDER_REMINDER_LABEL + 1];

    /* ---- 与后端对齐的扩展字段（见 elder_sync.c 的映射说明）---- */
    uint32_t server_id;                  /* 后端 Reminder.id；0 = 设备本地自建 */
    uint8_t  rtype;                      /* 后端 ReminderType：0=服药 1=日程 2=喝水 3=运动 4=自定义 */
    uint8_t  schedule_kind;              /* 后端 ScheduleKind：0=一次 1=每天 2=每周 3=每月 4=隔N天 */
    int64_t  fired_due_at;               /* 本次触发的到期时刻（unix 秒），回执用它定位实例 */
} elder_rmd_item_t;

/* NVS blob 的结构版本号。
 *
 * 表结构一旦变化，设备上可能还存着旧固件写下的 blob，尺寸与 CRC 覆盖范围
 * 都不再相同。版本号 + 尺寸共同决定用哪套布局解读它。
 *   V1：没有 fired_today / fmt_ver（历史布局，见下方 elder_rmd_table_v1_t，
 *       升级时按老尺寸读出并迁移）
 *   V2：新增 fired_today 位图 + 本版本号
 *
 * ⚠️ 加字段必须**只追加在结构体末尾**。V1 之所以能零成本迁移，
 *    正是因为 V1 是 V2 的逐字节前缀（下方有编译期断言钉死它）——
 *    一旦有人把新字段插到中间，老 blob 的尺寸和 CRC 就再也对不上，
 *    load() 会判为「无数据」并清空整张表，老用户升级后服药提醒全部消失。 */
#define ELDER_RMD_FMT_V1  1u
#define ELDER_RMD_FMT_V2  2u

typedef struct {
    elder_rmd_item_t items[ELDER_REMINDER_MAX];
    uint8_t          count;
    /* 今日完成状态：bit i = 1 表示 items[i] 今日已完成 */
    uint16_t         done_today;
    uint16_t         pending_today;
    int32_t          last_day_ordinal;   /* 跨天检测用 */
    uint32_t         crc32;              /* NVS 完整性校验（覆盖整个结构体） */

    /* ---- V2 新增字段（见上方「只追加在末尾」的约束）---- */

    /* 「今天已经响过」位图：bit i = 1 表示 items[i] 今日已经响过铃。
     *
     * 为什么必须落盘：运行态的 fired_today[] 原本只活在 RAM 里，重启后靠
     * C 静态零初始化变成全 false；而 tick() 的触发条件是
     * `!fired_today[i] && state[i]==IDLE && now_min>=due`，
     * 于是「当天任意时刻重启 + 已过服药时刻」就会让提醒二次响铃，
     * 并因 `(now_min-due)>=15` 在同一 tick 内直接升级成 ESCALATED ——
     * 子女端收到假「漏服」告警，老人已吃过药却被要求再吃一次。 */
    uint16_t         fired_today;

    /* 结构版本号，见 ELDER_RMD_FMT_* */
    uint8_t          fmt_ver;
} elder_rmd_table_t;

/**
 * @brief V1（历史）布局 —— **只为升级迁移而存在**，不是活跃数据结构。
 *
 * 老设备 NVS 里存的是这个布局的 blob：尺寸更小，且 crc32 是按
 * `sizeof(elder_rmd_table_v1_t)` 算的。把它原样保留在头文件里，
 * 是为了让「读旧 blob」这件事有一份**与当年布局逐字节一致**的定义可依，
 * 而不是靠猜「大概是前 N 个字节」。
 *
 * ⚠️ 这个结构一旦与真实历史布局不符，老用户升级固件时就会读出垃圾数据
 *    （服药时刻变成乱码 = 老人照着乱码吃药）。修改本文件前请先确认
 *    git 历史里的定义确实与此处一致。
 */
typedef struct {
    elder_rmd_item_t items[ELDER_REMINDER_MAX];
    uint8_t          count;
    uint16_t         done_today;
    uint16_t         pending_today;
    int32_t          last_day_ordinal;
    uint32_t         crc32;
} elder_rmd_table_v1_t;

/* 编译期钉死：V1 必须是 V2 的**逐字节前缀**。
 *
 * 这条断言是「加字段安全」的全部依据：
 *   - 尺寸：老 blob 读进 V2 的缓冲区时不会越界，且前 sizeof(V1) 字节
 *     与按 V1 解读的结果完全一致（字段偏移没动）；
 *   - CRC：老 crc32 按 sizeof(V1) 算出，按同一段字节重算必然吻合。
 * 任何一次「把新字段插到 count 之前」都会让它编译失败 ——
 * 那正是会让老设备升级后整张提醒表被清空（医疗事故级）的改法。
 */
typedef char elder_rmd_v1_prefix_assert[
    (sizeof(elder_rmd_table_v1_t) == offsetof(elder_rmd_table_t, fired_today)) ? 1 : -1];

/* 同理钉死：今日三个位图（done_today / pending_today / fired_today）都是
 * uint16_t，一个 bit 对应一个 items[] 下标。把 ELDER_REMINDER_MAX 调大到
 * 17 及以上时，第 17 条起的「今天响过 / 已完成 / 待办」就**存不下来** ——
 * 而它们恰恰是重启后不二次响铃的唯一依据。
 * 改成 32 位位图是一次真正的改动（连同 V3 布局与迁移），不是改个常量。
 */
typedef char elder_rmd_bitmap_width_assert[
    (ELDER_REMINDER_MAX <= 16) ? 1 : -1];

typedef void (*elder_rmd_cb_t)(const elder_rmd_item_t *item,
                               elder_rmd_state_t new_state, void *ctx);

/**
 * @brief 初始化：读 NVS → 起 tick 任务（1s 轮询）
 */
esp_err_t elder_reminder_init(elder_rmd_cb_t cb, void *ctx);

esp_err_t elder_reminder_add(const elder_rmd_item_t *item);
esp_err_t elder_reminder_del(uint8_t id);
esp_err_t elder_reminder_set_enabled(uint8_t id, bool enabled);
esp_err_t elder_reminder_list(const elder_rmd_table_t **out);

/**
 * @brief 用后端下发的规则**整体替换**本地表（幂等，按 server_id 复用条目）。
 *        替换后 dirty 但不立即落盘，由 tick 去抖写 NVS。
 */
esp_err_t elder_reminder_replace_all(const elder_rmd_item_t *items, uint8_t count);

/** 按后端 reminder_id 查本地索引；找不到返回 -1 */
int elder_reminder_find_by_server_id(uint32_t server_id);

/** 取出本次触发的到期时刻（unix 秒）；未触发返回 0 */
time_t elder_reminder_get_due_at(uint8_t id);

/**
 * @brief 标记某条提醒已完成（语音确认 / 按键确认 / APP 远程确认都走这里）
 */
esp_err_t elder_reminder_confirm(uint8_t id);

/**
 * @brief 同 elder_reminder_confirm，但显式带上确认方式。
 * @param id            提醒索引
 * @param confirm_method elder_confirm_method_t 的数值（1=语音 2=按键 3=APP）；
 *                      离线语音确认必须传 1，否则回执会误报成按键确认。
 *
 * 为什么需要它：断网时「吃过了」是老人唯一能自助完成的确认动作，
 * 而 elder_sync_on_reminder_state 只看到状态机回调，拿不到「是谁确认的」。
 * 确认方式在这里暂存，由状态机回调带给 sync 层，保证离线补发的回执
 * 仍然是 VOICE 而不是 BUTTON。
 */
esp_err_t elder_reminder_confirm_ex(uint8_t id, uint8_t confirm_method);

/** 取上一次确认使用的确认方式（elder_confirm_method_t 数值） */
uint8_t elder_reminder_last_confirm_method(void);

/**
 * @brief 打开确认窗口（按键长按后由 input 事件驱动）
 */
void elder_reminder_arm_confirm(uint8_t id);

/**
 * @brief 当前是否有「服药未完成」——决定红灯是否长亮
 */
bool elder_reminder_has_pending_medicine(void);

/**
 * @brief 当前**正在响铃**的提醒下标（语音/按键确认的优选目标）；没有则 -1。
 *
 * 「响铃中」= state ∈ {PENDING, CONFIRM_WIN, ESCALATED} 且 enabled，
 * 与 elder_reminder_has_pending_medicine() 同一套状态集合；
 * 多条同时响铃时取 fired_due_at 最大（最近触发）的那条。
 *
 * 存在的理由（2026-10-22 用户实报）：喝水提醒正在响铃，老人说「弄好了」，
 * 确认却记到了另一条吃鼻炎药上。确认必须落在**此刻正在响**的那条，
 * 这是 elder_bridge.cc 的 FirstUndoneReminder() 选目标时的第一优先级。
 */
int elder_reminder_first_ringing(void);

/**
 * @brief 获取下一条待触发提醒距现在的秒数（-1 表示无）
 */
int32_t elder_reminder_next_in_sec(void);

/**
 * @brief 强制落盘（默认写 NVS 有去抖，避免频繁擦写）
 */
esp_err_t elder_reminder_flush(void);

/**
 * @brief 跑一次提醒状态机（推进一天/触发/超时升级，并做 NVS 去抖落盘）。
 *
 * 固件里由 elder_reminder_init() 起的 1s 周期任务调用，一般不需要外部调。
 * 单独暴露是为了让 tools/test_elder_host 能在宿主上按虚拟时钟驱动
 * 「到点触发 → 30s 确认窗超时 → 15 分钟升级」这条断网关键路径，
 * 不必烧板子、不必真的等 15 分钟。
 */
void elder_reminder_tick_once(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_REMINDER_H */
