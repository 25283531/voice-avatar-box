#include "elder_rgb.h"
#include "elder_board.h"

#include "driver/rmt_tx.h"
#include "driver/rmt_encoder.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include <string.h>

static const char *TAG = "elder.rgb";

/* WS2812 时序（单位 ns @ RMT 10MHz 分辨率） */
#define WS2812_T0H_NS   350
#define WS2812_T0L_NS   1000
#define WS2812_T1H_NS   1000
#define WS2812_T1L_NS   350
#define WS2812_RESET_NS 50000

typedef struct {
    bool            used;
    int             prio;
    elder_rgb_fx_t  fx;
    uint32_t        color;
    uint32_t        expire_ms;   /* 0 = 常驻 */
} slot_t;

#define ELDER_RGB_SLOTS 8

/* ---- 需求 8：提醒的闪灯节奏（见 elder_rgb.h 的接口注释） ----
 *
 * 只用 xTaskGetTickCount 的毫秒数，不另起定时器。三个常量都是"用户原话
 * 里的数字"，改它们就等于改产品行为，别顺手调：
 *   每 10 分钟一次 → NAG_PERIOD_MS
 *   每次亮 10 秒   → NAG_BLINK_MS
 *   2 小时后不再亮 → NAG_TOTAL_MS
 */
#define ELDER_NAG_PERIOD_MS (10u * 60u * 1000u)
#define ELDER_NAG_BLINK_MS  (10u * 1000u)
#define ELDER_NAG_TOTAL_MS  (2u * 60u * 60u * 1000u)

/* ---- 第 1 颗灯：按键指示灯 ----
 * 取值理由见 elder_rgb.h 的 elder_rgb_button_indicator 注释（适老化三条：
 * 暖色、慢呼吸、亮度封顶）。改这几个数字等于改产品观感，别顺手调。 */
#define ELDER_RGB_BTN_COLOR      0xFFB300u  /* 暖琥珀：老人晶状体黄化，暖色辨识度高于冷白/蓝 */
#define ELDER_RGB_BTN_BREATH_MS  3000u      /* 一次完整呼吸 3 秒（比常规 2.4s 更慢，不刺眼） */
#define ELDER_RGB_BTN_MAX_LEVEL  100        /* 亮度上限 ≈39%，夜里不晃眼 */
#define ELDER_RGB_BTN_TAIL_MS    2000u      /* 松开后余辉 2 秒：短按只有一百多毫秒，不留余辉等于没反馈 */

static struct {
    rmt_channel_handle_t  chan;
    rmt_encoder_handle_t  enc;
    uint8_t               pixels[ELDER_RGB_LEDS * 3];
    SemaphoreHandle_t     lock;
    TaskHandle_t          task;
    slot_t                slots[ELDER_RGB_SLOTS];
    bool                  ready;

    /* 提醒闪灯：active 为真时由 rgb_task 自己算"这一帧该不该亮" */
    bool                  nag_active;
    uint32_t              nag_start_ms;

    /* 按键指示灯（只驱动 #1，与上面那些灯效互不干扰） */
    bool                  btn_down;
    uint32_t              btn_start_ms;      /* 本次按下的起始时刻，呼吸相位从它算起 */
    uint32_t              btn_tail_until_ms; /* 松开后的余辉截止；0 = 无 */
} g;

/* ---------------- 灯效计算 ---------------- */

static uint32_t hsv2rgb(uint16_t h, uint8_t s, uint8_t v)
{
    uint8_t region = h / 43;
    uint8_t rem    = (h % 43) * 6;
    uint8_t p = (v * (255 - s)) >> 8;
    uint8_t q = (v * (255 - ((s * rem) >> 8))) >> 8;
    uint8_t t = (v * (255 - ((s * (255 - rem)) >> 8))) >> 8;
    switch (region) {
    case 0: return ((uint32_t)v << 16) | ((uint32_t)t << 8) | p;
    case 1: return ((uint32_t)q << 16) | ((uint32_t)v << 8) | p;
    case 2: return ((uint32_t)p << 16) | ((uint32_t)v << 8) | t;
    case 3: return ((uint32_t)p << 16) | ((uint32_t)q << 8) | v;
    case 4: return ((uint32_t)t << 16) | ((uint32_t)p << 8) | v;
    default:return ((uint32_t)v << 16) | ((uint32_t)p << 8) | q;
    }
}

