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
// 「Board 已构造完成」，也等价于「屏幕驱动就绪」。
//
// ── 激活码到底画在哪（2026-10-20 第三次实测后定稿）──────────────────────
// 板卡 lichuang-dev 的 sdkconfig 是 `CONFIG_USE_DEFAULT_MESSAGE_STYLE=y`
// （`CONFIG_USE_WECHAT_MESSAGE_STYLE` / `CONFIG_USE_EMOTE_MESSAGE_STYLE` 都
// 未开）⇒ 显示类是 SpiLcdDisplay，`LcdDisplay::SetChatMessage()` 落在
// lcd_display.cc 的默认样式分支（1014 行附近），写的是 `chat_message_label_`
// —— 屏幕下方那块高约 184px（屏高 70%）的大字区。**这条路是通的。**
//
// 之前的两次误判（写在这里防止再来一次）：
//   1. 旧实现用 `ShowNotification()` 写顶部状态栏的 notification_label_，
//      字号小、13 位连写，老人抄不动；
//   2. 上一轮改成 SetChatMessage 后仍看不到码，被误判为「固件没带上新代码」。
//      实测反证（命令可复现）：CI run 39278720905（c02727a, firmware-build）
//      的 artifact 12199537589 解压后，在 xiaozhi.bin 里按字节检索
//        b'激活码\n%s\n%s' -> 命中 1 次     （SetChatMessage 版，新）
//        b'激活码 %s'      -> 命中 0 次     （ShowNotification 版，旧）
//      ⇒ 代码确实编进了用户手上的固件。
//      ⚠️ 别拿仓库里的 `dist/release-1.1.0/` 当证据：那是 17:49 的旧构建，
//      里面确实是旧串，照它下结论会把排查方向带偏整整一轮。
//
// ── 真正的根因：绑定态判据用了 NVS，而上游从不清 NVS ─────────────────────
// 旧实现 `device_is_bound()` 只看 NVS `websocket/token` 非空，非空就**立刻
// 退出整个引导任务**。而 ota.cc 的 `ParseResponse()`（164-183 行）在服务端
// 返回 activation 段时只打印 "No websocket section found!"，**从不删除**
// 已存的 token。于是「设备曾在 APP 里绑过 → 后来在 APP 里删除/换库/换服」
// 这类最普通的调试操作，会在设备上留下一个**永久残留的 token**：
//   · 本模块第一轮就 break，激活码一次都没取过；
//   · 上游仍然按「未绑定」走 ShowActivationCode：播数字音、Alert 显示文案。
// 症状与用户反馈逐字吻合：**屏上没有码，语音只有数字**。
//
// ⇒ 判据改成**服务端说了算**：OTA 响应里有 websocket 段才算已绑定；
//   返回 activation 段就是未绑定，哪怕 NVS 里躺着 token。
//
// ── 与上游 OTA 激活循环的显示争用 ──────────────────────────────────────
// 上游每轮激活循环用 Alert() 覆盖 chat_message_label_（application.cc
// ShowActivationCode → Alert），一轮 = 1 次 Alert + 10 次 Activate 重试
// （3s/10s）⇒ 约 100s 覆盖一次；本模块每 15s 取码并重绘一次，占空比约 6/7，
// 激活码在绝大多数时间里占据屏幕。刻意不去改上游 application.cc —— 改动
// 冻结镜像的成本远大于收益。

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
#include "elder_activation.h"   // elder_activation_format：码分组 123-4567（C 接口，.cc 可直接调）
#include "elder_net.h"
#include "config_report.h"

#define TAG "elder.bind"

// 取码 + 重绘周期：15s 一次足够跟上「用户在 APP 上完成绑定」这个慢动作。
// 上游 Alert() 一轮约 100s（10 次 Activate 重试 × 10s 间隔）才覆盖一次，
// 15s 重绘的占空比约 6/7，码能稳定留在屏上 —— 不必做到秒级。
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

/* ------------------------------------------------------------ 探针结果 */

/**
 * 一轮探测的结论。**绑定态只能由服务端回答**，见文件头「真正的根因」。
 */
enum class Probe {
    kBound,      // 服务端下发了 websocket 段 ⇒ 真已绑定，收工
    kUnbound,    // 服务端下发了 activation.code ⇒ 未绑定，把码画上去
    kNoNetwork,  // WiFi 还没拿到 IP，本轮什么都别做
    kError,      // 网络通了但请求/解析失败，只显示引导文案
};

/* ------------------------------------------------------------ 网络就绪门控 */

