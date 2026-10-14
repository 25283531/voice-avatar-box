#ifndef ELDER_BOARD_H
#define ELDER_BOARD_H

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
/* 注意：立创板 BOOT/RESET 已被自动下载电路接管，板上「BOOT」键实为用户键(GPIO0)。
   上电 3 秒内 GPIO0 长按会进入 USB 下载模式 → 长按检测必须在启动 3s 后才使能。 */
#define ELDER_KEY_SOS           GPIO_NUM_0
#define ELDER_KEY_ACTIVE_LEVEL  0
#define ELDER_KEY_BOOT_GUARD_MS 3000

/* ---------- 外部扩展：WS2812 RGB 指示灯 ---------- */
#define ELDER_RGB_GPIO          GPIO_NUM_10  /* 唯一空闲 GPIO 之一 */
#define ELDER_RGB_LEDS          1
/* 硬件要点：WS2812 DIN 要求 >=0.7*VDD ≈ 3.5V，ESP32-S3 只有 3.3V。
   量产必须加 74HCT 系列电平转换；飞线验证可短距离直连但不保证稳定。 */

/* ---------- 板上自带 ---------- */
#define ELDER_BUILTIN_LED       GPIO_NUM_48

#endif /* ELDER_BOARD_H */
