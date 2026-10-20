#include "elder_input.h"
#include "elder_board.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "elder.input";

#define LONG_HOLD_MS        3000    /* 长按达标：3s（用户指定） */
#define CONFIRM_WINDOW_MS   30000   /* 松开后 30s 内短按有效（用户指定） */
#define DEBOUNCE_MS         30
#define POLL_MS             20
#define STUCK_MS            20000   /* 连续低电平超过 20s ⇒ 判为引脚被拉低/按键卡住 */

static struct {
    elder_key_cb_t cb;
    void          *ctx;
    int64_t        enabled_at_us;
    int64_t        press_start_us;
    bool           pressed;
    bool           long_fired;
    int64_t        window_deadline_us;

    /* ---- 反向守卫（2026-10-20 真机事故的补丁）----
     *
     * 事实链：设备每 21.8 秒重启一次，串口原文
     *     I (16808) elder.input: long hold 3s reached
     *     W (21808) elder.input: SOS hold 8s
     *     ***ERROR*** A stack overflow in task elder_input has been detected.
     *     rst:0xc (RTC_SW_CPU_RST)
     * 而**没有人按过**这个键：GPIO0 是 BOOT 脚，板上自动下载电路把它和 CH340 的
     * DTR/RTS 三极管接在一起，USB 线插着但没有主机 / 串口被打开时，IO0 会被
     * 持续拉低。旧代码在「启动 3s 后」直接看电平，于是把「引脚一直是低」
     * 当成「老人一直按着」，到点就进长按流程。
     *
     * ⇒ 判据改成**边沿**而不是电平：没见过一次「确认过的释放」（高电平）之前，
     *   任何低电平都不算按压；连续低超过 STUCK_MS 直接判定卡住，直到真的松开。 */
    bool           released_seen;
    int64_t        low_since_us;
    bool           stuck;

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

/* 电平跟踪：维护 released_seen / stuck 两个判据。
 * 返回 true 表示「这个低电平可以被当成一次真实按压的起点」。 */
static bool track_level(bool stable_down)
{
    if (stable_down) {
        if (g.low_since_us == 0) {
            g.low_since_us = now_us();
        } else if (!g.stuck && (now_us() - g.low_since_us) > (STUCK_MS * 1000LL)) {
            g.stuck = true;
            ESP_LOGW(TAG, "GPIO%d 持续低电平 %.0fs → 判为按键卡住/被自动下载电路拉低，"
                          "本次按压不予采信（松开后自动恢复）",
                     ELDER_KEY_SOS, (now_us() - g.low_since_us) / 1e6);
            /* 卡住时必须把进行中的按压状态清掉，否则松开时会误发 RELEASE */
            g.pressed = false;
            g.long_fired = false;
        }
    } else {
        g.low_since_us = 0;
        if (g.stuck) {
            g.stuck = false;
            ESP_LOGI(TAG, "按键回到释放状态，恢复检测");
        }
        g.released_seen = true;
    }
    return g.released_seen && !g.stuck;
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

        bool acceptable = track_level(stable_down);

        if (stable_down && !g.pressed) {
            if (acceptable) {
                g.pressed = true;
                g.press_start_us = now_us();
                g.long_fired = false;
                ESP_LOGD(TAG, "press start");
            }
            /* acceptable=false（开机就是低 / 已判卡住）⇒ 连按压起点都不记 */
        } else if (stable_down && g.pressed) {
            int64_t held = now_us() - g.press_start_us;
            if (!g.long_fired && held >= (LONG_HOLD_MS * 1000LL)) {
                g.long_fired = true;
                ESP_LOGI(TAG, "long hold 3s reached");
                emit(ELDER_KEY_EV_LONG_HOLD);
            }
        } else if (!stable_down && g.pressed) {
            int64_t held = now_us() - g.press_start_us;
            g.pressed = false;
            if (g.long_fired) {
                ESP_LOGI(TAG, "release after long hold (%.1fs)", held / 1e6);
                emit(ELDER_KEY_EV_RELEASE);
            } else {
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

    /* 开机就把 GPIO0 的真实电平打出来。
     * 这条日志是排查「设备莫名重启 / 莫名叮咚」的第一现场证据 ——
     * 若这里是 0 且之后从没变成 1，说明是自动下载电路在拉低，不是老人按键。 */
    ESP_LOGI(TAG, "GPIO%d 初始电平=%d（1=松开，0=被拉低/按住）",
             ELDER_KEY_SOS, gpio_get_level(ELDER_KEY_SOS));

    /* 栈 4096：emit() 是**同步**调用 elder.bridge::OnKey 的，那条 switch 里会跑
     * UI 状态切换 + 提示音 + 日志格式化。2560 字节实测被撑爆过（2026-10-20）。
     * ⚠️ 但**不要**指望靠加栈来扛 HTTPS：联网上报必须留在独立任务里。 */
    xTaskCreatePinnedToCore(input_task, "elder_input", 4096, NULL, 6, &g.task, 1);
    ESP_LOGI(TAG, "key on GPIO%d ready (long-press armed after %dms)",
             ELDER_KEY_SOS, ELDER_KEY_BOOT_GUARD_MS);
    return ESP_OK;
}

bool elder_input_open_confirm_window(void)
{
    g.window_deadline_us = now_us() + (CONFIRM_WINDOW_MS * 1000LL);
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
