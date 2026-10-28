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
#include <string>
#include <string_view>

#include "sdkconfig.h"
#include "esp_log.h"
#include "esp_random.h"
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

// 切 EAF 形象：走 Display::SetEmotion()，名字必须与
// main/boards/lichuang-dev/emote.json 中的 emote 字段一致。
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

void OnReminder(const elder_rmd_item_t *item, elder_rmd_state_t st, void *ctx)
{
    (void)ctx;
    ESP_LOGW(TAG, "提醒 '%s' (rid=%lu) -> state %d", item->label,
             (unsigned long)item->server_id, (int)st);

    switch (st) {
    case ELDER_RMD_PENDING: {
        // 形象切到「惊讶/提醒」，保持 30s 后自动回落
        elder_ui_set_state(ELDER_UI_REMINDER, 30000);

        char text[96];
        if (item->type == ELDER_RMD_TYPE_MEDICINE) {
            snprintf(text, sizeof(text), "该吃%s了", item->label);
        } else {
            snprintf(text, sizeof(text), "%s时间到了", item->label);
        }

        /* 需求 8：屏幕图片不再显示，全屏大字显示任务内容，直到确认或 4 小时。
         *
         * 全屏文字里**多一行确认提示**：老人看到"该吃药了"之后的第一反应
         * 是"我该怎么办"。只写任务、不写怎么确认，等于把下一步留成空白 ——
         * 而这正是按键/SOS 都取消之后最容易卡住的地方。
         * 提示不进 TTS：语音只念事项本身，多念一句"请说吃过了"很吵。
         *
         * ⚠️ 2026-10-22 两处修正：
         *  ① 确认话术**必须按类型分开**。原来不管什么提醒都写「说「我吃过了」」，
         *     于是「喝水」提醒的屏上写着"说「我吃过了」"—— 老人照念反而答非所问，
         *     而且服务端的确认正则按语义分档，对不上就是白念。
         *  ② 删掉「或按一下按钮」。用户明确说 GPIO11 还没接开关，屏幕上让人
         *     按按钮，老人只会摸半天找不到 —— 提示不能指向不存在的操作。
         *     （开关接上后要同时改回这里和服务端 session.py 的 _PENDING_REMINDER_HINT。） */
        const char *how_to_confirm = (item->type == ELDER_RMD_TYPE_MEDICINE)
                                         ? "说「我吃过了」" : "说「弄好了」";
        char screen_text[160];
        snprintf(screen_text, sizeof(screen_text), "%s\n\n%s", text,
                 how_to_confirm);
        elder_ui_show_reminder(screen_text);

        /* 1) 先响固件内置的提醒音乐（约 4.9 秒，离线也有声音）
         * 2) 音乐放完再念事项内容（在线 TTS；离线只屏显+说明）
         * 两步分开而不是"边响边说"：见 ScheduleAnnounce 上方的注释。 */
        elder_ui_play_tone(ELDER_TONE_REMINDER);
        ScheduleAnnounce(text);
        break;
    }
    case ELDER_RMD_DONE:
        elder_ui_hide_reminder();          /* 确认完成 ⇒ 全屏提醒立刻收起 */
        elder_ui_set_state(ELDER_UI_CONFIRMED, 8000);
        elder_ui_play_tone(ELDER_TONE_SUCCESS);
        break;
    case ELDER_RMD_ESCALATED:
        /* 超时升级：**不收全屏提醒**（用户明确要求"没确认就一直显示"），
         * 只换表情 + 响一声感叹音。灯也不再改成"一直闪"——
         * 交给 elder_rgb 的 2 小时节奏，见 reminder.c 的说明。 */
        elder_ui_set_state(ELDER_UI_MISSED, 0);
        elder_ui_play_tone(ELDER_TONE_EXCLAMATION);
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
                elder_input_open_confirm_window();   /* 让界面立刻进确认态 */
                s_arming_id = idx;
            }
        }

        if (idx >= 0) {
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
             * #1 指示灯给（input.c 调 elder_rgb_button_press/release）。 */
            ESP_LOGI(TAG, "短按：当前没有待确认的提醒 —— 本按键只做确认，不做对话开关");
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
         * 而不是等提醒引擎落完 NVS 才亮。 */
        int idx = FirstUndoneReminder();
        if (idx >= 0) {
            elder_rgb_button_confirmed();
            ConfirmMedicine(idx, ELDER_CFM_BUTTON);
        } else {
            ESP_LOGI(TAG, "长按 3 秒：当前没有待确认的提醒，不确认");
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
        /* 需求 7（2026-10-22 用户新需求）：进待机立刻播动态熊猫 ——
         * 「进入待机状态强制切换到待机动态熊猫图案，以便区分硬件是否
         * 在监听声音，让用户知道是否需要唤醒词进行唤醒」。
         * 详见 LcdDisplay::PlayIdleAnimation() 的注释（为什么不能等
         * 防烧屏的 60 秒）。
         *
         * 两道保护，缺一不可：
         *   ① 函数开头的 return 只挡「ELDER_UI_REMINDER 表情态还在」
         *     这一种；而全屏提醒挂 4 小时、表情态 30 秒就回落，所以
         *     这里必须再确认一次 overlay 没挂着 —— 提醒中抢屏 = 老人
         *     看不到该做什么（吃药/喝水），那是医疗级事故；
         *   ② AsLcdDisplay() 可能返回 nullptr（无屏/非本板卡），那时
         *      静默跳过：待机本身（收音/唤醒词）不受影响，只是没熊猫。 */
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
        ESP_LOGW(TAG, "语音确认请求：当前没有待确认的服药提醒，忽略");
        return -1;
    }
    ConfirmMedicine(idx, ELDER_CFM_VOICE);
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
        /* 需求 7（2026-10-22）：待机屏立刻换成动态熊猫。
         *
         * 为什么这里还要再调一次：设备**已经**是 idle 时
         * SetDeviceState 不产生状态变化事件，OnDeviceStateChanged 里
         * 的播放不会发生 —— 只有直接调才能保证熊猫一定播上。
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
