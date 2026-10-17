/* nvs_flash.h —— 宿主测试桩。 */
#ifndef STUB_NVS_FLASH_H
#define STUB_NVS_FLASH_H
/* reminder.c 只用 ESP_ERROR_CHECK_WITHOUT_ABORT，桩里当普通检查即可 */
#define ESP_ERROR_CHECK_WITHOUT_ABORT(x) do { esp_err_t _e = (x); (void)_e; } while (0)
#endif