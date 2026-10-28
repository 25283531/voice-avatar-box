#include <stdio.h>
#include <string.h>
#include <time.h>

#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "esp_log.h"

#include "elder_board.h"
#include "elder_rgb.h"
#include "elder_input.h"
#include "elder_reminder.h"
#include "elder_activation.h"
#include "elder_ui.h"
#include "elder_sync.h"
#include "elder_net.h"

/*
 * 组件自测固件（不依赖 xiaozhi 主程序，用于单独验证 elder 组件）
 *
 * 与正式固件的区别：
 *   - 没有 xiaozhi 的 Display / AudioService，因此 UI 钩子留空，
 *     elder_ui 会打印「无实现」日志而不是崩溃；
 *   - 没有 Wi-Fi，elder_net 的所有请求都会失败并进入退避，
 *     正好用来验证「离线兜底」：本地提醒照常触发、回执入队不丢。
 */
static const char *TAG = "elder.app";

/* 当前正在等待确认的提醒；-1 表示无 */
static int s_arming_id = -1;

static void on_reminder(const elder_rmd_item_t *item, elder_rmd_state_t st, void *ctx)
{
    (void)ctx;
    ESP_LOGW(TAG, "提醒 '%s' (rid=%lu) -> state %d",
             item->label, (unsigned long)item->server_id, (int)st);

    switch (st) {
    case ELDER_RMD_PENDING: {
        elder_ui_set_state(ELDER_UI_REMINDER, 30000);
        char text[96];
        if (item->type == ELDER_RMD_TYPE_MEDICINE) {
            snprintf(text, sizeof(text), "该吃%s了", item->label);
        } else {
            snprintf(text, sizeof(text), "%s时间到了", item->label);
        }
        /* 联网走 TTS，离线自动降级为本地提示音 */
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

    /* 回执入队；离线时攒着，联网后由同步任务补发 */
    elder_sync_on_reminder_state(item, st);
}

static void on_key(elder_key_event_t ev, void *ctx)
{
    (void)ctx;
    switch (ev) {
    case ELDER_KEY_EV_SHORT:
        if (elder_input_confirm_window_active() && s_arming_id >= 0) {
            ESP_LOGI(TAG, "按键确认：提醒 #%d 已完成", s_arming_id);
            elder_reminder_confirm((uint8_t)s_arming_id);
            elder_input_close_confirm_window();
            s_arming_id = -1;
        }
        break;

    case ELDER_KEY_EV_LONG_HOLD:
        ESP_LOGI(TAG, "请松开按键，30 秒内再短按一次确认");
        break;

    case ELDER_KEY_EV_RELEASE: {
        /* 取第一条今日未完成的服药提醒挂起确认 */
        const elder_rmd_table_t *tbl = NULL;
        if (elder_reminder_list(&tbl) == ESP_OK && tbl) {
            for (uint8_t i = 0; i < tbl->count && i < ELDER_REMINDER_MAX; i++) {
                if (tbl->items[i].type != ELDER_RMD_TYPE_MEDICINE) continue;
                if (!tbl->items[i].enabled) continue;
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

    /* 已删除 SOS 分支（2026-10-20 用户拍板）：按键职责改为纯服药确认，
     * elder_key_event_t 里也不再有 ELDER_KEY_EV_SOS —— 这里不要再写
     * `case ELDER_KEY_EV_SOS:`，枚举里已经没有这个名字，写了编译不过。
     * 想改回 SOS 必须先读 elder_input.h 头注释（当年是长按同步跑 HTTPS
     * 把 2560 字节的输入任务栈撑爆，设备重启、求助根本没发出去）。 */

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

    /* 与后端 TZ=Asia/Shanghai 对齐 */
    setenv("TZ", "CST-8", 1);
    tzset();

#if CONFIG_ELDER_ENABLE_RGB
    ESP_ERROR_CHECK(elder_rgb_init());
    /* 开机即待机灯效（与 elder_bridge.cc 同源）。
     * 原来这里也是 PRIO_IDLE + BREATH 紫色呼吸，四态重定义后同样会露出
     * 旧灯效 —— 见 elder_bridge.cc 里那段注释。 */
    elder_rgb_set_ui_state(ELDER_RGB_UI_IDLE);
#endif
    ESP_ERROR_CHECK(elder_input_init(on_key, NULL));
    ESP_ERROR_CHECK(elder_reminder_init(on_reminder, NULL));

    /* 自测模式下不注入 UI 钩子：elder_ui 会打印「无实现」而不是崩溃 */

#if CONFIG_ELDER_DEMO_DATA
    /* 出厂演示数据：早 8:00 / 晚 20:00 服药 */
    {
        const elder_rmd_table_t *tbl = NULL;
        elder_reminder_list(&tbl);
        if (!tbl || tbl->count == 0) {
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
        }
    }
#endif

    /* 激活码自检：算法须与 server/app/security.py 逐位一致 */
    char code[ELDER_ACT_CODE_LEN + 1] = {0};
    elder_activation_generate(code, sizeof(code));
    char disp[ELDER_ACT_DISPLAY_LEN] = {0};
    elder_activation_format(code, disp, sizeof(disp));
    ESP_LOGI(TAG, "激活码: %s (Luhn 校验 %s)", disp,
             elder_activation_verify(code) ? "OK" : "FAIL");

    /* 后端同步（无 Wi-Fi 时会持续退避，不影响本地提醒） */
    elder_sync_cfg_t cfg = {
        .base_url = CONFIG_ELDER_SERVER_URL,
        .token = CONFIG_ELDER_SERVER_TOKEN,
        .fw_version = "elder-selftest-1.0.0",
        .interval_sec = CONFIG_ELDER_SYNC_INTERVAL_SEC,
    };
    if (elder_sync_init(&cfg) == ESP_OK) {
        elder_sync_start();
    }

    while (1) {
        time_t now = time(NULL);
        struct tm tm_now;
        localtime_r(&now, &tm_now);
        ESP_LOGI(TAG, "%02d:%02d 待服药未完成=%d, 下一条 %d 秒后, 在线=%d, 待补发回执=%d",
                 tm_now.tm_hour, tm_now.tm_min,
                 elder_reminder_has_pending_medicine(),
                 elder_reminder_next_in_sec(),
                 (int)elder_net_is_online(),
                 elder_sync_queue_pending());
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}
