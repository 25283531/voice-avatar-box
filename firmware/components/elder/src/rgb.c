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

static struct {
    rmt_channel_handle_t  chan;
    rmt_encoder_handle_t  enc;
    uint8_t               pixels[ELDER_RGB_LEDS * 3];
    SemaphoreHandle_t     lock;
    TaskHandle_t          task;
    slot_t                slots[ELDER_RGB_SLOTS];
    bool                  ready;
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

/* 取当前应当生效的最高优先级请求 */
static bool active_slot(slot_t *out, uint32_t now_ms)
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

static void write_pixels(uint32_t color)
{
    for (int i = 0; i < ELDER_RGB_LEDS; i++) {
        /* WS2812 为 GRB 顺序 */
        g.pixels[i * 3 + 0] = (color >> 8) & 0xFF;
        g.pixels[i * 3 + 1] = (color >> 16) & 0xFF;
        g.pixels[i * 3 + 2] = color & 0xFF;
    }
    rmt_transmit_config_t tx = {.loop_count = 0};
    rmt_transmit(g.chan, g.enc, g.pixels, sizeof(g.pixels), &tx);
    rmt_tx_wait_all_done(g.chan, portMAX_DELAY);
}

static void rgb_task(void *arg)
{
    uint32_t last = 0xFFFFFFFF;
    while (1) {
        uint32_t now = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
        slot_t s;
        uint32_t color = 0;

        if (xSemaphoreTake(g.lock, pdMS_TO_TICKS(50)) == pdTRUE) {
            if (active_slot(&s, now)) color = fx_color(&s, now);
            xSemaphoreGive(g.lock);
        }

        if (color != last) { write_pixels(color); last = color; }
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
    ESP_LOGI(TAG, "WS2812 ready on GPIO%d, %d LED(s)", ELDER_RGB_GPIO, ELDER_RGB_LEDS);
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
    elder_rgb_release(ELDER_RGB_PRIO_MED_PEND);
    elder_rgb_release(ELDER_RGB_PRIO_ALARM);
    elder_rgb_request(ELDER_RGB_PRIO_MED_PEND, ELDER_FX_SOLID, 0x00FF00, 60000);
}
void elder_rgb_med_escalate(void)
{
    elder_rgb_request(ELDER_RGB_PRIO_ALARM, ELDER_FX_BLINK_FAST, 0xFF0000, 0);
}
