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

#include "elder_board.h"
#include "elder_rgb.h"
#include "elder_input.h"
#include "elder_reminder.h"
#include "elder_activation.h"
#include "elder_ui.h"
#include "elder_net.h"
#include "elder_sync.h"
#include "elder_cmd.h"

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

/* ---------------------------------------------------------------- 提示音 */

// 全部来自固件内置 ogg（EMBED_FILES 编进 .rodata），**不依赖网络**
const char *ToneName(elder_tone_t tone)
{
    switch (tone) {
    case ELDER_TONE_POPUP:       return "popup";
    case ELDER_TONE_EXCLAMATION: return "exclamation";
    case ELDER_TONE_SUCCESS:     return "success";
    case ELDER_TONE_VIBRATION:   return "vibration";
    case ELDER_TONE_LOW_BATTERY: return "low_battery";
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
        // 在线走 TTS，离线自动降级为本地 popup 提示音
        elder_ui_announce(text, ELDER_TONE_POPUP);
        break;
    }
    case ELDER_RMD_DONE:
        elder_ui_set_state(ELDER_UI_CONFIRMED, 8000);
        elder_ui_play_tone(ELDER_TONE_SUCCESS);
        break;
    case ELDER_RMD_ESCALATED:
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

void OnKey(elder_key_event_t ev, void *ctx)
{
    (void)ctx;
    auto &app = Application::GetInstance();

    switch (ev) {
    case ELDER_KEY_EV_SHORT:
        if (elder_input_confirm_window_active() && s_arming_id >= 0) {
            ESP_LOGI(TAG, "按键确认：提醒 #%d 已完成", s_arming_id);
            elder_reminder_confirm((uint8_t)s_arming_id);
            elder_input_close_confirm_window();
            s_arming_id = -1;
        } else {
            // 没有待确认提醒时，短按复用为「开始/停止对话」
            app.ToggleChatState();
        }
        break;

    case ELDER_KEY_EV_LONG_HOLD:
        ESP_LOGI(TAG, "请松开按键，30 秒内再短按一次确认");
        break;

    case ELDER_KEY_EV_RELEASE: {
        // 取第一条「未完成」的提醒挂起确认
        const elder_rmd_table_t *tbl = nullptr;
        if (elder_reminder_list(&tbl) == ESP_OK && tbl) {
            for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
                if (tbl->items[i].type != ELDER_RMD_TYPE_MEDICINE) continue;
                if (!tbl->items[i].enabled) continue;
                // 已完成的不必再确认
                if (tbl->done_today & (1u << i)) continue;
                s_arming_id = (int)i;
                elder_input_open_confirm_window();
                elder_reminder_arm_confirm(i);
                ESP_LOGI(TAG, "进入确认窗口：#%d %s", i, tbl->items[i].label);
                break;
            }
        }
        break;
    }

    case ELDER_KEY_EV_SOS:
        ESP_LOGE(TAG, "SOS：本地告警 + 联网后上报");
        elder_ui_set_state(ELDER_UI_ALARM, 0);
        elder_ui_play_tone(ELDER_TONE_VIBRATION);
        elder_rgb_request(ELDER_RGB_PRIO_ALARM, ELDER_FX_BLINK_FAST, 0xFF0000, 60000);
        elder_sync_report_interaction(ELDER_IT_SOS, 0);
        break;

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

/* ---------------------------------------------------------------- 离线命令词 */

// MultiNet（或任何离线识别层）命中命令词后调这个入口。
//
// ⚠️ 当前状态：MultiNet **未启用**（缺 model 分区 + 未选
// CONFIG_USE_CUSTOM_WAKE_WORD），所以断网时这个函数还没有调用方。
// 保留它是为了让识别层接通时只需加一行调用，而不用改分发逻辑。
void OnCommandRecognized(const char *phrase)
{
    elder_cmd_result_t r;
    esp_err_t err = elder_cmd_dispatch(phrase, &r);

    if (err != ESP_OK || !r.recognized) {
        // 认不出来就老实说认不出来，并提示走按键兜底（医疗场景宁可多按一下）
        ESP_LOGW(TAG, "未识别的离线指令: %s", phrase ? phrase : "(null)");
        elder_ui_set_state(ELDER_UI_OFFLINE, 0);
        OnNotify("没听清，请长按按键两下再操作", nullptr, nullptr);
        OnPlayTone(ELDER_TONE_EXCLAMATION, nullptr);
        return;
    }

    ESP_LOGI(TAG, "离线指令: '%s' -> action=%d ok=%d: %s",
             phrase, (int)r.action, (int)r.ok, r.feedback);

    // 命令词一律走屏显 + 内置提示音，**不走 TTS**：
    // 触发离线命令的前提就是没网，此时 TTS 必然失败。
    OnNotify(r.feedback, nullptr, nullptr);
    OnPlayTone(r.tone, nullptr);
    if (r.ok) {
        elder_ui_set_state(ELDER_UI_CONFIRMED, 4000);
    }
}

// 后端尚未提供「设备自助换取 device_token」的接口（见交付报告的遗留问题），
// 这里保留本地生成 + 屏显的完整流程，一旦后端补上 /api/device/hello
// 只需在下面填入 token 即可自动进入同步。
void ShowActivationCode()
{
    char code[ELDER_ACT_CODE_LEN + 1] = {0};
    if (elder_activation_generate(code, sizeof(code)) != ESP_OK) return;

    char disp[ELDER_ACT_DISPLAY_LEN] = {0};
    elder_activation_format(code, disp, sizeof(disp));

    ESP_LOGI(TAG, "激活码: %s（本地校验 %s）", disp,
             elder_activation_verify(code) ? "OK" : "FAIL");

    auto display = Board::GetInstance().GetDisplay();
    if (display) {
        char msg[64];
        snprintf(msg, sizeof(msg), "激活码 %s", disp);
        display->ShowNotification(msg, 600000);   // 10 分钟，与有效期一致
    }
    elder_ui_set_state(ELDER_UI_ACTIVATING, 0);
}

}  // namespace

/* ---------------------------------------------------------------- 离线命令词入口 */

// 必须放在匿名 namespace **之外**：extern "C" 若声明在匿名 namespace 内，
// 符号会退化成内部链接，识别层就链接不到这个入口了。
extern "C" void elder_cmd_on_recognized(const char *phrase)
{
    OnCommandRecognized(phrase);
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

    // 2) 按键：单键三段式确认手势
    if (elder_input_init(OnKey, nullptr) != ESP_OK) {
        ESP_LOGW(TAG, "按键初始化失败");
    }

    // 3) 提醒引擎（读 NVS，离线可用）
    if (elder_reminder_init(OnReminder, nullptr) != ESP_OK) {
        ESP_LOGE(TAG, "提醒引擎初始化失败");
    }

    // 3b) 断网自建服药提醒的默认时段。
    // 「新建吃药提醒」说出口后落到哪几个时间点，全靠这张表 ——
    // 老人不会报时间，只会用早/中/晚三个粗粒度时段。
    // 数值可后续按实际用药方案调整。
    {
        elder_cmd_med_times_t mt = {};
        mt.n = 3;
        mt.slots[0] = { 8,  0 };
        mt.slots[1] = { 12, 0 };
        mt.slots[2] = { 20, 0 };
        elder_cmd_set_med_times(&mt);
    }

    // 4) UI 钩子：把 elder 的抽象动作接到 xiaozhi
    elder_ui_hooks_t hooks = {};
    hooks.set_emotion = OnSetEmotion;
    hooks.notify = OnNotify;
    hooks.play_tone = OnPlayTone;
    hooks.speak = OnSpeak;
    hooks.is_online = OnIsOnline;
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

    // 8) 未绑定时屏显激活码
    if (!elder_net_has_token()) {
        ShowActivationCode();
    }

    ESP_LOGI(TAG, "elder 组件接入完成；离线提醒可用=%s",
             elder_time_synced() ? "已校时" : "未校时（依赖本地 RTC）");
}
