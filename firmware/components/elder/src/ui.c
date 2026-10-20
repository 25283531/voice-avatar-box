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
} g;

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
    ESP_LOGI(TAG, "hooks: emotion=%d notify=%d tone=%d speak=%d",
             g.hooks.set_emotion != NULL, g.hooks.notify != NULL,
             g.hooks.play_tone != NULL, g.hooks.speak != NULL);

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

/* 保持时长到期 → 回落到待机 */
void elder_ui_tick(void)
{
    if (!g.expire_us) return;
    if (esp_timer_get_time() < g.expire_us) return;

    elder_ui_state_t from = g.state;
    g.expire_us = 0;
    g.state = ELDER_UI_IDLE;
    ESP_LOGI(TAG, "状态 %d 保持结束，回落 IDLE", (int)from);
    push_emotion(ELDER_UI_IDLE);
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
