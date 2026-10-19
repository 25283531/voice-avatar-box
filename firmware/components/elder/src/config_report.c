/*
 * config_report.c —— 开机自检：把「编译期真正生效的关键配置」打进串口日志
 *
 * ── 为什么需要它 ────────────────────────────────────────────────────────
 * 本项目踩过一个毫无征兆的坑：改了 sdkconfig.defaults 里的唤醒词，
 * 重新编译后固件里还是旧词。原因不在代码，而在 ESP-IDF 的 kconfig 优先级——
 * sdkconfig（本地生成、不入库）优先级高于 sdkconfig.defaults，后者只在
 * **首次生成**时参与，kconfig 对已存在选项默认不覆盖、不报错、不告警。
 *
 * 编译期检查（tools/prebuild_check.py）能在 CI 上挡住这类问题，但烧进设备
 * 之后呢？串口上没有任何线索，用户只会说「我明明改了」。本模块就是补上这一段：
 * 开机第一时间把实际编译进去的值原样打出来，并对不合理的状态大声报错。
 *
 * ── 为什么不放在 elder_bridge.cc ────────────────────────────────────────
 * elder_bridge 由 elder_boot 任务拉起，而那个任务是在 app.Start() **之后**
 * 才创建的；未绑定设备上 app.Start() 永不返回 ⇒ 永远看不到这段日志。
 * 而绑定引导在 app.Start() 之前就起了任务，且它是本项目**唯一**保证在
 * 已绑定/未绑定两种设备上都会执行的最早钩子（内部才判断绑定态）。
 *
 * ── 判定原则 ────────────────────────────────────────────────────────────
 * 「不符合预期」必须 ESP_LOGE，不能只 ESP_LOGW——老人设备的现场日志往往只有
 * 这一段，WARNING 会被淹没，ERROR 才看得到。
 */
#include "config_report.h"

#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "sdkconfig.h"
#include "esp_log.h"

static const char *TAG = "elder.cfg";

/* ── 期望值 ──────────────────────────────────────────────────────────────
 * 这里是全项目唯一一处「硬编码的期望唤醒词」。改唤醒词时必须同步改这里，
 * 否则设备开机会报 ERROR（这是有意的：宁可吵，也不要静默地跑错配置）。
 * 想临时绕过（例如做对照实验）可编译时 -DEXPECTED_WAKE_WORD_DISPLAY=\"xxx\"。
 */
#ifndef EXPECTED_WAKE_WORD_DISPLAY
#define EXPECTED_WAKE_WORD_DISPLAY "你好，小暖"
#endif

#ifndef EXPECTED_IDF_TARGET
#define EXPECTED_IDF_TARGET "esp32s3"
#endif

static bool str_eq(const char *a, const char *b)
{
    if (a == NULL || b == NULL) {
        return false;
    }
    return strcmp(a, b) == 0;
}

/*
 * 唤醒词自检。
 *
 * 判据是「实际值 == 期望值」，不是「值是不是非空」——非空只能证明配过，
 * 证明不了配的是哪一个。而我们要挡的恰恰是「配了但没生效」这一类。
 */
static void report_wake_word(void)
{
    const char *pinyin = CONFIG_CUSTOM_WAKE_WORD;
    const char *display = CONFIG_CUSTOM_WAKE_WORD_DISPLAY;

    ESP_LOGI(TAG, "唤醒词 拼音=[%s] 屏显=[%s] 阈值=%d",
             pinyin, display, CONFIG_CUSTOM_WAKE_WORD_THRESHOLD);

    if (!str_eq(display, EXPECTED_WAKE_WORD_DISPLAY)) {
        ESP_LOGE(TAG, "唤醒词屏显不是期望值：期望=[%s] 实际=[%s]",
                 EXPECTED_WAKE_WORD_DISPLAY, display);
        ESP_LOGE(TAG, "多半是 sdkconfig 残留（它优先级高于 sdkconfig.defaults 且不覆盖）"
                      "—— 删掉 sdkconfig 与 build/ 后 set-target esp32s3 重编");
    }

    /* 屏显是给老人看的，拼音是给唤醒引擎匹配的，两者必须成对存在。
     * 只有一侧有值说明 sdkconfig.defaults 的唤醒词段被改残了。 */
    if ((display == NULL || display[0] == '\0') !=
        (pinyin == NULL || pinyin[0] == '\0')) {
        ESP_LOGE(TAG, "唤醒词拼音与屏显不成对：拼音=[%s] 屏显=[%s]",
                 pinyin, display);
    }

    int threshold = CONFIG_CUSTOM_WAKE_WORD_THRESHOLD;
    if (threshold <= 0 || threshold > 100) {
        ESP_LOGE(TAG, "唤醒词阈值越界：%d（应为 1..100）", threshold);
    }
}

/*
 * 显示风格自检。
 *
 * 我们在 sdkconfig.defaults 里刻意关掉了 emote（CONFIG_USE_EMOTE_MESSAGE_STYLE），
 * 改用标准 LVGL 消息样式：elder 定制了 70/30 对话布局（对话区 70%、状态区 30%），
 * 而 emote 是一整块全屏动画，会把对话区顶掉——两者在同一份 sdkconfig 里互斥，
 * 误开的表现是「开机有大表情、对话文字被盖住」，不看配置根本猜不到原因。
 */
static void report_display_style(void)
{
#if defined(CONFIG_USE_DEFAULT_MESSAGE_STYLE) && CONFIG_USE_DEFAULT_MESSAGE_STYLE
    ESP_LOGI(TAG, "显示风格=标准 LVGL（elder 70/30 对话布局生效）");
#else
    ESP_LOGE(TAG, "标准 LVGL 消息样式未启用，对话布局定制不会生效");
#endif

#ifdef CONFIG_USE_EMOTE_MESSAGE_STYLE
    ESP_LOGE(TAG, "emote 表情风格处于启用态（CONFIG_USE_EMOTE_MESSAGE_STYLE），"
                  "它会覆盖 elder 的 70/30 对话布局 —— 请在 sdkconfig.defaults 里关掉它");
#endif
}

/* 目标芯片自检：删 build/ 后不先 set-target，IDF 会默认按 esp32 编，
 * 那样摄像头组件会被组件管理器判成无用依赖并删掉，是另一类难归因的故障。 */
static void report_target(void)
{
#ifdef CONFIG_IDF_TARGET
    ESP_LOGI(TAG, "目标芯片=%s", CONFIG_IDF_TARGET);
    if (!str_eq(CONFIG_IDF_TARGET, EXPECTED_IDF_TARGET)) {
        ESP_LOGE(TAG, "目标芯片不是期望值：期望=[%s] 实际=[%s] —— "
                      "请先 set-target %s 再 build",
                 EXPECTED_IDF_TARGET, CONFIG_IDF_TARGET, EXPECTED_IDF_TARGET);
    }
#endif
}

void elder_config_report(void)
{
    ESP_LOGI(TAG, "===== elder 开机自检（编译期实际生效值）=====");
    report_target();
    report_wake_word();
    report_display_style();

    /* 分区/OTA 这类宏在部分板型下不存在，用 #ifdef 包起来，
     * 免得在没启用这些功能的板型上直接编译不过。 */
#ifdef CONFIG_OTA_URL
    ESP_LOGI(TAG, "OTA 地址=%s", CONFIG_OTA_URL);
#endif
#ifdef CONFIG_ELDER_SERVER_URL
    ESP_LOGI(TAG, "elder 服务地址=%s", CONFIG_ELDER_SERVER_URL);
#endif
    ESP_LOGI(TAG, "===== 自检结束 =====");
}