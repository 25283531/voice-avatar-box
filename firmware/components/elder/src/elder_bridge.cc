// elder_bridge.cc —— elder 组件 ↔ xiaozhi-esp32 v2.0.5 的适配层（C++）
//
// 编译方式：由 tools/merge_to_xiaozhi.py 追加进 xiaozhi/main/CMakeLists.txt
// 的 target_sources()，因此本文件属于 **main 组件**，可以自由使用
// Application / Board / Display / AudioService 等 xiaozhi 的 C++ API。
//
// 职责：
//   1. 把 xiaozhi 的设备状态（待机/聆听/说话/…）映射成 elder 的形象状态，
//      进而驱动 EAF 序列帧（esp_emote_gfx）切换表情；
//   2. 把 elder 的「切形象 / 屏显 / 播提示音 / TTS」钩子接到 xiaozhi 上；
//   3. 开机启动提醒引擎、按键、RGB 与后端同步任务；
//   4. 断网时保证本地提醒照常触发，并用固件内置 ogg 做提示音兜底。
//
// 注意：本文件不修改任何上游源文件，只在 main 里新增一个编译单元。

#include <cstring>
#include <cstdio>
#include <cstdint>
#include <ctime>
#include <string>
#include <string_view>

#include "sdkconfig.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"   // esp_timer_get_time()：按键发起标记的时间戳（纯 RAM）
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "application.h"
#include "board.h"
#include "display.h"
#include "device_state_event.h"
#include "assets/lang_config.h"   // Lang::Sounds::OGG_* 内置提示音（离线可用）
#include "lcd_display.h"          // 需求 8：全屏提醒（ShowReminder/HideReminder）

#include "elder_board.h"
#include "elder_clip.h"
#include "elder_rgb.h"
#include "elder_input.h"
#include "elder_reminder.h"
#include "elder_rtc.h"
// elder_activation.h 已移除：激活码改为向服务端要（真码才有效），逻辑搬到
// src/elder_bind_guide.cc（且必须在 app.Start() 之前起任务，见该文件头注释）。
#include "elder_ui.h"
#include "elder_net.h"
#include "elder_sync.h"

#define TAG "elder.bridge"

extern "C" void elder_bridge_start(void);

