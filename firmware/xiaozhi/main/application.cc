#include "application.h"
#include "board.h"
#include "display.h"
#include "system_info.h"
#include "audio_codec.h"
#include "websocket_protocol.h"
#include "assets/lang_config.h"
#include "mcp_server.h"
#include "assets.h"
#include "settings.h"

#include <cstring>
#include <esp_log.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <nvs.h>
#include <cJSON.h>
#include <driver/gpio.h>
#include <arpa/inet.h>
#include <font_awesome.h>

#define TAG "Application"

/* ---- 字幕对齐参数（2026-11-03 起，2026-11-04 收紧兜底）-------------------
 * 轮询 50ms：一帧音频 60ms，所以「判据成立」到「真的画上去」之间最多
 * 差 50ms，老人感知不到。再密就是白烧 CPU（LVGL 刷一屏也要时间）。
 *
 * 兜底 1 秒（2026-11-04 从 6 秒收紧，用户反馈「字幕刷新率很低，完全跟不上
 * 语音」）：
 *   门限本身是对的 —— `played > frame_mark` 等的是"这句第一个字真正出声"，
 *   这段等待不是延迟，是**同步**，别把它当成 bug 去掉。
 *   真正的问题是 6 秒这个兜底：解码队列 40 包 + 播放队列 16 包 ≈ 56 帧 ×
 *   60ms ≈ 3.36 秒的积压，一旦音频迟迟不播（TTS 失败 / 丢帧 / 被打断），
 *   字幕会被一路拖到 3 秒以上，看起来就是"字追不上声音"。
 *   1 秒的含义是"超过一句正常起播时间还没声音 ⇒ 本句大概率没有音频"，
 *   此时宁可字早一点也不让它卡在队列里 —— 但那是**异常路径**，不是常态
 *   等待。超时上屏仍打 WARNING，让这个数在串口日志里可数。 */
static constexpr int kSubtitlePollMs = 50;
static constexpr int64_t kSubtitleMaxWaitUs = 1LL * 1000 * 1000;

// >>> BEGIN elder-companion overlay >>>
// VAD 边沿（老人开口 / 说完）转发给 elder 灯效模块。
// 定义在 components/elder/src/elder_bridge.cc；elder 组件没有对外头文件，
// 沿用本仓既有做法（见 mcp_server.cc 的裸 extern "C" 声明）。
extern "C" void elder_on_vad_change(bool speaking);
// <<< END elder-companion overlay <<<

/* ---------------------------------------------------------------- 启动诊断
 * 缘由（2026-10-20）：设备会在运行 30 秒~2 分钟后**自发重启**，但串口抓不到
 * ROM 打印的 `rst:0x..(RESET_REASON)` 那一行。
 *
 * 真因不在固件：本板是 ESP32-S3 的**原生 USB-Serial-JTAG**，复位瞬间 USB
 * 会重新枚举，主机侧 COM 句柄随之失效，开机头（约 0~16 秒）整段丢失。
 * 三次抓取的起点分别是 16.1s / 0.0s(由 esptool 主动复位) —— 自发重启那几次
 * 全都从 16 秒处才开始有日志，正好把复位原因漏掉。
 *
 * 所以改为**固件自己解码**，并且在启动后 2 分钟内每 10 秒重打一次：
 * 这样任何一个抓取窗口都能看到，不依赖抢在复位瞬间连着串口。
 *
 * 判读表（见 ResetReasonName）：BROWNOUT=供电不足；PANIC=程序崩溃（多为
 * 内存分配失败后 abort）；TASK_WDT/INT_WDT=有任务卡住；SW=有代码主动重启。
 * ---------------------------------------------------------------------- */
static const char* ResetReasonName(esp_reset_reason_t r) {
    switch (r) {
        case ESP_RST_POWERON:   return "POWERON(上电/拔插USB)";
        case ESP_RST_EXT:       return "EXT(外部复位脚)";
        case ESP_RST_SW:        return "SW(软件调用 esp_restart)";
        case ESP_RST_PANIC:     return "PANIC(崩溃/abort，多为内存分配失败)";
        case ESP_RST_INT_WDT:   return "INT_WDT(中断看门狗)";
        case ESP_RST_TASK_WDT:  return "TASK_WDT(任务看门狗，有任务卡住)";
        case ESP_RST_WDT:       return "WDT(其它看门狗)";
        case ESP_RST_DEEPSLEEP: return "DEEPSLEEP(深睡唤醒)";
        case ESP_RST_BROWNOUT:  return "BROWNOUT(欠压！供电不足)";
        case ESP_RST_SDIO:      return "SDIO(复位)";
        default:                return "UNKNOWN(未知)";
    }
}

static char s_reset_reason[64] = "未知";
static uint32_t s_boot_count = 0;
/* 开机时刻（esp_timer 微秒）。**必须独立记录，不能复用 clock_ticks_**：
 * clock_ticks_ 在 SetDeviceState() 里每次状态切换都清零（idle→connecting→listening
 * 唤醒一次就清两次），拿它当「已运行秒数」会把唤醒后的日志打成「已运行=10s」，
 * 与真实运行时间矛盾 —— 2026-10-20 就因此把「没重启」误判成「重启了一次」。
 * esp_timer 是单调时钟，不随状态机变化，才是运行时长的正确来源。 */
static int64_t s_boot_us = 0;

/* 只在开机调一次：解码复位原因 + 累加启动次数（存 NVS，跨重启可累计）。
 * 启动次数是判断「偶发重启」还是「重启循环」的关键证据。 */
