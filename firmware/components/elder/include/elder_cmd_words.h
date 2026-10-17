#ifndef ELDER_CMD_WORDS_H
#define ELDER_CMD_WORDS_H

/*
 * 离线命令词表 —— **唯一事实来源**（single source of truth）。
 *
 * 这张表同时被两处消费，任何一处都不允许另抄一份：
 *   1. firmware/components/elder/src/elder_cmd.c
 *      —— 运行时按「语句」查表分发（新建/确认/查询）；
 *   2. tools/test_elder_host/ 的宿主测试 —— 验证查表与分发逻辑。
 *
 * ⚠️ 格式铁律：每条必须单独一行，严格写成 { ELDER_CMD_XXX, "短语", "说明" }。
 *   短语用中文常见字（MultiNet 的 check_speech_command 会逐条校验，
 *   不合法会在启动日志里报 invalid command 并被跳过）；
 *   只增不改：改了短语等于改了老人的肌肉记忆，只能追加别名。
 *
 * ⚠️ 消费方注意事项：实现本表需要先 #define ELDER_CMD_WORDS_IMPLEMENTATION
 *   再包含本头文件，且**必须**通过 elder_cmd.h 包含（枚举要先定义）。
 *   直接单独包含本文件会因缺 ELDER_CMD_XXX 枚举而编译失败。
 *
 * 为什么只有这些词（医疗安全级的保守取舍）
 * ------------------------------------------------
 * 语音命令的误判有两个方向，代价完全不对称：
 *   - 漏识别 → 老人多说一句话，走按键兜底，代价=多一步；
 *   - 误识别 → 可能把一次**服药完成**记成已完成，子女端不再告警，
 *              这一次漏服药就永远不会有人发现，代价=漏服药。
 * 所以词表刻意做「窄」：宁可认不出来，也不要为了召回率放宽。
 * 放宽的诱惑很具体（老人说「我吃过药了吗」是疑问句，不是完成声明），
 * 但把疑问句也收进 CONFIRM 就等于允许误报漏服药，本表因此不收。
 *
 * 逐类说明：
 *   - 新建 4 条：都是完整的「动词+时段+对象+提醒」长句，
 *     日常口语撞上它们的概率极低；误建一条的代价也可控
 *     （老人自己会听到回显，且不会被删除命令误伤）。
 *   - 确认 3 条：全部是「完成体」短句，不收疑问句。
 *     即便误判，find_confirmable() 也会挡掉「今天没响过」的提醒，
 *     最多误确认今天真实响过的那一条。
 *   - 查询 2 条：只读，误识别零伤害。
 *   - **没有任何删除 / 改期 / 停用命令**：误删一条服药提醒不可逆，
 *     收益（少说一句话）远小于风险。
 *
 * ⚠️ 现状提醒（不要误以为这张表已经在起作用）：
 *   表里的短语需要先注册进 esp-sr MultiNet 才能被离线识别，而
 *   MultiNet 目前**未启用**（缺 model 分区 + 未选 USE_CUSTOM_WAKE_WORD，
 *   见 docs/决策记录.md）。所以现在断网时这张表不会被喂词，
 *   真正的兜底是 elder_input.c 的按键确认路径。词表是为接通后准备的。
 */

/* 断网自建服药提醒的默认时段。数值可由 bridge 启动时用
 * elder_cmd_set_med_times() 覆盖（如从 Kconfig 解析）。 */

typedef struct {
    int         action;   /* elder_cmd_action_t */
    const char *phrase;   /* 命令短语（MultiNet 识别 + 屏显匹配键） */
    const char *hint;     /* 人类可读说明（日志/文档用） */
} elder_cmd_word_t;

#ifdef ELDER_CMD_WORDS_IMPLEMENTATION
static const elder_cmd_word_t k_elder_cmd_words[] = {
    /* ---- 新建（断网可建，默认时段见 elder_cmd_med_times_t）---- */
    { ELDER_CMD_CREATE_MORNING, "新建早上吃药提醒", "每天早上服药" },
    { ELDER_CMD_CREATE_NOON,    "新建中午吃药提醒", "每天中午服药" },
    { ELDER_CMD_CREATE_EVENING, "新建晚上吃药提醒", "每天晚上服药" },
    { ELDER_CMD_CREATE_ALL,     "新建吃药提醒",     "按默认时段服药" },

    /* ---- 确认（仅确认今天第一条未完成的服药，安全优先）---- */
    { ELDER_CMD_CONFIRM,        "吃过了",           "确认服药" },
    { ELDER_CMD_CONFIRM,        "我吃过了",         "确认服药" },
    { ELDER_CMD_CONFIRM,        "吃完药了",         "确认服药" },

    /* ---- 查询（只读）---- */
    { ELDER_CMD_QUERY_NEXT,     "下一个提醒",       "下一条提醒" },
    { ELDER_CMD_QUERY_TODAY,    "今天的提醒",       "今日待办" },
};
#define ELDER_CMD_WORD_COUNT \
    ((int)(sizeof(k_elder_cmd_words) / sizeof(k_elder_cmd_words[0])))
#endif /* ELDER_CMD_WORDS_IMPLEMENTATION */

#endif /* ELDER_CMD_WORDS_H */