namespace {

/* ---------------------------------------------------------------- 形象 */

// xiaozhi 设备状态 → elder 形象状态
elder_ui_state_t MapDeviceState(DeviceState st)
{
    switch (st) {
    case kDeviceStateIdle:       return ELDER_UI_IDLE;
    case kDeviceStateListening:  return ELDER_UI_LISTENING;
    case kDeviceStateSpeaking:   return ELDER_UI_SPEAKING;
    case kDeviceStateConnecting: return ELDER_UI_LISTENING;
    case kDeviceStateStarting:   return ELDER_UI_ACTIVATING;   // 未配网/未绑定
    case kDeviceStateWifiConfiguring: return ELDER_UI_ACTIVATING;
    case kDeviceStateActivating: return ELDER_UI_ACTIVATING;
    case kDeviceStateUpgrading:  return ELDER_UI_LISTENING;
    case kDeviceStateFatalError: return ELDER_UI_ALARM;
    default:                     return ELDER_UI_IDLE;
    }
}

// 切表情：走 Display::SetEmotion()，名字必须与 twemoji_64 目录下的文件名
// 逐字一致（`78__xiaozhi-fonts/png/twemoji_64/<name>.png`）。写错不会报错，
// 只会静默回落到 FontAwesome 图标 —— 即"表情没变"，很难在现场看出来。
void OnSetEmotion(const char *name, void *ctx)
{
    (void)ctx;
    auto display = Board::GetInstance().GetDisplay();
    if (!display) return;
    display->SetEmotion(name);
}

void OnNotify(const char *text, const char *emotion, void *ctx)
{
    (void)ctx;
    auto display = Board::GetInstance().GetDisplay();
    if (!display) return;
    // ShowNotification 在 EmoteDisplay / LcdDisplay 上都有实现
    display->ShowNotification(text ? text : "", 8000);
    if (emotion && emotion[0]) {
        display->SetEmotion(emotion);
    }
}

/* ---- 需求 8：全屏大字提醒 ----
 *
 * 为什么用 dynamic_cast 而不是给基类 Display 加虚函数：display.h 是上游
 * 文件，加一个纯虚函数等于要求 OLED / Emote / RGB 各实现一遍（它们根本
 * 没有"全屏大字"这个概念）。这块屏才有意义，所以只对 LcdDisplay 生效。
 * 本工程开了 RTTI（编译参数 -frtti），dynamic_cast 可用。
 *
 * 拿不到 LcdDisplay 时**静默返回**：无屏设备上本来就没有全屏提醒，
 * 提醒该做的其他事（闪灯、语音、回执）一点也不能少 —— 所以这里不能
 * 抛错让上层中断。 */
LcdDisplay *AsLcdDisplay()
{
    auto *display = Board::GetInstance().GetDisplay();
    if (display == nullptr) return nullptr;
    return dynamic_cast<LcdDisplay *>(display);
}

void OnShowReminder(const char *text, void *ctx)
{
    (void)ctx;
    auto *lcd = AsLcdDisplay();
    if (lcd == nullptr) {
        ESP_LOGW(TAG, "当前显示不是 LcdDisplay，全屏提醒降级为屏显通知");
        return;
    }
    lcd->ShowReminder(text ? text : "");
}

void OnHideReminder(void *ctx)
{
    (void)ctx;
    auto *lcd = AsLcdDisplay();
    if (lcd != nullptr) lcd->HideReminder();
}

/* ---------------------------------------------------------------- 提示音 */

// 提醒专用音乐：**一段固定的、编译进固件的 OGG**（约 4.9 秒）。
//
// 来源与约束
// ----------
// * 文件：firmware/xiaozhi/main/assets/common/reminder_chime.ogg
//   由 tools/make_reminder_chime.py 一次性生成（五声音阶铃声），
//   生成后即入库，**运行时不再合成、所有设备听到的都是同一段**。
// * 嵌入：main/CMakeLists.txt 里 file(GLOB COMMON_SOUNDS .../assets/common/*.ogg)
//   把它收进 EMBED_FILES，链接期变成下面这两个符号（文件名里的 . 换成 _）。
// * 为什么本地播：提醒的第一声不能依赖网络。断网时语音内容可以没有，
//   但"有事"这个信号必须有 —— 否则老人根本不知道到点了。
extern const char ogg_reminder_chime_start[] asm("_binary_reminder_chime_ogg_start");
extern const char ogg_reminder_chime_end[] asm("_binary_reminder_chime_ogg_end");
static const std::string_view kReminderChime{
    static_cast<const char *>(ogg_reminder_chime_start),
    static_cast<size_t>(ogg_reminder_chime_end - ogg_reminder_chime_start)
};

// 全部来自固件内置 ogg（EMBED_FILES 编进 .rodata），**不依赖网络**
const char *ToneName(elder_tone_t tone)
{
    switch (tone) {
    case ELDER_TONE_POPUP:       return "popup";
    case ELDER_TONE_EXCLAMATION: return "exclamation";
    case ELDER_TONE_SUCCESS:     return "success";
    case ELDER_TONE_VIBRATION:   return "vibration";
    case ELDER_TONE_LOW_BATTERY: return "low_battery";
    case ELDER_TONE_REMINDER:    return "reminder_chime";
    default:                     return nullptr;
    }
}

void OnPlayTone(elder_tone_t tone, void *ctx)
{
    (void)ctx;

    std::string_view sound;
    switch (tone) {
    case ELDER_TONE_POPUP:       sound = Lang::Sounds::OGG_POPUP;       break;
    case ELDER_TONE_EXCLAMATION: sound = Lang::Sounds::OGG_EXCLAMATION; break;
    case ELDER_TONE_SUCCESS:     sound = Lang::Sounds::OGG_SUCCESS;     break;
    case ELDER_TONE_VIBRATION:   sound = Lang::Sounds::OGG_VIBRATION;   break;
    case ELDER_TONE_LOW_BATTERY: sound = Lang::Sounds::OGG_LOW_BATTERY; break;
    case ELDER_TONE_REMINDER:
        // 提醒音乐：固件内置固定资源，离线可用
        sound = kReminderChime;
        break;
    default:
        return;
    }

    /* ⚠️ 必须 Schedule，不能就地 PlaySound。
     *
     * AudioService::PlaySound 是**同步**的：它当场遍历 OGG 页、调
     * codec->EnableOutput(true) 打开 ES8311、再逐帧推 I2S。而本函数会被
     * 两条**小栈**路径同步调到：
     *   ① elder_input 任务（按键确认 → OnReminder(DONE) → 提示音），
     *      2026-10-27 长按确认偶发重启就发生在这一条上；
     *   ② esp_event 默认任务（状态变化回调里也可能间接触发）。
     * 在主事件循环（main_event_loop，栈 8192）里执行，调用方立刻返回，
     * 谁调都不会因为"顺便播个音"把自己的栈压穿。
     * sound 指向的都是静态常量，lambda 里存 string_view 安全。 */
    Application::GetInstance().Schedule([sound]() {
        Application::GetInstance().PlaySound(sound);
    });
}

/* ------------------------------------------------------- 预录语音片段 */
/*
 * 7 条常用话术的「提醒音 + 语音」整段 OGG，由 tools/make_elder_clips.py
 * 用**服务端自己的 TTS**（腾讯云 601009，与线上同一音色）合成后入库，
 * 编进 .rodata。为什么这么做见 elder_clip.h 头注释。
 *
 * 资源约束（改任何一条都必须先读这段）
 * ----------------------------------
 * * 24kHz / mono / 每帧 60ms —— 硬契约，不是风格。
 *   `PlaySound` 从 OpusHead 读采样率、把每个包按**硬编码 60ms** 当一帧推给
 *   解码器（audio_service.cc 的 PlaySound 与 398-411 的 SetDecodeSampleRate）。
 *   采样率不是 24k 就会每次播放都重建解码器，带来开销和爆音；
 *   帧长不是 60ms 就会解出"卡带/回声"。
 *   `tools/verify_elder_clips.py` 逐字节核这两条，生成后必跑。
 * * 文件放 firmware/xiaozhi/main/assets/common/，由 CMakeLists 的
 *   file(GLOB COMMON_SOUNDS ...) 把该目录下全部 .ogg 自动收进 EMBED_FILES，
 *   所以**不用改 CMakeLists 文本**，只要按 `<文件名去点>_ogg_start/_end` 取名。
 *   ⚠️ 但 `file(GLOB)` 是 **configure 期**展开的：**新增/删除** ogg 必须重新
 *   跑一次 cmake（本机 `python tools/idfwin.py build` 会自己重配；只 `idf.py
 *   build` 不会），否则新文件根本不进 EMBED_FILES，报的是链接期
 *   `undefined reference to _binary_xxx_start`。**改内容不用重配**。
 *   CI 每次都是干净 checkout，configure 必然重跑，所以这条只坑本机构建。
 * * 进的是 **app 分区**（EMBED_FILES → 编进 xiaozhi.bin 的 .rodata），
 *   不是 assets 分区。所以它吃的是 ota_0 的 4.03MB 预算，与
 *   build_default_assets.py 生成的 assets.bin（表情/字体那 8MB）互不相干。
 * * 符号名规则：elder_medicine.ogg → _binary_elder_medicine_ogg_start / _end。
 *   写错不会编译报错，是**链接期** undefined reference（构建最后一步才炸）。
 */
extern const char ogg_elder_medicine_start[] asm("_binary_elder_medicine_ogg_start");
extern const char ogg_elder_medicine_end[] asm("_binary_elder_medicine_ogg_end");
extern const char ogg_elder_todo_start[] asm("_binary_elder_todo_ogg_start");
extern const char ogg_elder_todo_end[] asm("_binary_elder_todo_ogg_end");
extern const char ogg_elder_confirm_hint_start[] asm("_binary_elder_confirm_hint_ogg_start");
extern const char ogg_elder_confirm_hint_end[] asm("_binary_elder_confirm_hint_ogg_end");
extern const char ogg_elder_done_start[] asm("_binary_elder_done_ogg_start");
extern const char ogg_elder_done_end[] asm("_binary_elder_done_ogg_end");
extern const char ogg_elder_escalated_start[] asm("_binary_elder_escalated_ogg_start");
extern const char ogg_elder_escalated_end[] asm("_binary_elder_escalated_ogg_end");
extern const char ogg_elder_skipped_start[] asm("_binary_elder_skipped_ogg_start");
extern const char ogg_elder_skipped_end[] asm("_binary_elder_skipped_ogg_end");
extern const char ogg_elder_no_reminder_start[] asm("_binary_elder_no_reminder_ogg_start");
extern const char ogg_elder_no_reminder_end[] asm("_binary_elder_no_reminder_ogg_end");
/* 2026-11-03 长按即时应答（用户需求）：
 *   elder_sched_done.ogg  → 「日程已确认完成，我就不在屏幕显示了」
 *   elder_med_confirm.ogg → 「您确认已经完成这次服药了吗？」
 * 两条都由 tools/make_elder_clips.py 生成（在生产容器里跑，本机没有 TTS 密钥），
 * 文件放在 main/assets/common/ 下，CMake 的 COMMON_SOUNDS 用 GLOB 收，不用改配置。 */
extern const char ogg_elder_sched_done_start[] asm("_binary_elder_sched_done_ogg_start");
extern const char ogg_elder_sched_done_end[] asm("_binary_elder_sched_done_ogg_end");
extern const char ogg_elder_med_confirm_start[] asm("_binary_elder_med_confirm_ogg_start");
extern const char ogg_elder_med_confirm_end[] asm("_binary_elder_med_confirm_ogg_end");

// id → 内嵌 OGG 的视图。返回空的 string_view 表示"没有这条片段"，
// 调用方（elder_clip_play 的上游）据此回落 TTS。
static std::string_view ClipSound(int id)
{
    switch (id) {
    case ELDER_CLIP_MEDICINE:
        return {static_cast<const char *>(ogg_elder_medicine_start),
                static_cast<size_t>(ogg_elder_medicine_end - ogg_elder_medicine_start)};
    case ELDER_CLIP_TODO:
        return {static_cast<const char *>(ogg_elder_todo_start),
                static_cast<size_t>(ogg_elder_todo_end - ogg_elder_todo_start)};
    case ELDER_CLIP_CONFIRM_HINT:
        return {static_cast<const char *>(ogg_elder_confirm_hint_start),
                static_cast<size_t>(ogg_elder_confirm_hint_end - ogg_elder_confirm_hint_start)};
    case ELDER_CLIP_DONE:
        return {static_cast<const char *>(ogg_elder_done_start),
                static_cast<size_t>(ogg_elder_done_end - ogg_elder_done_start)};
    case ELDER_CLIP_ESCALATED:
        return {static_cast<const char *>(ogg_elder_escalated_start),
                static_cast<size_t>(ogg_elder_escalated_end - ogg_elder_escalated_start)};
    case ELDER_CLIP_SKIPPED:
        return {static_cast<const char *>(ogg_elder_skipped_start),
                static_cast<size_t>(ogg_elder_skipped_end - ogg_elder_skipped_start)};
    case ELDER_CLIP_NO_REMINDER:
        return {static_cast<const char *>(ogg_elder_no_reminder_start),
                static_cast<size_t>(ogg_elder_no_reminder_end - ogg_elder_no_reminder_start)};
    case ELDER_CLIP_SCHED_DONE:
        return {static_cast<const char *>(ogg_elder_sched_done_start),
                static_cast<size_t>(ogg_elder_sched_done_end - ogg_elder_sched_done_start)};
    case ELDER_CLIP_MED_CONFIRM:
        return {static_cast<const char *>(ogg_elder_med_confirm_start),
                static_cast<size_t>(ogg_elder_med_confirm_end - ogg_elder_med_confirm_start)};
    default:
        return {};
    }
}

void OnPlayClip(int id, void *ctx)
{
    (void)ctx;

    std::string_view sound = ClipSound(id);
    if (sound.empty()) {
        // 不抛错：id 越界是编码错误，但让它变成"没声音"比让设备重启好。
        // 真正需要回落的地方在调用方（elder_clip_play 返回 false 那条路）。
        ESP_LOGW(TAG, "预录片段 id=%d 无对应资源", id);
        return;
    }

    /* ⚠️ 必须 Schedule，不能就地 PlaySound —— 理由与 OnPlayTone 完全相同，
     * 但这里的代价更明显：片段长 2~7 秒，PlaySound 是同步推 I2S 的，
     * 就地调用会把调用方所在的任务（提醒引擎任务 / elder_input 任务）
     * 整个卡住 1~3.5 秒。在 elder_input 任务上卡住 = 长按 3 秒的判定被
     * 音频播放挤歪；在主事件循环里跑则只占一个队列任务，不影响任何计时。 */
    Application::GetInstance().Schedule([sound]() {
        Application::GetInstance().PlaySound(sound);
    });
}

// 联网 TTS：通过 MCP/协议让服务端合成并播报一段话。
//
// ⚠️ 离线时**不能**走这里 —— 服务端不可达，SendMcpMessage 会静默失败，
// 老人听到的是「设备毫无反应」，看起来像设备坏了。离线必须明确告知
// （用户原话）：「当前设备未连接网络，将使用设备自身能力进行处理」。
static const char *const kOfflineNotice = "当前设备未连接网络，将使用设备自身能力进行处理";

bool OnIsOnline(void *ctx)
{
    (void)ctx;
    return Application::GetInstance().GetDeviceState() != kDeviceStateStarting
           && Application::GetInstance().GetDeviceState() != kDeviceStateWifiConfiguring;
}

void OnSpeak(const char *text, void *ctx)
{
    (void)ctx;
    auto &app = Application::GetInstance();

    // 离线：明确告知 + 本地提示音（固件内置 ogg，不依赖网络），绝不尝试 TTS
    if (!OnIsOnline(nullptr)) {
        ESP_LOGW(TAG, "离线播报降级: %s", text ? text : "");
        // 先播离线说明，再播原本要说的内容，两条都只走屏显+提示音
        OnNotify(kOfflineNotice, nullptr, nullptr);
        OnPlayTone(ELDER_TONE_POPUP, nullptr);
        if (text && text[0]) {
            OnNotify(text, nullptr, nullptr);
        }
        return;
    }

    if (app.GetDeviceState() == kDeviceStateIdle) {
        // 空闲态才能安全插入一次播报，避免打断正在进行的对话
        std::string payload = "{\"type\":\"tts\",\"text\":\"";
        payload += text ? text : "";
        payload += "\"}";
        app.SendMcpMessage(payload);
    }
}

/* ---------------------------------------------------------------- 提醒 */
/* 提醒的节奏：**先响音乐，再念事项**。
 *
 * ⚠️ 2026-11-03：这条节奏现在只用于**回落路径**。正常路径播预录片段
 *    （音乐与话术已合成在同一条 OGG 里，天然串行、不需要任何延时），
 *    见 OnReminder(ELDER_RMD_PENDING) 与 elder_clip.h。
 *    下面的说明保留，是因为回落路径仍然照它执行。
 *
 * 为什么用延时任务而不是"等播放结束回调"：`PlaySound` 只是把 OGG 入队给
 * 音频服务，没有任何完成回调可用；按已知时长 + 余量延后，是这套 API 下
 * 唯一确定的顺序保证。余量 400ms 给解码器起播与 I2S 缓冲 —— 宁可晚一点，
 * 也不能让音乐盖住第一句话（老人只听得清开头，被盖住就等于没说）。
 *
 * 另一个必须延后的理由：音乐与语音若同时下发，两路 PCM 会争同一个输出
 * 通道，实测是"音乐断续 + 语音只出半句"这种最难查的故障。
 *
 * ⚠️ **不要改用 esp_timer**：定时器回调跑在 esp_timer 任务里，默认栈只有
 * CONFIG_ESP_TIMER_TASK_STACK_SIZE（约 3.5KB），而这里往下要走
 * `elder_ui_announce → Display::ShowNotification`（LVGL 绘制 + 锁 + 字符串
 * 格式化）—— 栈溢出的表现是随机的 LoadProhibited，事后极难归因。
 * 专用任务可以自己定栈大小，做完自删，代价只有一次 xTaskCreate。 */
/* 提醒音乐多长（毫秒）→ 到点后开始念事项内容。
 *
 * ⚠️ 2026-11-03 起，这个常数**只服务于 TTS 回落路径**。
 *    正常路径已经改成播预录片段（ELDER_CLIP_MEDICINE / ELDER_CLIP_TODO，
 *    音乐与话术在同一条 OGG 里），根本不进 AnnounceTask，也就没有"等 4.9 秒"
 *    这一步。只有 elder_clip_play() 返回 false（没有资源/没有钩子）时，
 *    才会走「播音乐 + 延时 + TTS」这条老路。
 *    ⇒ 改这里的值不会影响预录片段的行为。片段时长见 elder_clip.c 的 kClipMs。
 *
 * 2026-10-22 实测（串口日志 151442 响铃 / 158282 才开始 TTS）：
 * 原来写 4900+400，但真正卡住间隔的不是这个常数，而是 AnnounceTask 里
 * 先调 elder_ui_announce() 的 **notify 刷屏**（要抢 LVGL 锁重排整屏，
 * 实测吃掉约 1.5 秒）。用户听到的间隔因此被撑到 2 秒以上。
 *
 * 改法：AnnounceTask 改用 elder_ui_speak()（只念不刷屏，屏显早已由
 * show_reminder 做过），常数同时收回 4900 —— 让 TTS 请求**正好在音乐
 * 结束时发出**，剩下的间隔就只有 TTS 首帧时延（约 0.8~1.2 秒），
 * 符合用户「有 1 秒的间隔就够了」。
 *
 * ⚠️ 不要再把 400 加回去：那是给"刷屏耗时"留的余量，刷屏已经不在
 * 关键路径上了，留着只会白白拉长间隔。 */
static constexpr int64_t kChimeMs = 4900;
static constexpr uint32_t kAnnounceTaskStack = 5120;
static constexpr UBaseType_t kAnnounceTaskPrio = 3;   /* 低优先级：不抢音频/网络 */

static void AnnounceTask(void *arg)
{
    auto *text = static_cast<std::string *>(arg);
    vTaskDelay(pdMS_TO_TICKS(kChimeMs));
    if (text != nullptr) {
        /* 只念不刷屏：屏上已经是全屏提醒了，再刷一次既重复又拖慢间隔。
         * 离线时 elder_ui_speak 会降级成一声提示音（音乐已经响过，
         * 这里只是保证"有反馈"，不叠加 TTS）。 */
        elder_ui_speak(text->c_str());
        delete text;
    }
    vTaskDelete(nullptr);
}

static void ScheduleAnnounce(const char *text)
{
    if (!text || !text[0]) {
        return;
    }
    auto *payload = new std::string(text);
    if (xTaskCreate(AnnounceTask, "rmd_announce", kAnnounceTaskStack,
                    payload, kAnnounceTaskPrio, nullptr) != pdPASS) {
        // 建任务失败（堆紧张）：宁可现在就念，也不能"音乐响完了没人说话"
        ESP_LOGE(TAG, "提醒播报任务创建失败，改为立即播报");
        elder_ui_announce(payload->c_str(), ELDER_TONE_NONE);
        delete payload;
    }
}

int s_arming_id = -1;

/* 上一次**真正播报过**的提醒分钟（= fired_due_at / 60）。
 *
 * 为什么键是「提醒实例的到期分钟」而不是 time(NULL)/60：
 *   · sync.c 的 due_of_item() 也是按 fired_due_at 定位实例的，两边必须
 *     同一个口径，否则回执说的"哪一次"和播报说的"哪一次"会对不上；
 *   · 用墙上时钟时，8:00:59 触发的两条会被算成"8:00 这一分钟"，而 8:01:00
 *     触发的第三条又算成"8:01"，结果是**该合并的没合并**；反过来，跨分钟
 *     迟到的提醒会被上一分钟的播报吃掉，**一条都不播**。
 *   · fired_due_at 由 reminder.c 用条目自己的 hour/minute + 本地时区算出，
 *     同一时刻触发的多条提醒天然相等，正好就是"同一分钟"的定义。
 *
 * 只活在 RAM：重启后重播一次可以接受（比"重启后这一分钟彻底不响"安全）。
 * -1 = 本次上电还没播过。 */
static int64_t s_last_announced_min = -1;

/* 上一次**真正播过升级话术**的提醒分钟（= fired_due_at / 60）。
 *
 * 为什么必须与 s_last_announced_min 分开（2026-11-03 残留修复）：
 *   同一条提醒的 PENDING 与 ESCALATED 用的是**同一个 fired_due_at** ——
 *   升级只是状态变了，到期时刻不变，所以两者的 due_min 完全相同。
 *   共用一份变量的话，8:00 播过 PENDING 之后，8:15 的升级会被判成
 *   "这一分钟已经播过"而静默 ⇒ 「请长按按钮确认」这句永远不播，
 *   而它正是老人唯一能自救的指令。
 *   （"T 分钟 / T+15 分钟"是墙上时钟，不是这里用的 fired_due_at。）
 *
 * 这个变量的作用只有一条：3 条服药提醒同时升级时，别把 4,560ms 的
 * confirm_hint 连播三遍（≈13.7 秒）。判据与 PENDING 一致，用提醒实例的
 * 到期分钟而不是 time(NULL)，理由见 s_last_announced_min 的注释。 */
static int64_t s_last_escalated_min = -1;

/* 「这一次服药确认是**按键发起**的」标记（2026-11-03 用户裁决）。
 *
 * 语义（这是本变量的全部意义，别简化成"老人按过键"）：
 *   老人长按了服药提醒的确认键 → 设备不直接确认，而是追问「您吃的是哪种药？」
 *   → 服务端在后续对话里问清药名、判定完成 → 这次确认是**语音 + 按键**，
 *   服务端 ConfirmMethod.VOICE_AND_BUTTON(5) = 用户说的"全置信"。
 *   老人没按任何键、直接在对话里说「我吃了降压药」⇒ 仅语音(1)，不是全置信。
 *
 * 为什么必须两个字段：
 *   · server_id 定位"哪一条提醒"。否则老人先按了 8:00 的降压药、转头去说
 *     20:00 的钙片，那条钙片会被算成全置信 —— 替老人编造了一次没做过的按键。
 *   · 时间戳是有效期。老人按了键却聊了半小时才说药名，中间可能已经干别的
 *     去了，"按键发起"这个前提已经不成立，必须自动失效。
 *
 * 有效期 10 分钟的理由：与半置信复核窗（reminder.c 的 HALF 窗口）同量级，
 * 足够走完「问一句 → 老人想一下 → 回答 → 服务端判定」这段对话；再长就会
 * 把一次按键"贷"到下一顿饭上。
 *
 * 只活在 RAM（不进 NVS、不进 g.table）：重启后标记消失 ⇒ 退化成"仅语音"。
 * 宁可少记一次全置信，也不能凭一个来历不明的旧标记替老人编造按键 ——
 * 依从率数据一旦掺假就再也修不回来。 */
static uint32_t s_med_armed_server_id = 0;
static int64_t s_med_armed_us = 0;
static const int64_t kMedArmedTtlUs = 10LL * 60 * 1000000;   /* 10 分钟 */

/* 时段词（用户 2026-11-03 定义）：
 *   上午 <12:00、中午 12:00–13:59、下午 14:00–17:59、晚上 ≥18:00 */
static const char *TimeOfDayWord(int hour)
{
    if (hour < 12) return "上午";
    if (hour < 14) return "中午";
    if (hour < 18) return "下午";
    return "晚上";
}

/* 取「这条提醒自己的钟点」。
 *
 * 用 fired_due_at（本次实例的到期时刻）而不是 time(NULL)：
 *   11:59 的提醒因为 tick 晚了一秒在 12:00:01 触发时，用墙上时钟会念成
 *   「您今天中午」，而屏幕上写的是 11:59 —— 老人会以为设备算错了时间。
 * fired_due_at 为 0（异常路径）时退回条目自己的 hour。 */
static int DueHour(const elder_rmd_item_t *item)
{
    if (item->fired_due_at > 0) {
        time_t t = (time_t)item->fired_due_at;
        struct tm tmv;
        if (localtime_r(&t, &tmv) != nullptr) {
            return tmv.tm_hour;
        }
    }
    return (int)item->hour;
}

void OnReminder(const elder_rmd_item_t *item, elder_rmd_state_t st, void *ctx)
{
    (void)ctx;
    ESP_LOGW(TAG, "提醒 '%s' (rid=%lu) -> state %d", item->label,
             (unsigned long)item->server_id, (int)st);

    switch (st) {
    case ELDER_RMD_PENDING: {
        // 形象切到「惊讶/提醒」，保持 30s 后自动回落
        elder_ui_set_state(ELDER_UI_REMINDER, 30000);

        /* ---- 同一分钟只播一次（2026-11-03 用户需求）----
         *
         * 现场：老人 8:00 同时有「降压药」「降糖药」「喝水」三条提醒时，
         * 三条各自回调一次 OnReminder(PENDING)，于是三遍铃声 + 三句话
         * 叠在一起播 —— 听不清、也分不出有几件事。
         *
         * 判据是**提醒实例的到期分钟**（fired_due_at/60），不是墙上时钟，
         * 理由见 s_last_announced_min 的注释。 */
        const int64_t due_min =
            (item->fired_due_at > 0) ? (item->fired_due_at / 60) : -1;
        const bool same_minute = (due_min > 0 && due_min == s_last_announced_min);
        if (due_min > 0) s_last_announced_min = due_min;

        /* 屏幕：**每条都推**，即使这一分钟已经播过音。
         *
         * 老人需要知道"不止一条"——文案由 elder_reminder_screen_text() 统一
         * 生成（内部带「（还有 N 条）」），与 reminder.c 的反向对账共用同一份
         * 实现。原先两处各写一份 snprintf、靠注释约定「改一处要改两处」，
         * 这一轮加了条数提示，手写两份必然漂移；漂移的表现是屏幕上的句子
         * 在两条路径之间来回跳，老人会以为是两条不同的提醒。
         *
         * 文案里的主行动仍然是「长按按钮」（2026-10-29 用户实报后定的）：
         * xiaozhi 只在 listening 态才上传音频，待机时对着设备说话不可能有
         * 反应，所以屏幕不能把「说一句」写成指引。 */
        char screen_text[256];
        elder_reminder_screen_text(item, screen_text, sizeof(screen_text));
        elder_ui_show_reminder(screen_text);

        /* ---- 日程类提醒：播报即完成（2026-11-03 用户需求）----
         *
         * 用户原话：「日程类提醒只要进行提醒就算完成流程了，可以在对话中向
         * 用户询问。例如『您让我提醒您的今天要还信用卡，您已经完成了吗？』」。
         *
         * 也就是：日程类**不需要**老人按任何键或回一句话，播出来流程就走完了。
         * 服药类不走这条 —— 它必须确认，依从率和漏服告警都看状态。
         *
         * 放在推屏**之后**、same_minute 提前返回**之前**：
         *   · 之后：屏幕已经推上去了，完成状态不会把屏收掉（auto_done 刻意
         *     不回调 OnReminder(DONE)，见 elder_reminder_auto_done 的注释）；
         *   · 之前：同一分钟里的第 2、3 条也要完成，不能因为"这一分钟已经
         *     播过音"就让它们永远挂着待办。
         *
         * 为什么不让服务端去做这件事：设备可能离线，而日程提醒是设备本地
         * 时钟触发的，本地不落完成就会一直占着待办位、重启后还会二次响铃。
         * 服务端那边也有一份（device_api.report_event 收到 FIRED 时落
         * CONFIRMED/AUTO），两边各自闭环、互不依赖。 */
        if (item->type != ELDER_RMD_TYPE_MEDICINE) {
            esp_err_t rc = elder_reminder_auto_done(item->id);
            if (rc != ESP_OK && rc != ESP_ERR_INVALID_STATE) {
                ESP_LOGW(TAG, "日程类自动完成失败（id=%u rc=%d），保持待办",
                         (unsigned)item->id, (int)rc);
            }
        }

        if (same_minute) {
            ESP_LOGI(TAG, "同一分钟（%lld）已播报过，只推屏不出声：%s",
                     (long long)due_min, item->label);
            break;
        }

        /* ---- 话术带时段，必须走 TTS（2026-11-03 用户需求）----
         * 「提醒音效 + 您今天上午/中午/下午/晚上 + 有服药提醒，请您知悉」。
         *
         * 2026-11-03 用户第二版措辞：去掉「一条」—— 老人听到"一条"会以为
         * 还有别的、去数，而他此刻只需要知道"这个时段有这件事"。
         *
         * 为什么不能用预录片段：片段是编译进 .rodata 的固定资源，
         * `PlaySound` 只吃一个 `string_view`，运行时**零参数化能力** ——
         * 它永远只会说「您有一条服药提醒」，念不出时段（更念不出药名）。
         * 所以这条路径走 ScheduleAnnounce() 的 TTS 分支。 */
        char speak[96];
        snprintf(speak, sizeof(speak), "您今天%s有%s，请您知悉",
                 TimeOfDayWord(DueHour(item)),
                 item->type == ELDER_RMD_TYPE_MEDICINE ? "服药提醒" : "待办事项");

        /* 「在线」用两个判据一起看：
         *   · OnIsOnline()   —— xiaozhi 设备状态不是配网/启动中（TTS 通道前提）；
         *   · elder_net_is_online() —— 后端 HTTP 最近一次成功（软状态）。
         * 只看前者会在"Wi-Fi 掉了但设备停在 Idle"时误判成在线，TTS 请求
         * 静默失败 —— 老人只听到一声音乐、一个字都没有。 */
        if (!OnIsOnline(nullptr) || !elder_net_is_online()) {
            /* 离线：TTS 一定不可用。回落预录片段 —— 它虽然不带时段，
             * 但含提醒音 + 与在线**同口径**的话术（「您今天有服药提醒，
             * 请您知悉」），比"只有一声音乐"强。
             * 断网时提醒内容可以简化，但不能没有，这是本工程一贯的口径。
             *
             * ⚠️ 2026-11-03：片段里**不再含**「请长按…确认」。此前片段比在线
             * 多一句行动指令，导致同一条提醒"断网时催按键、联网时不催"，
             * 老人听到什么取决于当时有没有 Wi-Fi。行动指令现在只由
             * CONFIRM_HINT 在升级态播 —— 那才是该催的时机。
             * 改片段话术必须同步改 tools/make_elder_clips.py 并重新生成 .ogg。 */
            ESP_LOGW(TAG, "离线：时段话术不可用，回落预录片段");
            elder_clip_id_t clip = (item->type == ELDER_RMD_TYPE_MEDICINE)
                                       ? ELDER_CLIP_MEDICINE : ELDER_CLIP_TODO;
            if (!elder_clip_play(clip)) {
                elder_ui_play_tone(ELDER_TONE_REMINDER);
                ScheduleAnnounce(speak);
            }
            break;
        }

        /* 在线：提醒音效 + 时段话术。
         *
         * 音效用 ELDER_TONE_REMINDER（那段 4.9 秒的铃声），不是 POPUP：
         * ScheduleAnnounce 的 kChimeMs 就是按它的长度校准的（4900ms）。
         * 换成 0.5 秒的 POPUP，TTS 会在 4.4 秒静默之后才开口，老人会以为
         * 设备卡住了；而且"提醒音效"本来就是指这段铃声，不是一声叮咚。 */
        elder_ui_play_tone(ELDER_TONE_REMINDER);
        ScheduleAnnounce(speak);
        break;
    }
    case ELDER_RMD_DONE:
        elder_ui_hide_reminder();          /* 确认完成 ⇒ 全屏提醒立刻收起 */
        elder_ui_set_state(ELDER_UI_CONFIRMED, 8000);
        /* ⚠️ 2026-11-03：**故意不接** ELDER_CLIP_DONE，仍然播 0.5 秒的
         * SUCCESS 哔声。不是漏接，别"顺手补上"。
         *
         * 原因：`PlaySound` 把包推进解码队列，队列满就阻塞等（上限 40 包 =
         * 2.4 秒，见 elder_clip.h 的算式）。所以任何后到的声音都必须等
         * **前面整段播完**才开始。老人按确认时 MEDICINE 片段往往还在播
         * （10.56 秒），此刻 DONE 无论播片段（2.58 秒）还是播 tone（0.5 秒），
         * 起点都是"片段播完那一刻"—— 也就是说延迟是片段长度决定的，
         * **换实现并不能让它更早响**。
         * 那为什么还要用 tone：tone 只有 0.5 秒，不会在老人已经确认之后
         * 再占住音频通道 2.6 秒（这段时间里他想再说一句话就排不进去）。
         * 按下的即时反馈由 #1 指示灯（闪深蓝绿 3 次）+ 屏幕切确认态提供，
         * 不依赖声音。
         * ⇒ 要真正缩短"按了键才听到已记录"的延迟，只能缩短 MEDICINE/TODO
         *    的话术（每减 1 个汉字约省 0.21 秒），或动音频通路加打断（红线）。 */
        elder_ui_play_tone(ELDER_TONE_SUCCESS);
        break;
    case ELDER_RMD_ESCALATED: {
        /* 超时升级：**不收全屏提醒**（用户明确要求"没确认就一直显示"），
         * 只换表情 + 说一句话。
         * 灯也不再改成"一直闪"——交给 elder_rgb 的 2 小时节奏，
         * 见 reminder.c 的说明。
         *
         * 2026-11-03 用户需求：「请长按机身顶部凸起的橙色大按键确认」这句
         * 只在「服药日程生效但未确认、已升级」时主动播放。这里正是那个
         * 唯一入口 —— PENDING 不播它（那时老人刚听到提醒，再催一遍只会
         * 显得聒噪），SKIPPED/DONE 更不播。
         *
         * 为什么服药类**只播 confirm_hint、不和 ESCALATED 连播**：
         *   · 两条连播 2.52 + 4.56 ≈ 7.1 秒。老人已经等了 15 分钟，
         *     再让他听 7 秒才听到"该按哪个键"，只会更烦躁；
         *   · 两条语义重叠（escalated 是「该吃药了，请长按按钮」），
         *     先播它等于把关键指令推后 2.5 秒；
         *   · 此刻屏幕已经是 MISSED 表情 + 全屏大字，"还没确认"这件事
         *     已经用灯和屏说过两遍了，声音只该补"该按哪个键"。
         * ⇒ 服药类只播 confirm_hint（4.56 秒，一句话说清位置和动作）；
         *   非服药（喝水/运动/日程）继续播 escalated —— confirm_hint 里
         *   明说"服药确认"，对它们是错的，会让老人以为要吃错东西。 */
        elder_ui_set_state(ELDER_UI_MISSED, 0);

        /* ---- 同一分钟只播一次（2026-11-03 残留修复）----
         *
         * 现场：老人 8:00 有「降压药」「降糖药」「喝水」三条提醒，15 分钟后
         * 三条同时升级 ⇒ 三次 confirm_hint（4,560ms × 3 ≈ 13.7 秒）连着播。
         * 老人等这一句"该按哪个键"已经等了 15 分钟，再排队听三遍只会更烦。
         *
         * 屏幕照旧**每条都切** MISSED（在去重之前就切了）：老人要知道不止一条；
         * 声音只该有一遍。
         *
         * ⚠️ 判据用 fired_due_at/60，且必须用**独立**的 s_last_escalated_min，
         *    不能复用 PENDING 那个 s_last_announced_min —— 同一条提醒的
         *    PENDING 与 ESCALATED 共用同一个 fired_due_at，共用变量会把升级
         *    这次判成"同分钟已播"而静默。理由见变量定义处的注释。 */
        const int64_t esc_min =
            (item->fired_due_at > 0) ? (item->fired_due_at / 60) : -1;
        const bool esc_same_minute =
            (esc_min > 0 && esc_min == s_last_escalated_min);
        if (esc_min > 0) s_last_escalated_min = esc_min;
        if (esc_same_minute) {
            ESP_LOGI(TAG, "同一分钟（%lld）已播过升级提示，只切表情不出声：%s",
                     (long long)esc_min, item->label);
            break;
        }

        if (item->type == ELDER_RMD_TYPE_MEDICINE) {
            if (!elder_clip_play(ELDER_CLIP_CONFIRM_HINT)) {
                elder_ui_play_tone(ELDER_TONE_EXCLAMATION);
            }
        } else if (!elder_clip_play(ELDER_CLIP_ESCALATED)) {
            elder_ui_play_tone(ELDER_TONE_EXCLAMATION);
        }
        break;
    }
    case ELDER_RMD_SKIPPED:
    case ELDER_RMD_HALF_EXPIRED:
        /* 这两态都是"这一次作废/跳过了" —— 引擎里已经没有待确认项，
         * #1 的橙色呼吸也已经灭了（set_state 里调了 button_pending(false)）。
         * **全屏提醒必须一起收掉**（2026-10-29 用户实报后加）。
         *
         * 事故现场：8 点的服药提醒，10 点屏幕还挂着"说「我吃过了」"，
         * 老人念了没反应、长按按钮也没反应，只能先唤醒再确认。
         * 根因就是这里 —— 屏幕的显示时长是 elder_ui 的 4 小时（独立计时），
         * 而按键确认的判据是 elder_reminder_first_ringing()（只认 PENDING /
         * CONFIRM_WIN / ESCALATED / HALF_CONFIRMED）。一旦引擎把这条判成
         * SKIPPED，屏幕还在喊、按键却选不中目标 ⇒ 彻底没反应。
         *
         * 而语音那条反而"能用"，因为服务端 MCP 直接调 confirm(id)，
         * 不经过 first_ringing()。两条确认路判据不一致，症状就是
         * "语音能确认、按键不能"。
         *
         * 收掉屏幕是**兜底**：根治在 reminder.c 的 replace_all
         * （server_id 变了要按内容兜底继承运行态，别把已响过的提醒
         * 当成全新条目重置成 IDLE）。 */
        elder_ui_hide_reminder();
        ESP_LOGW(TAG, "提醒 '%s' 作废/跳过 ⇒ 收掉全屏提醒（不再让屏幕"
                      "提示一个按不动的操作）", item->label);
        break;
    default:
        break;
    }

    // 回执入队（离线则攒着，联网后补发）
    elder_sync_on_reminder_state(item, st);
}

/* ---------------------------------------------------------------- 按键 */

/* 找「此刻正在等确认」的那条提醒。返回下标，没有则 -1。
 * 抽成函数的原因：短按直接确认、长按确认、语音确认三条路必须用**同一套判据**，
 * 否则会出现「长按能确认、短按确认到别的条」这种对不上的行为。
 *
 * 2026-10-22 需求 8 把它从"只看服药"扩展到"任意提醒"：
 * 现在日程提醒也会全屏显示并要求"语音/按键确认完成"，判据若仍只认服药，
 * 老人对日程说「做完了」会被回一句"没有待确认的提醒"。
 *
 * 2026-10-22 用户实报 bug（喝水提醒响铃时说「弄好了」，确认记到了吃药
 * 提醒上）：根因是"见到第一条服药就返回"，正在响铃的日程提醒永远选不中。
 * 所以目标只能是 elder_reminder_first_ringing()：状态在
 * PENDING / CONFIRM_WIN / ESCALATED 且 enabled 的那条，多条时取
 * fired_due_at 最大（最近触发）的一条 —— 老人回应的就是刚响的那条。
 *
 * 2026-10-27 删掉了原来的「兜底」分支（原逻辑：没有在响的，就退回
 * "任意 enabled 且今天没确认过的"，服药优先）。两个真机后果：
 *   ① 老人长按 3 秒，灯闪了"成功"，可 #1 从来没亮过橙色呼吸 ——
 *      因为确认走的是兜底分支，而 #1 的"待确认"判据只认 PENDING 集合。
 *      用户原话：「现在没有了长按颜色渐变的过程，只有长按 3 秒之后
 *      显示一次深蓝绿色」。灯和行为各说各话，比不亮更糟。
 *   ② 兜底会确认**还没到点**的提醒：上午 10 点按一下，就能把晚上的药
 *      标成"已完成"。对一个吃药的老人，这是真事故，不是小瑕疵。
 *   ⇒ 只认"正在等确认"，没有就明确地什么都不做。 */
int FirstUndoneReminder()
{
    return elder_reminder_first_ringing();
}

/* 按下标取条目（只读）。越界 / 表未就绪返回 nullptr。
 *
 * 为什么不在 OnKey 里直接调 elder_reminder_list()：本文件要用"这一条是不是
 * 服药类"两次（短按防误触、长按记按键发起标记），各写一遍遍历迟早漂移，
 * 而漂移的表现是"短按被挡、长按没记标记"这类只在真机上看得见的行为差异。 */
static const elder_rmd_item_t *ItemAt(int idx)
{
    const elder_rmd_table_t *tbl = nullptr;
    if (elder_reminder_list(&tbl) != ESP_OK || tbl == nullptr) return nullptr;
    if (idx < 0 || idx >= tbl->count) return nullptr;
    return &tbl->items[idx];
}

/* method 区分「按键确认」与「语音确认」（ELDER_CFM_BUTTON / ELDER_CFM_VOICE）。
 * 这个值会随回执上报服务端，子女端据此区分「老人自己按的」还是「跟小智说的」，
 * 所以两条路不能都填 BUTTON —— 否则事后无法判断确认的真实来源。 */
void ConfirmMedicine(int idx, uint8_t method)
{
    if (idx < 0) return;
    ESP_LOGI(TAG, "%s确认服药：提醒 #%d",
             method == ELDER_CFM_VOICE ? "语音" : "按键", idx);
    elder_reminder_confirm_ex((uint8_t)idx, method);
    elder_input_close_confirm_window();
    s_arming_id = -1;
}

/* 老人回应过之后，把"已经没有下文"的全屏提醒收掉。
 *
 * 判据只有一个：引擎里真的没有待确认项（`elder_reminder_has_pending_confirm()`，
 * 与 reminder.c 的 has_pending_confirm() 同一口径）。还有别的在等就**不动屏**
 * —— 那条才是此刻该看的。这是 D71 那条纪律的又一次落地：屏幕与引擎必须同源，
 * 不许用屏幕自己记的文案/时长做判断。
 *
 * 为什么需要它（2026-11-03 用户实报「我用语音确认喝水完成了，但是界面一直
 * 停留在提醒页面」）：
 *   日程类（喝水/运动/待办）在 OnReminder(PENDING) 里"播报即完成" ——
 *   elder_reminder_auto_done() **刻意不回调** OnReminder(DONE)，所以它从来
 *   走不到上面 case ELDER_RMD_DONE 那条收屏路径。而屏幕的显示时长是 elder_ui
 *   自己的 4 小时，于是老人说完「我喝完了」、设备也应了声，屏上那张卡片还能
 *   再挂 4 小时；按键同样选不中它（first_ringing() = -1）。
 *   ⇒ 屏幕上挂着一个按不动、也赶不走的操作 —— 正是 2026-10-29 修过的那个形态，
 *     这次是从"日程类自动完成"这个新口子漏回来的。
 *
 * 为什么不在 auto_done 里直接收屏：auto_done 紧跟 show_reminder 之后执行，
 * 那样卡片**刚推上去就消失**，老人根本来不及看。收屏的时机是"老人回应了"，
 * 不是"引擎记完了"。 */
static void HideReminderIfResolved(const char *why)
{
    if (!elder_ui_reminder_showing()) return;
    if (elder_reminder_has_pending_confirm()) return;
    elder_ui_hide_reminder();
    ESP_LOGW(TAG, "%s：引擎里已无待确认项 ⇒ 收掉全屏提醒", why);
}

/* 「先说一句、再进对话」的专用任务（2026-11-03，多药提醒）。
 *
 * 为什么顺序必须是「先说 → 等说完 → 再听」，三条都是 application.cc 的硬约束：
 *   ① TTS 只能从**待机**发起：OnSpeak() 里 `if (GetDeviceState()==kDeviceStateIdle)`
 *      才发 SendMcpMessage，其余状态**静默丢弃**（见本文件 OnSpeak）；
 *   ② StartListening()（application.cc:400-433）只在 Idle / Speaking 两个状态
 *      有动作：Idle → 开音频通道 + 进聆听；Speaking → **先 AbortSpeaking()**。
 *      先听再说会把刚发出去的问题当场掐断；
 *   ③ 它进入的是 kListeningModeManualStop，而 TTS 播完时固件在 ManualStop 下
 *      回 Idle（application.cc:585-589）—— 所以必须在"播完之后"再显式调一次
 *      StartListening，否则老人开口时设备已经不在听了。
 *
 * 为什么不在 elder_input 任务里同步做：StartListening() 里的 OpenAudioChannel()
 * 是联网握手（几百毫秒到几秒），在按键任务上阻塞会把「长按 3 秒」的判定挤歪
 * （elder_input.h 里已经踩过这个坑）。所以开独立任务，栈 5KB（与
 * AnnounceTask 同值 —— 它走的也是同一条 elder_ui_speak 路径）。
 *
 * 前置条件（已核对代码，不是推测）：
 *   · protocol_ 为空（未激活 / 未连上）时 StartListening 只打日志就返回，
 *     不崩、不死循环 —— 所以离线长按的最终表现是"问不出来"，提醒仍然挂屏；
 *   · 设备不在 Idle 时本任务直接放弃，不去抢正在进行的对话。 */

/* 此刻能不能"说一句 + 开麦"。
 *
 * 三个判据缺一不可：
 *   · OnIsOnline()          —— 设备状态不是配网/启动中（TTS 通道前提）；
 *   · elder_net_is_online() —— 后端最近一次 HTTP 成功（软状态）；
 *   · 设备在 Idle           —— StartListening 只在 Idle / Speaking 有动作，
 *                              Speaking 分支会先 AbortSpeaking() 把声音掐断。
 *
 * 注意这里**不是**"能不能播预录片段"的判据：片段是本地资源，离线也能播。
 * 它判的是"播完能不能接着开麦进对话"。 */
static bool CanStartDialog()
{
    return OnIsOnline(nullptr) && elder_net_is_online() &&
           Application::GetInstance().GetDeviceState() == kDeviceStateIdle;
}

/* 此刻能不能**播一句本地预录片段**。
 *
 * ⚠️ 2026-11-03 用户实报「长按服药确认按键，没有提示音，松开之后也没有播放
 *    声音，我主动说我吃过药了，才进入服用了哪种药物的流程」——**完全静音**。
 *
 * 根因不是片段资源坏了，而是这一行原来写的是 `CanStartDialog()`：
 *   · 那个判据要求设备在 **Idle**（它判的是"播完能不能接着开麦"）；
 *   · 而连续对话（realtime）下设备一直停在 **Listening**，Idle 判据恒假；
 *   · 于是片段不播 → 落到 TTS 兜底 → AskThenListenTask 一看"不在待机"
 *     立刻 return ⇒ 两条路都不出声。老人按满了 3 秒，听到的是彻底的安静。
 *
 * 片段是编在 .rodata 里的本地资源，**播放不需要联网、也不需要设备待机**，
 * 只要音频输出没被正在进行的播报占着（Speaking）就行。所以判据放宽到
 * 「Idle 或 Listening」：
 *   · Idle      → 播完再 StartListening（ListenAfterClipTask 负责）；
 *   · Listening → 本来就在听，播完什么都不用做。
 *
 * 不放行 Speaking / Connecting：前者会把正在说的话顶掉（ResetDecoder 在
 * SetDeviceState(Speaking) 里刚做过，插一段进去必然破音），后者音频通道还没
 * 建好，PlaySound 只会丢帧。 */
static bool CanPlayClip()
{
    const DeviceState st = Application::GetInstance().GetDeviceState();
    return OnIsOnline(nullptr) &&
           (st == kDeviceStateIdle || st == kDeviceStateListening);
}

/* 「先播预录问句、播完再开麦」的专用任务（2026-11-03 用户需求）。
 *
 * 用户原话：「如果用户长按时显示的是服药提醒，就播放第二句，并进入聆听状态，
 * **避免因等待服务端合成而长时间没有说话，让老人以为设备没有响应**。」
 *
 * 为什么用"实测时长 + 余量"而不是等回调：`AudioService::PlaySound` 没有任何
 * 完成回调（见 elder_clip.h 的说明），按 kClipMs 延时是这套 API 下唯一确定的
 * 顺序保证 —— 与 ScheduleAnnounce 的 kChimeMs 同一套做法。
 *
 * 为什么余量只给 400ms 而不是更多：这句问完老人就会开口，等太久他会以为
 * 设备没听见、再说一遍，反而把这一轮挤掉。400ms 只够解码器起播与 I2S 缓冲。
 *
 * 栈 4096：本任务只做 vTaskDelay + StartListening()，不拼 JSON、不碰 LVGL。
 * （对比 AskThenListenTask 的 5120 —— 它要过一次 elder_ui_speak。） */
struct ClipListenArg {
    elder_clip_id_t id;
};

static void ListenAfterClipTask(void *arg)
{
    auto *p = static_cast<ClipListenArg *>(arg);
    const elder_clip_id_t id = p->id;
    delete p;

    auto &app = Application::GetInstance();
    const uint32_t wait_ms = elder_clip_duration_ms(id) + 400;
    vTaskDelay(pdMS_TO_TICKS(wait_ms));

    /* ---- 等片段播完之后，**轮询**到能开麦为止（2026-11-04 用户实报）----
     *
     * 用户原话：「也触发了『您确认已经完成这次服药了吗』的语音提醒，但是没有
     * 进入聆听状态，而是进入了待机状态。」
     *
     * 根因：PlaySound 只是把包推进解码队列，队列里若还压着前面的提醒播报，
     * 片段**实际起播**会比 elder_clip_duration_ms() 晚。原来这里是一次性判定
     * —— 到点看一眼状态，不是 Idle/Listening 就只打一条 WARN 走人，于是
     * 老人听完问句、设备却回待机，没有任何人开麦。
     *
     * 改成轮询：每 100ms 看一次，最多再等 4000ms。
     *   · 见到 Idle      ⇒ StartListening()（Application 在 ManualStop 模式下
     *                       播完会回 Idle，这是唯一需要主动开麦的时刻）；
     *   · 见到 Listening ⇒ 连续对话下本来就在听，**不要**再 StartListening
     *                       （那会先 AbortSpeaking，白重开一次音频通道）；
     *   · 其余状态（Speaking/Connecting/配网中）⇒ 继续等，不抢。
     * 只有超时才报 ERROR —— 那是真的没开麦，必须能从日志里看出来。
     *
     * 只做 vTaskDelay + GetDeviceState + StartListening，不引入 std::string/JSON，
     * 所以栈仍然是 4096（见 ListenAfterClip 的 xTaskCreate）。 */
    const uint32_t poll_ms = 100;
    const uint32_t extra_max_ms = 4000;
    uint32_t extra_ms = 0;
    DeviceState last_st = app.GetDeviceState();

    for (;;) {
        const DeviceState st = app.GetDeviceState();

        /* 只在**状态变化**时打日志，别每 100ms 刷一行。 */
        if (st != last_st) {
            ESP_LOGI(TAG, "预录问句等待开麦：状态 %d -> %d（已等 %lu ms）",
                     (int)last_st, (int)st,
                     (unsigned long)(wait_ms + extra_ms));
            last_st = st;
        }

        if (st == kDeviceStateIdle) {
            app.StartListening();
            ESP_LOGI(TAG, "预录问句播完（共等 %lu ms），进入聆听",
                     (unsigned long)(wait_ms + extra_ms));
            break;
        }
        if (st == kDeviceStateListening) {
            ESP_LOGI(TAG, "预录问句播完（共等 %lu ms），设备已在聆听态",
                     (unsigned long)(wait_ms + extra_ms));
            break;
        }
        if (extra_ms >= extra_max_ms) {
            /* 必须带**最后一次看到的状态值**：没有它就无法区分"还在播"
             * 和"卡在配网/连接"，下次还是只能靠猜。 */
            ESP_LOGE(TAG, "预录问句播完，又等了 %lu ms 仍未进待机/聆听"
                          "（最后状态=%d，共等 %lu ms），本次不开麦",
                     (unsigned long)extra_ms, (int)st,
                     (unsigned long)(wait_ms + extra_ms));
            break;
        }

        vTaskDelay(pdMS_TO_TICKS(poll_ms));
        extra_ms += poll_ms;
    }

    vTaskDelete(nullptr);
}

static void ListenAfterClip(elder_clip_id_t id)
{
    auto *arg = new ClipListenArg{id};
    if (xTaskCreate(ListenAfterClipTask, "rmd_listen", 4096, arg, 3,
                    nullptr) != pdPASS) {
        /* 建不出任务 ⇒ 不会开麦。片段已经播出去了，老人会以为"它问完了在等我"，
         * 所以必须留下 ERROR，不能静默。 */
        ESP_LOGE(TAG, "开麦任务创建失败，本次不会进入聆听");
        delete arg;
    }
}

static void AskThenListenTask(void *arg)
{
    auto *text = static_cast<std::string *>(arg);
    auto &app = Application::GetInstance();

    if (app.GetDeviceState() != kDeviceStateIdle) {
        ESP_LOGW(TAG, "长按追问：设备不在待机（state=%d），不打断当前对话",
                 (int)app.GetDeviceState());
        delete text;
        vTaskDelete(nullptr);
        return;
    }

    elder_ui_speak(text->c_str());   /* 在线走 TTS，离线自动降级成提示音 */
    delete text;

    /* 等这句话真的开始播。TTS 是异步的：请求发出后 0.3~1.2 秒才收到首帧，
     * 这段时间状态仍是 Idle。此刻立刻 StartListening 会先开麦、随后被 TTS
     * 推到 Speaking，播完在 ManualStop 下回 Idle —— 老人开口时已经没人听。
     * 最多等 3 秒；离线时永远等不到，直接落到下一步（由 StartListening
     * 自己打日志失败，不在这里死等）。 */
    int waited_ms = 0;
    while (waited_ms < 3000 && app.GetDeviceState() == kDeviceStateIdle) {
        vTaskDelay(pdMS_TO_TICKS(100));
        waited_ms += 100;
    }

    /* 等播完：回到 Idle，或（AutoStop / Realtime 下）自动回到 Listening。 */
    while (waited_ms < 15000) {
        DeviceState st = app.GetDeviceState();
        if (st == kDeviceStateIdle || st == kDeviceStateListening) break;
        vTaskDelay(pdMS_TO_TICKS(100));
        waited_ms += 100;
    }

    if (app.GetDeviceState() == kDeviceStateIdle) {
        app.StartListening();
    }
    vTaskDelete(nullptr);
}

/* 这个按键**只做日程/服药确认**（2026-10-20 用户拍板取消 SOS；
 * 2026-10-27 用户进一步拍板：连"开始/结束对话"也去掉，单一功能）：
 *   提醒响铃期间（ELDER_UI_REMINDER）短按 → 直接确认，不需要任何前置手势；
 *   确认窗口内短按                   → 确认长按挂起的那一条；
 *   其余情况短按                     → **什么都不做**（只打日志）。
 * 对话开关不在这里：板载用户按键（GPIO0 BOOT）才是一键唤醒/打断，见
 * lichuang_dev_board.cc 的 OnClick。
 * SOS 分支已删除，原因见 elder_input.h 头注释（长按同步跑联网上报把栈撑爆，
 * 设备重启，求助根本发不出去）。**不要把 HTTPS 放回这个回调里** ——
 * 它是 elder_input 任务同步调用的（栈 8192，但那是给 UI/提示音留的，
 * 不是给 TLS 留的）。 */
void OnKey(elder_key_event_t ev, void *ctx)
{
    (void)ctx;

    switch (ev) {
    case ELDER_KEY_EV_SHORT: {
        int idx = -1;
        bool open_window = false;
        if (elder_input_confirm_window_active() && s_arming_id >= 0) {
            idx = s_arming_id;
        } else if (elder_ui_reminder_showing() ||
                   elder_ui_get_state() == ELDER_UI_REMINDER) {
            /* 老人听到「该吃药了」，直接按一下就行 —— 这是本按键的主用途。
             *
             * 判据要**同时**看"全屏提醒是否在显示"：全屏提醒会挂 4 小时，
             * 而 ELDER_UI_REMINDER 这个表情态 30 秒就自动回落 IDLE。
             * 只看表情态的话，老人 30 秒后才去按按钮，这一次就选不中目标，
             * 全屏提醒照旧挂着 —— 用户的诉求「按键确认任务完成」直接失效。
             * （这条判据在 2026-10-27 去掉"短按切换对话"之后**更加**重要：
             *  现在没有兜底分支了，选不中目标就是彻底没反应。） */
            idx = FirstUndoneReminder();
            if (idx >= 0) {
                /* 先**不**开确认窗：下面要先判"是不是服药类"。服药类连窗都
                 * 不能开 —— 确认窗 + #1 呼吸灯是"按一下就能确认"的视觉承诺，
                 * 而这个动作对服药提醒已经被禁掉了（见下方防误触注释）。 */
                open_window = true;
            }
        }

        if (idx >= 0) {
            /* ---- 服药类**禁止短按确认**（2026-11-03 用户需求，防误触）----
             *
             * 短按是 0.3 秒的动作，老人擦屏幕、挪设备、手抖碰一下都可能触发；
             * 而服药确认是医疗行为：误确认会让设备停掉提醒（老人真的漏服），
             * 也会让子女端看到"已服药"的假依从率。所以服药类必须走长按 3 秒
             * （要持续用力，几乎不可能误触），短按在这里**什么都不做**。
             *
             * 非服药（喝水/日程/运动）保持原来的短按确认：那些事误确认的
             * 代价只是少提醒一次，不值得让老人多花 3 秒。
             *
             * ⚠️ 确认窗必须**在判完类型之后**才开（上面的 open_window 就是
             *    为此延后的）：先开再关会让屏幕先闪一下确认态，老人看到的是
             *    "设备认了、又反悔了"。已经开着的窗（idx 来自 s_arming_id）
             *    也一并收掉 —— 那种状态只可能来自旧逻辑残留，留着就是亮着
             *    一个按不动的承诺。
             *
             * ---- 顺带核实：本项目有没有"可配置的短按定义"？----
             * **没有，一处都没有。** 2026-11-03 逐个 grep 过：
             *   · sync.c 解析服务端下发只认提醒规则（reminders + when），
             *     没有任何按键/动作字段；NVS 里只有 evtq / token / epoch /
             *     tzmin 四个键，也没有按键映射；
             *   · elder_reminder.h 的 elder_rmd_item_t 全是提醒字段
             *     （type/label/hour/minute/repeat_mask/enabled…），
             *     没有 short_press_action 之类；
             *   · ui.c 只负责"显示什么"，不参与按键行为决策；
             *   · elder_input 只上报 SHORT / LONG_HOLD / RELEASE 三个**原始
             *     事件**，行为全部在 OnKey 里写死（本文件就是唯一真源）。
             * 上游 xiaozhi 板级代码里的 `short_press_time = 500` 是**去抖/
             * 判定阈值**（多少毫秒算短按），不是"短按做什么"，别混为一谈。
             * ⇒ 对用户可以明确回答：不存在短按定义配置项，短按行为在代码里
             *   固定，所以"不能设置短按定义"是代码事实，不是漏做了功能。 */
            const elder_rmd_item_t *target = ItemAt(idx);
            if (target == nullptr) {
                /* 取不到条目 = 表未就绪或下标错。服药确认是医疗行为，
                 * 信息不明时**宁可不确认**（fail-closed），不能赌它是日程。 */
                elder_input_close_confirm_window();
                s_arming_id = -1;
                ESP_LOGE(TAG, "短按：取不到提醒 #%d 的条目，拒绝确认（fail-closed）",
                         idx);
                break;
            }
            if (target->type == ELDER_RMD_TYPE_MEDICINE) {
                elder_input_close_confirm_window();
                s_arming_id = -1;
                ESP_LOGI(TAG, "短按：提醒 #%d「%s」是服药提醒，不支持短按确认"
                              "（防误触），请长按 3 秒", idx, target->label);
                break;
            }
            if (open_window) {
                elder_input_open_confirm_window();   /* 让界面立刻进确认态 */
                s_arming_id = idx;
            }
            elder_rgb_button_confirmed();   /* #1 闪深蓝绿 3 次 */
            ConfirmMedicine(idx, ELDER_CFM_BUTTON);
        } else {
            /* 没有待确认提醒时，这一下**什么都不做**（2026-10-27 用户拍板）。
             *
             * 原先这里退化成 app.ToggleChatState()，效果是「没有提醒时按一下
             * 就切换对话」：设备正在听/正在说，按一下就直接进待机；老人想
             * 确认任务，结果设备静音了 —— 语义完全错位，且老人会以为设备坏了。
             *
             * 现在职责彻底分开：
             *   本按键（GPIO11）  → 只有「确认日程完成」一个功能
             *   板载用户按键（GPIO0 BOOT）→ 一键唤醒 / 打断说话，见
             *                        lichuang_dev_board.cc 的 OnClick
             *
             * 不发声、不闪屏：没有提醒可确认时，「安静地没反应」比
             * 「莫名其妙开始说话/静音」安全得多。按下的即时反馈由
             * #1 指示灯给（input.c 调 elder_rgb_button_press/release）。
             *
             * ⚠️ 2026-11-03：这里的"选不中目标"不再一律当成脱节。日程类提醒
             * 在播报时就 auto_done 了（喝水/运动/待办不需要确认），屏却还挂着
             * 4 小时 —— 那不是 bug，是新语义；老人此刻按一下的意图仍然是
             * "这件事我处理了"，所以**收屏**而不是报错。真脱节（屏上还有服药
             * 提醒在等）仍会由 HideReminderIfResolved 的第二个判据挡下并留下
             * 下面那条 ERROR。 */
            HideReminderIfResolved("短按：当前无待确认项");
            if (elder_ui_reminder_showing()) {
                ESP_LOGE(TAG, "⚠️ 全屏提醒还挂着，但引擎里没有待确认项 —— "
                              "短按无效。screen 与引擎状态已脱节");
            } else {
                ESP_LOGI(TAG, "短按：当前没有待确认的提醒 —— 本按键只做确认，"
                              "不做对话开关");
            }
        }
        break;
    }

    case ELDER_KEY_EV_LONG_HOLD: {
        /* 2026-10-27 用户重新定义：**按住够 3 秒 = 确认完成**，
         * 不再是"长按挂起 + 30 秒内再短按一次"的两步流程。
         *
         * 为什么必须改成一步：新灯效是「按住期间 #1 从浅绿渐变到深绿，
         * 走满即完成」（见 elder_rgb.h 的 #1 状态机）。灯已经走到最深绿了，
         * 却还要再按一下才算数 —— 老人在这一步犹豫一下就超时，
         * 失败率反而比一步高。灯说什么，行为就得是什么。
         *
         * 反馈：确认成功 → #1 闪深蓝绿 3 次（rgb.c 负责）；
         *       没确认成 → 松开时 #1 闪深粉红 5 次。
         * 先给灯再改状态：老人按下 3 秒那一刻就要看到"成了"，
         * 而不是等提醒引擎落完 NVS 才亮。
         *
         * ⚠️ 2026-11-03 起，「长按 = 直接确认」只对**日程/待办类**成立。
         *    服药类（不论几味药）长按改成"先播一句预录问句 + 进对话"，
         *    由服务端大模型判断哪条日程完成（见下面的分支与 ListenAfterClip）。
         *    原因：固件只拿到"有几味药、第一味叫什么"，拿不到"老人吃了哪几味"；
         *    直接标完成等于替老人谎报服药。
         *    唯一的例外是"药数未知且开不了麦"（离线）—— 那时直接确认，
         *    否则老人会按了没反应。 */
        int idx = FirstUndoneReminder();
        /* ---- 进入本分支的第一现场日志（2026-11-04）----
         *
         * 用户两次报「长按不能确认」，两次都只能靠猜：日志里既没有选中了哪一条，
         * 也没有当时"能不能播片段 / 能不能开麦"。这一行把决定后续走向的
         * 全部输入一次打全 —— idx 选中了谁、type 是什么（日程还是服药）、
         * 几味药、两个能力判据、设备状态。idx<0 时 type 打 -1。
         *
         * ⚠️ 只增日志，不改任何判据。 */
        {
            const elder_rmd_item_t *dbg_item = (idx >= 0) ? ItemAt(idx) : nullptr;
            ESP_LOGI(TAG, "长按确认：idx=%d type=%d med_n=%u CanPlayClip=%d "
                          "CanStartDialog=%d state=%d",
                     idx, (dbg_item != nullptr) ? (int)dbg_item->type : -1,
                     (idx >= 0) ? (unsigned)elder_reminder_med_count((uint8_t)idx) : 0u,
                     (int)CanPlayClip(), (int)CanStartDialog(),
                     (int)Application::GetInstance().GetDeviceState());
        }
        /* ---- 3 秒达标就先给回执，**在任何分支之前**（2026-11-04 用户实报）----
         *
         * 用户原话：「我按了按键，没有语音提示，没有屏幕内容切换，粉色灯闪烁了
         * 5 次」，并且确认自己「按了 3 秒以上，灯是慢慢变亮的」。
         *
         * 粉红闪 5 次只有一个来源：rgb.c 的 elder_rgb_button_release() 发现
         * 「还在渐变、本轮没出过结果」。也就是说，**只要有任何一条分支静默
         * break 而没有调 acknowledge，老人按满 3 秒也会被判成"你没按够"**。
         *
         * 2026-11-03 01:21 的 a334cca 把服药类长按从「无条件 ConfirmMedicine」
         * 改成「播问句 + 进对话」，从那以后长按要过 CanPlayClip / elder_clip_play
         * / CanStartDialog 三道闸；任何一道不过就只打一行日志 —— 不确认、不出声、
         * 灯还闪粉红。老人看到的是"我按对了，设备说我按错了"。
         *
         * 所以回执必须与"后面成不成"解耦：**按压达标这件事本身先确认掉**。
         * 只有真的没有可确认对象（引擎空且屏也没显示）时才不给回执 —— 那时
         * 蓝闪同样是谎报。
         *
         * elder_rgb_button_acknowledged() 只在本轮仍处于渐变态时生效，重复调用
         * 无副作用（mode 已变 FLASH 就直接返回），所以下面各分支原有的调用
         * 不需要删。 */
        if (idx >= 0 || elder_ui_reminder_showing()) {
            elder_rgb_button_acknowledged();
        }
        if (idx >= 0) {
            /* ---- 日程/待办类：播预录应答并**退回对话状态**（2026-11-03 用户需求）----
             *
             * 用户原话：「如果用户长按按键时屏幕显示的是日程，就播放第一句，
             * 并从提醒状态的显示退回到对话状态」。
             *
             * 为什么这一类可以"按了就记上"：日程/喝水/运动误确认的代价只是
             * 少提醒一次；服药类才必须问清（见下面那条分支）。原逻辑对这类
             * 本来就是直接确认，这次只补一句人声 —— 老人按满 3 秒后要立刻
             * 听到"成了"，而不是只有一声哔。
             *
             * 顺序：先确认（收全屏提醒 + 0.5 秒 SUCCESS 哔），再播片段。
             * 反过来会让老人先听完 3.8 秒才看到屏收起来，"按了没反应"的
             * 观感一点没变。
             *
             * 判据用 type 而不是 med_count：med_count 为 0 的**服药**提醒
             * 不能走这里（那会把"没问过"的服药当成日程记完成）。 */
            const elder_rmd_item_t *head_item = ItemAt(idx);
            if (head_item != nullptr &&
                head_item->type != ELDER_RMD_TYPE_MEDICINE) {
                elder_rgb_button_confirmed();
                ConfirmMedicine(idx, ELDER_CFM_BUTTON);
                if (!elder_clip_play(ELDER_CLIP_SCHED_DONE)) {
                    /* 片段资源缺失（旧分区 / play_clip 未接线）：退回 TTS。
                     * 在线时才有声；离线至少屏幕已经收了，不会再挂一张
                     * "按不动的卡片"。 */
                    elder_ui_speak("日程已确认完成，我就不在屏幕显示了");
                }
                ESP_LOGI(TAG, "长按确认结论：日程类提醒 #%d 已确认"
                              "（播预录应答并退回对话态）", idx);
                break;
            }
            /* ---- 多药提醒（2026-11-03 用户需求）----
             *
             * 服药种类 > 1：长按后**不确认**，改为问「您吃的是哪种药？」
             * 并进入对话，由服务端大模型判断哪一条日程完成了、再追问
             * 没完成的那几味 —— 固件不知道老人吃了哪几味，替他把整条
             * 提醒标成"全部完成"就是谎报服药。
             *
             * 服药种类 == 1：同样不直接确认，改成念「我帮您确认一下，
             * 您刚才已经服用过<药名>了吗？」，让老人答一句"吃了"再算完成。
             * 只有一味时理论上可以直接确认，但用户明确要求"根据用户回答
             * 确定日程是否完成"，所以走同一条对话路径。
             *
             * 药数 == 0（未知，典型是刚开机还没同步到 medicines）：
             * 能进对话就照样问一句（问法不点药名，由服务端按待确认清单判断）；
             * **只有开不了麦时**（离线 / 设备忙）才退回直接确认 —— 信息缺失
             * 不能让老人确认不了，"按了就记上"比"按了没反应"安全得多。 */
            uint8_t med_n = elder_reminder_med_count((uint8_t)idx);
            if (med_n == 0 && !CanPlayClip()) {
                elder_rgb_button_confirmed();
                ConfirmMedicine(idx, ELDER_CFM_BUTTON);
                ESP_LOGW(TAG, "长按确认结论：药数未知且此刻播不了片段（离线/忙）"
                              "⇒ 直接确认 #%d", idx);
                break;
            }

            /* ---- 记下"这次确认是按键发起的"（2026-11-03 用户裁决）----
             *
             * 只有走到这里（长按 + 有药名信息 ⇒ 进追问）才算。上面 med_n==0
             * 那条是直接确认，回执本来就报 BUTTON，不需要这个标记。
             *
             * 标记的用途：老人接下来在对话里说清吃了哪味药，服务端判定完成
             * 后回灌 confirm_reminder(server_id)，设备据此报 BUTTON，服务端
             * 把「先 VOICE 后 BUTTON」合并成 VOICE_AND_BUTTON(5) —— 全置信。
             * 没有这个标记就只能报 VOICE（仅语音），用户明确说那不是全置信。
             *
             * server_id == 0（本地自建、服务端不认识的提醒）：不置位。
             * 那种条目本来就不会被服务端回灌确认，标记只会挂在那里过期。
             * 有效期与清空时机见变量定义处。 */
            const elder_rmd_item_t *armed_item = ItemAt(idx);
            if (armed_item != nullptr && armed_item->server_id != 0) {
                s_med_armed_server_id = armed_item->server_id;
                s_med_armed_us = esp_timer_get_time();
                ESP_LOGI(TAG, "按键发起标记置位：server_id=%lu（提醒 #%d，%u 味药），"
                              "有效期 10 分钟",
                         (unsigned long)armed_item->server_id, idx,
                         (unsigned)med_n);
            }

            char prompt[192];
            if (med_n >= 2) {
                snprintf(prompt, sizeof(prompt), "您吃的是哪种药？");
            } else {
                const char *name = elder_reminder_first_med_name((uint8_t)idx);
                if (name != nullptr && name[0] != '\0') {
                    snprintf(prompt, sizeof(prompt),
                             "我帮您确认一下，您刚才已经服用过%s了吗？", name);
                } else {
                    /* 有一味药但名字没解析到（脏数据）：不念半句药名，
                     * 退回不带药名的问法，语义仍然成立。 */
                    snprintf(prompt, sizeof(prompt),
                             "我帮您确认一下，您刚才已经服用过这次的药了吗？");
                }
            }

            /* 这里**不额外播提示音**，也不闪"确认成功"灯效：
             *   · 闪成功会让老人以为已经记上了，然后走开不再回答 ——
             *     这一步什么都没确认，#1 的待确认呼吸灯本来就还亮着；
             *   · 在线时问题本身 1 秒内就会开口，再加一声 POPUP 只会占住
             *     解码队列、把问题推后；离线时 elder_ui_speak 自己会播一声
             *     POPUP 兜底，这里再播一次就是连响两声。
             *
             * ⚠️ 但**必须**告诉指示灯"这一轮已经受理"（2026-11-03 用户实报
             *    「长按服药键一直是错误状态，闪烁错误事件灯，但我的操作是
             *    正确的长按 3 秒以上」）。不调的话，input.c 松开时会走
             *    elder_rgb_button_release() 的"渐变未出结果"分支 ⇒ 深粉红闪
             *    5 次（= 你没按够），而老人按得完全正确。见
             *    elder_rgb_button_acknowledged() 的说明。 */
            elder_rgb_button_acknowledged();

            /* ---- 先出声、再开麦（2026-11-03 用户需求，本次改造的核心）----
             *
             * 预录片段是编在 .rodata 里的本地资源：长按到达 3 秒那一刻就出声，
             * 不经过「请求发出 → 厂商首帧 → 下发 → 解码」这条链（实测 1~3 秒
             * 完全没声音 —— 正是用户投诉的那个感受）。
             *
             * 服务端不需要知道"刚才问的是哪句"：每轮对话开头它都会把
             * 「已到点、尚未确认的服药提醒清单 + 逐味进度」注入 system
             * （server/app/voice/session.py 的 _PENDING_REMINDER_HINT），
             * 老人回一句"吃了"它就够判断了。
             *
             * 片段不可用 / 开不了麦 ⇒ 逐字退回原来的 TTS 追问（下面那段）。 */
            if (CanPlayClip() && elder_clip_play(ELDER_CLIP_MED_CONFIRM)) {
                ESP_LOGI(TAG, "长按确认：开始播预录问句并排队开麦"
                              "（clip=%d，%u 味药，提醒 #%d，state=%d）",
                         (int)ELDER_CLIP_MED_CONFIRM, (unsigned)med_n, idx,
                         (int)Application::GetInstance().GetDeviceState());
                ListenAfterClip(ELDER_CLIP_MED_CONFIRM);
                ESP_LOGI(TAG, "长按确认结论：进追问（预录片段已排队，"
                              "开麦任务已建，播完由任务自己打状态）");
                break;
            }

            /* 走到这里说明片段没播出去 —— **必须留下原因**。
             * 2026-11-03 的现场就是这里静默失败：片段被 CanStartDialog()
             * 挡住、TTS 兜底又被 AskThenListenTask 的"不在待机"挡掉，
             * 老人按满 3 秒却一点声音都没有，串口里连一行日志都找不到。 */
            ESP_LOGW(TAG, "长按：预录问句未播（state=%d，clip=%d），退回 TTS 追问",
                     (int)Application::GetInstance().GetDeviceState(),
                     (int)CanPlayClip());

            /* TTS 兜底的前提是 **Idle**：OnSpeak() 里 `state==Idle` 才发
             * MCP 请求，其余状态静默丢弃。原来不管三七二十一先建任务，
             * 任务自己再判一次并静默 return —— 那正是"按了没声音"的第二个
             * 静默出口。现在在**建任务之前**就判，判不过就明说。 */
            if (!CanStartDialog()) {
                /* ---- 追问进不去 ⇒ **兜底直接确认**，不许静默失败 ----
                 *
                 * 2026-11-04 用户实报：「我按了按键，没有语音提示，没有屏幕
                 * 内容切换，粉色灯闪烁了 5 次」。
                 *
                 * 这里原来是 `break` —— 只打一行 ERROR 就走人。而 2026-11-03
                 * 01:21 的 a334cca 之后，服药类长按已经不再自己确认，改成
                 * 「播问句 + 进对话」；一旦片段播不出去、设备又不在待机，
                 * 老人按满 3 秒的结果就是：没声音、屏幕不动、灯闪"失败"。
                 * 他做对了，设备说他不算。
                 *
                 * 为什么这里可以退回直接确认：**信息缺失不能让老人确认不了**。
                 * 上面 med_n==0 那条分支早就写明了同一个取舍（"按了就记上"
                 * 比"按了没反应"安全得多）。追问只是"想把吃了哪几味问清楚"
                 * 的加分项，问不了的场合必须让底线成立。
                 *
                 * 代价要说清：这条提醒若有多味药，兜底会把整条标成完成，
                 * 等于替老人报"都吃了"。这是**有意的**取舍 —— 与 a334cca
                 * 之前的老行为一致，也是用户明确要求"按键一定能确认"的
                 * 直接结果。若日后要更精确，正确做法是让服务端在收到
                 * BUTTON 回执时按"逐味待确认"再追问，而不是让按键没反应。 */
                ESP_LOGW(TAG, "长按确认结论：进追问失败（state=%d，clip=%d）"
                              "⇒ 兜底直接确认 #%d（%u 味药）",
                         (int)Application::GetInstance().GetDeviceState(),
                         (int)CanPlayClip(), idx, (unsigned)med_n);
                elder_rgb_button_confirmed();
                ConfirmMedicine(idx, ELDER_CFM_BUTTON);
                /* 本地预录片段，不依赖网络：确认了就必须让老人听见。 */
                if (!elder_clip_play(ELDER_CLIP_DONE)) {
                    elder_ui_speak("已记录，今天不用再吃了");
                }
                break;
            }

            auto *payload = new std::string(prompt);
            /* 栈 5120 与 kAnnounceTaskStack 对齐：两者都走
             * elder_ui_speak → OnSpeak → std::string 拼 JSON + ESP_LOG，
             * 那个值是真机验证过的，不另调小。 */
            if (xTaskCreate(AskThenListenTask, "rmd_ask", 5120, payload, 3,
                            nullptr) != pdPASS) {
                ESP_LOGE(TAG, "追问任务创建失败，改为直接播报（不进对话）");
                elder_ui_speak(prompt);
                delete payload;
            }
            ESP_LOGI(TAG, "长按确认结论：进追问（退回 TTS，%u 味药，提醒 #%d）：%s",
                     (unsigned)med_n, idx, prompt);
        } else {
            /* 诊断：这是 2026-10-29「屏幕挂着提醒但按什么都没反应」的现场
             * 特征 —— 屏幕（elder_ui，4 小时独立计时）说要做，引擎里却
             * 没有待确认项（已判 SKIPPED / 被 replace_all 重置成 IDLE）。
             * 打出来是为了下次一眼能看出来，不用再靠猜。
             *
             * 2026-11-03 追加：日程类"播报即完成"也会落到这里（喝水/运动/
             * 待办 auto_done 之后引擎里就没有待确认项了）。老人长按的意思
             * 仍然是"这件事我做了"，所以先收屏，再决定这条日志是 ERROR
             * 还是 INFO —— 收不掉才说明真脱节。 */
            const bool screen_was_showing = elder_ui_reminder_showing();
            HideReminderIfResolved("长按：当前无待确认项");
            if (elder_ui_reminder_showing()) {
                ESP_LOGE(TAG, "⚠️ 全屏提醒还挂着，但引擎里没有待确认项 —— "
                              "长按无效。screen 与引擎状态已脱节，"
                              "请把这条日志连同开机日志一起反馈");
            } else if (screen_was_showing) {
                /* 屏上原本挂着日程卡（"播报即完成"，引擎里已无待确认项）：
                 * 老人按这一下的意思仍然是"这件事我做了"，所以给他一句
                 * 「日程已确认完成，我就不在屏幕显示了」—— 否则屏突然收掉、
                 * 一声不响，他会以为是自己按错了。 */
                if (!elder_clip_play(ELDER_CLIP_SCHED_DONE)) {
                    elder_ui_speak("日程已确认完成，我就不在屏幕显示了");
                }
                ESP_LOGI(TAG, "长按确认结论：日程卡已收（引擎无待确认项），"
                              "播预录应答");
            } else {
                ESP_LOGI(TAG, "长按确认结论：无待确认项 —— 长按 3 秒不确认");
            }
        }
        break;
    }

    case ELDER_KEY_EV_RELEASE:
        /* 确认已经在上面的 LONG_HOLD 里做完了，这里**只打日志**。
         * 再叠一层"挂起 / 开确认窗口"会让同一轮按键产生两次确认机会，
         * 而且 rgb.c 会把"确认之后才松开"误判成中途放弃（粉色闪 5 次），
         * 老人看到的是"明明成了却报错"。 */
        ESP_LOGI(TAG, "按键松开");
        break;

    default:
        break;
    }
}

/* ---------------------------------------------------------------- 状态机钩子 */

/* 本轮对话里老人是否已经开过口。
 * 只在"本轮"内有效：进入 listening / idle 就清，由 OnDeviceStateChanged 负责。
 *
 * 为什么要这个标志：VAD 的 speaking=false 既可能是"老人说完了"，
 * 也可能是"还没开口、在等老人说话"。前者要亮流水灯（设备在干活），
 * 后者必须亮蓝色（等你说话）—— 混起来的话，老人刚唤醒就看到五彩乱转，
 * 会以为设备在自己玩。
 * volatile：写的一方在音频处理任务，读的一方在事件任务。 */
static volatile bool s_spoke_in_turn = false;

void OnDeviceStateChanged(DeviceState prev, DeviceState cur)
{
    ESP_LOGI(TAG, "xiaozhi 状态 %d -> %d", (int)prev, (int)cur);
    elder_ui_state_t ui = MapDeviceState(cur);
    // 提醒态优先级高于普通状态，不要被待机覆盖
    if (elder_ui_get_state() == ELDER_UI_REMINDER && ui == ELDER_UI_IDLE) {
        return;
    }
    elder_ui_set_state(ui, 0);

    /* 换轮次就清"已开口"标志。不清的话，上一轮说过话之后，下一轮刚进
     * listening 就会因为残留的 true 而立刻显示流水灯。 */
    if (cur == kDeviceStateListening || cur == kDeviceStateIdle ||
        cur == kDeviceStateSpeaking) {
        s_spoke_in_turn = false;
    }

    if (cur == kDeviceStateIdle) {
        elder_rgb_set_emotion(ELDER_EMO_NEUTRAL);
        /* 需求 7（2026-10-22 用户新需求）：进待机立刻播待机指示灯 ——
         * 「进入待机状态强制切换到待机动态图案，以便区分硬件是否
         * 在监听声音，让用户知道是否需要唤醒词进行唤醒」。
         * 2026-11-03 用户定调：原「待机熊猫」删掉，这个位置改放跑马灯
         * （表情逐张从右往左滚过图片区），见 LcdDisplay::StartIdleMarquee()。
         * 详见 LcdDisplay::PlayIdleAnimation() 的注释（为什么不能等
         * 防烧屏的 60 秒）。
         *
         * 两道保护，缺一不可：
         *   ① 函数开头的 return 只挡「ELDER_UI_REMINDER 表情态还在」
         *     这一种；而全屏提醒挂 4 小时、表情态 30 秒就回落，所以
         *     这里必须再确认一次 overlay 没挂着 —— 提醒中抢屏 = 老人
         *     看不到该做什么（吃药/喝水），那是医疗级事故；
         *   ② AsLcdDisplay() 可能返回 nullptr（无屏/非本板卡），那时
         *      静默跳过：待机本身（收音/唤醒词）不受影响，只是没跑马灯。 */
        if (!elder_ui_reminder_showing()) {
            if (auto *lcd = AsLcdDisplay()) {
                lcd->PlayIdleAnimation();
            }
        }
    } else if (cur == kDeviceStateListening) {
        elder_rgb_set_emotion(ELDER_EMO_LISTENING);
    } else if (cur == kDeviceStateConnecting) {
        /* 2026-10-28 用户定义：「流星替掉现在的 CHASE」，用于"网络传输处理中"。
         *
         * 这一态原先被 MapDeviceState 折进 LISTENING（同一个形象），
         * 但灯效必须分开：老人看不出设备"在听"还是"在想"，等两秒没声音
         * 就会以为没听清、再喊一遍，把这次请求打断。
         * **流星跑过去** = 正在干活（亮头 + 拖尾，一秒钟穿过灯带）。 */
        elder_rgb_set_ui_state(ELDER_RGB_UI_CONNECTING);
    } else if (cur == kDeviceStateSpeaking) {
        elder_rgb_set_emotion(ELDER_EMO_SPEAKING);
    }
}

}  // namespace