static void BootDiagInit() {
    s_boot_us = esp_timer_get_time();
    snprintf(s_reset_reason, sizeof(s_reset_reason), "%s",
             ResetReasonName(esp_reset_reason()));

    nvs_handle_t h;
    if (nvs_open("diag", NVS_READWRITE, &h) == ESP_OK) {
        uint32_t v = 0;
        nvs_get_u32(h, "boot", &v);
        v++;
        nvs_set_u32(h, "boot", v);
        nvs_commit(h);
        nvs_close(h);
        s_boot_count = v;
    }
    ESP_LOGW("BootDiag", "复位原因=%s 累计启动=%u 次 最小空闲堆=%u 字节",
             s_reset_reason, (unsigned)s_boot_count,
             (unsigned)esp_get_minimum_free_heap_size());

    /* NVS 余量：2026-10-27 语音调音量重启的现场是
     *   nvs_open("audio", RW) 失败 → 句柄 0 → nvs_set_i32 返回 0x1107 → abort。
     * 当时 nvs 分区只有 0x4000（16KB，3 个可用页），而 elder 的两条 blob
     * （evtq 约 90 entry、提醒表约 52 entry）几乎吃满了全部余量 ——
     * 实测「已用 325 / 可用 53」，evtq 一次重写都写不进去。
     * **2026-10-29 已扩到 0x10000（64KB，16 页，约 2016 entry，3.75 倍）。**
     * 这行日志仍是判断"NVS 是否又快满"的唯一直接证据，别删。 */
    nvs_stats_t st;
    if (nvs_get_stats(NULL, &st) == ESP_OK) {
        ESP_LOGW("BootDiag", "NVS: 已用 %u / 共 %u 条，可用 %u 条，命名空间 %u 个"
                             "（扩容后约 2016 条；可用长期 <100 才需要再看）",
                 (unsigned)st.used_entries, (unsigned)st.total_entries,
                 (unsigned)st.available_entries, (unsigned)st.namespace_count);
    } else {
        ESP_LOGE("BootDiag", "nvs_get_stats 失败：NVS 分区可能未初始化");
    }
}


static const char* const STATE_STRINGS[] = {
    "unknown",
    "starting",
    "configuring",
    "idle",
    "connecting",
    "listening",
    "speaking",
    "upgrading",
    "activating",
    "audio_testing",
    "fatal_error",
    "invalid_state"
};

Application::Application() {
    event_group_ = xEventGroupCreate();

#if CONFIG_USE_DEVICE_AEC && CONFIG_USE_SERVER_AEC
#error "CONFIG_USE_DEVICE_AEC and CONFIG_USE_SERVER_AEC cannot be enabled at the same time"
#elif CONFIG_USE_DEVICE_AEC
    aec_mode_ = kAecOnDeviceSide;
#elif CONFIG_USE_SERVER_AEC
    aec_mode_ = kAecOnServerSide;
#else
    aec_mode_ = kAecOff;
#endif

    esp_timer_create_args_t clock_timer_args = {
        .callback = [](void* arg) {
            Application* app = (Application*)arg;
            xEventGroupSetBits(app->event_group_, MAIN_EVENT_CLOCK_TICK);
        },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "clock_timer",
        .skip_unhandled_events = true
    };
    esp_timer_create(&clock_timer_args, &clock_timer_handle_);

    /* 字幕对齐轮询器：只在有 pending 字幕时才跑（见 QueueSubtitle）。
     * 回调只做一件事 —— 把「检查」丢回主任务，避免在 esp_timer 任务里
     * 碰 display / pending_subtitles_。 */
    esp_timer_create_args_t subtitle_timer_args = {
        .callback = [](void* arg) {
            Application* app = (Application*)arg;
            app->Schedule([app]() { app->FlushSubtitles(false); });
        },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "subtitle_sync",
        .skip_unhandled_events = true
    };
    esp_timer_create(&subtitle_timer_args, &subtitle_timer_handle_);
}

Application::~Application() {
    if (clock_timer_handle_ != nullptr) {
        esp_timer_stop(clock_timer_handle_);
        esp_timer_delete(clock_timer_handle_);
    }
    if (subtitle_timer_handle_ != nullptr) {
        esp_timer_stop(subtitle_timer_handle_);
        esp_timer_delete(subtitle_timer_handle_);
    }
    vEventGroupDelete(event_group_);
}

/* ---------------------------------------------------------------- 字幕对齐
 * 三件事：入队（收到 sentence_start）、判据成立就上屏（轮询）、
 * 一轮结束时收尾（tts stop 强制清空 / tts start 丢弃上一轮残句）。
 * 详见 application.h 里 PendingSubtitle 的说明。
 * ---------------------------------------------------------------------- */
void Application::QueueSubtitle(const std::string& text, int frame_mark) {
    pending_subtitles_.push_back(PendingSubtitle{text, frame_mark, esp_timer_get_time()});
    if (subtitle_timer_handle_ != nullptr &&
        !esp_timer_is_active(subtitle_timer_handle_)) {
        esp_timer_start_periodic(subtitle_timer_handle_, kSubtitlePollMs * 1000);
    }
    // 门可能已经开了（本句音频先于字幕到达 / 队列本来就是空的），
    // 那就立刻上屏，不要白等一个 50ms 周期。
    FlushSubtitles(false);
}

void Application::FlushSubtitles(bool force) {
    auto display = Board::GetInstance().GetDisplay();
    if (display == nullptr) {
        pending_subtitles_.clear();
        return;
    }

    const int played = audio_service_.PlayedFrameCount();
    const int64_t now_us = esp_timer_get_time();

    while (!pending_subtitles_.empty()) {
        auto& p = pending_subtitles_.front();
        const int64_t waited_us = now_us - p.queued_us;
        const bool audio_started = played > p.frame_mark;
        const bool timed_out = waited_us >= kSubtitleMaxWaitUs;
        if (!audio_started && !timed_out && !force) {
            break;
        }
        if (!audio_started) {
            /* 走到这里只有两种可能：本句没有音频（TTS 失败/丢帧/打断），
             * 或者这一轮被强制收尾。两者都说明「等声音」这条路走不通了，
             * 必须让字先出来 —— 但要在日志里留下证据，否则下次调参时
             * 又只能靠猜。
             *
             * ⚠️⚠️ 2026-11-04 真机崩溃：这里原来写的是 `%lld` + `(long long)`，
             * 本工程开了 CONFIG_NEWLIB_NANO_FORMAT=y，**nano-printf 不支持
             * `%lld`** ⇒ va_arg 错位 ⇒ 把某个整数当成指针去读 ⇒
             * LoadProhibited（EXCVADDR=0），崩在 _printf_i 里。
             * 用 map 解 backtrace 得到的调用链：
             *   _printf_i ← _vfprintf_r ← vprintf ← esp_log ←
             *   Application::FlushSubtitles ← MainEventLoop
             * 触发时刻：一句 TTS 字幕正好上屏（09:22:46）。
             * 同一个坑 ota.cc:251 早就写过一遍（那里用的是 `%ld`），
             * 字幕代码是后来加的，又踩回来了 —— 所以现在有静态门禁
             * `tools/smoke_fw_printf_fmt.py` 兜住。 */
            ESP_LOGW(TAG, "subtitle shown without audio: mark=%d played=%d waited=%dms%s",
                     p.frame_mark, played, (int)(waited_us / 1000),
                     force ? " (forced)" : " (timeout)");
        } else {
            ESP_LOGI(TAG, "subtitle synced: waited=%dms mark=%d played=%d",
                     (int)(waited_us / 1000), p.frame_mark, played);
        }
        display->SetChatMessage("assistant", p.text.c_str());
        pending_subtitles_.pop_front();
    }

    if (pending_subtitles_.empty() && subtitle_timer_handle_ != nullptr &&
        esp_timer_is_active(subtitle_timer_handle_)) {
        esp_timer_stop(subtitle_timer_handle_);
    }
}

