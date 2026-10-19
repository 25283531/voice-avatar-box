// elder_bind_guide.cc —— 未绑定设备的「请去 APP 绑定」引导（屏显 + 提示音）
//
// 需求（用户 2026-10-19 实测）：
//   配网完成后设备一直显示「云服务断开」标志，也没有设备连接序列号。
//   希望此时语音播报一句「请使用手机 APP 或者登录网站进行设备绑定」。
//
// ── 为什么这个模块必须独立于 elder_bridge.cc ────────────────────────────
// elder_bridge_start() 由 elder_boot 任务调用，而那个任务是在
// `app.Start()` **返回之后**才 xTaskCreate 的。未绑定设备上 app.Start()
// 会在 CheckNewVersion() 的 while(true) 里永久阻塞（ShowActivationCode →
// Activate 连试 10 次 → 回循环），永远不返回 ⇒ elder_boot 任务压根不会被创建。
// 把引导写进 elder_bridge 就等于写在一个永不执行的函数里。
//
// ── 就绪判据为什么是 Board::GetInstance().GetDisplay() ─────────────────
// Application 没有暴露 GetDisplay()（只有 GetAudioService()）。Display 挂在
// Board 上，而 Board::GetInstance() 是 C++ 魔法静态：第一次调用会构造单例
// （构造函数里已 new 好 SpiLcdDisplay），因此「GetDisplay() 非空」既等价于
// 「Board 已构造完成」，也等价于「屏幕驱动就绪」。这里用 Display* 非空而
// 不是 DisplayLockGuard，等价但更轻——不与上游刷屏争锁。
//
// ── 与上游 OTA 激活循环的显示争用（已评估：接受）──────────────────────
// 未绑定时上游每轮 CheckVersion() 失败都会 Alert(ACTIVATION, …)，
// 写 status_label_ 与 chat_message_label_；本模块用 ShowNotification
// （notification_label_，同一顶区）约每 15s 刷新一次，实测可压过上游的
// 逐位念码。刻意不去改上游 application.cc —— 那是冻结镜像（见 VENDOR.md），
// 改动成本远大于收益。

#include <cstdio>
#include <cstring>
#include <string>

#include "sdkconfig.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "board.h"
#include "application.h"   // PlaySound（内置 ogg）+ GetDeviceState
#include "display.h"
#include "settings.h"
#include "system_info.h"
#include "assets/lang_config.h"   // Lang::Sounds::OGG_POPUP（内置提示音，离线可用）
#include "ota.h"                  // Settings("wifi") 的 ota_url 与服务端契约

#include "elder_bind_guide.h"
#include "elder_net.h"

#define TAG "elder.bind"

// 主循环周期：15s 一次足够跟上「用户在 APP 上完成绑定」这个慢动作，
// 再密只是白耗电与白刷屏。
#define GUIDE_POLL_MS        15000
// 首次播报的等待：Display 就绪后还要等网络连上 + OTA 拿到激活码，
// 12s 是「联网成功」的典型耗时。太早播报等于对着云断开图标说「去绑定」。
#define GUIDE_FIRST_TONE_MS  12000
// 两次提示音之间的最小间隔：老人需要时间去操作，3 分钟一次不扰人。
#define GUIDE_TONE_GAP_MS    180000

// 提示文案（屏显 + 语音提示音的同一份事实来源）。
static const char *const kGuideText =
    "请使用手机APP或者登录网站进行设备绑定";