/* ------------------------------------------------------- VAD 边沿（灯效） */

/* 由 application.cc 的 on_vad_change 回调转发，跑在**音频处理任务**里。
 *
 * 这里只做一次灯效请求（内部只有一把互斥锁），不碰音频数据、不做任何
 * 阻塞操作 —— 音频链路是本产品的红线（用户明确要求"不要再修的破音、卡顿"），
 * 绝不能在采集回调里做重活。
 *
 * ⚠️ 必须定义在匿名命名空间**外面**：放在里面的话 `extern "C"` 仍然只有
 * 内部链接，application.cc 那边会 undefined reference（链接期才发现，
 * 症状是"构建到最后一步才炸"，很难定位）。 */
extern "C" void elder_on_vad_change(bool speaking)
{
    if (speaking) {
        s_spoke_in_turn = true;
        elder_rgb_set_ui_state(ELDER_RGB_UI_LISTENING);   /* 深天蓝：正在听你说 */
        return;
    }
    /* 老人刚说完、服务端还在 ASR/LLM/合成 —— 这一两秒里没有任何声音。
     * 老人以为没听清就会再喊一遍，反而把这次请求打断。
     * "亮点在跑"的五彩灯 = 设备在干活，是这段时间唯一的信号。 */
    if (s_spoke_in_turn) {
        elder_rgb_set_ui_state(ELDER_RGB_UI_CONNECTING);
    }
}