void Application::ClearPendingSubtitles() {
    pending_subtitles_.clear();
    if (subtitle_timer_handle_ != nullptr && esp_timer_is_active(subtitle_timer_handle_)) {
        esp_timer_stop(subtitle_timer_handle_);
    }
}

void Application::CheckAssetsVersion() {
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto& assets = Assets::GetInstance();

    if (!assets.partition_valid()) {
        ESP_LOGW(TAG, "Assets partition is disabled for board %s", BOARD_NAME);
        return;
    }
    
    Settings settings("assets", true);
    // Check if there is a new assets need to be downloaded
    std::string download_url = settings.GetString("download_url");

    if (!download_url.empty()) {
        settings.EraseKey("download_url");

        char message[256];
        snprintf(message, sizeof(message), Lang::Strings::FOUND_NEW_ASSETS, download_url.c_str());
        Alert(Lang::Strings::LOADING_ASSETS, message, "cloud_arrow_down", Lang::Sounds::OGG_UPGRADE);
        
        // Wait for the audio service to be idle for 3 seconds
        vTaskDelay(pdMS_TO_TICKS(3000));
        SetDeviceState(kDeviceStateUpgrading);
        board.SetPowerSaveMode(false);
        display->SetChatMessage("system", Lang::Strings::PLEASE_WAIT);

        bool success = assets.Download(download_url, [display](int progress, size_t speed) -> void {
            std::thread([display, progress, speed]() {
                char buffer[32];
                snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
                display->SetChatMessage("system", buffer);
            }).detach();
        });

        board.SetPowerSaveMode(true);
        vTaskDelay(pdMS_TO_TICKS(1000));

        if (!success) {
            Alert(Lang::Strings::ERROR, Lang::Strings::DOWNLOAD_ASSETS_FAILED, "circle_xmark", Lang::Sounds::OGG_EXCLAMATION);
            vTaskDelay(pdMS_TO_TICKS(2000));
            return;
        }
    }

    // Apply assets
    assets.Apply();
    display->SetChatMessage("system", "");
    display->SetEmotion("microchip_ai");
}

void Application::CheckNewVersion(Ota& ota) {
    const int MAX_RETRY = 10;
    int retry_count = 0;
    int retry_delay = 10; // 初始重试延迟为10秒

    auto& board = Board::GetInstance();
    while (true) {
        SetDeviceState(kDeviceStateActivating);
        auto display = board.GetDisplay();
        display->SetStatus(Lang::Strings::CHECKING_NEW_VERSION);

        esp_err_t err = ota.CheckVersion();
        if (err != ESP_OK) {
            retry_count++;
            if (retry_count >= MAX_RETRY) {
                ESP_LOGE(TAG, "Too many retries, exit version check");
                return;
            }

            char error_message[128];
            snprintf(error_message, sizeof(error_message), "code=%d, url=%s", err, ota.GetCheckVersionUrl().c_str());
            char buffer[256];
            snprintf(buffer, sizeof(buffer), Lang::Strings::CHECK_NEW_VERSION_FAILED, retry_delay, error_message);
            Alert(Lang::Strings::ERROR, buffer, "cloud_slash", Lang::Sounds::OGG_EXCLAMATION);

            ESP_LOGW(TAG, "Check new version failed, retry in %d seconds (%d/%d)", retry_delay, retry_count, MAX_RETRY);
            for (int i = 0; i < retry_delay; i++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                if (device_state_ == kDeviceStateIdle) {
                    break;
                }
            }
            retry_delay *= 2; // 每次重试后延迟时间翻倍
            continue;
        }
        retry_count = 0;
        retry_delay = 10; // 重置重试延迟时间

        if (ota.HasNewVersion()) {
            if (UpgradeFirmware(ota)) {
                return; // This line will never be reached after reboot
            }
            // If upgrade failed, continue to normal operation (don't break, just fall through)
        }

        // No new version, mark the current version as valid
        ota.MarkCurrentVersionValid();
        if (!ota.HasActivationCode() && !ota.HasActivationChallenge()) {
            xEventGroupSetBits(event_group_, MAIN_EVENT_CHECK_NEW_VERSION_DONE);
            // Exit the loop if done checking new version
            break;
        }

        display->SetStatus(Lang::Strings::ACTIVATION);
        // Activation code is shown to the user and waiting for the user to input
        if (ota.HasActivationCode()) {
            ShowActivationCode(ota.GetActivationCode(), ota.GetActivationMessage());
        }

        // This will block the loop until the activation is done or timeout
        for (int i = 0; i < 10; ++i) {
            ESP_LOGI(TAG, "Activating... %d/%d", i + 1, 10);
            esp_err_t err = ota.Activate();
            if (err == ESP_OK) {
                xEventGroupSetBits(event_group_, MAIN_EVENT_CHECK_NEW_VERSION_DONE);
                break;
            } else if (err == ESP_ERR_TIMEOUT) {
                vTaskDelay(pdMS_TO_TICKS(3000));
            } else {
                vTaskDelay(pdMS_TO_TICKS(10000));
            }
            if (device_state_ == kDeviceStateIdle) {
                break;
            }
        }
    }
}

void Application::ShowActivationCode(const std::string& code, const std::string& message) {
    struct digit_sound {
        char digit;
        const std::string_view& sound;
    };
    static const std::array<digit_sound, 10> digit_sounds{{
        digit_sound{'0', Lang::Sounds::OGG_0},
        digit_sound{'1', Lang::Sounds::OGG_1}, 
        digit_sound{'2', Lang::Sounds::OGG_2},
        digit_sound{'3', Lang::Sounds::OGG_3},
        digit_sound{'4', Lang::Sounds::OGG_4},
        digit_sound{'5', Lang::Sounds::OGG_5},
        digit_sound{'6', Lang::Sounds::OGG_6},
        digit_sound{'7', Lang::Sounds::OGG_7},
        digit_sound{'8', Lang::Sounds::OGG_8},
        digit_sound{'9', Lang::Sounds::OGG_9}
    }};

    // This sentence uses 9KB of SRAM, so we need to wait for it to finish
    Alert(Lang::Strings::ACTIVATION, message.c_str(), "link", Lang::Sounds::OGG_ACTIVATION);

    for (const auto& digit : code) {
        auto it = std::find_if(digit_sounds.begin(), digit_sounds.end(),
            [digit](const digit_sound& ds) { return ds.digit == digit; });
        if (it != digit_sounds.end()) {
            audio_service_.PlaySound(it->sound);
        }
    }
}

