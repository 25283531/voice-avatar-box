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
    auto &app = Application::GetInstance();
    switch (tone) {
    case ELDER_TONE_POPUP:
        app.PlaySound(Lang::Sounds::OGG_POPUP);
        break;
    case ELDER_TONE_EXCLAMATION:
        app.PlaySound(Lang::Sounds::OGG_EXCLAMATION);
        break;
    case ELDER_TONE_SUCCESS:
        app.PlaySound(Lang::Sounds::OGG_SUCCESS);
        break;
    case ELDER_TONE_VIBRATION:
        app.PlaySound(Lang::Sounds::OGG_VIBRATION);
        break;
    case ELDER_TONE_LOW_BATTERY:
        app.PlaySound(Lang::Sounds::OGG_LOW_BATTERY);
        break;
    case ELDER_TONE_REMINDER:
        // 提醒音乐：固件内置固定资源，离线可用
        app.PlaySound(kReminderChime);
        break;
    default:
        break;
    }
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

/* 找第一条「今天还没确认」的提醒。**正在响铃的优先**，其次服药优先。
 * 返回下标，没有则 -1。抽成函数的原因：短按直接确认、长按挂窗口、
 * 语音确认三条路必须用**同一套判据**，否则会出现「长按能确认、短按确认到
 * 别的条」这种对不上的行为。
 *
 * 2026-10-22 需求 8 把它从"只看服药"扩展到"任意提醒"：
 * 现在日程提醒也会全屏显示并要求"语音/按键确认完成"，判据若仍只认服药，
 * 老人对日程说「做完了」会被回一句"没有待确认的提醒"。
 *
 * 2026-10-22 用户实报 bug（喝水提醒响铃时说「弄好了」，确认记到了吃药
 * 提醒上）：根因是下面的循环"见到第一条服药就返回"，正在响铃的日程
 * 提醒永远选不中。所以现在的优先级是：
 *   ① 先问 elder_reminder_first_ringing()：此刻有提醒在响就确认它
 *      （fired_due_at 最大 = 最近触发的那条，正是老人刚回应的那条）；
 *   ② 都没有在响时，才退回服药优先启发式 —— 两条都待确认时确认药那一条
 *      （药漏了是真事故，日程漏了不是），不动老行为。 */
int FirstUndoneReminder()
{
    int ringing = elder_reminder_first_ringing();
    if (ringing >= 0) return ringing;

    const elder_rmd_table_t *tbl = nullptr;
    if (elder_reminder_list(&tbl) != ESP_OK || tbl == nullptr) {
        return -1;
    }
    int fallback = -1;
    for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
        if (!tbl->items[i].enabled) continue;
        if (tbl->done_today & (1u << i)) continue;   /* 已完成的不必再确认 */
        if (tbl->items[i].type == ELDER_RMD_TYPE_MEDICINE) {
            return (int)i;                            /* 服药优先，见到就返回 */
        }
        if (fallback < 0) fallback = (int)i;
    }
    return fallback;
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

/* 这个按键**只做服药确认**（2026-10-20 用户拍板：SOS 取消）：
 *   提醒响铃期间（ELDER_UI_REMINDER）短按 → 直接确认，不需要任何前置手势；
 *   确认窗口内短按                   → 确认长按挂起的那一条；
 *   其余情况短按                     → 退化成「开始/结束对话」。
 * SOS 分支已删除，原因见 elder_input.h 头注释（长按同步跑联网上报把栈撑爆，
 * 设备重启，求助根本发不出去）。**不要把 HTTPS 放回这个回调里** ——
 * 它是 elder_input 任务同步调用的，栈只有 4096。 */
void OnKey(elder_key_event_t ev, void *ctx)
{
    (void)ctx;
    auto &app = Application::GetInstance();

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
             * 只看表情态的话，老人 30 秒后才去按按钮，这一下会被当成
             * "开始对话"（ToggleChatState），全屏提醒照旧挂着 ——
             * 用户的诉求「按键确认任务完成」直接失效。 */
            idx = FirstUndoneReminder();
            if (idx >= 0) {
                elder_input_open_confirm_window();   /* 让界面立刻进确认态 */
                s_arming_id = idx;
            }
        }

        if (idx >= 0) {
            ConfirmMedicine(idx, ELDER_CFM_BUTTON);
        } else {
            /* 没有待确认提醒时，短按复用为「开始/停止对话」 */
            app.ToggleChatState();
        }
        break;
    }

    case ELDER_KEY_EV_LONG_HOLD:
        /* 长按只是**提示**，真正的挂起在松开时做（见 RELEASE） */
        ESP_LOGI(TAG, "请松开按键，30 秒内再短按一次确认");
        break;

    case ELDER_KEY_EV_RELEASE: {
        int idx = FirstUndoneReminder();
        if (idx >= 0) {
            s_arming_id = idx;
            elder_input_open_confirm_window();
            elder_reminder_arm_confirm((uint8_t)idx);
            ESP_LOGI(TAG, "进入确认窗口：#%d", idx);
        } else {
            ESP_LOGI(TAG, "长按松开，但没有待确认的服药提醒 → 不进入确认窗口");
        }
        break;
    }

    default:
        break;
    }
}

/* ---------------------------------------------------------------- 状态机钩子 */

void OnDeviceStateChanged(DeviceState prev, DeviceState cur)
{
    ESP_LOGI(TAG, "xiaozhi 状态 %d -> %d", (int)prev, (int)cur);
    elder_ui_state_t ui = MapDeviceState(cur);
    // 提醒态优先级高于普通状态，不要被待机覆盖
    if (elder_ui_get_state() == ELDER_UI_REMINDER && ui == ELDER_UI_IDLE) {
        return;
    }
    elder_ui_set_state(ui, 0);

    if (cur == kDeviceStateIdle) {
        elder_rgb_set_emotion(ELDER_EMO_NEUTRAL);
    } else if (cur == kDeviceStateListening) {
        elder_rgb_set_emotion(ELDER_EMO_LISTENING);
    } else if (cur == kDeviceStateSpeaking) {
        elder_rgb_set_emotion(ELDER_EMO_SPEAKING);
    }
}

}  // namespace

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
        elder_rgb_request(ELDER_RGB_PRIO_IDLE, ELDER_FX_BREATH, 0x8E24AA, 0);
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