/* 取当前应当生效的最高优先级请求；best_prio 回带该请求的优先级（-1 = 没有） */
static bool active_slot(slot_t *out, uint32_t now_ms, int *best_prio)
{
    slot_t *best = NULL;
    for (int i = 0; i < ELDER_RGB_SLOTS; i++) {
        slot_t *s = &g.slots[i];
        if (!s->used) continue;
        if (s->expire_ms && (int32_t)(s->expire_ms - now_ms) <= 0) {
            s->used = false;              /* TTL 自然到期 */
            continue;
        }
        if (!best || s->prio > best->prio) best = s;
    }
    if (best_prio) *best_prio = best ? best->prio : -1;
    if (!best) return false;
    *out = *best;
    return true;
}

static uint32_t fx_color(const slot_t *s, uint32_t now_ms)
{
    uint8_t r = (s->color >> 16) & 0xFF;
    uint8_t gg = (s->color >> 8) & 0xFF;
    uint8_t b = s->color & 0xFF;

    switch (s->fx) {
    case ELDER_FX_BREATH: {
        /* 2.4s 一个呼吸周期 */
        uint32_t phase = now_ms % 2400;
        uint8_t lvl = (phase < 1200) ? (phase * 255 / 1200) : ((2400 - phase) * 255 / 1200);
        return ((r * lvl / 255) << 16) | ((gg * lvl / 255) << 8) | (b * lvl / 255);
    }
    case ELDER_FX_BLINK_FAST:
        return ((now_ms % 500) < 250) ? s->color : 0;
    case ELDER_FX_RAINBOW:
        return hsv2rgb((now_ms / 8) % 360, 255, 255);
    case ELDER_FX_SOLID:
    default:
        return s->color;
    }
}

/* 按键指示灯的颜色：从「本次按下时刻」起算的慢呼吸。
 *
 * 相位刻意从按下时刻算，而不是从系统 tick 算 —— 后者会让每次按下都
 * 落在一个随机相位上，有时按下去正好是全暗的那一帧，老人看到的是"没反应"。
 * 松开后余辉继续沿用同一相位，避免松手瞬间亮度跳变（跳变看起来像故障）。 */
static uint32_t btn_color(uint32_t now_ms)
{
    const uint32_t half = ELDER_RGB_BTN_BREATH_MS / 2;
    uint32_t phase = (now_ms - g.btn_start_ms) % ELDER_RGB_BTN_BREATH_MS;
    uint32_t lvl = (phase < half) ? (phase * 255u / half)
                                  : ((ELDER_RGB_BTN_BREATH_MS - phase) * 255u / half);
    lvl = lvl * ELDER_RGB_BTN_MAX_LEVEL / 255u;
    uint8_t r = (ELDER_RGB_BTN_COLOR >> 16) & 0xFF;
    uint8_t gg = (ELDER_RGB_BTN_COLOR >> 8) & 0xFF;
    uint8_t b = ELDER_RGB_BTN_COLOR & 0xFF;
    return ((uint32_t)(r * lvl / 255) << 16) | ((uint32_t)(gg * lvl / 255) << 8) |
           (uint32_t)(b * lvl / 255);
}

/* #1 = 按键指示灯，其余 = 灯效。两路分开写，互不覆盖。
 * WS2812 为 GRB 顺序，别按 RGB 顺序写（写错的表现是红蓝互换）。 */
static void write_pixels(uint32_t fx_color, uint32_t btn)
{
    g.pixels[0] = (uint8_t)((btn >> 8) & 0xFF);
    g.pixels[1] = (uint8_t)((btn >> 16) & 0xFF);
    g.pixels[2] = (uint8_t)(btn & 0xFF);
    for (int i = 1; i < ELDER_RGB_LEDS; i++) {
        g.pixels[i * 3 + 0] = (uint8_t)((fx_color >> 8) & 0xFF);
        g.pixels[i * 3 + 1] = (uint8_t)((fx_color >> 16) & 0xFF);
        g.pixels[i * 3 + 2] = (uint8_t)(fx_color & 0xFF);
    }
    rmt_transmit_config_t tx = {.loop_count = 0};
    rmt_transmit(g.chan, g.enc, g.pixels, sizeof(g.pixels), &tx);
    rmt_tx_wait_all_done(g.chan, portMAX_DELAY);
}

