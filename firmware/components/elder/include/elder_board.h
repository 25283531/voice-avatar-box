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
/* 按键极性：**1 = 低电平有效**（按键另一端接 GND，内部上拉，按下读到 0）。
 *
 * ⚠️ 这个宏不是"按下时的电平值"，而是一个**布尔开关**：
 *     input.c 里写的是 `ACTIVE_LEVEL ? (lvl == 0) : (lvl != 0)`。
 *     填 1 → 低电平算按下；填 0 → 高电平算按下。
 *   2026-10-26 用户确认接线是「GPIO11 直接接 GND」+ 内部上拉 ⇒ 必须填 1。
 *   曾经这里填的是 0（注释却写着"低电平有效"），实际含义是**高电平有效**：
 *   空闲被上拉到 1 会被当成"一直按着"，`released_seen` 永远为假 ⇒
 *   按键一次都不响应，20s 后还会误报"按键卡住"。 */
#define ELDER_KEY_ACTIVE_LEVEL  1

/* 「按住多久算确认完成」—— **唯一真源**。
 *
 * 3 秒是用户定的（2026-10-20 定 3s，2026-10-27 重新设计灯效时再次确认）：
 * 短到老人等得住，长到能把「误碰一下」和「真的想确认」分开。
 *
 * 为什么必须放在板级头文件、而不是各自 .c 里写一个 3000：
 *   input.c 用它判定长按达标，rgb.c 用它算 #1 从浅绿到深绿的过渡进度。
 *   两处各写一份的话，只要有人改了一处，老人就会看到「灯已经过渡到最深绿、
 *   但按键还没算确认」或者反过来 —— **灯在骗人**，比没有灯更糟。
 *   ⚠️ input.c 里的 LONG_HOLD_MS 后续会改成引用本宏（本次不动 input.c，
 *   避免与并行改动冲突）；在它改之前，两个数字必须保持相等。
 */
#define ELDER_BTN_CONFIRM_HOLD_MS   3000

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

/* 驱动灯珠数量：**9 颗**（2026-10-27 用户实测硬件后确认）。
 *
 * 实际接的是 20 颗 WS2812B RGB5050 级联，但只用前 9 颗：
 *   #1       按键指示灯（按下时呼吸，见 elder_rgb.h）
 *   #2 ~ #9  灯效（情绪 / 提醒闪烁 / 服药未确认 / 告警）
 * 后面 11 颗一律送 0 —— 不驱动它们既省电流（20 颗全白约 1.2A，
 * 远超扩展口供电），也避免夜里整条灯带发光干扰老人睡眠。
 *
 * ⚠️ 为什么不是 20：这个数字决定 RMT 一次要发多少符号。
 *    每颗 24 bit，1 颗 = 24 字节 = 192 个 RMT 符号，正好吃满
 *    ESP32-S3 一个 RMT 组的全部内存（8 通道 × 48 字 = 384，实测
 *    mem_block_symbols=64 时按 48 符号一块 ping-pong 流式发送）。
 *    9 颗 = 27 字节 = 216 符号，仍在驱动可流式发送的范围内（已验证
 *    1 颗时 192 符号就是靠流式发出去的）。若要再往上加颗数，
 *    先确认 rmt_transmit 仍能连续、无 >50µs 断流（断流会被灯珠
 *    当成复位，后半段颜色不更新）。 */
#define ELDER_RGB_LEDS          9

/* ---------- 板上自带 ---------- */
#define ELDER_BUILTIN_LED       GPIO_NUM_48

#endif /* ELDER_BOARD_H */
