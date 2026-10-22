#ifndef _BOARD_CONFIG_H_
#define _BOARD_CONFIG_H_

#include <driver/gpio.h>

#define AUDIO_INPUT_SAMPLE_RATE  24000
#define AUDIO_OUTPUT_SAMPLE_RATE 24000

#define AUDIO_INPUT_REFERENCE    true

#define AUDIO_I2S_GPIO_MCLK GPIO_NUM_38
#define AUDIO_I2S_GPIO_WS GPIO_NUM_13
#define AUDIO_I2S_GPIO_BCLK GPIO_NUM_14
#define AUDIO_I2S_GPIO_DIN  GPIO_NUM_12
#define AUDIO_I2S_GPIO_DOUT GPIO_NUM_45

#define AUDIO_CODEC_USE_PCA9557
#define AUDIO_CODEC_I2C_SDA_PIN  GPIO_NUM_1
#define AUDIO_CODEC_I2C_SCL_PIN  GPIO_NUM_2
#define AUDIO_CODEC_ES8311_ADDR  ES8311_CODEC_DEFAULT_ADDR
#define AUDIO_CODEC_ES7210_ADDR  0x82

#define BUILTIN_LED_GPIO        GPIO_NUM_48
#define BOOT_BUTTON_GPIO        GPIO_NUM_0
#define VOLUME_UP_BUTTON_GPIO   GPIO_NUM_NC
#define VOLUME_DOWN_BUTTON_GPIO GPIO_NUM_NC

#define DISPLAY_WIDTH   320
#define DISPLAY_HEIGHT  240
/* ⚠️⚠️ 这三个宏**不是**「横屏 / 竖屏」开关，是面板的**坐标变换**。
 *
 * 2026-10-20 第二次踩坑记录（用户报「图标跑到左上、WiFi 跑到左下、背景伪花屏」）：
 *   上一版把 SWAP_XY 改成了 false（以为「关掉交换 = 横屏」），真机立刻花屏。
 *   真因：这块屏的**面板原生方向是竖屏 240x320**（ST7789 的 GRAM 是 240 列 x 320 行），
 *   官方 BSP（wiki.lckfb.com lcd-display 一节）必须
 *       esp_lcd_panel_swap_xy(panel, true);
 *       esp_lcd_panel_mirror(panel, true, false);
 *   才能得到**逻辑横屏 320x240**。
 *   ST7789 的 MV 位（swap_xy 写的就是 MADCTL.MV）交换行列后，CASET 才允许 0..319；
 *   一旦 MV=0，我们按 320 宽去写一个只有 240 列的窗口 ⇒ 每行溢出 80px 把下一行顶歪
 *   ⇒ 整幅图斜切（＝用户看到的「伪花屏」＋元素满屏乱跑）。
 *   IDF 的 st7789 驱动**只改 MADCTL，不做软件换坐标**（esp_lcd_panel_st7789.c），
 *   所以这里写错，画出来的每个区域都会真歪。
 *
 * ⇒ 结论（与立创官方 BSP 逐字一致，改之前先读这段）：
 *   DISPLAY_WIDTH/HEIGHT  永远是**逻辑**分辨率（横屏 320x240）；
 *   SWAP_XY=true + MIRROR_X=true 才是「按键朝上、外扩口朝下」的正向横屏。
 *   触摸侧必须同步：x_max/y_max 对调 + flags{swap_xy=1, mirror_x=1, mirror_y=0}，
 *   见 lichuang_dev_board.cc:InitializeTouch（那边注释同样重要）。
 */
#define DISPLAY_MIRROR_X true
#define DISPLAY_MIRROR_Y false
#define DISPLAY_SWAP_XY true

#define DISPLAY_OFFSET_X  0
#define DISPLAY_OFFSET_Y  0

#define DISPLAY_BACKLIGHT_PIN GPIO_NUM_42
#define DISPLAY_BACKLIGHT_OUTPUT_INVERT true


#endif // _BOARD_CONFIG_H_