static void rgb_task(void *arg)
{
    uint32_t last_fx = 0xFFFFFFFF, last_btn = 0xFFFFFFFF;
    while (1) {
        uint32_t now = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
        slot_t s;
        uint32_t color = 0;
        uint32_t btn = 0;

        if (xSemaphoreTake(g.lock, pdMS_TO_TICKS(50)) == pdTRUE) {
            int prio = -1;
            if (active_slot(&s, now, &prio)) {
                color = fx_color(&s, now);
            }

            /* 需求 8：提醒闪灯（每 10 分钟的前 10 秒，共 2 小时）。
             *
             * 优先级刻意排在"服药未确认 / 求助告警"之下：老人正按着求助键
             * 或系统在报漏服时，灯不该被提醒节奏抢走。
             * 计时全部用 tick 毫秒数相减 —— 不另起定时器、不加任务，
             * 原因见 elder_rgb.h 里 nag_start 的注释。 */
            if (g.nag_active && prio < (int)ELDER_RGB_PRIO_MED_PEND) {
                uint32_t el = now - g.nag_start_ms;
                if (el >= ELDER_NAG_TOTAL_MS) {
                    /* 2 小时到点：灯自己熄灭。**不依赖上层来关** ——
                     * 上层要记得关，就一定会有忘记关的那一次。 */
                    g.nag_active = false;
                    ESP_LOGI(TAG, "提醒闪灯已满 2 小时，熄灭");
                } else if ((el % ELDER_NAG_PERIOD_MS) < ELDER_NAG_BLINK_MS) {
                    /* 0.5s 亮 / 0.5s 灭，10 秒共 10 个周期 */
                    color = ((now % 500u) < 250u) ? 0xFF0000u : 0u;
                }
            }

            /* 按键指示灯：按住期间呼吸；松开后余辉 2 秒再熄。
             * 两者用同一相位，松手不跳变。 */
            if (g.btn_down) {
                btn = btn_color(now);
            } else if (g.btn_tail_until_ms &&
                       (int32_t)(g.btn_tail_until_ms - now) > 0) {
                btn = btn_color(now);
            } else if (g.btn_tail_until_ms) {
                g.btn_tail_until_ms = 0;   /* 余辉结束，回到全灭 */
            }
            xSemaphoreGive(g.lock);
        }

        if (color != last_fx || btn != last_btn) {
            write_pixels(color, btn);
            last_fx = color;
            last_btn = btn;
        }
        vTaskDelay(pdMS_TO_TICKS(30));   /* 灯效刷新 ~33Hz */
    }
}

/* ---------------- 对外接口 ---------------- */

esp_err_t elder_rgb_init(void)
{
    if (g.ready) return ESP_OK;

#if !ELDER_RGB_ENABLED
    /* 原型期未接灯珠（CONFIG_ELDER_ENABLE_RGB=n 或 ELDER_RGB_GPIO=-1）。
     * 返回 ESP_OK 而不是错误：少一颗灯不该让整个启动流程报错，
     * 灯效相关的 request 会因为 g.ready=false 自动变成空操作。 */
    ESP_LOGW(TAG, "未启用 WS2812（RGB 关闭或 GPIO=-1），灯效功能关闭");
    return ESP_OK;
#endif

    rmt_tx_channel_config_t ch = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .gpio_num = ELDER_RGB_GPIO,
        .mem_block_symbols = 64,
        .resolution_hz = 10 * 1000 * 1000,   /* 10MHz → 100ns/符号 */
        .trans_queue_depth = 4,
        .flags.invert_out = false,
        .flags.with_dma = false,
    };
    ESP_ERROR_CHECK(rmt_new_tx_channel(&ch, &g.chan));

    rmt_bytes_encoder_config_t enc = {
        .bit0 = {.level0 = 1, .duration0 = WS2812_T0H_NS / 100,
                 .level1 = 0, .duration1 = WS2812_T0L_NS / 100},
        .bit1 = {.level0 = 1, .duration0 = WS2812_T1H_NS / 100,
                 .level1 = 0, .duration1 = WS2812_T1L_NS / 100},
        .flags.msb_first = 1,
    };
    ESP_ERROR_CHECK(rmt_new_bytes_encoder(&enc, &g.enc));
    ESP_ERROR_CHECK(rmt_enable(g.chan));

    g.lock = xSemaphoreCreateMutex();
    memset(g.slots, 0, sizeof(g.slots));
    g.ready = true;

    xTaskCreatePinnedToCore(rgb_task, "elder_rgb", 3072, NULL, 5, &g.task, 1);
    ESP_LOGI(TAG, "WS2812 ready on GPIO%d, %d LED(s)：#1 按键指示灯，#2~#%d 灯效",
             ELDER_RGB_GPIO, ELDER_RGB_LEDS, ELDER_RGB_LEDS);
    return ESP_OK;
}