/* ------------------------------------------------------- 语音确认（MCP 工具） */

/* 老人对小智说「我吃过了」→ 服务端 LLM 调 confirm_reminder 工具 →
 * WS 下发 {"type":"mcp","payload":{"method":"tools/call",
 * "params":{"name":"self.elder.confirm_reminder"}}} →
 * mcp_server.cc 的 lambda 调到这里。
 *
 * 为什么必须有这个入口（2026-10-21）：
 *   elder_reminder_confirm() 原先**只有一个**调用点，在按键回调里。按键已
 *   迁到 GPIO11 且原型期没接线，离线命令词也已随上游回归删除 ⇒ 提醒响完
 *   没有任何办法确认 ⇒ 15 分钟后升级 ESCALATED ⇒ 回执 ELDER_EV_MISSED ⇒
 *   子女端每条提醒都收到「老人漏服」的假告警。这条通道是把闭环接回来。
 *
 * 与按键**共用** FirstUndoneReminder() 判据：两条路若各写一套，
 * 会出现「按键确认第 1 条、语音确认第 3 条」的错乱。
 *
 * 返回：0 = 确认成功；-1 = 当前没有待确认的服药提醒（不是错误，
 *       老人可能只是顺口说了句「吃过了」，此时不要让 LLM 报错）。 */
