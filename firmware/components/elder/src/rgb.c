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
    uint8_t         level;       /* 0~255 统一亮度，见 elder_rgb_request_ex */
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

/* ---- 对话四态的取值（2026-10-27 用户指定，改数字 = 改产品观感） ---- */
#define ELDER_UI_COLOR_IDLE      0x87CEFAu  /* 天蓝 */
#define ELDER_UI_LEVEL_IDLE      77         /* 30% of 255 */
#define ELDER_UI_COLOR_LISTENING 0x0000FFu  /* 纯蓝 */
#define ELDER_UI_COLOR_SPEAKING  0xFF8C00u  /* 深橙 */
#define ELDER_UI_LEVEL_FLOW      204        /* 80% of 255 */
#define ELDER_UI_FULL_LEVEL      255        /* 100% */

/* 流水灯速度：60°/秒（完整循环 6 秒），逐颗偏移 45°。
 * "低速"是用户明确要求的 —— 快速闪烁对老人有光敏性癫痫风险，
 * 而且夜里看会晕。要调快之前先想清楚这一点。 */
#define ELDER_FLOW_DEG_PER_TICK   3u        /* 每 50ms 走 3° ⇒ 60°/秒 */
#define ELDER_FLOW_TICK_MS        50u
#define ELDER_FLOW_LED_STEP_DEG   45u       /* 8 颗灯铺满一圈色相 */

/* ---- 第 1 颗灯：按键指示灯（取值理由见 elder_rgb.h） ---- */
#define ELDER_RGB_BTN_ATTRACT_COLOR 0xFF8C00u  /* 深橙：有待确认提醒 */
#define ELDER_RGB_BTN_GRAD_FROM     0x90EE90u  /* 浅绿：刚按下 */
#define ELDER_RGB_BTN_GRAD_TO       0x006400u  /* 深绿：按住够 3 秒 */
#define ELDER_RGB_BTN_OK_COLOR      0x00CED1u  /* 深蓝绿：确认完成 */
#define ELDER_RGB_BTN_FAIL_COLOR    0xFF1493u  /* 深粉红：中途松开没确认 */
#define ELDER_RGB_BTN_BREATH_MS     3000u      /* 吸引注意的呼吸周期 */
#define ELDER_RGB_BTN_FLASH_ON_MS   200u       /* 亮 200ms / 灭 200ms： */
#define ELDER_RGB_BTN_FLASH_OFF_MS  200u       /* 慢到老人看得清次数 */
#define ELDER_RGB_BTN_OK_TIMES      3
#define ELDER_RGB_BTN_FAIL_TIMES    5

/* #1 的四个状态（互斥） */
typedef enum {
    BTN_OFF = 0,     /* 常态：不亮 */
    BTN_ATTRACT,     /* 有待确认提醒：橙色呼吸 */
    BTN_GRADIENT,    /* 按住中：浅绿 → 深绿 */
    BTN_FLASH        /* 结果闪烁（成功 3 次 / 失败 5 次） */
} btn_mode_t;

static struct {
    rmt_channel_handle_t  chan;
    rmt_encoder_handle_t  enc;
    uint8_t               pixels[ELDER_RGB_LEDS * 3];
    uint8_t               last[ELDER_RGB_LEDS * 3];   /* 上一帧，用于"没变就不发" */
    bool                  last_valid;
    SemaphoreHandle_t     lock;
    TaskHandle_t          task;
    slot_t                slots[ELDER_RGB_SLOTS];
    bool                  ready;

    /* 提醒闪灯：active 为真时由 rgb_task 自己算"这一帧该不该亮" */
    bool                  nag_active;
    uint32_t              nag_start_ms;

    /* #1 按键指示灯状态机 */
    btn_mode_t            btn_mode;
    bool                  btn_pending;    /* 存在待确认提醒（吸引注意态的真源） */
    bool                  btn_resolved;   /* 本轮按下已经出过结果，别再重复反馈 */
    uint32_t              btn_phase_ms;   /* 呼吸/渐变的相位起点 */
    uint32_t              btn_flash_start_ms;
    uint32_t              btn_flash_color;
    uint8_t               btn_flash_times;
} g;