namespace {

bool s_started = false;
int64_t s_last_tone_us = 0;
char s_last_code[40] = {0};

/* ------------------------------------------------------------ 绑定态 */

/**
 * 设备是否已绑定。
 *
 * 判据 = NVS "websocket" 里的 token 非空。这是**唯一可靠**的绑定信号：
 * ota.cc 的 CheckVersion() 只有在服务端返回了 websocket 段（即已绑定）时
 * 才把它写进 Settings("websocket")。而 elder_net_has_token() 看的是
 * CONFIG_ELDER_SERVER_TOKEN（编译期配置，默认空），那是「配置态」不是「绑定态」。
 */
bool device_is_bound()
{
    Settings settings("websocket", false);
    return !settings.GetString("token").empty();
}

/* ------------------------------------------------------------ 激活码 */

/**
 * 向服务端 OTA 端点要一个激活码。
 *
 * 走的是 ota.cc 同一个 URL（Settings("wifi").ota_url，回落 CONFIG_OTA_URL），
 * 同一个身份头（Device-Id = MAC）。服务端对未绑定设备返回
 *   {"activation": {"code": "...", "message": "..."}}
 * 激活码按 MAC 复用（server/app/voice/__init__.py 的 _ensure_activation_code），
 * 所以重复调用拿到的是同一个码，不会把用户在 APP 里已输过的码作废。
 *
 * @param out 至少 40 字节
 * @return 拿到码返回 true
 */
bool fetch_activation_code(char *out, size_t out_len)
{
    out[0] = '\0';

    auto &board = Board::GetInstance();
    auto network = board.GetNetwork();
    if (!network) {
        ESP_LOGW(TAG, "无网络接口，无法取激活码");
        return false;
    }

    Settings wifi("wifi", false);
    std::string url = wifi.GetString("ota_url");
    if (url.empty()) {
        url = CONFIG_OTA_URL;
    }
    if (url.length() < 10) {
        ESP_LOGE(TAG, "OTA URL 未配置（先配网）");
        return false;
    }

    auto http = network->CreateHttp(0);
    if (!http) {
        ESP_LOGW(TAG, "CreateHttp 失败");
        return false;
    }
    // 头部与 ota.cc SetupHttp() 保持一致：服务端只认 Device-Id。
    http->SetHeader("Device-Id", SystemInfo::GetMacAddress());
    http->SetHeader("Activation-Version", "1");
    http->SetHeader("Client-Id", board.GetUuid());
    http->SetHeader("User-Agent", SystemInfo::GetUserAgent());
    http->SetHeader("Accept-Language", Lang::CODE);

    // GET 而非 POST：这里只要激活码，不需要提交 system info，
    // 少传一次板卡 JSON 在 2C2G 机器上也省点内存。
    if (!http->Open("GET", url)) {
        ESP_LOGW(TAG, "OTA 请求失败 code=0x%x", http->GetLastError());
        return false;
    }
    if (http->GetStatusCode() != 200) {
        ESP_LOGW(TAG, "OTA 返回 %d", http->GetStatusCode());
        http->Close();
        return false;
    }
    std::string body = http->ReadAll();
    http->Close();

    // 轻量解析：只找 "code":"..." 的第一个值。用 strstr 而不是 cJSON 是为了
    // 不给这个「顺手做的小功能」引入一个 JSON 对象的栈开销（PSRAM 虽多，
    // 但老人设备上没理由为一个字符串付出解析器）。
    const char *key = "\"code\":\"";
    size_t kp = body.find(key);
    if (kp == std::string::npos) {
        // 已绑定的设备不会返 code —— 这是正常状态，不是错误。
        ESP_LOGI(TAG, "响应里没有激活码段（可能已绑定）");
        return false;
    }
    kp += strlen(key);
    size_t ke = body.find('"', kp);
    if (ke == std::string::npos) {
        return false;
    }
    std::string code = body.substr(kp, ke - kp);
    if (code.empty() || code.size() >= out_len) {
        return false;
    }
    snprintf(out, out_len, "%s", code.c_str());
    return true;
}

/* ------------------------------------------------------------ 播报 */

/**
 * 屏显引导文案 + 附带激活码（取到时）。
 *
 * 只用 ShowNotification，不碰 SetStatus/SetChatMessage —— 那两个是上游的
 * 状态位，抢过来会和 OTA 激活提示互相顶掉。
 */
void show_guide(const char *code)
{
    auto display = Board::GetInstance().GetDisplay();
    if (!display) {
        return;
    }

    char text[96];
    if (code && code[0]) {
        // 服务端下发的已是 "A-3829-4471-0056" 形态（server/app/security.py
        // 的 format_code），直接拼即可，不必再过 elder_activation_format。
        snprintf(text, sizeof(text), "%s\n激活码 %s", kGuideText, code);
    } else {
        snprintf(text, sizeof(text), "%s\n请检查网络后重试", kGuideText);
    }
    // 25s > 15s 轮询周期，保证两次之间不留空白窗口（老人看到空白会以为死机）。
    display->ShowNotification(text, 25000);
}

/* ------------------------------------------------------------ 主循环 */

void guide_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "绑定引导任务启动");

    bool tone_done = false;

    while (true) {
        // 已绑定 ⇒ 立刻收工，把屏幕让回正常对话流程。
        if (device_is_bound()) {
            ESP_LOGI(TAG, "检测到设备已绑定，绑定引导结束");
            break;
        }

        char code[40] = {0};
        if (fetch_activation_code(code, sizeof(code))) {
            // 码变了才刷屏，避免每 15s 用同样的内容闪一次（闪久了像坏了）。
            if (strcmp(code, s_last_code) != 0) {
                snprintf(s_last_code, sizeof(s_last_code), "%s", code);
                ESP_LOGI(TAG, "已取到激活码: %s", s_last_code);
            }
            show_guide(s_last_code);
        } else {
            show_guide(nullptr);
        }

        // 提示音：首次等网络，第二次起每 3 分钟一次。
        int64_t now = esp_timer_get_time();
        if (!tone_done && now > (int64_t)GUIDE_FIRST_TONE_MS * 1000) {
            auto &app = Application::GetInstance();
            app.PlaySound(Lang::Sounds::OGG_POPUP);
            ESP_LOGI(TAG, "播放绑定引导提示音");
            tone_done = true;
            s_last_tone_us = now;
        } else if (tone_done &&
                   now - s_last_tone_us > (int64_t)GUIDE_TONE_GAP_MS * 1000) {
            Application::GetInstance().PlaySound(Lang::Sounds::OGG_POPUP);
            s_last_tone_us = now;
        }

        vTaskDelay(pdMS_TO_TICKS(GUIDE_POLL_MS));
    }

    auto display = Board::GetInstance().GetDisplay();
    if (display) {
        display->ShowNotification("设备已绑定，正在连接服务", 5000);
    }

    // 任务正常结束，让出栈空间。
    vTaskDelete(NULL);
}

}  // namespace

extern "C" esp_err_t elder_bind_guide_start(void)
{
    if (s_started) {
        return ESP_OK;
    }
    s_started = true;

    // 栈 6144：guide_task 里 Http + std::string 链比纯 NVS 操作深。
    // 栈不够的症状是第一次进循环就随机崩在 esp_http_client 里，
    // 表现为「配好网就重启」，极难归因。
    BaseType_t ok = xTaskCreate(guide_task, "elder_bind_guide", 6144, NULL, 4, NULL);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "引导任务创建失败");
        return ESP_FAIL;
    }
    return ESP_OK;
}