extern "C" int elder_confirm_pending_medicine(void)
{
    int idx = FirstUndoneReminder();
    if (idx < 0) {
        /* 引擎里没有待确认项，但老人**明确回应了**（他刚才说的就是"我喝完了"
         * 这一类）。日程类早已 auto_done，屏却还挂着 —— 这里收掉。
         * 不收的话用户的感受就是"我说了、它也答应了，屏幕一直赶不走"。 */
        HideReminderIfResolved("语音确认：当前无待确认项");
        ESP_LOGW(TAG, "语音确认请求：当前没有待确认的服药提醒，忽略");
        return -1;
    }
    ConfirmMedicine(idx, ELDER_CFM_VOICE);
    return 0;
}

/* 定向版：服务端大模型判断出「哪条提醒完成了」之后，带着 reminders.id
 * （= 本地 server_id）回来确认**那一条**。
 *
 * 为什么需要它（2026-11-03 多药提醒改造的闭环缺口）：
 *   长按多药提醒不再自己确认，改成进对话让服务端判断哪条日程完成。
 *   服务端判断完如果不回灌设备，设备侧那条提醒会一直挂在 PENDING/ESCALATED
 *   —— 屏幕和闪灯再挂 4 小时、state[] 一直停在"待确认"、NVS 里也不会记下
 *   老人已经回应过。所以服务端按约定调
 *   device_call("self.elder.confirm_reminder", {"server_id": id})。
 *
 * ⚠️ 落到的状态是 **DONE**（不是 HALF_CONFIRMED）—— 走的是
 *    elder_reminder_confirm_server() 这条专用入口。半置信的前提是"只有
 *    一句『吃了』、没人核实"，而这里是服务端在完整对话里问清了哪味药之后
 *    才判定的，置信度已经够；落 HALF 会让 done_today 不置位、全屏提醒不收、
 *    10 分钟后 HALF_EXPIRED 又什么都不上报（终态永远到不了家属端）。
 *
 * ---- 上报的 method 由「这次确认是不是按键发起」决定（2026-11-03 用户裁决）----
 * 用户原话：「先按了服药提醒按键进行确认，再通过追问的方式确认服用的哪一种药，
 * 那就属于语音+按键的全置信」。所以：
 *   · 命中 s_med_armed_server_id（同一条提醒 + 10 分钟内）⇒ **BUTTON**。
 *     设备报 CONFIRMED + BUTTON，服务端把它和库里已有的 VOICE 合并成
 *     VOICE_AND_BUTTON(5) —— 这就是"全置信"。
 *     顺序上是成立的：服务端 voice/tools.py 的 confirm_reminder **先**把
 *     ev.confirm_method 写成 VOICE 并 commit，**之后**才 device_call 通知
 *     设备；所以这条 BUTTON 回执到达时，库里确实还是 VOICE。
 *   · 未命中 ⇒ **VOICE**。老人没按过键，只是说了句"我吃了降压药"——
 *     那就是仅语音，不是全置信。
 * 注意：命不命中都走同一条 force_done=true 的路径，**状态都是 DONE**。
 * "已确认"是状态，"置信度"是另一个维度 —— 别因为 method 是 BUTTON 就把
 * 这里换成非 force_done 的 confirm_ex，那会让状态退回 HALF_CONFIRMED，
 * 屏幕收不起来、家属端也等不到终态。
 *
 * server_id == 0（没传 / 类型不对 / 传了 0）：**先按"正在响的那条"定位**
 *   （``FirstUndoneReminder()``），定位到了就能判断它是不是按键发起的：
 *     · 命中按键发起标记 ⇒ 与上面完全同一条定向路径（报 BUTTON、落 DONE）。
 *       服务端拿不到候选（规则被停用/已删、或时间窗对不上）时会退回不带参数
 *       的旧调用，而老人**确实按了键**——不认这个标记就等于把一次全置信降级
 *       成"仅语音 + 半置信"，老人还得再按一次，而他刚按过。
 *     · 未命中 ⇒ 与改造前逐字一致（``ConfirmMedicine(idx, VOICE)``、落
 *       HALF_CONFIRMED）。老人只是顺口说了句"吃过了"，没有任何"人在场"的
 *       证据，不能替他升成全置信。
 *   拿不到 server_id 时**只消费不置位**：标记是靠 server_id 定位的，这里
 *   定位到的是"此刻正在响的那条"，两者相等才成立（见 MedArmedFor）。
 *
 * 返回：0 = 已确认；-1 = 本地没有正在等确认的该条（服务端收到 false，
 *       据此知道设备状态没同步上）。 */

