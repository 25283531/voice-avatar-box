/* elder_cmd.c —— 离线命令词分发器（纯 C，可宿主测试）
 *
 * 输入是「一条语句原文」，输出是「执行结果 + 屏显文案 + 提示音」。
 * 本文件不碰音频、不碰网络、不碰显示 —— 那些由 elder_bridge.cc 负责。
 *
 * ⚠️ 本文件**不做语音识别**。它假设调用方（未来的 MultiNet 接入层、
 * 或宿主测试）已经拿到了识别后的文字。断网时若 MultiNet 未接通，
 * 这套分发逻辑处于「已就绪但无人喂词」状态，按键路径才是真正的兜底。
 */
/* 词表本体在 elder_cmd_words.h 里，由 elder_cmd.h 在枚举定义之后包含。
 * 关键：ELDER_CMD_WORDS_IMPLEMENTATION 必须在包含 elder_cmd.h **之前**定义，
 * 否则词表那次展开被 include guard 吃掉，k_elder_cmd_words 不会被定义，
 * 下面的查表函数就全部编译不过（未定义符号）。头文件只被包含一次，
 * 不存在 typedef 重定义问题。 */
#define ELDER_CMD_WORDS_IMPLEMENTATION
#include "elder_cmd.h"
#undef ELDER_CMD_WORDS_IMPLEMENTATION

#include <string.h>
#include <stdio.h>
#include <time.h>

#include "esp_log.h"   /* append_slot() 截断时需要 ESP_LOGE */

/* 低于这个时刻认为系统时钟不可信（未校时/掉电归零），
 * 与 reminder.c 的守卫阈值保持一致 */
#define ELDER_MIN_TRUSTED_EPOCH 1577836800L   /* 2020-01-01T00:00:00Z */

/* 确认方式（数值与 elder_sync.h 的 elder_confirm_method_t 一致） */
#define ELDER_CFM_VOICE_NUM 1

static elder_cmd_med_times_t g_times = {
    .n = 3,
    .slots = { {8, 0}, {12, 0}, {20, 0} },
};

/* ---------------------------------------------------------------- 基础 */

__attribute__((weak)) time_t elder_cmd_now(void)
{
    return time(NULL);
}

void elder_cmd_set_med_times(const elder_cmd_med_times_t *t)
{
    if (!t || t->n == 0 || t->n > 4) return;
    for (uint8_t i = 0; i < t->n; i++) {
        if (t->slots[i].hour > 23 || t->slots[i].minute > 59) return;
    }
    g_times = *t;
}

const elder_cmd_med_times_t *elder_cmd_get_med_times(void)
{
    return &g_times;
}

const elder_cmd_word_t *elder_cmd_lookup(const char *phrase)
{
    if (!phrase || !phrase[0]) return NULL;
    for (int i = 0; i < ELDER_CMD_WORD_COUNT; i++) {
        if (strcmp(phrase, k_elder_cmd_words[i].phrase) == 0) return &k_elder_cmd_words[i];
    }
    return NULL;
}

int elder_cmd_word_count(void)
{
    return ELDER_CMD_WORD_COUNT;
}

const elder_cmd_word_t *elder_cmd_word_at(int i)
{
    if (i < 0 || i >= ELDER_CMD_WORD_COUNT) return NULL;
    return &k_elder_cmd_words[i];
}

/* ---------------------------------------------------------------- 内部工具 */

/* 安全地把一段文字追加到定长缓冲尾部。
 *
 * 为什么不能直接写 `snprintf(buf, sizeof(buf), "%s%s", buf, add)`：
 *   源与目标是**同一个对象**，属于未定义行为。GCC 会直接报
 *   `-Werror=restrict`（'snprintf' argument N overlaps destination），
 *   而且即便不报，截断时也可能把尾部写坏。ESP-IDF 把这两个警告当错误，
 *   编译必挂。这里统一走「先算剩余空间、再一次性写完」，行为可预期。
 *
 * 返回 false 表示放不下（已按「宁可少显示也不越界」处理）。
 */
static bool append_text(char *buf, size_t cap, const char *add)
{
    if (!buf || !add || cap == 0) return false;
    size_t used = strlen(buf);
    if (used + 1 >= cap) return false;              /* 至少要留得下 NUL */
    size_t room = cap - used;
    size_t n = strlen(add);
    if (n >= room) n = room - 1;                    /* 截断，不越界 */
    memcpy(buf + used, add, n);
    buf[used + n] = '\0';
    return true;
}