// 网络没连上时是否已经提示过一次（避免每轮刷屏同一个警告）
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
 * 向服务端 OTA 端点要一个激活码，并**同时判定绑定态**。
 *
 * 走的是 ota.cc 同一个 URL（Settings("wifi").ota_url，回落 CONFIG_OTA_URL），
 * 同一个身份头（Device-Id = MAC）。服务端对未绑定设备返回
 *   {"activation": {"code": "...", "message": "..."}}
 * 对已绑定设备返回 `{"websocket": {...}}`（且**没有** activation 段）。
 *
 * 激活码按 MAC 复用（server/app/voice/__init__.py 的 _ensure_activation_code），
 * 所以重复调用拿到的是同一个码，不会把用户在 APP 里已输过的码作废。
 *
 * @param out     至少 40 字节
 * @return 见 Probe；只有 kUnbound 时 out 里才有码
 */
Probe fetch_activation_code(char *out, size_t out_len)
{
    out[0] = '\0';

    // ⚠️ 门控必须在最前面，且必须在任何 Board/网络访问之前。
    // 原因见 network_ready() 的注释：esp_netif_init() 没跑就发 HTTP
    // = lwIP assert = 无限重启。
    if (!network_ready()) {
        return Probe::kNoNetwork;
    }

    auto &board = Board::GetInstance();
    auto network = board.GetNetwork();
    if (!network) {
        ESP_LOGW(TAG, "无网络接口，无法取激活码");
        return Probe::kError;
    }

    Settings wifi("wifi", false);
    std::string url = wifi.GetString("ota_url");
    if (url.empty()) {
        url = CONFIG_OTA_URL;
    }
    if (url.length() < 10) {
        ESP_LOGE(TAG, "OTA URL 未配置（先配网）");
        return Probe::kError;
    }

    auto http = network->CreateHttp(0);
    if (!http) {
        ESP_LOGW(TAG, "CreateHttp 失败");
        return Probe::kError;
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
        return Probe::kError;
    }
    if (http->GetStatusCode() != 200) {
        ESP_LOGW(TAG, "OTA 返回 %d", http->GetStatusCode());
        http->Close();
        return Probe::kError;
    }
    std::string body = http->ReadAll();
    http->Close();

    // 轻量解析：只找 "code":"..." 的第一个值。用 find 而不是 cJSON 是为了
    // 不给这个「顺手做的小功能」引入一个 JSON 对象的栈开销。
    const char *key = "\"code\":\"";
    size_t kp = body.find(key);
    if (kp == std::string::npos) {
        // 没有 code 段：可能是已绑定（正常），也可能是服务端换了错误格式。
        // 判据用 websocket 段 —— 与服务端 ota_check 的分支一一对应。
        if (body.find("\"websocket\"") != std::string::npos) {
            return Probe::kBound;
        }
        ESP_LOGW(TAG, "响应里既没有激活码段也没有 websocket 段，无法判定绑定态");
        return Probe::kError;
    }
    kp += strlen(key);
    size_t ke = body.find('"', kp);
    if (ke == std::string::npos) {
        return Probe::kError;
    }
    std::string code = body.substr(kp, ke - kp);
    if (code.empty() || code.size() >= out_len) {
        return Probe::kError;
    }
    snprintf(out, out_len, "%s", code.c_str());
    return Probe::kUnbound;
}

/* ------------------------------------------------------------ 屏显 */

/**
 * 屏显激活码（取到时）+ 引导文案，落在下方大字字幕区。
 *
 * 布局（SetChatMessage → 默认样式分支 → chat_message_label_，
 * 全宽、LV_LABEL_LONG_WRAP、高约屏高 70%）：
 *     激活码
 *     123-4567
 *     请使用手机APP或者登录网站进行设备绑定
 *
 * 为什么必须分组：服务端下发的是**原始 7 位码**（如 4827193，无短横线 ——
 * server/app/voice/__init__.py 的 activation.code 原样下发，format_code
 * 只用于服务端日志与 APP 展示），不过 elder_activation_format 就上屏的话，
 * 7 位连写在 320x240 上照样容易抄错。
 *
 * 为什么全数字（2026-10-20）：内置音效表只有数字音，字母根本播不出来，
 * 见 elder_activation.h 的注释与 docs/决策记录.md D42。
 */
