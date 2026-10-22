#include "elder_ui.h"

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "elder.ui";

static struct {
    elder_ui_hooks_t hooks;
    bool             has_hooks;
    elder_ui_state_t state;
    int64_t          expire_us;   /* 0 = 常驻 */

    /* 需求 8：全屏提醒。showing 为真时 reminder_expire_us 是 4 小时后的绝对
     * 时刻；到点由 elder_ui_tick() 收掉（见 elder_ui.h 的三层分工说明）。 */
    bool             reminder_showing;
    int64_t          reminder_expire_us;
} g;

/* 需求 8：全屏提醒最长显示多久。用户原话「持续时间 4 个小时」。 */
#define ELDER_UI_REMINDER_HOLD_US (4LL * 60 * 60 * 1000 * 1000)

/* 状态 → EAF 情绪名（与 main/boards/lichuang-dev/emote.json 对齐） */
static const char *const EMOTION[] = {
    "neutral",    /* ELDER_UI_IDLE       */
    "thinking",   /* ELDER_UI_LISTENING  */
    "happy",      /* ELDER_UI_SPEAKING   */
    "shocked",    /* ELDER_UI_REMINDER   */
    "happy",      /* ELDER_UI_CONFIRMED  */
    "angry",      /* ELDER_UI_MISSED     */
    "angry",      /* ELDER_UI_ALARM      */
    "sleepy",     /* ELDER_UI_OFFLINE    */
    "winking",    /* ELDER_UI_ACTIVATING */
};

/* 状态 → 本地提示音 */
static const elder_tone_t TONE[] = {
    ELDER_TONE_NONE,         /* IDLE */
    ELDER_TONE_NONE,         /* LISTENING */
    ELDER_TONE_NONE,         /* SPEAKING */
    ELDER_TONE_POPUP,        /* REMINDER */
    ELDER_TONE_SUCCESS,      /* CONFIRMED */
    ELDER_TONE_EXCLAMATION,  /* MISSED */
    ELDER_TONE_VIBRATION,    /* ALARM */
    ELDER_TONE_NONE,         /* OFFLINE */
    ELDER_TONE_NONE          /* ACTIVATING */
};

const char *elder_ui_emotion_name(elder_ui_state_t st)
{
    if ((int)st < 0 || (size_t)st >= sizeof(EMOTION) / sizeof(EMOTION[0])) return "neutral";
    return EMOTION[(int)st];
}

elder_tone_t elder_ui_tone_of(elder_ui_state_t st)
{
    if ((int)st < 0 || (size_t)st >= sizeof(TONE) / sizeof(TONE[0])) return ELDER_TONE_NONE;
    return TONE[(int)st];
}

void elder_ui_set_hooks(const elder_ui_hooks_t *hooks)
{
    if (!hooks) {
        memset(&g.hooks, 0, sizeof(g.hooks));
        g.has_hooks = false;
        return;
    }
    g.hooks = *hooks;
    g.has_hooks = true;
    /* show/hide_reminder 必须一起打出来：需求 8 的「全屏大字」完全依赖这两个
     * 钩子，它们没注入时 elder_ui_show_reminder() 只记状态、屏上什么都不变
     * （见该函数里的注释）。开机日志里少打这两项，现场就只能靠"猜"为什么
     * 提醒响了但屏幕没字。 */
    ESP_LOGI(TAG, "hooks: emotion=%d notify=%d tone=%d speak=%d "
                  "show_reminder=%d hide_reminder=%d",
             g.hooks.set_emotion != NULL, g.hooks.notify != NULL,
             g.hooks.play_tone != NULL, g.hooks.speak != NULL,
             g.hooks.show_reminder != NULL, g.hooks.hide_reminder != NULL);

    /* 钩子变更时把当前形象立刻推一次，避免切换后屏幕停在旧表情 */
    if (g.hooks.set_emotion) {
        g.hooks.set_emotion(elder_ui_emotion_name(g.state), g.hooks.ctx);
    }
}

elder_ui_state_t elder_ui_get_state(void)
{
    return g.state;
}

static void push_emotion(elder_ui_state_t st)
{
    if (!g.has_hooks || !g.hooks.set_emotion) {
        ESP_LOGD(TAG, "形象切换 %s（无实现，忽略）", elder_ui_emotion_name(st));
        return;
    }
    g.hooks.set_emotion(elder_ui_emotion_name(st), g.hooks.ctx);
}