/* 追加「、08:00」这种时段项；首次追加不加顿号 */
static void append_slot(char *buf, size_t cap, uint8_t hh, uint8_t mm)
{
    char item[24];
    snprintf(item, sizeof(item), "%s%02u:%02u", buf[0] ? "、" : "", hh, mm);
    if (!append_text(buf, cap, item)) {
        ESP_LOGE("elder.cmd", "时段列表已满，截断显示: %s", buf);
    }
}

static bool time_trusted(void)
{
    return elder_cmd_now() >= (time_t)ELDER_MIN_TRUSTED_EPOCH;
}

/* 与 reminder.c 同款：bit0=周日 .. bit6=周六 */
static int weekday_mask_from_tm(const struct tm *tm)
{
    return 1 << tm->tm_wday;
}

static bool item_due_today(const elder_rmd_item_t *it, const struct tm *tm)
{
    if (!it->enabled) return false;
    if (it->repeat_mask == 0) return true;
    return (it->repeat_mask & weekday_mask_from_tm(tm)) != 0;
}

static bool item_done_today(const elder_rmd_table_t *tbl, int idx)
{
    return (tbl->done_today & (1u << idx)) != 0;
}

/* 找今天第一条「未完成且已触发」的服药提醒；没有返回 -1 */
static int find_confirmable(const elder_rmd_table_t *tbl)
{
    for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
        const elder_rmd_item_t *it = &tbl->items[i];
        if (it->type != ELDER_RMD_TYPE_MEDICINE) continue;
        if (!it->enabled) continue;
        if (item_done_today(tbl, i)) continue;
        if (it->fired_due_at == 0) continue;   /* 今天还没响过 */
        return (int)i;
    }
    return -1;
}

static bool same_schedule_exists(const elder_rmd_table_t *tbl,
                                 uint8_t hour, uint8_t minute, const char *label)
{
    for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
        const elder_rmd_item_t *it = &tbl->items[i];
        if (it->hour != hour || it->minute != minute) continue;
        if (it->type != ELDER_RMD_TYPE_MEDICINE) continue;
        if (!it->enabled) continue;
        if (strcmp(it->label, label) == 0) return true;
    }
    return false;
}

static const char *meal_label(elder_cmd_action_t act)
{
    switch (act) {
    case ELDER_CMD_CREATE_MORNING: return "早上的药";
    case ELDER_CMD_CREATE_NOON:    return "中午的药";
    case ELDER_CMD_CREATE_EVENING: return "晚上的药";
    default:                       return "吃药";
    }
}

/* ---------------------------------------------------------------- 动作：新建 */

