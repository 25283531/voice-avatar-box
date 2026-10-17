/* elder_mn.c —— esp-sr MultiNet 离线命令词的接线层（纯 C）
 *
 * 它解决一个具体的工程问题：`elder_cmd_dispatch()` 是**同步**函数，
 * 内部会写 NVS（elder_reminder_add / confirm_ex），在 ESP-IDF 上一次
 * NVS 提交是毫秒级；而 MultiNet 的识别回调跑在 `audio_communication`
 * 任务里（CustomWakeWord::Feed）。在音频任务里直接写 NVS 会阻塞喂音频，
 * 表现为「说完话设备卡一下」，严重时丢帧。
 *
 * 所以这里用「有界队列 + 独立任务」把两者隔开：
 *   识别回调（音频任务，实时）→ 只做 memcpy + xQueueSend（微秒级）
 *                            → elder_mn_task（低优先级）→ 同步执行
 *
 * 队列满时**丢弃**而不是阻塞：宁可漏识别（老人按键兜底），也不能让音频卡顿。
 * 医疗安全口径与 elder_cmd_words.h 一致：宁可认不出来，也不要阻塞音频链路。
 */
#include "elder_mn.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "esp_log.h"

#include "elder_cmd.h"

#define TAG "elder.mn"

/* 队列深度。老人一句话最多触发一次识别，深度 4 足以吸收连说两遍；
 * 再深也只是把「丢识别」推迟几毫秒，换不来正确性。 */
#define ELDER_MN_QUEUE_LEN 4

/* 槽位字节数：能装下词表里最长的短语（"新建早上吃药提醒"=8 汉字=24 字节）
 * 并留出余量。取 64 是为了将来追加别名时不必改这里。 */
#define ELDER_MN_SLOT_BYTES 64

/* 任务栈：调用链最深是 elder_cmd_dispatch → act_create → snprintf，
 * 加上 elder_cmd_on_recognized 在 bridge 里的 OnNotify/OnPlayTone，
 * 3KB 与 reminder.c 同档（那里也要 snprintf 一长串文案）。 */
#define ELDER_MN_TASK_STACK 3072
#define ELDER_MN_TASK_PRIO  4

static QueueHandle_t s_q;
static TaskHandle_t s_task;

/* ---------------------------------------------------------------- 内部 */

/* 真正执行命令的地方。跑在独立任务里，可以放心做 NVS 写入与屏显。
 *
 * ⚠️ 这里**只**转交 elder_cmd_on_recognized()，不要自己再调
 * elder_cmd_dispatch()：那个函数在 elder_bridge.cc 里已经调过一次，
 * 并负责「屏显 + 提示音 + 形象切换 + 未识别时的按键兜底提示」。
 * 本文件如果再调一次，同一条提醒会被写进 NVS 两次。
 */
static void elder_mn_run(const char *phrase)
{
    elder_cmd_on_recognized(phrase);
}

static void elder_mn_task(void *arg)
{
    (void)arg;
    char slot[ELDER_MN_SLOT_BYTES];

    for (;;) {
        if (xQueueReceive(s_q, slot, sizeof(slot), portMAX_DELAY) != pdTRUE) {
            continue;
        }
        /* NUL 兜底：万一队列里的数据被写坏，也不能让 dispatch 读越界 */
        slot[ELDER_MN_SLOT_BYTES - 1] = '\0';
        elder_mn_run(slot);
    }
}

/* ---------------------------------------------------------------- 入口 */

esp_err_t elder_mn_init(void)
{
    if (s_q != NULL) {
        return ESP_OK;   /* 幂等 */
    }

    s_q = xQueueCreate(ELDER_MN_QUEUE_LEN, ELDER_MN_SLOT_BYTES);
    if (s_q == NULL) {
        ESP_LOGE(TAG, "队列创建失败（RAM 不足？）");
        return ESP_ERR_NO_MEM;
    }

    /* 不设 MALLOC_CAP_SPIRAM：任务栈要放内部 RAM，且 3KB 很小 */
    if (xTaskCreatePinnedToCore(elder_mn_task, "elder_mn", ELDER_MN_TASK_STACK,
                                NULL, ELDER_MN_TASK_PRIO, &s_task, 1) != pdPASS) {
        ESP_LOGE(TAG, "命令词任务创建失败");
        vQueueDelete(s_q);
        s_q = NULL;
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "离线命令词执行通道就绪（队列深度 %d，栈 %dB，优先级 %d）",
             ELDER_MN_QUEUE_LEN, ELDER_MN_TASK_STACK, ELDER_MN_TASK_PRIO);
    return ESP_OK;
}

/* 识别回调入口。**必须**非阻塞：调用方是音频任务。 */
bool elder_mn_post(const char *phrase)
{
    if (s_q == NULL || phrase == NULL || phrase[0] == '\0') {
        return false;
    }

    size_t n = strlen(phrase);
    if (n >= ELDER_MN_SLOT_BYTES) {
        /* 词表里不该有这么长的短语；截断并明确告警，不静默丢字 */
        ESP_LOGW(TAG, "指令超长(%u > %d)，已截断: \"%.40s...\"",
                 (unsigned)n, ELDER_MN_SLOT_BYTES - 1, phrase);
        n = ELDER_MN_SLOT_BYTES - 1;
    }

    char slot[ELDER_MN_SLOT_BYTES];
    memset(slot, 0, sizeof(slot));
    memcpy(slot, phrase, n);

    /* 0 超时：宁可丢，也不要在音频任务里等 */
    if (xQueueSend(s_q, slot, 0) != pdTRUE) {
        ESP_LOGW(TAG, "命令词队列已满，丢弃本次识别: \"%s\"", phrase);
        return false;
    }
    return true;
}