/* 按键发起标记是否命中 ``server_id``（并顺手清掉过期的）。
 *
 * 抽成函数而不是在两条分支里各写一遍：TTL 判断漏一处，表现是"过期标记被
 * 当成有效" —— 老人上午按了一次键、下午随口说"吃了"，下午那条被算成全置信，
 * 而日志里一切正常（只是少了行"已过期"）。 */
static bool MedArmedFor(uint32_t server_id)
{
    if (s_med_armed_us <= 0 || server_id == 0) return false;

    const int64_t now_us = esp_timer_get_time();
    /* 先清过期的：过期标记留着没有任何用处，还会让"是不是按键发起"这个问题
     * 每次都多一次分支判断。清掉之后下面的判据只剩"同一条提醒"一个条件。 */
    if ((now_us - s_med_armed_us) > kMedArmedTtlUs) {
        ESP_LOGI(TAG, "按键发起标记已过期（%lld 秒 > 600 秒），作废",
                 (long long)((now_us - s_med_armed_us) / 1000000));
        s_med_armed_server_id = 0;
        s_med_armed_us = 0;
        return false;
    }
    return s_med_armed_server_id == server_id;
}

/* 标记用一次就作废：这次按键的"贷款"已经还掉了，留着会让同一条提醒的下一轮
 * （比如明天的同一个 server_id）也被算成全置信。 */