static uint32_t now_ms(void)
{
    return (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
}

/* ---------------- 颜色工具 ---------------- */

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

/* 统一亮度：**算完灯效之后**再乘，见 elder_rgb.h 里 ELDER_RGB_UI_* 的注释 */
static uint32_t scale_color(uint32_t c, uint32_t lvl)
{
    if (lvl >= 255u) return c;
    uint32_t r = ((c >> 16) & 0xFFu) * lvl / 255u;
    uint32_t g2 = ((c >> 8) & 0xFFu) * lvl / 255u;
    uint32_t b = (c & 0xFFu) * lvl / 255u;
    return (r << 16) | (g2 << 8) | b;
}

/* 线性插值：num/den 为进度。den==0 时直接给终点色，避免除零。 */
static uint32_t lerp_color(uint32_t a, uint32_t b, uint32_t num, uint32_t den)
{
    if (den == 0u || num >= den) return b;
    int32_t n = (int32_t)num;
    int32_t d = (int32_t)den;
    int32_t ar = (int32_t)((a >> 16) & 0xFFu), ag = (int32_t)((a >> 8) & 0xFFu), ab = (int32_t)(a & 0xFFu);
    int32_t br = (int32_t)((b >> 16) & 0xFFu), bg = (int32_t)((b >> 8) & 0xFFu), bb = (int32_t)(b & 0xFFu);
    int32_t r  = ar + (br - ar) * n / d;
    int32_t g2 = ag + (bg - ag) * n / d;
    int32_t b2 = ab + (bb - ab) * n / d;
    return ((uint32_t)r << 16) | ((uint32_t)g2 << 8) | (uint32_t)b2;
}

/* ---------------- #2~#9 灯效 ---------------- */

/* 取当前应当生效的最高优先级请求；best_prio 回带该请求的优先级（-1 = 没有） */
static bool active_slot(slot_t *out, uint32_t t_ms, int *best_prio)
{
    slot_t *best = NULL;
    for (int i = 0; i < ELDER_RGB_SLOTS; i++) {
        slot_t *s = &g.slots[i];
        if (!s->used) continue;
        if (s->expire_ms && (int32_t)(s->expire_ms - t_ms) <= 0) {
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

/* 单颗灯的最终颜色。led_idx 是灯带索引（1..ELDER_RGB_LEDS-1），
 * 流水的相位偏移按 led_idx-1 算 —— #1 不参与灯效，不能让它占掉一格色相。 */
static uint32_t fx_pixel(const slot_t *s, uint32_t t_ms, int led_idx)
{
    uint32_t c;

    switch (s->fx) {
    case ELDER_FX_BREATH: {
        /* 2.4s 一个呼吸周期 */
        uint32_t phase = t_ms % 2400u;
        uint32_t lvl = (phase < 1200u) ? (phase * 255u / 1200u)
                                       : ((2400u - phase) * 255u / 1200u);
        c = scale_color(s->color, lvl);
        break;
    }
    case ELDER_FX_BLINK_FAST:
        c = ((t_ms % 500u) < 250u) ? s->color : 0u;
        break;
    case ELDER_FX_FLOW: {
        uint32_t idx = (led_idx > 0) ? (uint32_t)(led_idx - 1) : 0u;
        uint32_t hue = ((t_ms / ELDER_FLOW_TICK_MS) * ELDER_FLOW_DEG_PER_TICK
                        + idx * ELDER_FLOW_LED_STEP_DEG) % 360u;
        c = hsv2rgb((uint16_t)hue, 255, 255);
        break;
    }
    case ELDER_FX_RAINBOW:
        c = hsv2rgb((uint16_t)((t_ms / 8u) % 360u), 255, 255);
        break;
    case ELDER_FX_SOLID:
    default:
        c = s->color;
        break;
    }

    return scale_color(c, s->level);
}

/* ---------------- #1 按键指示灯状态机 ----------------
 *
 * 全部在 g.lock 保护下调用（btn_step 会改 g.btn_mode）。 */

static void btn_enter_flash(uint32_t t_ms, uint32_t color, uint8_t times)
{
    g.btn_mode = BTN_FLASH;
    g.btn_flash_start_ms = t_ms;
    g.btn_flash_color = color;
    g.btn_flash_times = times;
    g.btn_resolved = true;   /* 这一轮已经有结果了，松开时不能再补一次粉色闪 */
}

static uint32_t btn_step(uint32_t t_ms)
{
    /* 闪烁放完 → 有待确认就继续吸引注意，否则熄灭 */
    if (g.btn_mode == BTN_FLASH) {
        uint32_t period = ELDER_RGB_BTN_FLASH_ON_MS + ELDER_RGB_BTN_FLASH_OFF_MS;
        if ((t_ms - g.btn_flash_start_ms) >= (uint32_t)g.btn_flash_times * period) {
            g.btn_mode = g.btn_pending ? BTN_ATTRACT : BTN_OFF;
            g.btn_phase_ms = t_ms;
            g.btn_resolved = false;
        }
    }

    switch (g.btn_mode) {
    case BTN_ATTRACT: {
        uint32_t half  = ELDER_RGB_BTN_BREATH_MS / 2u;
        uint32_t phase = (t_ms - g.btn_phase_ms) % ELDER_RGB_BTN_BREATH_MS;
        uint32_t lvl = (phase < half) ? (phase * 255u / half)
                                      : ((ELDER_RGB_BTN_BREATH_MS - phase) * 255u / half);
        return scale_color(ELDER_RGB_BTN_ATTRACT_COLOR, lvl);
    }
    case BTN_GRADIENT:
        /* 进度条语义：3 秒走完，走完就是最深绿 */
        return lerp_color(ELDER_RGB_BTN_GRAD_FROM, ELDER_RGB_BTN_GRAD_TO,
                          t_ms - g.btn_phase_ms,
                          (uint32_t)ELDER_BTN_CONFIRM_HOLD_MS);
    case BTN_FLASH: {
        uint32_t period = ELDER_RGB_BTN_FLASH_ON_MS + ELDER_RGB_BTN_FLASH_OFF_MS;
        uint32_t el = t_ms - g.btn_flash_start_ms;
        return ((el % period) < ELDER_RGB_BTN_FLASH_ON_MS) ? g.btn_flash_color : 0u;
    }
    case BTN_OFF:
    default:
        return 0u;
    }
}

/* ---------------- 帧渲染 ---------------- */

/* #1 = 按键指示灯，其余 = 灯效。两路分开写，互不覆盖。
 * WS2812 为 GRB 顺序，别按 RGB 顺序写（写错的表现是红蓝互换）。 */
static void render_frame(const slot_t *fx, bool has_nag, uint32_t nag_color,
                         uint32_t t_ms, uint32_t btn)
{
    g.pixels[0] = (uint8_t)((btn >> 8) & 0xFFu);
    g.pixels[1] = (uint8_t)((btn >> 16) & 0xFFu);
    g.pixels[2] = (uint8_t)(btn & 0xFFu);

    for (int i = 1; i < ELDER_RGB_LEDS; i++) {
        uint32_t c = 0u;
        if (has_nag) {
            c = nag_color;            /* 提醒闪灯整条一起闪 */
        } else if (fx) {
            c = fx_pixel(fx, t_ms, i);
        }
        g.pixels[i * 3 + 0] = (uint8_t)((c >> 8) & 0xFFu);
        g.pixels[i * 3 + 1] = (uint8_t)((c >> 16) & 0xFFu);
        g.pixels[i * 3 + 2] = (uint8_t)(c & 0xFFu);
    }

    /* 值不变就不发：呼吸/流水每帧都在变，但"常亮""熄灭"这类静态画面
     * 如果也每 30ms 发一次，RMT 会被白白占满，还会拖慢同核的其他任务。 */
    if (g.last_valid && memcmp(g.pixels, g.last, sizeof(g.pixels)) == 0) {
        return;
    }
    memcpy(g.last, g.pixels, sizeof(g.pixels));
    g.last_valid = true;

    rmt_transmit_config_t tx = {.loop_count = 0};
    rmt_transmit(g.chan, g.enc, g.pixels, sizeof(g.pixels), &tx);
    rmt_tx_wait_all_done(g.chan, portMAX_DELAY);
}

static void rgb_task(void *arg)
{
    (void)arg;

    while (1) {
        uint32_t t_ms = now_ms();
        slot_t   s;
        bool     has_fx = false;
        bool     has_nag = false;
        uint32_t nag_color = 0u;
        uint32_t btn = 0u;

        if (xSemaphoreTake(g.lock, pdMS_TO_TICKS(50)) == pdTRUE) {
            int prio = -1;
            has_fx = active_slot(&s, t_ms, &prio);

            /* 需求 8：提醒闪灯（每 10 分钟的前 10 秒，共 2 小时）。
             *
             * 优先级刻意排在"服药未确认 / 求助告警"之下：老人正按着求助键
             * 或系统在报漏服时，灯不该被提醒节奏抢走。
             * 计时全部用 tick 毫秒数相减 —— 不另起定时器、不加任务，
             * 原因见 elder_rgb.h 里 nag_start 的注释。 */
            if (g.nag_active && prio < (int)ELDER_RGB_PRIO_MED_PEND) {
                uint32_t el = t_ms - g.nag_start_ms;
                if (el >= ELDER_NAG_TOTAL_MS) {
                    /* 2 小时到点：灯自己熄灭。**不依赖上层来关** ——
                     * 上层要记得关，就一定会有忘记关的那一次。 */
                    g.nag_active = false;
                    ESP_LOGI(TAG, "提醒闪灯已满 2 小时，熄灭");
                } else if ((el % ELDER_NAG_PERIOD_MS) < ELDER_NAG_BLINK_MS) {
                    /* 0.5s 亮 / 0.5s 灭，10 秒共 10 个周期 */
                    has_nag = true;
                    nag_color = ((t_ms % 500u) < 250u) ? 0xFF0000u : 0u;
                }
            }

            btn = btn_step(t_ms);
            xSemaphoreGive(g.lock);
        }

        render_frame(has_fx ? &s : NULL, has_nag, nag_color, t_ms, btn);
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
    g.btn_mode = BTN_OFF;
    g.last_valid = false;
    g.ready = true;

    xTaskCreatePinnedToCore(rgb_task, "elder_rgb", 3072, NULL, 5, &g.task, 1);
    ESP_LOGI(TAG, "WS2812 ready on GPIO%d, %d LED(s)：#1 按键指示灯，#2~#%d 灯效",
             ELDER_RGB_GPIO, ELDER_RGB_LEDS, ELDER_RGB_LEDS);
    return ESP_OK;
}

esp_err_t elder_rgb_request_ex(elder_rgb_prio_t prio, elder_rgb_fx_t fx,
                               uint32_t color, uint8_t level, uint32_t ttl_ms)
{
    if (!g.ready) return ESP_ERR_INVALID_STATE;
    uint32_t t_ms = now_ms();

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
    dst->level = level;
    dst->expire_ms = ttl_ms ? t_ms + ttl_ms : 0;
    xSemaphoreGive(g.lock);
    return ESP_OK;
}

esp_err_t elder_rgb_request(elder_rgb_prio_t prio, elder_rgb_fx_t fx,
                            uint32_t color, uint32_t ttl_ms)
{
    return elder_rgb_request_ex(prio, fx, color, ELDER_UI_FULL_LEVEL, ttl_ms);
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

/* ---- 对话四态（2026-10-27 用户定义） ---- */
void elder_rgb_set_ui_state(elder_rgb_ui_t st)
{
    /* 四个状态共用一个槽位（ELDER_RGB_PRIO_EMOTION）⇒ 切换就是替换。
     * 若各用各的优先级，从"说话"回"待机"时旧的 speaking 槽位不会被清掉，
     * 只要它的优先级更高就会一直压着待机呼吸 —— 表现为"说完话灯还亮着橙"。
     *
     * ttl 一律 0（常驻）：原来给 8000 会让待机呼吸 8 秒后灯自己灭掉，
     * 而待机是**长期**状态，老人看到灯灭了只会以为设备坏了。 */
    switch (st) {
    case ELDER_RGB_UI_LISTENING:
        elder_rgb_request_ex(ELDER_RGB_PRIO_EMOTION, ELDER_FX_SOLID,
                             ELDER_UI_COLOR_LISTENING, ELDER_UI_FULL_LEVEL, 0);
        break;
    case ELDER_RGB_UI_CONNECTING:
        /* 五彩流水：颜色由 fx 自己按色相算，这里的 color 参数不用 */
        elder_rgb_request_ex(ELDER_RGB_PRIO_EMOTION, ELDER_FX_FLOW,
                             0u, ELDER_UI_LEVEL_FLOW, 0);
        break;
    case ELDER_RGB_UI_SPEAKING:
        elder_rgb_request_ex(ELDER_RGB_PRIO_EMOTION, ELDER_FX_SOLID,
                             ELDER_UI_COLOR_SPEAKING, ELDER_UI_FULL_LEVEL, 0);
        break;
    case ELDER_RGB_UI_IDLE:
    default:
        elder_rgb_request_ex(ELDER_RGB_PRIO_EMOTION, ELDER_FX_BREATH,
                             ELDER_UI_COLOR_IDLE, ELDER_UI_LEVEL_IDLE, 0);
        break;
    }
}

static uint32_t emotion_color(elder_emotion_t emo)
{
    switch (emo) {
    case ELDER_EMO_HAPPY:     return 0x00C853;   /* 绿 */
    case ELDER_EMO_SAD:       return 0x2962FF;   /* 蓝 */
    case ELDER_EMO_ANGRY:     return 0xD50000;   /* 红 */
    default:                  return 0x8E24AA;   /* 紫 */
    }
}

void elder_rgb_set_emotion(elder_emotion_t emo)
{
    /* 三个"有明确灯效定义"的情绪直接走四态，避免两处颜色各写一份 */
    switch (emo) {
    case ELDER_EMO_NEUTRAL:   elder_rgb_set_ui_state(ELDER_RGB_UI_IDLE);      return;
    case ELDER_EMO_LISTENING: elder_rgb_set_ui_state(ELDER_RGB_UI_LISTENING); return;
    case ELDER_EMO_SPEAKING:  elder_rgb_set_ui_state(ELDER_RGB_UI_SPEAKING);  return;
    default: break;
    }
    elder_rgb_request(ELDER_RGB_PRIO_EMOTION, ELDER_FX_SOLID, emotion_color(emo), 0);
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
    g.nag_start_ms = now_ms();
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

void elder_rgb_button_press(void)
{
    if (!g.ready) return;
    uint32_t t_ms = now_ms();

    xSemaphoreTake(g.lock, portMAX_DELAY);
    /* 没有待确认提醒 ⇒ **不进渐变**，#1 保持熄灭。
     * 正在放结果闪烁时也不打断：一次按下的反馈必须放完，
     * 否则老人按得快一点就永远看不到"成了还是没成"。 */
    if (g.btn_pending && g.btn_mode != BTN_FLASH) {
        g.btn_mode = BTN_GRADIENT;
        g.btn_phase_ms = t_ms;
        g.btn_resolved = false;
    }
    xSemaphoreGive(g.lock);
}

void elder_rgb_button_release(void)
{
    if (!g.ready) return;
    uint32_t t_ms = now_ms();

    xSemaphoreTake(g.lock, portMAX_DELAY);
    if (g.btn_mode == BTN_GRADIENT) {
        if (!g.btn_resolved) {
            /* 渐变还没走完就松手 ⇒ 没完成确认 */
            btn_enter_flash(t_ms, ELDER_RGB_BTN_FAIL_COLOR, ELDER_RGB_BTN_FAIL_TIMES);
        } else {
            g.btn_mode = g.btn_pending ? BTN_ATTRACT : BTN_OFF;
            g.btn_phase_ms = t_ms;
        }
    }
    xSemaphoreGive(g.lock);
}

void elder_rgb_button_confirmed(void)
{
    if (!g.ready) return;
    uint32_t t_ms = now_ms();

    xSemaphoreTake(g.lock, portMAX_DELAY);
    btn_enter_flash(t_ms, ELDER_RGB_BTN_OK_COLOR, ELDER_RGB_BTN_OK_TIMES);
    xSemaphoreGive(g.lock);
}

void elder_rgb_button_pending(bool pending)
{
    if (!g.ready) return;
    uint32_t t_ms = now_ms();

    xSemaphoreTake(g.lock, portMAX_DELAY);
    if (g.btn_pending == pending) {          /* 比较也要在锁内，否则会漏掉边沿 */
        xSemaphoreGive(g.lock);
        return;
    }
    g.btn_pending = pending;
    /* 只在"熄灭 ↔ 吸引注意"之间切；正在按住或正在放结果时不打断 */
    if (g.btn_mode == BTN_OFF && pending) {
        g.btn_mode = BTN_ATTRACT;
        g.btn_phase_ms = t_ms;
    } else if (g.btn_mode == BTN_ATTRACT && !pending) {
        g.btn_mode = BTN_OFF;
    }
    xSemaphoreGive(g.lock);
}