void show_guide(const char *code)
{
    auto display = Board::GetInstance().GetDisplay();
    if (!display) {
        return;
    }

    // 缓冲区按 UTF-8 字节算：kGuideText 共 54 字节（17 个汉字 + "APP"），
    // 加上标题/分组码/换行最坏 82 字节，160 留足一倍余量。
    char text[160];
    if (code && code[0]) {
        if (strlen(code) >= ELDER_ACT_CODE_LEN) {
            // elder_activation_format 要求 out 至少 ELDER_ACT_DISPLAY_LEN
            // （9 字节："123-4567" + '\0'），这里逐字对齐，不靠巧合。
            char grouped[ELDER_ACT_DISPLAY_LEN];
            elder_activation_format(code, grouped, sizeof(grouped));
            snprintf(text, sizeof(text), "激活码\n%s\n%s", grouped, kGuideText);
        } else {
            // 码长不足（服务端异常）：原样显示也比空白有用。
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
    int64_t last_fetch_us = 0;
    bool have_code = false;

    // NVS 里有 token 只作为**「可能已绑定」的初值**，不是判据：
    // 真已绑定的设备上不该让引导去刷屏（会把正常对话字幕顶掉），
    // 但一旦服务端明确说「未绑定」，就说明这个 token 是残骸，照常上屏。
    Settings ws_probe("websocket", false);
    bool may_paint = ws_probe.GetString("token").empty();

    while (true) {
        int64_t now = esp_timer_get_time();
        bool do_fetch = (last_fetch_us == 0) ||
                        (now - last_fetch_us >= (int64_t)GUIDE_POLL_MS * 1000);

        if (do_fetch) {
            last_fetch_us = now;
            char code[40] = {0};
            Probe p = fetch_activation_code(code, sizeof(code));

            if (p == Probe::kBound) {
                // 只有**服务端确认**已绑定才收工。NVS 里的 token 可能是
                // 「APP 里删过设备」留下的残骸，不能当判据（见文件头）。
                ESP_LOGI(TAG, "服务端确认已绑定，绑定引导结束");
                break;
            }

            if (p == Probe::kUnbound) {
                if (!may_paint) {
                    // 走到这里 = NVS 有 token 但服务端说未绑定 ⇒ 残骸。
                    //
                    // ⚠️ **刻意不擦 token**。擦除对「让码上屏」零贡献（上屏只看
                    // may_paint），却有两个真实副作用：
                    //   1. 设备侧没有通知通道（Display::ShowNotification 是只打
                    //      日志的桩），被登出对老人是**完全静默**的；
                    //   2. 服务端 device_id 存在「固件带冒号 / 存量不带冒号」的
                    //      格式分叉（system_info.cc vs models.Device 注释），
                    //      擦掉后可能**再也拿不回新 token**，设备永久不可用。
                    // ⇒ 只记日志、按未绑定继续上屏；擦除等 MAC 归一化落地后再开。
                    ESP_LOGW(TAG, "检测到 NVS 残骸 token（服务端判定未绑定），"
                                  "按未绑定处理；不擦除，避免静默登出");
                    may_paint = true;
                }
                have_code = true;
                if (strcmp(code, s_last_code) != 0) {
                    snprintf(s_last_code, sizeof(s_last_code), "%s", code);
                    ESP_LOGI(TAG, "已取到激活码: %s", s_last_code);
                }
            } else if (p == Probe::kNoNetwork) {
                // 显式分支：只是"还没连上网"，不是错误，别和 kError 混在一起刷日志
                ESP_LOGD(TAG, "网络未就绪，本轮不上屏、不计失败");
            } else if (p == Probe::kError) {
                // 取不到码也保留上一次的码：网络抖一下不该把屏上的码抹掉。
                ESP_LOGW(TAG, "本轮取码失败（保留上一枚码=%s）",
                         have_code ? s_last_code : "无");
            }

            // 提示音：**首次必须等网络真的连上**再播（kUnbound 已蕴含）。
            // 第二声起每 3 分钟一次，与网络无关。
            if (p == Probe::kUnbound) {
                if (!tone_done) {
                    Application::GetInstance().PlaySound(Lang::Sounds::OGG_POPUP);
                    ESP_LOGI(TAG, "播放绑定引导提示音（网络已就绪）");
                    tone_done = true;
                    s_last_tone_us = now;
                } else if (now - s_last_tone_us > (int64_t)GUIDE_TONE_GAP_MS * 1000) {
                    Application::GetInstance().PlaySound(Lang::Sounds::OGG_POPUP);
                    s_last_tone_us = now;
                }
            }
        }

        // **每轮都重绘**：上游 Alert() 每约 100s 覆盖一次同一标签，
        // 15s 重绘足以把码压回去；取码失败时也重绘上一枚码，网络抖一下
        // 不该把屏上的码抹掉。may_paint 为假（疑似已绑定且尚未被服务端
        // 否定）时一个字都不写，免得把正常对话的字幕顶掉。
        if (may_paint) {
            show_guide(have_code ? s_last_code : nullptr);
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
