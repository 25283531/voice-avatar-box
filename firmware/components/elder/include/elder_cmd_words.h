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
 * ⚠️ 格式铁律：每条必须单独一行，严格写成 { ELDER_CMD_XXX, "短语", "说明", "拼音" }。
 *   短语用中文常见字（屏显匹配键，也是老人心里的那句话）；
 *   拼音是**同一句话**的空格分隔小写形式，供 esp-sr MultiNet 注册 ——
 *   MultiNet 中文模型的最小单元是**拼音音节**（见 mn7_cn/vocab：405 个
 *   音节如 "xin"/"jian"/"yao"），传汉字会被 check_speech_command() 判为
 *   invalid command 而**静默不入表**。
 *   两列必须逐条对齐：**改了短语就必须同步改拼音**，否则老人说的话
 *   与注册进模型的音串对不上，识别永远命中不了。
 *   只增不改：改了短语等于改了老人的肌肉记忆，只能追加别名。
 *   （新增别名时也**只能追加**：追加一行的拼音不会影响已有短语，
 *   而修改已有行的拼音会让老人已经习惯的那句话失配。）
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
 * ⚠️ 离线识别链路现状（已接通，别再当成"没启用"）：
 *   表里的短语现在**真的会被离线识别**了，链路是
 *     custom_wake_word.cc（esp-sr MultiNet，main 组件）
 *       → esp_mn_commands_add(拼音)
 *       → 命中后 elder_mn_post()（有界队列 + 独立任务，非阻塞）
 *       → elder_cmd_on_recognized() → elder_cmd_dispatch()
 *   两个必须记住的事实：
 *   1. 模型走 **assets 分区里的 srmodels.bin**（assets.cc 读 index.json 的
 *      srmodels 字段后 srmodel_load），**不需要** model 分区；
 *   2. 识别不可靠时（老人方言口音、噪声、模型版本）仍有
 *      elder_input.c 的**按键兜底**路径，且按键路径不受本次改动影响。
 *   词表本身一直都在固件二进制里：elder_bridge_start() 末尾会遍历它逐条
 *   打印（同时防止被链接器 --gc-sections 裁掉），开机日志里可以直接看到。
 */

typedef struct {
    int         action;   /* elder_cmd_action_t */
    const char *phrase;   /* 命令短语（汉字，屏显匹配键 + 老人心里的那句话） */
    const char *hint;     /* 人类可读说明（日志/文档用） */
    const char *pinyin;   /* 同一句话的空格分隔拼音 —— MultiNet 注册用的就是这个 */
} elder_cmd_word_t;

#ifdef ELDER_CMD_WORDS_IMPLEMENTATION
static const elder_cmd_word_t k_elder_cmd_words[] = {
    /* ---- 新建（断网可建，默认时段见 elder_cmd_med_times_t）---- */
    { ELDER_CMD_CREATE_MORNING, "新建早上吃药提醒", "每天早上服药", "xin jian zao shang chi yao ti xing" },
    { ELDER_CMD_CREATE_NOON,    "新建中午吃药提醒", "每天中午服药", "xin jian zhong wu chi yao ti xing" },
    { ELDER_CMD_CREATE_EVENING, "新建晚上吃药提醒", "每天晚上服药", "xin jian wan shang chi yao ti xing" },
    { ELDER_CMD_CREATE_ALL,     "新建吃药提醒",     "按默认时段服药", "xin jian chi yao ti xing" },

    /* ---- 确认（仅确认今天第一条未完成的服药，安全优先）---- */
    { ELDER_CMD_CONFIRM,        "吃过了",           "确认服药", "chi guo le" },
    { ELDER_CMD_CONFIRM,        "我吃过了",         "确认服药", "wo chi guo le" },
    { ELDER_CMD_CONFIRM,        "吃完药了",         "确认服药", "chi wan yao le" },

    /* ---- 查询（只读）---- */
    { ELDER_CMD_QUERY_NEXT,     "下一个提醒",       "下一条提醒", "xia yi ge ti xing" },
    { ELDER_CMD_QUERY_TODAY,    "今天的提醒",       "今日待办",   "jin tian de ti xing" },
};
#define ELDER_CMD_WORD_COUNT \
    ((int)(sizeof(k_elder_cmd_words) / sizeof(k_elder_cmd_words[0])))
#endif /* ELDER_CMD_WORDS_IMPLEMENTATION */

#endif /* ELDER_CMD_WORDS_H */