static void MedArmedConsume(uint32_t server_id)
{
    if (s_med_armed_server_id == server_id) {
        s_med_armed_server_id = 0;
        s_med_armed_us = 0;
    }
}

extern "C" int elder_confirm_reminder_by_server_id(uint32_t server_id)
{
    if (server_id == 0) {
        /* 旧路径：服务端没带 server_id（拿不到候选时它会退成这样）。
         * 先按"正在响的那条"定位 —— 定位到之后就能判断它是不是按键发起的，
         * 见本函数 docstring 里 server_id == 0 那一段。 */
        const int idx = FirstUndoneReminder();
        if (idx < 0) {
            /* 同上：老人回应了，日程类已 auto_done，屏得收掉。 */
            HideReminderIfResolved("语音确认（无 server_id）：当前无待确认项");
            ESP_LOGW(TAG, "语音确认请求（无 server_id）：当前没有待确认的提醒，忽略");
            return -1;
        }
        const elder_rmd_item_t *it = ItemAt(idx);
        const uint32_t resolved = (it != nullptr) ? it->server_id : 0;

        if (MedArmedFor(resolved)) {
            ESP_LOGI(TAG, "无 server_id 但命中按键发起标记（解析到 server_id=%lu，"
                          "提醒 #%d）⇒ 按定向路径上报 BUTTON（全置信）",
                     (unsigned long)resolved, idx);
            esp_err_t rc = elder_reminder_confirm_server(resolved,
                                                         ELDER_CFM_BUTTON);
            if (rc == ESP_OK) {
                MedArmedConsume(resolved);
                elder_input_close_confirm_window();
                s_arming_id = -1;
                return 0;
            }
            /* 定向失败就退回旧路径：不能因为"想记全置信"而让确认本身失败 ——
             * 老人按了键却什么都没发生，比少记一次全置信严重得多。 */
            ESP_LOGW(TAG, "无 server_id 的定向确认失败（rc=%d），退回旧路径", (int)rc);
        }

        /* 未命中标记 ⇒ 与改造前**逐字一致**：走纯语音入口（落 HALF_CONFIRMED）。
         * 这里不直接调 ConfirmMedicine(idx, VOICE)，是为了让"旧路径"只有
         * 一份实现 —— 它同时是 test_elder_host T13 的被测对象，两处各写一遍
         * 迟早会漂移，而漂移的表现是"真机上半置信、host 测试里全置信"。 */
        return elder_confirm_pending_medicine();
    }

    /* 按键发起标记：命中 ⇒ 报 BUTTON（服务端合并成全置信），否则报 VOICE。 */
    const bool armed = MedArmedFor(server_id);

    uint8_t method = ELDER_CFM_VOICE;
    if (armed) {
        method = ELDER_CFM_BUTTON;
        ESP_LOGI(TAG, "定向确认：server_id=%lu 是按键发起的（%lld 秒前）⇒ "
                      "上报 BUTTON，服务端合并成语音+按键（全置信）",
                 (unsigned long)server_id,
                 (long long)((esp_timer_get_time() - s_med_armed_us) / 1000000));
    }

    esp_err_t rc = elder_reminder_confirm_server(server_id, method);
    if (rc != ESP_OK) {
        ESP_LOGW(TAG, "定向语音确认失败（server_id=%lu, method=%u, rc=%d）",
                 (unsigned long)server_id, (unsigned)method, (int)rc);
        /* 定向确认失败有两种原因，对屏幕的处理完全相反：
         *   · 本地没有这条 / 状态已经不是"待确认"（典型：日程类 auto_done）
         *     ⇒ 引擎里确实没有下文，屏必须收掉；
         *   · 还有别的提醒在等 ⇒ HideReminderIfResolved 自己会跳过。
         * 放在这里而不是只在 return 前，是因为这条路径也是"老人回应了"。 */
        HideReminderIfResolved("定向语音确认未命中待确认项");
        return -1;
    }

    MedArmedConsume(server_id);

    /* 收尾与 ConfirmMedicine() 保持一致：状态机已经由 reminder.c 改完
     * （含 flush 落盘），这里只负责关掉确认窗、清掉长按待确认标记 ——
     * 否则 #1 会继续按"待确认"呼吸，屏幕上还留着一条已经确认过的提醒。 */
    elder_input_close_confirm_window();
    s_arming_id = -1;
    return 0;
}