void Application::Alert(const char* status, const char* message, const char* emotion, const std::string_view& sound) {
    ESP_LOGW(TAG, "Alert [%s] %s: %s", emotion, status, message);
    auto display = Board::GetInstance().GetDisplay();
    display->SetStatus(status);
    display->SetEmotion(emotion);
    display->SetChatMessage("system", message);
    if (!sound.empty()) {
        audio_service_.PlaySound(sound);
    }
}

void Application::DismissAlert() {
    if (device_state_ == kDeviceStateIdle) {
        auto display = Board::GetInstance().GetDisplay();
        display->SetStatus(Lang::Strings::STANDBY);
        display->SetEmotion("neutral");
        display->SetChatMessage("system", "");
    }
}

void Application::ToggleChatState() {
    if (device_state_ == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (device_state_ == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    } else if (device_state_ == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }

    if (device_state_ == kDeviceStateIdle) {
        Schedule([this]() {
            if (!protocol_->IsAudioChannelOpened()) {
                SetDeviceState(kDeviceStateConnecting);
                if (!protocol_->OpenAudioChannel()) {
                    return;
                }
            }

            SetListeningMode(aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime);
        });
    } else if (device_state_ == kDeviceStateSpeaking) {
        Schedule([this]() {
            AbortSpeaking(kAbortReasonNone);
        });
    } else if (device_state_ == kDeviceStateListening) {
        Schedule([this]() {
            protocol_->CloseAudioChannel();
        });
    }
}

void Application::WakeUp() {
    if (device_state_ == kDeviceStateSpeaking) {
        /* 打断 + 立刻收听。
         *
         * 收听模式必须与 ToggleChatState 的待机分支用**同一个判据**：
         * 若这里固定用 kListeningModeManualStop，就会出现"唤醒进来是自动
         * 停止、打断进来却要再按一次才停"的不一致 —— 老人记不住，而且
         * 走开后设备会一直开着麦收音。
         * 用 AutoStop 的语义是：老人说完，静音一会儿就自己结束。 */
        Schedule([this]() {
            AbortSpeaking(kAbortReasonNone);
            SetListeningMode(aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime);
        });
        return;
    }
    /* 其余状态（待机→收听、收听→停止、配网等）复用既有逻辑，
     * 不在这里另写一份，避免两处行为随时间分叉。 */
    ToggleChatState();
}

void Application::StartListening() {
    if (device_state_ == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (device_state_ == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }
    
    if (device_state_ == kDeviceStateIdle) {
        Schedule([this]() {
            if (!protocol_->IsAudioChannelOpened()) {
                SetDeviceState(kDeviceStateConnecting);
                if (!protocol_->OpenAudioChannel()) {
                    return;
                }
            }

            SetListeningMode(kListeningModeManualStop);
        });
    } else if (device_state_ == kDeviceStateSpeaking) {
        Schedule([this]() {
            AbortSpeaking(kAbortReasonNone);
            SetListeningMode(kListeningModeManualStop);
        });
    }
}

void Application::StopListening() {
    if (device_state_ == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    }

    const std::array<int, 3> valid_states = {
        kDeviceStateListening,
        kDeviceStateSpeaking,
        kDeviceStateIdle,
    };
    // If not valid, do nothing
    if (std::find(valid_states.begin(), valid_states.end(), device_state_) == valid_states.end()) {
        return;
    }

    Schedule([this]() {
        if (device_state_ == kDeviceStateListening) {
            protocol_->SendStopListening();
            SetDeviceState(kDeviceStateIdle);
        }
    });
}

void Application::Start() {
    auto& board = Board::GetInstance();
    SetDeviceState(kDeviceStateStarting);

    /* 复位原因必须在最早的时机解码：此时 nvs_flash_init() 已由 main 完成，
     * 之后再取 esp_reset_reason() 也仍是同一次开机的值，但越早越不会被
     * 后续初始化把它掩盖。 */
    BootDiagInit();

    /* Setup the display */
    auto display = board.GetDisplay();

    // Print board name/version info
    display->SetChatMessage("system", SystemInfo::GetUserAgent().c_str());

    /* Setup the audio service */
    auto codec = board.GetAudioCodec();
    audio_service_.Initialize(codec);
    audio_service_.Start();

    AudioServiceCallbacks callbacks;
    callbacks.on_send_queue_available = [this]() {
        xEventGroupSetBits(event_group_, MAIN_EVENT_SEND_AUDIO);
    };
    callbacks.on_wake_word_detected = [this](const std::string& wake_word) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_WAKE_WORD_DETECTED);
    };
    callbacks.on_vad_change = [this](bool speaking) {
        /* >>> BEGIN elder-companion overlay >>>
         * 灯效要区分「在听老人说话」和「老人说完了、在等服务端回复」。
         * 后者在设备状态机里**没有对应状态**（realtime 模式全程停在
         * kDeviceStateListening，服务端 VAD 才决定何时开口），
         * 唯一可用的信号就是这里。回调跑在音频处理任务里，只做一次
         * 互斥锁保护的灯效请求，不碰音频数据。
         * 声明沿用 mcp_server.cc 的裸 extern "C" 模式（elder 组件没有对外头文件）。 */
        elder_on_vad_change(speaking);
        // <<< END elder-companion overlay <<<
        xEventGroupSetBits(event_group_, MAIN_EVENT_VAD_CHANGE);
    };
    audio_service_.SetCallbacks(callbacks);

    // Start the main event loop task with priority 3
    xTaskCreate([](void* arg) {
        ((Application*)arg)->MainEventLoop();
        vTaskDelete(NULL);
    }, "main_event_loop", 2048 * 4, this, 3, &main_event_loop_task_handle_);

    /* Start the clock timer to update the status bar */
    esp_timer_start_periodic(clock_timer_handle_, 1000000);

    /* Wait for the network to be ready */
    board.StartNetwork();

    // Update the status bar immediately to show the network state
    display->UpdateStatusBar(true);

    // Check for new assets version
    CheckAssetsVersion();

    // Check for new firmware version
    Ota ota;
    CheckNewVersion(ota);

    // Initialize the protocol
    display->SetStatus(Lang::Strings::LOADING_PROTOCOL);

    // Add MCP common tools before initializing the protocol
    auto& mcp_server = McpServer::GetInstance();
    mcp_server.AddCommonTools();
    mcp_server.AddUserOnlyTools();

    /* 2026-10-22 MQTT 协议删除（用户确认固定走 WebSocket）。
     * OTA 配置的 MQTT 分支不再可达；统一走 WebSocketProtocol。 */
    protocol_ = std::make_unique<WebsocketProtocol>();

    protocol_->OnConnected([this]() {
        DismissAlert();
    });

    protocol_->OnNetworkError([this](const std::string& message) {
        last_error_message_ = message;
        xEventGroupSetBits(event_group_, MAIN_EVENT_ERROR);
    });
    protocol_->OnIncomingAudio([this](std::unique_ptr<AudioStreamPacket> packet) {
        if (device_state_ == kDeviceStateSpeaking) {
            audio_service_.PushPacketToDecodeQueue(std::move(packet));
        }
    });
    protocol_->OnAudioChannelOpened([this, codec, &board]() {
        board.SetPowerSaveMode(false);
        if (protocol_->server_sample_rate() != codec->output_sample_rate()) {
            ESP_LOGW(TAG, "Server sample rate %d does not match device output sample rate %d, resampling may cause distortion",
                protocol_->server_sample_rate(), codec->output_sample_rate());
        }
    });
    protocol_->OnAudioChannelClosed([this, &board]() {
        board.SetPowerSaveMode(true);
        Schedule([this]() {
            // 🔴 这里**刻意不清空字幕**（2026-10-20，真机现象驱动）。
            //
            // 原实现是 `display->SetChatMessage("system", "")`：每轮音频通道一关
            // 立刻把刚说的话擦掉。设备端 `sentence_start` 确实到了、`<< 文本`
            // 也确实打进了串口日志，但老人在设备说完之后抬头看屏幕时，
            // 字幕早就被这行清空了 —— 于是"屏幕上没有字幕"。
            //
            // 症状是「代码两侧都通、屏幕上却没有」，所以修法不是加显示，
            // 而是**别在老人还没看完的时候擦掉**。字幕保留到下一次
            // kDeviceStateConnecting（新一轮真正开始）时由 SetDeviceState 清。
            SetDeviceState(kDeviceStateIdle);
        });
    });
    protocol_->OnIncomingJson([this, display](const cJSON* root) {
        // Parse JSON data
        auto type = cJSON_GetObjectItem(root, "type");
        if (strcmp(type->valuestring, "tts") == 0) {
            auto state = cJSON_GetObjectItem(root, "state");
            if (strcmp(state->valuestring, "start") == 0) {
                Schedule([this]() {
                    aborted_ = false;
                    // 新一轮开始：上一轮没来得及上屏的残句直接丢（它们属于
                    // 已经结束/被打断的那一轮，等它们的音频永远等不到）。
                    ClearPendingSubtitles();
                    if (device_state_ == kDeviceStateIdle || device_state_ == kDeviceStateListening) {
                        SetDeviceState(kDeviceStateSpeaking);
                    }
                });
            } else if (strcmp(state->valuestring, "stop") == 0) {
                Schedule([this]() {
                    // 收尾：本句音频已经播完，还没上屏的字现在就该出来，
                    // 否则老人只能看到"说到一半"的字幕。
                    FlushSubtitles(true);
                    if (device_state_ == kDeviceStateSpeaking) {
                        if (listening_mode_ == kListeningModeManualStop) {
                            SetDeviceState(kDeviceStateIdle);
                        } else {
                            SetDeviceState(kDeviceStateListening);
                        }
                    }
                });
            } else if (strcmp(state->valuestring, "sentence_start") == 0) {
                auto text = cJSON_GetObjectItem(root, "text");
                if (cJSON_IsString(text)) {
                    ESP_LOGI(TAG, "<< %s", text->valuestring);
                    /* ⚠️ frame_mark 必须在这里（协议任务）取，不能放进
                     * Schedule 回调里取：回调跑起来时后续音频帧可能已经
                     * 收进来了，门就提前打开，字幕又跑回声音前面 —— 那正是
                     * 这次要修的现象。取到的是「本句音频之前已收的帧数」，
                     * 因为控制消息和音频帧共用同一条 FIFO、按序到达。 */
                    const int frame_mark = audio_service_.ReceivedFrameCount();
                    Schedule([this, message = std::string(text->valuestring), frame_mark]() {
                        QueueSubtitle(message, frame_mark);
                    });
                }
            }
        } else if (strcmp(type->valuestring, "stt") == 0) {
            auto text = cJSON_GetObjectItem(root, "text");
            if (cJSON_IsString(text)) {
                ESP_LOGI(TAG, ">> %s", text->valuestring);
                Schedule([this, display, message = std::string(text->valuestring)]() {
                    display->SetChatMessage("user", message.c_str());
                });
            }
        } else if (strcmp(type->valuestring, "llm") == 0) {
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(emotion)) {
                Schedule([this, display, emotion_str = std::string(emotion->valuestring)]() {
                    display->SetEmotion(emotion_str.c_str());
                });
            }
        } else if (strcmp(type->valuestring, "mcp") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            if (cJSON_IsObject(payload)) {
                McpServer::GetInstance().ParseMessage(payload);
            }
        } else if (strcmp(type->valuestring, "system") == 0) {
            auto command = cJSON_GetObjectItem(root, "command");
            if (cJSON_IsString(command)) {
                ESP_LOGI(TAG, "System command: %s", command->valuestring);
                if (strcmp(command->valuestring, "reboot") == 0) {
                    // Do a reboot if user requests a OTA update
                    Schedule([this]() {
                        Reboot();
                    });
                } else {
                    ESP_LOGW(TAG, "Unknown system command: %s", command->valuestring);
                }
            }
        } else if (strcmp(type->valuestring, "alert") == 0) {
            auto status = cJSON_GetObjectItem(root, "status");
            auto message = cJSON_GetObjectItem(root, "message");
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(status) && cJSON_IsString(message) && cJSON_IsString(emotion)) {
                Alert(status->valuestring, message->valuestring, emotion->valuestring, Lang::Sounds::OGG_VIBRATION);
            } else {
                ESP_LOGW(TAG, "Alert command requires status, message and emotion");
            }
#if CONFIG_RECEIVE_CUSTOM_MESSAGE
        } else if (strcmp(type->valuestring, "custom") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            ESP_LOGI(TAG, "Received custom message: %s", cJSON_PrintUnformatted(root));
            if (cJSON_IsObject(payload)) {
                Schedule([this, display, payload_str = std::string(cJSON_PrintUnformatted(payload))]() {
                    display->SetChatMessage("system", payload_str.c_str());
                });
            } else {
                ESP_LOGW(TAG, "Invalid custom message format: missing payload");
            }
#endif
        } else {
            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);
        }
    });
    bool protocol_started = protocol_->Start();

    SystemInfo::PrintHeapStats();
    SetDeviceState(kDeviceStateIdle);

    has_server_time_ = ota.HasServerTime();
    if (protocol_started) {
        std::string message = std::string(Lang::Strings::VERSION) + ota.GetCurrentVersion();
        display->ShowNotification(message.c_str());
        display->SetChatMessage("system", "");
        /* 2026-10-20 去掉开机「叮咚」（原 audio_service_.PlaySound(OGG_SUCCESS)）。
         * 用户原话：「设备一直在隔一会叮咚一声，实际场景下这个声音很烦人」。
         * 这声「叮咚」就是这条 —— 每次协议起来都会响一次。它本来是「设备就绪」
         * 提示，但就绪状态屏上已经有字了，而老人家里每重启一次就多一声响，
         * 反而像故障。**提醒/服药到点的提示音不要动**（那是功能，不是噪声），
         * 只去掉「开机成功」这一类无信息量的声音。 */
        ESP_LOGI(TAG, "设备就绪（不再播开机提示音，见本行上方注释）");
    }
}