esp_err_t elder_ui_set_state(elder_ui_state_t st, uint32_t hold_ms)
{
    if ((int)st < 0 || (size_t)st >= sizeof(EMOTION) / sizeof(EMOTION[0])) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 去重：同状态只刷新保持时长，不重复下发 */
    if (g.state == st && (hold_ms == 0) == (g.expire_us == 0)) {
        if (hold_ms) g.expire_us = esp_timer_get_time() + (int64_t)hold_ms * 1000;
        return ESP_OK;
    }

    ESP_LOGI(TAG, "状态 %d -> %d (%s)%s", (int)g.state, (int)st,
             elder_ui_emotion_name(st),
             hold_ms ? "（定时回落）" : "（常驻）");

    g.state = st;
    g.expire_us = hold_ms ? (esp_timer_get_time() + (int64_t)hold_ms * 1000) : 0;
    push_emotion(st);
    return ESP_OK;
}

/* 保持时长到期 → 回落到待机；全屏提醒满 4 小时 → 收掉 */
void elder_ui_tick(void)
{
    /* 需求 8：4 小时到点自动收掉全屏提醒。
     *
     * 放在这里而不是让上层计时：tick 由 sync 任务循环驱动（离线时也能跑），
     * 是组件里唯一"一定会被周期性调用"的地方。精度只到一次 sync 周期
     * （秒级到分钟级），对"4 小时的屏显"完全够用 —— 不值得为它再起一个任务。 */
    if (g.reminder_showing && g.reminder_expire_us &&
            esp_timer_get_time() >= g.reminder_expire_us) {
        ESP_LOGI(TAG, "全屏提醒已满 4 小时，自动收掉");
        elder_ui_hide_reminder();
    }

    if (!g.expire_us) return;
    if (esp_timer_get_time() < g.expire_us) return;

    elder_ui_state_t from = g.state;
    g.expire_us = 0;
    g.state = ELDER_UI_IDLE;
    ESP_LOGI(TAG, "状态 %d 保持结束，回落 IDLE", (int)from);
    push_emotion(ELDER_UI_IDLE);
}

/* ------------------------------------------------------- 需求 8：全屏提醒 */

esp_err_t elder_ui_show_reminder(const char *text)
{
    if (!text || !text[0]) return ESP_ERR_INVALID_ARG;

    g.reminder_showing = true;
    g.reminder_expire_us = esp_timer_get_time() + ELDER_UI_REMINDER_HOLD_US;

    if (!g.has_hooks || !g.hooks.show_reminder) {
        /* 无屏设备 / 钩子尚未注入：仍然记状态。**不要在这里 return 错误** ——
         * 调用方（提醒回调）不该因为"这块板子没有屏"而少做别的事
         * （闪灯、语音、回执都还要照做）。 */
        ESP_LOGW(TAG, "全屏提醒无实现（%s），仅记录状态", text);
        return ESP_OK;
    }
    ESP_LOGI(TAG, "全屏提醒：%s", text);
    g.hooks.show_reminder(text, g.hooks.ctx);
    return ESP_OK;
}

void elder_ui_hide_reminder(void)
{
    if (!g.reminder_showing && !g.reminder_expire_us) return;
    g.reminder_showing = false;
    g.reminder_expire_us = 0;
    if (!g.has_hooks || !g.hooks.hide_reminder) return;
    ESP_LOGI(TAG, "全屏提醒收起");
    g.hooks.hide_reminder(g.hooks.ctx);
}

bool elder_ui_reminder_showing(void)
{
    return g.reminder_showing;
}

void elder_ui_play_tone(elder_tone_t tone)
{
    if (tone == ELDER_TONE_NONE) return;
    if (!g.has_hooks || !g.hooks.play_tone) {
        ESP_LOGW(TAG, "提示音 %d 无实现（离线兜底失效！）", (int)tone);
        return;
    }
    g.hooks.play_tone(tone, g.hooks.ctx);
}

static bool online_now(void)
{
    if (!g.has_hooks || !g.hooks.is_online) return false;
    return g.hooks.is_online(g.hooks.ctx);
}

void elder_ui_notice(const char *text, const char *emotion)
{
    if (!text || !text[0]) return;
    if (!g.has_hooks || !g.hooks.notify) return;
    g.hooks.notify(text, emotion, g.hooks.ctx);
}

void elder_ui_announce(const char *text, elder_tone_t tone)
{
    if (!text || !text[0]) return;

    bool net = online_now();

    if (g.has_hooks && g.hooks.notify) {
        g.hooks.notify(text, NULL, g.hooks.ctx);
    }

    if (net && g.has_hooks && g.hooks.speak) {
        /* 在线：走 TTS 念出提醒内容 */
        ESP_LOGI(TAG, "在线播报: %s", text);
        g.hooks.speak(text, g.hooks.ctx);
    } else {
        /* 离线 / 无 TTS：降级为本地提示音，保证「断网也能响」 */
        ESP_LOGW(TAG, "离线兜底播报（TTS 不可用）: %s", text);
        elder_ui_play_tone(tone);
    }
}
