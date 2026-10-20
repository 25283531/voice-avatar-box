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
// 2026-10-20 改回**横屏 320x240**（用户拍板：老人看得惯横屏，竖屏上下留白太多）。
// 原值是 MIRROR_X=true / SWAP_XY=true ⇒ 屏交换 x/y 变成逻辑 240x320 竖屏。
// 去掉 swap 后 LVGL 逻辑分辨率就是 320x240；mirror 一并关掉取屏的原生方向
// （ST7789 320x240 模组原生即正向，原来的 mirror_x 是为了配竖屏坐标系）。
// ⚠️ 触摸的 swap/mirror/x_max/y_max 必须同步改，见 lichuang_dev_board.cc:InitializeTouch，
//    否则画面正了、点哪儿都偏。
#define DISPLAY_MIRROR_X false
#define DISPLAY_MIRROR_Y false
#define DISPLAY_SWAP_XY false

#define DISPLAY_OFFSET_X  0
#define DISPLAY_OFFSET_Y  0

#define DISPLAY_BACKLIGHT_PIN GPIO_NUM_42
#define DISPLAY_BACKLIGHT_OUTPUT_INVERT true

/* Camera pins */
#define CAMERA_PIN_PWDN GPIO_NUM_NC
#define CAMERA_PIN_RESET GPIO_NUM_NC
#define CAMERA_PIN_XCLK GPIO_NUM_5
#define CAMERA_PIN_SIOD GPIO_NUM_1
#define CAMERA_PIN_SIOC GPIO_NUM_2

#define CAMERA_PIN_D7 GPIO_NUM_9
#define CAMERA_PIN_D6 GPIO_NUM_4
#define CAMERA_PIN_D5 GPIO_NUM_6
#define CAMERA_PIN_D4 GPIO_NUM_15
#define CAMERA_PIN_D3 GPIO_NUM_17
#define CAMERA_PIN_D2 GPIO_NUM_8
#define CAMERA_PIN_D1 GPIO_NUM_18
#define CAMERA_PIN_D0 GPIO_NUM_16
#define CAMERA_PIN_VSYNC GPIO_NUM_3
#define CAMERA_PIN_HREF GPIO_NUM_46
#define CAMERA_PIN_PCLK GPIO_NUM_7

#define XCLK_FREQ_HZ 20000000

/* 摄像头的 SCCB（I2C）地址。
 *
 * 值来自 esp_cam_sensor 组件：sensors/gc0308/include/gc0308.h 的
 * GC0308_SCCB_ADDR。这里**刻意抄一份常量**而不是 include 那个头文件 ——
 * esp_cam_sensor 是 esp_video 的**私有**依赖（idf_component.yml 里
 * `require: private`），它的头文件不保证出现在 main 组件的 include 路径上，
 * 直接 include 会在别人换组件版本时突然编译失败。
 *
 * 用途：InitializeCamera() 在上电后先拿它探一次总线，探不到就跳过初始化
 * （详见 lichuang_dev_board.cc 里 InitializeCamera 的注释）。
 * ⚠️ 换摄像头模组时，这个地址必须跟着换。 */
#define CAMERA_SCCB_ADDR 0x21


#endif // _BOARD_CONFIG_H_