// Add a async task to MainLoop
void Application::Schedule(std::function<void()> callback) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        main_tasks_.push_back(std::move(callback));
    }
    xEventGroupSetBits(event_group_, MAIN_EVENT_SCHEDULE);
}

// The Main Event Loop controls the chat state and websocket connection
// If other tasks need to access the websocket or chat state,
// they should use Schedule to call this function
void Application::MainEventLoop() {
    while (true) {
        auto bits = xEventGroupWaitBits(event_group_, MAIN_EVENT_SCHEDULE |
            MAIN_EVENT_SEND_AUDIO |
            MAIN_EVENT_WAKE_WORD_DETECTED |
            MAIN_EVENT_VAD_CHANGE |
            MAIN_EVENT_CLOCK_TICK |
            MAIN_EVENT_ERROR, pdTRUE, pdFALSE, portMAX_DELAY);

        if (bits & MAIN_EVENT_ERROR) {
            SetDeviceState(kDeviceStateIdle);
            Alert(Lang::Strings::ERROR, last_error_message_.c_str(), "circle_xmark", Lang::Sounds::OGG_EXCLAMATION);
        }

        if (bits & MAIN_EVENT_SEND_AUDIO) {
            while (auto packet = audio_service_.PopPacketFromSendQueue()) {
                if (protocol_ && !protocol_->SendAudio(std::move(packet))) {
                    break;
                }
            }
        }

        if (bits & MAIN_EVENT_WAKE_WORD_DETECTED) {
            OnWakeWordDetected();
        }

        if (bits & MAIN_EVENT_VAD_CHANGE) {
            if (device_state_ == kDeviceStateListening) {
                auto led = Board::GetInstance().GetLed();
                led->OnStateChanged();
            }
        }

        if (bits & MAIN_EVENT_SCHEDULE) {
            std::unique_lock<std::mutex> lock(mutex_);
            auto tasks = std::move(main_tasks_);
            lock.unlock();
            for (auto& task : tasks) {
                task();
            }
        }

        if (bits & MAIN_EVENT_CLOCK_TICK) {
            clock_ticks_++;
            auto display = Board::GetInstance().GetDisplay();
            display->UpdateStatusBar();

            /* 待机 25 秒后清掉屏幕下方的最后一句答复文字（2026-10-27 用户要求：
             * 「待机状态下不要显示图标下部最后一次的答复文字」）。
             *
             * 为什么不是"一进 idle 就清"：2026-10-20 真机事故就是这么修的
             * —— 音频通道一关立刻清空，老人说完话抬头看屏幕时字幕已经没了，
             * 现象是"两侧代码都通、屏幕上却没有字幕"。所以保留 25 秒：
             * 刚说完的这段时间老人能看，进入真正的待机态就干净。
             * clock_ticks_ 在 SetDeviceState() 里清零 ⇒ 每次进入 idle 只会
             * 命中一次 25，不需要额外状态位。 */
            if (device_state_ == kDeviceStateIdle && clock_ticks_ == 25) {
                display->SetChatMessage("system", "");
            }

            // Print the debug info every 10 seconds
            if (clock_ticks_ % 10 == 0) {
                // SystemInfo::PrintTaskCpuUsage(pdMS_TO_TICKS(1000));
                // SystemInfo::PrintTaskList();
                SystemInfo::PrintHeapStats();
                /* 前 120 秒把复位原因重打一遍：自发重启时 USB 会重新枚举，
                 * 开机头 16 秒的串口日志在主机侧整段丢失（见文件顶部说明）。
                 * 只重打 120 秒是为了不让它变成长跑噪声 —— 够覆盖任何抓取窗口。
                 * 时长用 s_boot_us 算（esp_timer 单调），不用 clock_ticks_（会被状态切换清零）。 */
                int up_s = (int)((esp_timer_get_time() - s_boot_us) / 1000000LL);
                if (up_s <= 120) {
                    ESP_LOGW("BootDiag", "复位原因=%s 累计启动=%u 次 已运行=%ds 当前空闲堆=%u 最小空闲堆=%u",
                             s_reset_reason, (unsigned)s_boot_count, up_s,
                             (unsigned)esp_get_free_heap_size(),
                             (unsigned)esp_get_minimum_free_heap_size());
                }
            }
        }
    }
}

