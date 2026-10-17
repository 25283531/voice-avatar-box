/* freertos/FreeRTOS.h —— 宿主测试桩。 */
#ifndef STUB_FREERTOS_H
#define STUB_FREERTOS_H

#include <stdint.h>

typedef int BaseType_t;
typedef unsigned int TickType_t;
typedef void *TaskHandle_t;

#define pdMS_TO_TICKS(ms)  ((TickType_t)(ms))
#define pdTRUE             1
#define pdFALSE            0
#define pdPASS             1

#endif