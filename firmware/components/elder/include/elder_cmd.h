#ifndef ELDER_CMD_H
#define ELDER_CMD_H

#include <stdint.h>
#include <stdbool.h>
#include <time.h>
#include "esp_err.h"

#include "elder_reminder.h"
#include "elder_ui.h"      /* elder_tone_t */

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 离线命令词分发器（断网可用的「新建 / 确认 / 查询」）。
 *
 * ⚠️ 离线语音识别的真实边界（别被这份文件的名字骗了）：
 *   本模块只做「意图 → 动作」的映射，**不含任何语音识别**。
 *   固件里唯一能离线听懂人话的是 esp-sr MultiNet（mn6_cn），而它需要
 *   CONFIG_USE_CUSTOM_WAKE_WORD + model 分区 + 词表注册，三者缺一不可。
 *   在 MultiNet 接通之前，断网时老人只能用**按键**确认（elder_input.c），
 *   语音命令词分发器处于「已就绪但无人喂词」的状态。见 docs/决策记录.md。
 *
 * 与 MultiNet 的分工：
 *   - 识别层（custom_wake_word.cc，main 组件）负责「听」——
 *     识别命中后调 elder_cmd_dispatch()；
 *   - 本模块负责「做」—— 纯 C、无 ESP 依赖（除 esp_err.h），
 *     因此可以在宿主机器上直接跑单元测试（tools/test_elder_host）。
 *
 * 安全口径：
 *   - 确认类只确认「今天第一条未完成的服药」，没有删除/改期命令；
 *   - 时间不可信（时钟未校过）时 reminder 引擎不会触发，本模块的
 *     查询结果同样会标注「时间未校准」。
 */

typedef enum {
    ELDER_CMD_NONE = 0,
    ELDER_CMD_CREATE_MORNING,   /* 新建早上吃药提醒 */
    ELDER_CMD_CREATE_NOON,      /* 新建中午吃药提醒 */
    ELDER_CMD_CREATE_EVENING,   /* 新建晚上吃药提醒 */
    ELDER_CMD_CREATE_ALL,       /* 新建吃药提醒（按配置的全部默认时段） */
    ELDER_CMD_CONFIRM,          /* 吃过了 / 我吃过了 / 吃完药了 */
    ELDER_CMD_QUERY_NEXT,       /* 下一个提醒 */
    ELDER_CMD_QUERY_TODAY,      /* 今天的提醒 */
} elder_cmd_action_t;

/* 词表类型（elder_cmd_words.h）。必须在枚举之后包含：词表里的每一条
 * 都用 ELDER_CMD_XXX 作为 action，枚举没定义就展开不了。 */
#include "elder_cmd_words.h"

/* 命令执行结果：屏显文案 + 提示音（离线绝不走 TTS） */
typedef struct {
    elder_cmd_action_t action;
    bool               recognized;          /* 是否命中命令词表 */
    bool               ok;                  /* 动作是否成功执行 */
    elder_tone_t       tone;                /* 反馈提示音 */
    char               feedback[96];        /* 屏显文案（UTF-8，NUL 结尾） */
} elder_cmd_result_t;

/* 断网自建服药提醒的默认时段（由 bridge 启动时从 Kconfig 解析填入） */
typedef struct {
    uint8_t n;                              /* 有效时段数，0..4 */
    struct {
        uint8_t hour;
        uint8_t minute;
    } slots[4];
} elder_cmd_med_times_t;

/** 设置默认服药时段（非法输入被忽略，保持上一次的配置） */
void elder_cmd_set_med_times(const elder_cmd_med_times_t *t);

/** 取当前默认服药时段 */
const elder_cmd_med_times_t *elder_cmd_get_med_times(void);

/** 按语句查命令词表；未命中返回 NULL */
const elder_cmd_word_t *elder_cmd_lookup(const char *phrase);

/** 命令词表条数 */
int elder_cmd_word_count(void);

/** 取第 i 条命令词（越界返回 NULL） */
const elder_cmd_word_t *elder_cmd_word_at(int i);

/**
 * @brief 执行一条语音命令（语句原文）。
 *        未命中时 out->recognized=false，其余字段为零值。
 */
esp_err_t elder_cmd_dispatch(const char *phrase, elder_cmd_result_t *out);

/**
 * @brief 当前时间（可被宿主测试替换的弱挂钩）。
 *        固件里就是 time(NULL)；测试里返回受控时钟。
 */
time_t elder_cmd_now(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_CMD_H */