void Application::OnWakeWordDetected() {
    if (!protocol_) {
        return;
    }

    if (device_state_ == kDeviceStateIdle) {
        audio_service_.EncodeWakeWord();

        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
            if (!protocol_->OpenAudioChannel()) {
                audio_service_.EnableWakeWordDetection(true);
                return;
            }
        }

        auto wake_word = audio_service_.GetLastWakeWord();
        ESP_LOGI(TAG, "Wake word detected: %s", wake_word.c_str());
#if CONFIG_SEND_WAKE_WORD_DATA
        // Encode and send the wake word data to the server
        while (auto packet = audio_service_.PopWakeWordPacket()) {
            protocol_->SendAudio(std::move(packet));
        }
        // Set the chat state to wake word detected
        protocol_->SendWakeWordDetected(wake_word);
        SetListeningMode(aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime);
#else
        SetListeningMode(aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime);
        // Play the pop up sound to indicate the wake word is detected
        audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
#endif
    } else if (device_state_ == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonWakeWordDetected);
    } else if (device_state_ == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::AbortSpeaking(AbortReason reason) {
    ESP_LOGI(TAG, "Abort speaking");
    aborted_ = true;
    /* 打断时把还没上屏的字幕丢掉：它们的音频不会再来（服务端已停止推流），
     * 留着只会在下一轮开头突然冒出一句上一轮的半截话。
     *
     * ⚠️ 必须走 Schedule 而不是直接清：AbortSpeaking 会被**按键任务 /
     * elder 任务**调用（老人按键打断走的就是这条），而 pending_subtitles_
     * 是主任务独占的 std::deque —— 跨任务直接 clear 是数据竞争，
     * 表现是偶发崩溃或字幕列表错乱，且与按键时机强相关、极难复现。 */
    Schedule([this]() { ClearPendingSubtitles(); });
    if (protocol_) {
        protocol_->SendAbortSpeaking(reason);
    }
}

void Application::SetListeningMode(ListeningMode mode) {
    listening_mode_ = mode;
    SetDeviceState(kDeviceStateListening);
}

void Application::SetDeviceState(DeviceState state) {
    if (device_state_ == state) {
        return;
    }
    
    clock_ticks_ = 0;
    auto previous_state = device_state_;
    device_state_ = state;
    ESP_LOGI(TAG, "STATE: %s", STATE_STRINGS[device_state_]);

    // Send the state change event
    DeviceStateEventManager::GetInstance().PostStateChangeEvent(previous_state, state);

    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto led = board.GetLed();
    led->OnStateChanged();
    switch (state) {
        case kDeviceStateUnknown:
        case kDeviceStateIdle:
            display->SetStatus(Lang::Strings::STANDBY);
            display->SetEmotion("neutral");
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(true);
            break;
        case kDeviceStateConnecting:
            display->SetStatus(Lang::Strings::CONNECTING);
            display->SetEmotion("neutral");
            display->SetChatMessage("system", "");
            break;
        case kDeviceStateListening:
            display->SetStatus(Lang::Strings::LISTENING);
            display->SetEmotion("neutral");

            /* ---- 显式唤醒必须"盖过"服务端的待机冷却（2026-11-03）----
             *
             * 现场：老人说完「不聊了」，服务端下发 standby、并进入
             * VOICE_DISMISS_COOLDOWN_SEC（默认 **180 秒**）的冷却 —— 冷却期内
             * 它丢弃所有上行音频、连 listen start 也忽略（session.py 的
             * _on_binary / _on_listen）。服务端靠什么解除？只有
             * `{"type":"listen","state":"detect"}`（唤醒词那条）。
             *
             * 而**按键唤醒走的是 ToggleChatState，从来不发 detect** ——
             * 于是老人按一下键、开口说话，设备在推流、服务端全扔，最长聋
             * 三分钟。用户实报「偶发性的我说话设备完全没有识别到」就是这条。
             *
             * 判据用 **Idle → Listening** 这一次跃迁：它就是"有人明确让设备
             * 开始听"（按键 / 唤醒词 / 提醒追问）。播报完自动回 listening 是
             * Speaking → Listening，不在此列 —— 那条正是冷却要挡的。
             *
             * 发 detect 而不是别的：服务端 _on_listen 的 detect 分支本来就
             * 是"唤醒词到达，解除待机"，语义完全对得上；它不播音、不弹提示，
             * 只是解冷却 + 清缓冲。 */
            if (previous_state == kDeviceStateIdle &&
                protocol_ != nullptr && protocol_->IsAudioChannelOpened()) {
                /* greet=false：这条 detect 只用来解服务端的待机冷却，
                 * **不要**唤醒应答。老人按下键就是在等自己说话被听见，
                 * 一句「你好，要聊点什么？」会让设备转成 Speaking、上行
                 * 音频被丢弃 —— 他刚开口的字正好没了。 */
                protocol_->SendWakeWordDetected("按键唤醒", false);
            }

            // Make sure the audio processor is running
            if (!audio_service_.IsAudioProcessorRunning()) {
                audio_service_.EnableVoiceProcessing(true);
            }
            /* ---- 每次真正转入 listening 都必须重新宣布（2026-11-04）----
             *
             * 用户实报：「语音播报了『您确认完成……』的完整语句后，屏幕显示
             * 设备从待机转换为连接中，然后转换为聆听中，但是我说话他一直
             * 识别不到，我说了十几次确认，他都听不到，状态灯也一直是聆听
             * 状态的蓝色，很久很久。」
             *
             * 服务端侧证据：WebSocket 建立（语音会话开始）之后就**一条消息
             * 都没有** —— 没有 listen start、没有音频帧、没有会话结束。
             * 也就是说设备开着麦、显示聆听中、灯是蓝的，但服务端根本不知道
             * 有人在听，上行音频全部落空。
             *
             * 根因是原来的条件：
             *     if (!IsAudioProcessorRunning() || mode == Realtime)
             * 只救了 realtime 一种模式（2026-10-20 那次聋 164 秒）。
             * 而老人长按按键的追问路径走的是 **ManualStop**
             * （见 StartListening()：SetListeningMode(kListeningModeManualStop)），
             * 刚播完本地预录片段时音频处理器仍在跑 ⇒ 条件恒假 ⇒ 这条消息
             * 不发。ManualStop 从来没被豁免过。
             *
             * 为什么改成"无条件发"是安全的：SetDeviceState() 对"状态没变"
             * 直接 return，所以本分支只在**真正转入** listening 时执行一次，
             * 不会把正在说的话清掉；服务端 _on_listen(start) 的重新武装也
             * 正是我们要的语义。通道没开时发送没有意义，故加一道通道判据。
             *
             * ⚠️ 不要再把条件收窄回"只在 realtime 下发"—— 那等于把这个 bug
             *    原样装回去，而且症状是"设备看起来在听、其实没在听"，最难查。 */
            if (protocol_->IsAudioChannelOpened()) {
                // Send the start listening command
                protocol_->SendStartListening(listening_mode_);
            } else {
                /* 通道没开却进了 listening：这是个必须留痕的异常状态，
                 * 否则又变成"屏幕上在听、服务端一无所知"的静默故障。 */
                ESP_LOGE(TAG, "进入 listening 但音频通道未打开，listen start "
                              "未发送（mode=%d）", (int)listening_mode_);
            }
            audio_service_.EnableWakeWordDetection(false);
            break;
        case kDeviceStateSpeaking:
            display->SetStatus(Lang::Strings::SPEAKING);

            if (listening_mode_ != kListeningModeRealtime) {
                audio_service_.EnableVoiceProcessing(false);
                // Only AFE wake word can be detected in speaking mode
                audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
            }
            audio_service_.ResetDecoder();
            break;
        default:
            // Do nothing
            break;
    }
}

void Application::Reboot() {
    ESP_LOGI(TAG, "Rebooting...");
    // Disconnect the audio channel
    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        protocol_->CloseAudioChannel();
    }
    protocol_.reset();
    audio_service_.Stop();

    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
}

