#ifndef ELDER_BOARD_H
#define ELDER_BOARD_H

#include "sdkconfig.h"
#include "driver/gpio.h"

/*
 * 立创·实战派 ESP32-S3（lc-szpi-esp32s3）引脚基线
 * 数据对齐自 78/xiaozhi-esp32: main/boards/lckfb/szpi-esp32s3/
 * 移植到 ESP32-S3-BOX-3 / ESP-VoCat 时只需替换本文件，业务代码不动。
 */

/* ---------- 显示（SPI3 + ST7789 320x240）---------- */
#define ELDER_LCD_SPI_HOST      SPI3_HOST
#define ELDER_LCD_MOSI          GPIO_NUM_40
#define ELDER_LCD_SCLK          GPIO_NUM_41
#define ELDER_LCD_DC            GPIO_NUM_39
#define ELDER_LCD_CS            GPIO_NUM_NC   /* 走 PCA9557 P0 */
#define ELDER_LCD_RST           GPIO_NUM_NC
#define ELDER_LCD_BL            GPIO_NUM_42
#define ELDER_LCD_BL_INVERT     true
#define ELDER_LCD_WIDTH         320
#define ELDER_LCD_HEIGHT        240
#define ELDER_LCD_SWAP_XY       true
#define ELDER_LCD_MIRROR_X      true
#define ELDER_LCD_MIRROR_Y      false

/* ---------- I2C（ES8311 / ES7210 / PCA9557 / FT6336 共用）---------- */
#define ELDER_I2C_SDA           GPIO_NUM_1
#define ELDER_I2C_SCL           GPIO_NUM_2
#define ELDER_I2C_PORT          I2C_NUM_0
#define ELDER_PCA9557_ADDR      0x19

/* PCA9557 输出位定义（其余 5 路空闲，可作扩展） */
#define ELDER_PCA9557_LCD_CS    0
#define ELDER_PCA9557_PA_EN     1
#define ELDER_PCA9557_DVP_PWDN  2

/* ---------- 按键 ---------- */
/* 2026-10-20 硬件路线拍板：**GPIO0 归还上游 xiaozhi**。
 *
 * 原先 elder 的确认键直接占用 GPIO0（板载 BOOT 键），代价是两套逻辑同时监听
 * 同一个引脚（上游 lichuang_dev_board.cc 单击=切对话、双击=AEC），而且
 * GPIO0 被自动下载电路与 CH340 的 DTR/RTS 接管 —— USB 无主机/串口打开时
 * 会被持续拉低，旧代码只能靠「3 秒使能窗 + 先松开再按下」去绕。
 *
 * 现在按键挪到 GH1.25 5P 扩展口的 GPIO11（干净 IO，无上述坑），
 * 上面那两段守卫代码已一并删除。引脚号由 Kconfig 提供：
 * 产品级换板只改 ELDER_KEY_GPIO，不改本文件。 */
#if defined(CONFIG_ELDER_KEY_GPIO) && (CONFIG_ELDER_KEY_GPIO >= 0)
#define ELDER_KEY_GPIO          ((gpio_num_t)CONFIG_ELDER_KEY_GPIO)
#define ELDER_KEY_ENABLED       1
#else
/* 符号缺失（旧 sdkconfig）也走这条路：**宁可按键关闭，也不能默默退回 GPIO0**。
 * 退回 BOOT 键会同时踩两个坑（与上游双触发、被自动下载电路拉低），
 * 而且症状是"设备自己反复进对话"，极难排查。 */
#define ELDER_KEY_GPIO          GPIO_NUM_NC
#define ELDER_KEY_ENABLED       0
#endif
#define ELDER_KEY_ACTIVE_LEVEL  0   /* 按键另一端接 GND，低电平有效 */

/* ---------- 外部扩展：WS2812 RGB 指示灯 ---------- */
/* 扩展口只剩两根信号线：GPIO10 给彩灯、GPIO11 给按键，时钟走 I2C。
 * 硬件要点：WS2812 DIN 要求 >=0.7*VDD ≈ 3.5V，ESP32-S3 只有 3.3V。
 * 量产必须加 74HCT 系列电平转换；飞线验证可短距离直连但不保证稳定。 */
#if defined(CONFIG_ELDER_ENABLE_RGB) && CONFIG_ELDER_ENABLE_RGB && \
    defined(CONFIG_ELDER_RGB_GPIO) && (CONFIG_ELDER_RGB_GPIO >= 0)
#define ELDER_RGB_GPIO          ((gpio_num_t)CONFIG_ELDER_RGB_GPIO)
#define ELDER_RGB_ENABLED       1
#else
#define ELDER_RGB_GPIO          GPIO_NUM_NC
#define ELDER_RGB_ENABLED       0
#endif
#define ELDER_RGB_LEDS          1

/* ---------- 板上自带 ---------- */
#define ELDER_BUILTIN_LED       GPIO_NUM_48

#endif /* ELDER_BOARD_H */