static void act_create(elder_cmd_result_t *out, elder_cmd_action_t act)
{
    const elder_rmd_table_t *tbl = NULL;
    if (elder_reminder_list(&tbl) != ESP_OK || !tbl) {
        out->tone = ELDER_TONE_EXCLAMATION;
        snprintf(out->feedback, sizeof(out->feedback), "提醒引擎未就绪");
        return;
    }

    const char *label = meal_label(act);
    uint8_t first = (act == ELDER_CMD_CREATE_ALL) ? 0 : (uint8_t)(act - ELDER_CMD_CREATE_MORNING);
    uint8_t last  = (act == ELDER_CMD_CREATE_ALL) ? (uint8_t)(g_times.n - 1) : first;

    /* 时段列表缓冲。最多 4 个时段，每项「、HH:MM」= 3+5 = 8 字节，
 * 4 项 = 32 字节，32 足够。
 *
 * 取 32 而不是 64 是**为了让编译器能静态证明不越界**：feedback[160] 要装
 * 「已创建 %.31s，%.15s 已存在：%.31s」，固定部分约 9+3+13=25 字节，
 * 25 + 31*2 = 87 < 160。若列表给 64，同样的句式最坏是 25 + 63*2 = 151，
 * 再加上中文字符与 NUL 的余量，GCC 会判定可能截断并报
 * -Werror=format-truncation（ESP-IDF 把它当错误）。 */
char created[32] = {0};
    char existed[32] = {0};
    int  n_created = 0;
    bool any_exist = false;

    for (uint8_t s = first; s <= last && s < g_times.n; s++) {
        uint8_t hh = g_times.slots[s].hour, mm = g_times.slots[s].minute;
        if (same_schedule_exists(tbl, hh, mm, label)) {
            any_exist = true;
            append_slot(existed, sizeof(existed), hh, mm);
            continue;
        }

        elder_rmd_item_t it;
        memset(&it, 0, sizeof(it));
        it.hour = hh;
        it.minute = mm;
        it.type = ELDER_RMD_TYPE_MEDICINE;
        it.repeat_mask = 0x7F;            /* 每天 */
        it.enabled = true;
        it.rtype = 0;                     /* 后端 ReminderType.MEDICINE */
        it.schedule_kind = 1;             /* 后端 ScheduleKind.DAILY */
        it.server_id = 0;                 /* 设备本地自建，联网后上传 */
        snprintf(it.label, sizeof(it.label), "%s", label);

        if (elder_reminder_add(&it) != ESP_OK) {
            out->tone = ELDER_TONE_EXCLAMATION;
            snprintf(out->feedback, sizeof(out->feedback), "提醒数量已满（%d 条）",
                     ELDER_REMINDER_MAX);
            return;
        }
        n_created++;
        append_slot(created, sizeof(created), hh, mm);
        /* add 之后表变了，重新取 */
        elder_reminder_list(&tbl);
    }

    out->ok = true;
    if (n_created == 0) {
        /* 全部已存在：不重复建，明确告知 */
        out->tone = ELDER_TONE_POPUP;
        snprintf(out->feedback, sizeof(out->feedback), "已存在：每天 %.31s %.15s",
                 existed, label);
        return;
    }
    out->tone = ELDER_TONE_SUCCESS;
    if (any_exist) {
        snprintf(out->feedback, sizeof(out->feedback), "已创建 %.31s，%.15s 已存在：%.31s",
                 created, label, existed);
    } else if (n_created == 1 && act != ELDER_CMD_CREATE_ALL) {
        snprintf(out->feedback, sizeof(out->feedback), "已创建：每天 %.31s %.15s",
                 created, label);
    } else {
        snprintf(out->feedback, sizeof(out->feedback), "已创建 %d 个时段：%.31s %.15s",
                 n_created, created, label);
    }
}

/* ---------------------------------------------------------------- 动作：确认 */

static void act_confirm(elder_cmd_result_t *out)
{
    const elder_rmd_table_t *tbl = NULL;
    if (elder_reminder_list(&tbl) != ESP_OK || !tbl) {
        out->tone = ELDER_TONE_EXCLAMATION;
        snprintf(out->feedback, sizeof(out->feedback), "提醒引擎未就绪");
        return;
    }

    int idx = find_confirmable(tbl);
    if (idx < 0) {
        out->tone = ELDER_TONE_POPUP;
        snprintf(out->feedback, sizeof(out->feedback), "当前没有需要确认的服药");
        return;
    }

    const elder_rmd_item_t *it = &tbl->items[idx];
    char label[ELDER_REMINDER_LABEL + 1];
    snprintf(label, sizeof(label), "%s", it->label);
    uint8_t hh = it->hour, mm = it->minute;

    /* method=1（VOICE）：回执里区分「语音确认」与「按键确认」 */
    if (elder_reminder_confirm_ex((uint8_t)idx, ELDER_CFM_VOICE_NUM) != ESP_OK) {
        out->tone = ELDER_TONE_EXCLAMATION;
        snprintf(out->feedback, sizeof(out->feedback), "确认失败，请再试一次");
        return;
    }

    out->ok = true;
    out->tone = ELDER_TONE_SUCCESS;
    snprintf(out->feedback, sizeof(out->feedback), "已记录 %02u:%02u %.15s：已完成", hh, mm, label);
}

/* ---------------------------------------------------------------- 动作：查询 */

