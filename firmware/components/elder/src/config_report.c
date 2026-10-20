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

/* ── 期望值 ────────────────────────────────────────────────────────────
 * 2026-10-20 回归上游小智后，唤醒词由 WakeNet 模型决定（wn9_nihaoxiaozhi_tts），
 * 自定义 MultiNet 唤醒词（CONFIG_CUSTOM_WAKE_WORD）已删除，不再硬编码屏显期望值。
 */
#ifndef EXPECTED_IDF_TARGET
#define EXPECTED_IDF_TARGET "esp32s3"
#endif

/* ── WakeNet 唤醒词（2026-10-20 回归上游小智）────────────────────────
 * WakeNet 的唤醒词**训练在模型里**，Kconfig 只能选模型、不能选字符串。
 * 上游默认模型 wn9_nihaoxiaozhi_tts = 「你好小智」。
 */
#ifndef EXPECTED_WAKENET_WORD
#define EXPECTED_WAKENET_WORD "你好小智"
#endif

static bool str_eq(const char *a, const char *b)
{
    if (a == NULL || b == NULL) {
        return false;
    }
    return strcmp(a, b) == 0;
}

/*
 * WakeNet 自检：确认「专用唤醒模型」真的编进来了，且是我们挑的那一个。
 *
 * 判据是**具体符号**，不是「有打开任何一个 wn9」——只有具体符号能挡住
 * 「手滑选回你好小智」这种改动，而那种错误在真机上表现为「设备对别人家的
 * 名字应答」，不听串口根本发现不了。
 */
static void report_wakenet(void)
{
#if defined(CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS)
    ESP_LOGI(TAG, "WakeNet 唤醒词=[%s]（上游模型 wn9_nihaoxiaozhi_tts，主唤醒通路）",
             EXPECTED_WAKENET_WORD);
#else
    ESP_LOGE(TAG, "WakeNet 未启用（缺 CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS）⇒ "
                  "唤醒失效；请检查 sdkconfig 是否残留");
#endif

    /* 反向守卫：别的 wn9 模型被同时打开时会抢唤醒词（AfeWakeWord 只认最后一个
     * 匹配到的 wn 模型），这是「改了没生效」的另一种形态，必须吵出来。 */
#if defined(CONFIG_SR_WN_WN9_NIHAOXIAOAN_TTS2) || defined(CONFIG_SR_WN_WN9_XIAOAITONGXUE)
    ESP_LOGE(TAG, "检测到其它 wn9 唤醒模型被同时启用（自研旧词/小爱同学）⇒ "
                  "设备可能应答错误的唤醒词，请只保留 wn9_nihaoxiaozhi_tts");
#endif
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
    report_wakenet();
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