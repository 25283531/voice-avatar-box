#include "elder_input.h"
#include "elder_board.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "elder.input";

#define LONG_HOLD_MS        3000    /* 长按达标：3s（用户指定） */
#define SOS_HOLD_MS         8000    /* 再长一些判为求助 */
#define CONFIRM_WINDOW_MS   30000   /* 松开后 30s 内短按有效（用户指定） */
#define DEBOUNCE_MS         30
#define POLL_MS             20

static struct {
    elder_key_cb_t cb;
    void          *ctx;
    int64_t        enabled_at_us;
    int64_t        press_start_us;
    bool           pressed;
    bool           long_fired;
    bool           sos_fired;
    int64_t        window_deadline_us;
    TaskHandle_t   task;
} g;

static int64_t now_us(void) { return esp_timer_get_time(); }

static bool boot_guard_passed(void)
{
    return (now_us() - g.enabled_at_us) > (ELDER_KEY_BOOT_GUARD_MS * 1000LL);
}

static bool key_down(void)
{
    int lvl = gpio_get_level(ELDER_KEY_SOS);
    return ELDER_KEY_ACTIVE_LEVEL ? (lvl == 0) : (lvl != 0);
}

static void emit(elder_key_event_t ev)
{
    if (g.cb) g.cb(ev, g.ctx);
}

static void input_task(void *arg)
{
    bool stable_down = false;
    uint32_t stable_cnt = 0;

    while (1) {
        bool raw = key_down();

        /* 消抖：连续 DEBOUNCE_MS 同电平才认可 */
        if (raw == stable_down) {
            stable_cnt = 0;
        } else {
            stable_cnt += POLL_MS;
            if (stable_cnt >= DEBOUNCE_MS) {
                stable_down = raw;
                stable_cnt = 0;
            } else {
                vTaskDelay(pdMS_TO_TICKS(POLL_MS));
                continue;
            }
        }

        if (!boot_guard_passed()) {
            /* 启动 3s 内不做长按检测，避免误入 USB 下载模式 */
            vTaskDelay(pdMS_TO_TICKS(POLL_MS));
            continue;
        }

        if (stable_down && !g.pressed) {
            g.pressed = true;
            g.press_start_us = now_us();
            g.long_fired = false;
            g.sos_fired = false;
            ESP_LOGD(TAG, "press start");
        } else if (stable_down && g.pressed) {
            int64_t held = now_us() - g.press_start_us;
            if (!g.long_fired && held >= LONG_HOLD_MS * 1000LL) {
                g.long_fired = true;
                ESP_LOGI(TAG, "long hold 3s reached");
                emit(ELDER_KEY_EV_LONG_HOLD);
            }
            if (!g.sos_fired && held >= SOS_HOLD_MS * 1000LL) {
                g.sos_fired = true;
                ESP_LOGW(TAG, "SOS hold 8s");
                emit(ELDER_KEY_EV_SOS);
            }
        } else if (!stable_down && g.pressed) {
            int64_t held = now_us() - g.press_start_us;
            g.pressed = false;
            if (g.long_fired) {
                ESP_LOGI(TAG, "release after long hold (%.1fs)", held / 1e6);
                emit(ELDER_KEY_EV_RELEASE);
            } else if (!g.sos_fired) {
                ESP_LOGI(TAG, "short press (%.0fms)", held / 1e3);
                emit(ELDER_KEY_EV_SHORT);
            }
        }

        /* 确认窗口超时 */
        if (g.window_deadline_us && now_us() > g.window_deadline_us) {
            ESP_LOGW(TAG, "confirm window expired");
            elder_input_close_confirm_window();
        }

        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

esp_err_t elder_input_init(elder_key_cb_t cb, void *ctx)
{
    g.cb = cb;
    g.ctx = ctx;
    g.enabled_at_us = now_us();

    gpio_config_t io = {
        .pin_bit_mask = (1ULL << ELDER_KEY_SOS),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io));

    /* 栈从 2560 → 4096：emit() 是**同步**调用 elder.bridge::OnKey 的，
     * 那条 switch 里会跑 UI 状态切换 + 提示音 + RGB 请求 + 日志格式化，
     * 2560 字节在 SOS 路径上实测被撑爆（2026-10-20，见 elder_bridge.cc
     * 里 ELDER_KEY_EV_SOS 分支的串口原文）。
     * ⚠️ 但**不要**指望靠加栈来扛 HTTPS：联网上报必须留在独立任务里，
     * 这里只覆盖"纯本地、几百字节"的那部分调用。 */
    xTaskCreatePinnedToCore(input_task, "elder_input", 4096, NULL, 6, &g.task, 1);
    ESP_LOGI(TAG, "key on GPIO%d ready (long-press armed after %dms)",
             ELDER_KEY_SOS, ELDER_KEY_BOOT_GUARD_MS);
    return ESP_OK;
}

bool elder_input_open_confirm_window(void)
{
    g.window_deadline_us = now_us() + CONFIRM_WINDOW_MS * 1000LL;
    ESP_LOGI(TAG, "confirm window opened: %ds", CONFIRM_WINDOW_MS / 1000);
    return true;
}

bool elder_input_confirm_window_active(void)
{
    return g.window_deadline_us && (now_us() < g.window_deadline_us);
}

void elder_input_close_confirm_window(void)
{
    g.window_deadline_us = 0;
}