bool Application::UpgradeFirmware(Ota& ota, const std::string& url) {
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    
    // Use provided URL or get from OTA object
    std::string upgrade_url = url.empty() ? ota.GetFirmwareUrl() : url;
    std::string version_info = url.empty() ? ota.GetFirmwareVersion() : "(Manual upgrade)";
    
    // Close audio channel if it's open
    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        ESP_LOGI(TAG, "Closing audio channel before firmware upgrade");
        protocol_->CloseAudioChannel();
    }
    ESP_LOGI(TAG, "Starting firmware upgrade from URL: %s", upgrade_url.c_str());
    
    Alert(Lang::Strings::OTA_UPGRADE, Lang::Strings::UPGRADING, "download", Lang::Sounds::OGG_UPGRADE);
    vTaskDelay(pdMS_TO_TICKS(3000));

    SetDeviceState(kDeviceStateUpgrading);
    
    std::string message = std::string(Lang::Strings::NEW_VERSION) + version_info;
    display->SetChatMessage("system", message.c_str());

    board.SetPowerSaveMode(false);
    audio_service_.Stop();
    vTaskDelay(pdMS_TO_TICKS(1000));

    bool upgrade_success = ota.StartUpgradeFromUrl(upgrade_url, [display](int progress, size_t speed) {
        std::thread([display, progress, speed]() {
            char buffer[32];
            snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
            display->SetChatMessage("system", buffer);
        }).detach();
    });

    if (!upgrade_success) {
        // Upgrade failed, restart audio service and continue running
        ESP_LOGE(TAG, "Firmware upgrade failed, restarting audio service and continuing operation...");
        audio_service_.Start(); // Restart audio service
        board.SetPowerSaveMode(true); // Restore power save mode
        Alert(Lang::Strings::ERROR, Lang::Strings::UPGRADE_FAILED, "circle_xmark", Lang::Sounds::OGG_EXCLAMATION);
        vTaskDelay(pdMS_TO_TICKS(3000));
        return false;
    } else {
        // Upgrade success, reboot immediately
        ESP_LOGI(TAG, "Firmware upgrade successful, rebooting...");
        display->SetChatMessage("system", "Upgrade successful, rebooting...");
        vTaskDelay(pdMS_TO_TICKS(1000)); // Brief pause to show message
        Reboot();
        return true;
    }
}