esp_err_t elder_rgb_request(elder_rgb_prio_t prio, elder_rgb_fx_t fx,
                            uint32_t color, uint32_t ttl_ms)
{
    if (!g.ready) return ESP_ERR_INVALID_STATE;
    uint32_t now = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);

    xSemaphoreTake(g.lock, portMAX_DELAY);
    slot_t *dst = NULL;
    for (int i = 0; i < ELDER_RGB_SLOTS; i++) {
        if (g.slots[i].used && g.slots[i].prio == (int)prio) { dst = &g.slots[i]; break; }
        if (!g.slots[i].used && !dst) dst = &g.slots[i];
    }
    if (!dst) { xSemaphoreGive(g.lock); return ESP_ERR_NO_MEM; }

    dst->used = true;
    dst->prio = (int)prio;
    dst->fx = fx;
    dst->color = color;
    dst->expire_ms = ttl_ms ? now + ttl_ms : 0;
    xSemaphoreGive(g.lock);
    return ESP_OK;
}

void elder_rgb_release(elder_rgb_prio_t prio)
{
    if (!g.ready) return;
    xSemaphoreTake(g.lock, portMAX_DELAY);
    for (int i = 0; i < ELDER_RGB_SLOTS; i++) {
        if (g.slots[i].used && g.slots[i].prio == (int)prio) {
            memset(&g.slots[i], 0, sizeof(slot_t));
        }
    }
    xSemaphoreGive(g.lock);
}

static uint32_t emotion_color(elder_emotion_t emo)
{
    switch (emo) {
    case ELDER_EMO_HAPPY:     return 0x00C853;   /* 绿 */
    case ELDER_EMO_SAD:       return 0x2962FF;   /* 蓝 */
    case ELDER_EMO_ANGRY:     return 0xD50000;   /* 红 */
    case ELDER_EMO_LISTENING: return 0x00B0FF;   /* 浅蓝 */
    case ELDER_EMO_SPEAKING:  return 0xFF6D00;   /* 橙 */
    default:                  return 0x8E24AA;   /* 紫 */
    }
}

void elder_rgb_set_emotion(elder_emotion_t emo)
{
    elder_rgb_request(ELDER_RGB_PRIO_EMOTION,
                      (emo == ELDER_EMO_NEUTRAL) ? ELDER_FX_BREATH : ELDER_FX_SOLID,
                      emotion_color(emo), 8000);
}

/* ---- 需求 11：服药状态灯 ---- */
void elder_rgb_med_pending(void)
{
    elder_rgb_request(ELDER_RGB_PRIO_MED_PEND, ELDER_FX_SOLID, 0xFF0000, 0);
}
void elder_rgb_med_done(void)
{
    elder_rgb_reminder_nag_stop();          /* 已确认 ⇒ 闪灯节奏立刻停 */
    elder_rgb_release(ELDER_RGB_PRIO_MED_PEND);
    elder_rgb_release(ELDER_RGB_PRIO_ALARM);
    elder_rgb_request(ELDER_RGB_PRIO_MED_PEND, ELDER_FX_SOLID, 0x00FF00, 60000);
}
void elder_rgb_med_escalate(void)
{
    elder_rgb_request(ELDER_RGB_PRIO_ALARM, ELDER_FX_BLINK_FAST, 0xFF0000, 0);
}

/* ---- 需求 8：提醒的闪灯节奏 ---- */
void elder_rgb_reminder_nag_start(void)
{
    if (!g.ready) return;
    xSemaphoreTake(g.lock, portMAX_DELAY);
    g.nag_active = true;
    g.nag_start_ms = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
    xSemaphoreGive(g.lock);
    ESP_LOGI(TAG, "提醒闪灯开始：每 %u 分钟闪 %u 秒，共 %u 小时",
             (unsigned)(ELDER_NAG_PERIOD_MS / 60000),
             (unsigned)(ELDER_NAG_BLINK_MS / 1000),
             (unsigned)(ELDER_NAG_TOTAL_MS / 3600000));
}

void elder_rgb_reminder_nag_stop(void)
{
    if (!g.ready) return;
    xSemaphoreTake(g.lock, portMAX_DELAY);
    if (g.nag_active) ESP_LOGI(TAG, "提醒闪灯停止");
    g.nag_active = false;
    xSemaphoreGive(g.lock);
}

/* ---- 第 1 颗灯：按键指示灯（语义与取值理由见 elder_rgb.h） ---- */
void elder_rgb_button_indicator(bool down)
{
    if (!g.ready) return;
    uint32_t now = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);

    xSemaphoreTake(g.lock, portMAX_DELAY);
    if (down) {
        /* 只在"从松到按"的边沿重置相位：按住不放时不能每帧重置，
         * 否则亮度会一直停在最暗那一帧，看起来像没亮。 */
        if (!g.btn_down) g.btn_start_ms = now;
        g.btn_down = true;
        g.btn_tail_until_ms = 0;
    } else {
        if (g.btn_down) g.btn_tail_until_ms = now + ELDER_RGB_BTN_TAIL_MS;
        g.btn_down = false;
    }
    xSemaphoreGive(g.lock);
}
