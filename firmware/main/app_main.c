#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "esp_log.h"

#include "elder_board.h"
#include "elder_rgb.h"
#include "elder_input.h"
#include "elder_reminder.h"
#include "elder_activation.h"

static const char *TAG = "elder.app";

/* 当前正在等待确认的提醒；-1 表示无 */
static int s_arming_id = -1;

static void on_reminder(const elder_rmd_item_t *item, elder_rmd_state_t st, void *ctx)
{
    (void)ctx;
    ESP_LOGW(TAG, "reminder '%s' -> state %d", item->label, (int)st);

    if (st == ELDER_RMD_PENDING) {
        /* 屏显提示 + 语音播报（语音部分待接入 xiaozhi 的 TTS 通道） */
        ESP_LOGI(TAG, "TODO: 显示提示 & 播报：该吃 %s 了", item->label);
    }
}

static void on_key(elder_key_event_t ev, void *ctx)
{
    (void)ctx;
    switch (ev) {
    case ELDER_KEY_EV_SHORT:
        /* 30s 确认窗口内的短按 = 确认完成 */
        if (elder_input_confirm_window_active() && s_arming_id >= 0) {
            ESP_LOGI(TAG, "confirm accepted for reminder #%d", s_arming_id);
            elder_reminder_confirm((uint8_t)s_arming_id);
            elder_input_close_confirm_window();
            s_arming_id = -1;
        }
        break;

    case ELDER_KEY_EV_LONG_HOLD:
        ESP_LOGI(TAG, "请松开按键，30 秒内再短按一次确认");
        break;

    case ELDER_KEY_EV_RELEASE:
        /* 松开 → 打开 30s 窗口，并对最早一条未完成服药/日程挂起确认 */
        for (uint8_t i = 0; i < 16; i++) {
            /* 简化：由 reminder 模块在 PENDING 时登记，这里取第一条 pending 的 */
        }
        s_arming_id = 0;
        elder_input_open_confirm_window();
        elder_reminder_arm_confirm((uint8_t)s_arming_id);
        break;

    case ELDER_KEY_EV_SOS:
        ESP_LOGE(TAG, "SOS pressed -> 通知子女端（联网后补发）");
        elder_rgb_request(ELDER_RGB_PRIO_ALARM, ELDER_FX_BLINK_FAST, 0xFF0000, 60000);
        break;

    default:
        break;
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "老人陪伴 & 提醒助手 — 组件自测固件");

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(elder_rgb_init());
    ESP_ERROR_CHECK(elder_input_init(on_key, NULL));
    ESP_ERROR_CHECK(elder_reminder_init(on_reminder, NULL));

    /* 出厂演示数据：早 8:00 / 晚 20:00 服药 */
    elder_rmd_item_t m1 = {
        .hour = 8, .minute = 0, .type = ELDER_RMD_TYPE_MEDICINE,
        .repeat_mask = 0x7F, .enabled = true, .label = "降压药"
    };
    elder_rmd_item_t m2 = {
        .hour = 20, .minute = 0, .type = ELDER_RMD_TYPE_MEDICINE,
        .repeat_mask = 0x7F, .enabled = true, .label = "钙片"
    };
    elder_reminder_add(&m1);
    elder_reminder_add(&m2);

    char code[ELDER_ACT_CODE_LEN + 1] = {0};
    elder_activation_generate(code, sizeof(code));
    char disp[ELDER_ACT_DISPLAY_LEN] = {0};
    elder_activation_format(code, disp, sizeof(disp));
    ESP_LOGI(TAG, "激活码: %s (校验 %s)", disp,
             elder_activation_verify(code) ? "OK" : "FAIL");

    /* 待机呼吸灯 */
    elder_rgb_request(ELDER_RGB_PRIO_IDLE, ELDER_FX_BREATH, 0x8E24AA, 0);

    while (1) {
        ESP_LOGI(TAG, "待服药未完成=%d, 下一条提醒 %d 秒后",
                 elder_reminder_has_pending_medicine(),
                 elder_reminder_next_in_sec());
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}