void Application::WakeWordInvoke(const std::string& wake_word) {
    if (!protocol_) {
        return;
    }

    if (device_state_ == kDeviceStateIdle) {
        audio_service_.EncodeWakeWord();

        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
            if (!protocol_->OpenAudioChannel()) {
                audio_service_.EnableWakeWordDetection(true);
                return;
            }
        }

        ESP_LOGI(TAG, "Wake word detected: %s", wake_word.c_str());
#if CONFIG_USE_AFE_WAKE_WORD || CONFIG_USE_CUSTOM_WAKE_WORD
        // Encode and send the wake word data to the server
        while (auto packet = audio_service_.PopWakeWordPacket()) {
            protocol_->SendAudio(std::move(packet));
        }
        // Set the chat state to wake word detected
        protocol_->SendWakeWordDetected(wake_word);
        SetListeningMode(aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime);
#else
        SetListeningMode(aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime);
        // Play the pop up sound to indicate the wake word is detected
        audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
#endif
    } else if (device_state_ == kDeviceStateSpeaking) {
        Schedule([this]() {
            AbortSpeaking(kAbortReasonNone);
        });
    } else if (device_state_ == kDeviceStateListening) {   
        Schedule([this]() {
            if (protocol_) {
                protocol_->CloseAudioChannel();
            }
        });
    }
}

bool Application::CanEnterSleepMode() {
    if (device_state_ != kDeviceStateIdle) {
        return false;
    }

    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        return false;
    }

    if (!audio_service_.IsIdle()) {
        return false;
    }

    // Now it is safe to enter sleep mode
    return true;
}

void Application::SendMcpMessage(const std::string& payload) {
    if (protocol_ == nullptr) {
        return;
    }

    // Make sure you are using main thread to send MCP message
    if (xTaskGetCurrentTaskHandle() == main_event_loop_task_handle_) {
        protocol_->SendMcpMessage(payload);
    } else {
        Schedule([this, payload = std::move(payload)]() {
            protocol_->SendMcpMessage(payload);
        });
    }
}

void Application::SetAecMode(AecMode mode) {
    aec_mode_ = mode;
    Schedule([this]() {
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        switch (aec_mode_) {
        case kAecOff:
            audio_service_.EnableDeviceAec(false);
            display->ShowNotification(Lang::Strings::RTC_MODE_OFF);
            break;
        case kAecOnServerSide:
            audio_service_.EnableDeviceAec(false);
            display->ShowNotification(Lang::Strings::RTC_MODE_ON);
            break;
        case kAecOnDeviceSide:
            audio_service_.EnableDeviceAec(true);
            display->ShowNotification(Lang::Strings::RTC_MODE_ON);
            break;
        }

        // If the AEC mode is changed, close the audio channel
        if (protocol_ && protocol_->IsAudioChannelOpened()) {
            protocol_->CloseAudioChannel();
        }
    });
}

void Application::PlaySound(const std::string_view& sound) {
    audio_service_.PlaySound(sound);
}