/* ------------------------------------------------------- 待机（需求 7） */

/* 老人说「待机吧 / 退下吧 / 晚安吧 / 我睡了」→ 服务端念完告别语后调本函数。
 *
 * 为什么必须落到**设备状态机**上，不能只在服务端"不响应"：
 *   realtime（连续对话）模式下，设备播报完回到 kDeviceStateListening，
 *   这一态 **WakeNet 是关的**（见 application.cc 的 SetDeviceState）。
 *   服务端只做到"不再应答"的话，老人再喊唤醒词设备根本听不见 ——
 *   实测过一次 ASR 失败后设备聋了 164 秒（2026-10-20）。
 *   ⇒ 真正的"待机"只能是 kDeviceStateIdle：那里 EnableVoiceProcessing(false)
 *     停掉上行推流、EnableWakeWordTracking/EnableWakeWordDetection(true)
 *     把唤醒词重新打开，下一次"你好小智"才叫得应。
 *
 * 为什么走 Schedule 而不是直接 SetDeviceState：
 *   MCP 工具回包跑在协议收包任务里，而 SetDeviceState 会碰 LVGL
 *   （display->SetStatus / SetEmotion）。跨任务直接动 LVGL 不加锁，
 *   症状是随机的 LoadProhibited。Schedule 把它排进主事件循环，
 *   那里本来就是唯一动界面的线程。
 *
 * 为什么不顺手调 StopListening()：那个函数会向服务端发 `listen stop`，
 *   服务端收到 `stop` 会**立刻 finalize 一轮**（拿当前缓冲去识别）。
 *   老人刚说完"退下吧"，这一轮会把他自己的告别话再识别一次
 *   ⇒ 多一轮空回复，正好破坏"待机"的语义。只切状态、不发消息。 */
extern "C" void elder_enter_standby(void)
{
    auto &app = Application::GetInstance();
    ESP_LOGI(TAG, "进入待机：关闭收音、重新打开唤醒词（下次唤醒前不监听不应答）");
    app.Schedule([]() {
        Application::GetInstance().SetDeviceState(kDeviceStateIdle);
        /* 需求 7（2026-10-22）：待机屏立刻换成待机指示灯（2026-11-03 起
         * 是跑马灯，原动态熊猫已删除）。
         *
         * 为什么这里还要再调一次：设备**已经**是 idle 时
         * SetDeviceState 不产生状态变化事件，OnDeviceStateChanged 里
         * 的播放不会发生 —— 只有直接调才能保证跑马灯一定播上。
         *
         * 全屏提醒挂着时不播：提醒占着整屏，抢屏就是让老人看不到
         * 「该吃药了」。AsLcdDisplay() 为 nullptr（无屏设备）时静默
         * 跳过，不影响待机本身。 */
        if (!elder_ui_reminder_showing()) {
            if (auto *lcd = AsLcdDisplay()) {
                lcd->PlayIdleAnimation();
            }
        }
    });
}

/* ------------------------------------------------ 状态灯开关（2026-11-01） */

/* 老人说「把灯关了」→ 服务端调 self.elder.set_status_light → 落到这里。
 *
 * 只关**状态灯**（待机/聆听/说话/表情）。提醒闪灯、服药未确认、求助告警、
 * #1 按键指示灯一律照常 —— 判据在 rgb.c 的 active_slot 里按优先级放行，
 * 本函数不参与判断。用户原话：「关闭状态灯（严禁关闭事件灯）」。
 *
 * 返回**改完之后**的真实状态，而不是"成功/失败"：老人问「灯关了吗」时，
 * 服务端要能拿到一个和事实一致的答案。返回 1 = 开，0 = 关。
 */
extern "C" int elder_set_status_light(int on)
{
    elder_rgb_set_status_light(on != 0);
    return elder_rgb_status_light_enabled() ? 1 : 0;
}

extern "C" int elder_get_status_light(void)
{
    return elder_rgb_status_light_enabled() ? 1 : 0;
}

/* ---------------------------------------------------------------- 入口 */

extern "C" void elder_bridge_start(void)
{
    ESP_LOGI(TAG, "老人陪伴 & 提醒助手 —— 接入 xiaozhi v2.0.5 (板卡 %s)",
             BOARD_TYPE);

    // 与后端 TZ=Asia/Shanghai 保持一致；CST-8 = UTC+8 无夏令时
    setenv("TZ", "CST-8", 1);
    tzset();

    // 1) 底层：RGB 指示灯（可 Kconfig 关闭）
#if CONFIG_ELDER_ENABLE_RGB
    if (elder_rgb_init() != ESP_OK) {
        ESP_LOGW(TAG, "RGB 初始化失败（未外接 WS2812？），继续运行");
    } else {
        /* 开机立刻进入"待机灯效"。
         *
         * ⚠️ 这里原来写的是
         *     elder_rgb_request(ELDER_RGB_PRIO_IDLE, ELDER_FX_BREATH, 0x8E24AA, 0);
         *    —— 那是 2026-10-27 之前"待机 = 紫色呼吸"的旧定义。四态重定义
         *    （D66）之后灯效全改走 ELDER_RGB_PRIO_EMOTION 一个槽位，这句
         *    却漏删了，成了一个**永远没人覆盖的低优先级常驻槽位**。
         *
         *    后果（2026-10-28 用户实报）：开机到第一次唤醒之间，EMOTION
         *    槽位还是空的（OnSetEmotion 只设屏幕表情、不碰 RGB），
         *    active_slot() 只能选到这个紫色呼吸 —— 老人看到的是"近似白光
         *    的呼吸灯"。唤醒一次之后 OnDeviceStateChanged 才第一次写
         *    EMOTION 槽位（prio 10 > 0），灯效才变成设计的样子。
         *
         * 为什么改成直接调 set_ui_state(IDLE)、而不只是把颜色改对：
         *   ① 灯效定义只有一处真源（elder_rgb_set_ui_state 的 switch），
         *      这里再写一份 BREATH/TWINKLE 就是第二份定义，早晚又漂移；
         *   ② set_ui_state(IDLE) 会同时启动 g.idle_dim 的 1 分钟降亮计时
         *      （60% → 30%），开机就算"刚进待机"，符合用户定义；
         *   ③ 不再留 PRIO_IDLE 槽位：EMOTION 槽位被 release 的瞬间
         *      （比如提醒结束）会露出这个旧槽位，表现为"灯先闪一下紫
         *      再回星光"。四态互斥就该只有一个槽位在用。 */
        elder_rgb_set_ui_state(ELDER_RGB_UI_IDLE);
    }
#endif

    // 2) 按键：单键三段式确认手势（GPIO11，由 CONFIG_ELDER_KEY_GPIO 配置）
    if (elder_input_init(OnKey, nullptr) != ESP_OK) {
        ESP_LOGW(TAG, "按键初始化失败");
    }

    // 3) 时间基准：DS3231 → NVS 兜底。**必须早于提醒引擎**，
    //    否则 reminder 会先拿 1970 跑一轮 tick（守卫会拦，但日志会误导）。
    //    原型期没有时钟芯片，两个都探不到是预期状态，只打 WARN。
    elder_rtc_init();
    elder_time_restore_boot();

    // 4) 提醒引擎（读 NVS，离线可用）
    if (elder_reminder_init(OnReminder, nullptr) != ESP_OK) {
        ESP_LOGE(TAG, "提醒引擎初始化失败");
    }

    // 5) UI 钩子：把 elder 的抽象动作接到 xiaozhi
    elder_ui_hooks_t hooks = {};
    hooks.set_emotion = OnSetEmotion;
    hooks.notify = OnNotify;
    hooks.play_tone = OnPlayTone;
    hooks.speak = OnSpeak;
    hooks.is_online = OnIsOnline;
    hooks.show_reminder = OnShowReminder;
    hooks.hide_reminder = OnHideReminder;
    hooks.play_clip = OnPlayClip;
    hooks.ctx = nullptr;
    elder_ui_set_hooks(&hooks);
    elder_ui_set_state(ELDER_UI_IDLE, 0);

    // 5) xiaozhi 状态机 → 形象
    DeviceStateEventManager::GetInstance().RegisterStateChangeCallback(OnDeviceStateChanged);

    // 6) 后端同步（内部自带硬超时 + 指数退避，离线不影响任何本地功能）
    elder_sync_cfg_t cfg = {};
    cfg.base_url = CONFIG_ELDER_SERVER_URL;
    cfg.token = CONFIG_ELDER_SERVER_TOKEN[0] ? CONFIG_ELDER_SERVER_TOKEN : nullptr;
    cfg.fw_version = "elder-1.0.0";
    cfg.interval_sec = CONFIG_ELDER_SYNC_INTERVAL_SEC;
    if (elder_sync_init(&cfg) == ESP_OK) {
        elder_sync_start();
    }

    // 7) 出厂演示数据（默认关闭；无后端联调时可临时打开）
#if CONFIG_ELDER_DEMO_DATA
    {
        const elder_rmd_table_t *tbl = nullptr;
        elder_reminder_list(&tbl);
        if (!tbl || tbl->count == 0) {
            // C++ 中不用指定初始化器（C++20 才有），显式赋值保证各标准都能编过
            elder_rmd_item_t m1, m2;
            memset(&m1, 0, sizeof(m1));
            memset(&m2, 0, sizeof(m2));
            m1.hour = 8;  m1.minute = 0;
            m2.hour = 20; m2.minute = 0;
            m1.type = m2.type = ELDER_RMD_TYPE_MEDICINE;
            m1.repeat_mask = m2.repeat_mask = 0x7F;
            m1.enabled = m2.enabled = true;
            strncpy(m1.label, "降压药", sizeof(m1.label) - 1);
            strncpy(m2.label, "钙片", sizeof(m2.label) - 1);
            elder_reminder_add(&m1);
            elder_reminder_add(&m2);
            ESP_LOGI(TAG, "已写入 2 条演示提醒");
        }
    }
#endif

    // 8) 未绑定设备的引导**不在这里做**。
    //
    // 原先这里调 ShowActivationCode()（本地随机生成激活码），已删除，原因三条：
    //  1. elder_bridge_start() 跑在 app.Start() 之后，而未绑定设备上
    //     app.Start() 在 CheckNewVersion() 的 while(true) 里永久阻塞，
    //     根本不会返回 ⇒ 这段代码永远执行不到；
    //  2. 本地生成的码服务端**不认**（只有 server/app/voice 的
    //     _ensure_activation_code 落库并按 MAC 复用的码才算数），
    //     屏显一个输进去会失败的码比不屏显更糟；
    //  3. 现在由 elder_bind_guide.cc 负责：它在 app.Start() **之前**起任务，
    //     向服务端要真码并周期播报「请使用手机APP或者登录网站进行设备绑定」。

    ESP_LOGI(TAG, "elder 组件接入完成；离线提醒可用=%s",
             elder_time_synced() ? "已校时" : "未校时（依赖本地 RTC）");

    ESP_LOGI(TAG, "离线命令词已随上游回归移除（2026-10-20）：改用小智唤醒 + 对话确认");
}
