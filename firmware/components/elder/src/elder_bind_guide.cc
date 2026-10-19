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
// ── 与上游 OTA 激活循环的显示争用（策略已变，2026-10-19 重写）──────────
// 旧策略：激活码走 ShowNotification（notification_label_，顶部状态栏），
//   与上游各写各的标签、互不覆盖。
// 新策略：激活码改用 SetChatMessage 落到 chat_message_label_（屏幕下方
//   70% 的大字字幕区）—— 用户 2026-10-19 实测反馈「激活码只语音播报一遍、
//   首字母还没播、希望能显示在屏幕上照着输入」，而顶部状态栏字号小、
//   13 位连写抄错率极高，只有字幕区扛得动「大字 + 分组」。
// 争用真相：上游每轮激活循环会用 Alert() 把 chat_message_label_ 覆盖成
//   服务端下发的 message 文案（application.cc ShowActivationCode → Alert，
//   见 VENDOR.md 冻结镜像）。一轮激活循环 = 1 次 Alert + 10 次 Activate
//   重试（每次间隔 3s/10s）⇒ 上游约 30~100s 才重写一次；本模块每 15s
//   刷新一次，激活码在绝大多数时间里占据屏幕。刻意不去改上游
//   application.cc —— 改动冻结镜像的成本远大于收益。

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
#include "wifi_station.h"  // IsConnected()：lwIP 是否已就绪的唯一可靠判据
#include "assets/lang_config.h"   // Lang::Sounds::OGG_POPUP（内置提示音，离线可用）
#include "ota.h"                  // Settings("wifi") 的 ota_url 与服务端契约

#include "elder_bind_guide.h"
#include "elder_activation.h"   // elder_activation_format：码分组 X-XXXX-XXXX-XXXX（C 接口，.cc 可直接调）
#include "elder_net.h"
#include "config_report.h"

#define TAG "elder.bind"

// 主循环周期：15s 一次足够跟上「用户在 APP 上完成绑定」这个慢动作，
// 再密只是白耗电与白刷屏。
#define GUIDE_POLL_MS        15000
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

/* ------------------------------------------------------------ 网络就绪门控 */

// 网络没连上时是否已经提示过一次（避免 15s 一次地刷屏同一个警告）
static bool s_warned_no_network = false;

/**
 * lwIP / 网络栈是否已经可以安全使用。
 *
 * ── 为什么必须有这个门 ──────────────────────────────────────────────────
 * 本任务在 `app_main()` 里、`app.Start()` **之前**就被创建（未绑定设备上
 * app.Start() 永不返回，只能建在这之前，见文件头注释）。而本任务的优先级 4
 * 高于 app_main 的 1，`xTaskCreate` 返回后**立刻抢占** app_main ——
 * 于是第一次循环跑在 `app.Start()` 内的 `board.StartNetwork()` **之前**。
 *
 * 全工程只有 `WifiStation::Start()` 里那一处 `esp_netif_init()`（它由
 * board.StartNetwork() 调用）。在它之前就发 HTTP，esp_http_client 会去碰
 * lwIP，此时 tcpip_mbox 仍是 SYS_MBOX_NULL，直接触发：
 *
 *   assert failed: tcpip_send_msg_wait_sem ... (Invalid mbox)
 *
 * 这不是"请求失败"，是 **abort** ⇒ 设备无限重启，且每次都崩在同一处。
 * 实测现象正是：背光刚亮（背光是在 Board 构造里点的）就重启，崩溃点约 508ms，
 * 远早于 WiFi 连上所需的 600~700ms。
 *
 * ⚠️ **与「用域名还是用 IP」无关**。换成 152.136.230.132 照样崩：崩的是
 * 「lwIP 没初始化」，不是「DNS 解析不了」。IP 只是免了解析这一步而已。
 *
 * ── 为什么判据用 IsConnected() ─────────────────────────────────────────
 * 它读的是 `WifiStation::Start()` 注册的 IP_EVENT_STA_GOT_IP 事件组位。
 * 那个 handler 只可能在 `esp_netif_init()` 之后注册、只可能在拿到 IP 之后触发，
 * 所以「已连接」蕴含「esp_netif_init 已执行」—— 是一个**充分**条件。
 * 宁可多等几秒，不可早发一次请求换一个重启循环。
 */
