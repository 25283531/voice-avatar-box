#include <esp_log.h>
#include <esp_err.h>
#include <nvs.h>
#include <nvs_flash.h>
#include <driver/gpio.h>
#include <esp_event.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "application.h"
#include "system_info.h"

#define TAG "main"





// >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
// 老人陪伴 & 提醒助手：等 xiaozhi 的 Board / Display / AudioService 就绪后拉起
// 声明必须在**文件作用域**：extern "C" 写在函数体里是语法错误。
extern "C" void elder_bridge_start(void);
// <<< END elder-companion overlay <<<

extern "C" void app_main(void)
{
    // Initialize the default event loop
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    // Initialize NVS flash for WiFi configuration
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "Erasing NVS flash to fix corruption");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // Launch the application
    auto& app = Application::GetInstance();
    app.Start();

    // >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
    xTaskCreatePinnedToCore([](void *arg) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        elder_bridge_start();
        vTaskDelete(NULL);
    }, "elder_boot", 4096, NULL, 5, NULL, 1);
    // <<< END elder-companion overlay <<<


}
