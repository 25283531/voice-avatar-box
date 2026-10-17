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
 * 与 MultiNet 的分工：
 *   - 识别层（custom_wake_word.cc，main 组件）负责「听」——
 *     esp-sr MultiNet 命中后经 elder_mn_post() 异步转交到这里；
 *   - 本模块负责「做」—— 纯 C、无 ESP 依赖（除 esp_err.h），
 *     因此可以在宿主机器上直接跑单元测试（tools/test_elder_host）。
 *
 * ⚠️ 模型放在哪（别再写成「需要 model 分区」，那是错的）：
 *   模型由 main/assets.cc 从 **assets 分区**里的 srmodels.bin 加载
 *   （读 index.json 的 srmodels 字段 → srmodel_load → SetModelsList），
 *   8MB 的 assets 分区装得下 mn7_cn（2671229 字节）+ 字体 + emoji。
 *   若反而新增 model 分区，esp-sr 的 CMakeLists 会**再打包烧一份**
 *   srmodels.bin 到那里，纯浪费 —— 所以分区表不需要改。
 *
 * ⚠️ 与设备 AEC 的关系（读码结论，不是猜测）：
 *   application.cc 里 CONFIG_USE_DEVICE_AEC 只决定 aec_mode_ 与
 *   listening_mode_，AFE 的 AEC 配置在 afe_audio_processor.cc 里，
 *   两者都不读唤醒词类型。真正的耦合只有一处：
 *   Kconfig 的 WAKE_WORD_TYPE 是 **choice**，USE_AFE_WAKE_WORD 与
 *   USE_CUSTOM_WAKE_WORD 互斥 —— 想让 MultiNet 离线命令词生效就必须
 *   切到后者，代价是唤醒词从专用 WakeNet 换成 MultiNet（见 sdkconfig.defaults
 *   里的说明）。且 CustomWakeWord::Feed 拿的是**未经 AEC 的原始单声道**
 *   （audio_service.cc 里唤醒词与音频处理二选一，喂完就 continue），
 *   所以设备自己放音时离线命令词可能被自己的声音误触发。
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
    /* 屏显文案（UTF-8，NUL 结尾）。
     * 取 160 而非 96：「已创建 08:00、12:00，晚上的药 已存在：20:00」
     * 这类文案在满表时会超过 96 字节。所有写入处都带 %.Ns 精度上限，
     * 编译器可以静态证明不越界（ESP-IDF 把 format-truncation 当错误）。 */
    char               feedback[160];
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

/**
 * @brief 离线命令词接线口（C 链接，供识别层直接喂识别结果）。
 *
 *        识别层（esp-sr MultiNet）每识别出一句就把原文喂进来，内部查词表并执行。
 *        定义在 elder_bridge.cc —— 因为执行动作需要屏显/提示音这些
 *        只有 bridge 才持有的 xiaozhi 运行时句柄。
 *
 * @note 调用链：CustomWakeWord::Feed（音频任务）
 *          → elder_mn_post()（有界队列，非阻塞）
 *          → elder_mn 任务 → 本函数（此时可以同步写 NVS 与刷屏）
 *       识别层**不直接**调本函数，因为音频任务里不能阻塞。
 */
void elder_cmd_on_recognized(const char *phrase);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_CMD_H */