static bool network_ready()
{
    bool ready = WifiStation::GetInstance().IsConnected();

    if (!ready && !s_warned_no_network) {
        // 只警告一次：WiFi 重连可能要十几秒，每轮都刷会把串口日志冲掉，
        // 而这段日志恰恰是排查「为什么不取码」的唯一线索。
        ESP_LOGW(TAG, "网络尚未就绪，跳过本轮取码（等 WiFi 拿到 IP 后自动继续）");
        s_warned_no_network = true;
    } else if (ready) {
        if (s_warned_no_network) {
            ESP_LOGI(TAG, "网络已就绪，恢复取码");
            s_warned_no_network = false;
        }
    }
    return ready;
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

    // ⚠️ 门控必须在最前面，且必须在任何 Board/网络访问之前。
    // 原因见 network_ready() 的注释：esp_netif_init() 没跑就发 HTTP
    // = lwIP assert = 无限重启。
    if (!network_ready()) {
        return false;
    }

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

/* ------------------------------------------------------------ 屏显 */

/**
 * 屏显引导文案 + 激活码（取到时），多行落在大字字幕区。
 *
 * 布局（走 SetChatMessage，落在屏幕下方 70%、全宽、LONG_WRAP 的
 * chat_message_label_）：
 *     激活码
 *     A-3829-4471-0056
 *     请使用手机APP或者登录网站进行设备绑定
 *
 * 为什么从 ShowNotification 改成 SetChatMessage（2026-10-19 用户实测）：
 *   激活码原先只语音播报，而内置音效表（assets/lang_config.h）里**没有任何
 *   字母音**，只有 OGG_0..OGG_9 + 固定短语 ⇒ 首字母（如 W）被跳过不播，
 *   用户只听到 12 位数字、照输入必然被 APP 的 13 位校验拒掉。语音侧无解
 *   （补字母音不可行、未绑定设备也没有 TTS 通道），主路径只能是屏显。
 *   notification_label_ 在顶部状态栏、与网络/电池图标同区且字号小；
 *   chat_message_label_ 是唯一的大字区，所以搬到这里。
 *
 * 为什么必须分组：服务端下发的是**原始 13 位码**（如 W298422335600，
 * 无短横线 —— server/app/voice/__init__.py 的 activation.code 原样下发，
 * format_code 只用于服务端日志），不过 elder_activation_format 就上屏
 * 的话，12 位连写在 320x240 上抄错率极高（elder_activation.h 原话）。
 *
 * 与上游争用：每轮激活循环上游会用 Alert 把 chat_message_label_ 覆盖成
 * message 文案；本函数由主循环每 15s 调一次，把激活码重新压回去
 * （详见文件头「与上游 OTA 激活循环的显示争用」一节）。
 */
void show_guide(const char *code)
{
    auto display = Board::GetInstance().GetDisplay();
    if (!display) {
        return;
    }

    // 缓冲区按 UTF-8 字节算：kGuideText 共 54 字节（17 个汉字 + "APP"），
    // 加上标题/分组码/换行最坏 82 字节（已按字节实测），160 留足一倍余量。
    char text[160];
    if (code && code[0]) {
        if (strlen(code) >= ELDER_ACT_CODE_LEN) {
            // elder_activation_format 要求 out 至少 ELDER_ACT_DISPLAY_LEN
            // （17 字节：16 字符 + '\0'），这里逐字对齐，不靠巧合。
            char grouped[ELDER_ACT_DISPLAY_LEN];
            elder_activation_format(code, grouped, sizeof(grouped));
            snprintf(text, sizeof(text), "激活码\n%s\n%s", grouped, kGuideText);
        } else {
            // 码长不足 13 位（服务端异常）：原样显示也比空白有用，
            // 且不会去读 code[12] 越界（format 只在前一个分支调）。
            snprintf(text, sizeof(text), "激活码\n%s\n%s", code, kGuideText);
        }
    } else {
        snprintf(text, sizeof(text), "%s\n请检查网络后重试", kGuideText);
    }
    display->SetChatMessage("system", text);
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
        bool got_code = fetch_activation_code(code, sizeof(code));
        if (got_code) {
            // 码**变了**才更新并记日志（同一 MAC 服务端复用同一个码，正常情况下
            // 每轮都一样，不刷日志）；但 show_guide 每轮都调 —— 上游激活循环
            // 会周期性把 chat_message_label_ 覆盖成 message 文案，只有 15s
            // 级的重刷才能把激活码持续压回屏幕（见文件头争用说明）。
            if (strcmp(code, s_last_code) != 0) {
                snprintf(s_last_code, sizeof(s_last_code), "%s", code);
                ESP_LOGI(TAG, "已取到激活码: %s", s_last_code);
            }
            show_guide(s_last_code);
        } else {
            // 取不到码也照样显示引导文案：老人在等网的这十几秒里，
            // 看到「请用 APP 绑定」比看到空白屏有用得多（后面那句
            // 「请检查网络后重试」已经说明了当前状况）。
            show_guide(nullptr);
        }

        // 提示音：**首次必须等网络真的连上**再播，否则就是对着
        // 「云服务断开」图标喊「去绑定」，反而更让人困惑
        // （这正是 GUIDE_FIRST_TONE_MS 注释里写的意图）。
        // 之前用 esp_timer_get_time()（开机时间）近似，等于只靠"启动够久"
        // 来赌网络已就绪；现在有了 network_ready() 这个真判据，直接用它。
        // 第二声起仍是每 3 分钟一次，与网络无关。
        if (got_code) {
            int64_t now = esp_timer_get_time();
            if (!tone_done) {
                auto &app = Application::GetInstance();
                app.PlaySound(Lang::Sounds::OGG_POPUP);
                ESP_LOGI(TAG, "播放绑定引导提示音（网络已就绪）");
                tone_done = true;
                s_last_tone_us = now;
            } else if (now - s_last_tone_us > (int64_t)GUIDE_TONE_GAP_MS * 1000) {
                Application::GetInstance().PlaySound(Lang::Sounds::OGG_POPUP);
                s_last_tone_us = now;
            }
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

    // 开机配置自检：放在这里是因为本函数是本项目**保证会执行**的最早钩子
    // （在 app.Start() 之前，且已绑定/未绑定两种设备都会走到）。
    // elder_bridge 的日志在未绑定设备上永远看不到（app.Start() 不返回）。
    elder_config_report();

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