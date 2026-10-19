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
// 未绑定设备的绑定引导（必须在 app.Start() 之前启动，见 MAIN_CC_CALL_PRE 注释）
extern "C" esp_err_t elder_bind_guide_start(void);
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

    // >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
    // 未绑定设备的「请去 APP 绑定」引导。必须在 app.Start() **之前**起：
    // 未绑定设备上 app.Start() 会永久阻塞在 CheckNewVersion 的 while(true)，
    // 放在它之后的任何初始化都不会执行。详见 tools/merge_to_xiaozhi.py 的
    // MAIN_CC_CALL_PRE 注释与 docs/决策记录.md D24。
    elder_bind_guide_start();
    // <<< END elder-companion overlay <<<
    app.Start();

    // >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
    xTaskCreatePinnedToCore([](void *arg) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        elder_bridge_start();
        vTaskDelete(NULL);
    }, "elder_boot", 4096, NULL, 5, NULL, 1);
    // <<< END elder-companion overlay <<<

}