static void act_query_next(elder_cmd_result_t *out)
{
    const elder_rmd_table_t *tbl = NULL;
    if (elder_reminder_list(&tbl) != ESP_OK || !tbl) {
        out->tone = ELDER_TONE_EXCLAMATION;
        snprintf(out->feedback, sizeof(out->feedback), "提醒引擎未就绪");
        return;
    }
    if (!time_trusted()) {
        out->tone = ELDER_TONE_EXCLAMATION;
        snprintf(out->feedback, sizeof(out->feedback), "时间未校准，联网后自动校正");
        return;
    }

    time_t now = elder_cmd_now();
    struct tm tm_now;
    localtime_r(&now, &tm_now);
    int now_min = tm_now.tm_hour * 60 + tm_now.tm_min;

    int best = -1, best_delta = 0;
    for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
        const elder_rmd_item_t *it = &tbl->items[i];
        if (!item_due_today(it, &tm_now)) continue;
        if (item_done_today(tbl, i)) continue;
        int due = it->hour * 60 + it->minute;
        int delta = due - now_min;
        if (delta < 0) continue;                 /* 今天已过点的不算「下一个」 */
        if (best < 0 || delta < best_delta) {
            best = (int)i;
            best_delta = delta;
        }
    }

    if (best < 0) {
        out->ok = true;
        out->tone = ELDER_TONE_POPUP;
        snprintf(out->feedback, sizeof(out->feedback), "今天没有更多提醒了");
        return;
    }
    out->ok = true;
    out->tone = ELDER_TONE_POPUP;
    snprintf(out->feedback, sizeof(out->feedback), "下一个提醒 %02u:%02u %.15s",
             tbl->items[best].hour, tbl->items[best].minute, tbl->items[best].label);
}

static void act_query_today(elder_cmd_result_t *out)
{
    const elder_rmd_table_t *tbl = NULL;
    if (elder_reminder_list(&tbl) != ESP_OK || !tbl) {
        out->tone = ELDER_TONE_EXCLAMATION;
        snprintf(out->feedback, sizeof(out->feedback), "提醒引擎未就绪");
        return;
    }
    if (!time_trusted()) {
        out->tone = ELDER_TONE_EXCLAMATION;
        snprintf(out->feedback, sizeof(out->feedback), "时间未校准，联网后自动校正");
        return;
    }

    time_t now = elder_cmd_now();
    struct tm tm_now;
    localtime_r(&now, &tm_now);

    char list[96] = {0};
    int n = 0;
    for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
        const elder_rmd_item_t *it = &tbl->items[i];
        if (!item_due_today(it, &tm_now)) continue;
        if (item_done_today(tbl, i)) continue;
        /* 一项最坏 = 「、」(3) + "08"(2) + ":"(1) + "00"(2) + 空格(1) + 标题(31) = 40，
         * 加 NUL 要41 字节，所以这里给 48 并给 %s 加精度上限，
         * 让编译器能静态证明不截断（ESP-IDF 把该警告当错误）。 */
        char one[48];
        snprintf(one, sizeof(one), "%s%02u:%02u %.31s", list[0] ? "、" : "",
                 it->hour, it->minute, it->label);
        /* 放不下就停：宁可少列几条，也不要把半个词截断给老人看 */
        if (!append_text(list, sizeof(list), one)) break;
        n++;
    }

    out->ok = true;
    out->tone = ELDER_TONE_POPUP;
    if (n == 0) {
        snprintf(out->feedback, sizeof(out->feedback), "今天的提醒都完成了");
    } else {
        /* list 是 char[96]，最多 95 字节有效内容，故精度取 95 */
        snprintf(out->feedback, sizeof(out->feedback), "今天还有 %d 次：%.95s", n, list);
    }
}

/* ---------------------------------------------------------------- 入口 */

esp_err_t elder_cmd_dispatch(const char *phrase, elder_cmd_result_t *out)
{
    if (!out) return ESP_ERR_INVALID_ARG;
    memset(out, 0, sizeof(*out));

    const elder_cmd_word_t *w = elder_cmd_lookup(phrase);
    if (!w) {
        out->action = ELDER_CMD_NONE;
        out->tone = ELDER_TONE_POPUP;
        snprintf(out->feedback, sizeof(out->feedback), "未识别的指令");
        return ESP_ERR_NOT_FOUND;
    }

    out->action = (elder_cmd_action_t)w->action;
    out->recognized = true;
    switch (w->action) {
    case ELDER_CMD_CREATE_MORNING:
    case ELDER_CMD_CREATE_NOON:
    case ELDER_CMD_CREATE_EVENING:
    case ELDER_CMD_CREATE_ALL:
        act_create(out, (elder_cmd_action_t)w->action);
        break;
    case ELDER_CMD_CONFIRM:
        act_confirm(out);
        break;
    case ELDER_CMD_QUERY_NEXT:
        act_query_next(out);
        break;
    case ELDER_CMD_QUERY_TODAY:
        act_query_today(out);
        break;
    default:
        out->tone = ELDER_TONE_POPUP;
        snprintf(out->feedback, sizeof(out->feedback), "未支持的指令");
        return ESP_ERR_NOT_SUPPORTED;
    }
    return ESP_OK;
}
