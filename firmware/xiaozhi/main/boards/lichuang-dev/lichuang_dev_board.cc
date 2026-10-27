#include "wifi_board.h"
#include "codecs/box_audio_codec.h"
#include "display/lcd_display.h"
// emote 表情引擎只在 CONFIG_USE_EMOTE_MESSAGE_STYLE 下用；本产品固定 LVGL。
// 对应的 display/emote_display.cc 已从编译列表移除（见 main/CMakeLists.txt）。
#if defined(CONFIG_USE_EMOTE_MESSAGE_STYLE)
#include "display/emote_display.h"
#endif
#include "application.h"
#include "button.h"
#include "config.h"
#include "i2c_device.h"

#include <esp_log.h>
#include <esp_lcd_panel_vendor.h>
#include <cstring>
#include <cstdio>
#include <driver/i2c_master.h>
#include <driver/spi_common.h>
#include <wifi_station.h>
#include <esp_lcd_touch_ft5x06.h>
#include <esp_lvgl_port.h>
#include <lvgl.h>

#define TAG "LichuangDevBoard"

class Pca9557 : public I2cDevice {
public:
    Pca9557(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        WriteReg(0x01, 0x03);
        WriteReg(0x03, 0xf8);
    }

    void SetOutputState(uint8_t bit, uint8_t level) {
        uint8_t data = ReadReg(0x01);
        data = (data & ~(1 << bit)) | (level << bit);
        WriteReg(0x01, data);
    }
};

class CustomAudioCodec : public BoxAudioCodec {
private:
    Pca9557* pca9557_;

public:
    CustomAudioCodec(i2c_master_bus_handle_t i2c_bus, Pca9557* pca9557) 
        : BoxAudioCodec(i2c_bus, 
                       AUDIO_INPUT_SAMPLE_RATE, 
                       AUDIO_OUTPUT_SAMPLE_RATE,
                       AUDIO_I2S_GPIO_MCLK, 
                       AUDIO_I2S_GPIO_BCLK, 
                       AUDIO_I2S_GPIO_WS, 
                       AUDIO_I2S_GPIO_DOUT, 
                       AUDIO_I2S_GPIO_DIN,
                       GPIO_NUM_NC, 
                       AUDIO_CODEC_ES8311_ADDR, 
                       AUDIO_CODEC_ES7210_ADDR, 
                       AUDIO_INPUT_REFERENCE),
          pca9557_(pca9557) {
    }

    virtual void EnableOutput(bool enable) override {
        BoxAudioCodec::EnableOutput(enable);
        if (enable) {
            pca9557_->SetOutputState(1, 1);
        } else {
            pca9557_->SetOutputState(1, 0);
        }
    }
};

class LichuangDevBoard : public WifiBoard {
private:
    i2c_master_bus_handle_t i2c_bus_;
    i2c_master_dev_handle_t pca9557_handle_;
    Button boot_button_;
    Display* display_;
    Pca9557* pca9557_;

    void InitializeI2c() {
        // Initialize I2C peripheral
        i2c_master_bus_config_t i2c_bus_cfg = {
            .i2c_port = (i2c_port_t)1,
            .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
            .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {
                .enable_internal_pullup = 1,
            },
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus_));

        // Initialize PCA9557
        pca9557_ = new Pca9557(i2c_bus_, 0x19);
    }

    void InitializeSpi() {
        spi_bus_config_t buscfg = {};
        buscfg.mosi_io_num = GPIO_NUM_40;
        buscfg.miso_io_num = GPIO_NUM_NC;
        buscfg.sclk_io_num = GPIO_NUM_41;
        buscfg.quadwp_io_num = GPIO_NUM_NC;
        buscfg.quadhd_io_num = GPIO_NUM_NC;
        buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
        ESP_ERROR_CHECK(spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO));
    }

    void InitializeButtons() {
        /* 板载用户按键（GPIO0 BOOT）= **一键唤醒**（2026-10-27 用户拍板）。
         *
         * 原先是 ToggleChatState()：待机→收听没问题，但"正在说话时按一下"
         * 只会打断并回到待机，老人还得再按一次才能说话 —— 用户的原话是
         * "如果硬件正在说话，可以按键中断硬件说话，直接开始收听用户说话"。
         * 改用 WakeUp()：说话中 → 打断 + 直接进收听；其余状态行为不变。
         *
         * 注意：扩展口上的 GPIO11 按键**不是**这个键，它只做日程/服药确认，
         * 见 firmware/components/elder/src/elder_bridge.cc 的 OnKey。 */
        boot_button_.OnClick([this]() {
            auto& app = Application::GetInstance();
            if (app.GetDeviceState() == kDeviceStateStarting && !WifiStation::GetInstance().IsConnected()) {
                ResetWifiConfiguration();
            }
            app.WakeUp();
        });

#if CONFIG_USE_DEVICE_AEC
        boot_button_.OnDoubleClick([this]() {
            auto& app = Application::GetInstance();
            if (app.GetDeviceState() == kDeviceStateIdle) {
                app.SetAecMode(app.GetAecMode() == kAecOff ? kAecOnDeviceSide : kAecOff);
            }
        });
#endif
    }

    void InitializeSt7789Display() {
        esp_lcd_panel_io_handle_t panel_io = nullptr;
        esp_lcd_panel_handle_t panel = nullptr;
        // 液晶屏控制IO初始化
        ESP_LOGD(TAG, "Install panel IO");
        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = GPIO_NUM_NC;
        io_config.dc_gpio_num = GPIO_NUM_39;
        io_config.spi_mode = 2;
        io_config.pclk_hz = 80 * 1000 * 1000;
        io_config.trans_queue_depth = 10;
        io_config.lcd_cmd_bits = 8;
        io_config.lcd_param_bits = 8;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI3_HOST, &io_config, &panel_io));

        // 初始化液晶屏驱动芯片ST7789
        ESP_LOGD(TAG, "Install LCD driver");
        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = GPIO_NUM_NC;
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
        panel_config.bits_per_pixel = 16;
        ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(panel_io, &panel_config, &panel));
        
        esp_lcd_panel_reset(panel);
        pca9557_->SetOutputState(0, 0);

        esp_lcd_panel_init(panel);
        esp_lcd_panel_invert_color(panel, true);
        esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY);
        esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);
        esp_lcd_panel_disp_on_off(panel, true);

#if CONFIG_USE_EMOTE_MESSAGE_STYLE
        display_ = new emote::EmoteDisplay(panel, panel_io, DISPLAY_WIDTH, DISPLAY_HEIGHT);
#else
        display_ = new SpiLcdDisplay(panel_io, panel,
            DISPLAY_WIDTH, DISPLAY_HEIGHT, DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
#endif
    }

    /* 触摸屏：FT5x06，挂在音频 codec 那条共享 I2C 上（地址 0x38，见
     * ESP_LCD_TOUCH_IO_I2C_FT5x06_CONFIG()）。
     *
     * ★ **驱动配置已核对完整，不需要补任何 CONFIG_LV_TOUCH_***：
     *   * 驱动本体：main/idf_component.yml 里有 espressif/esp_lcd_touch_ft5x06，
     *     编译期无条件编进来，没有「开某个 Kconfig 才有驱动」这种开关；
     *   * esp_lvgl_port 的触摸支持也是自动的 —— esp_lvgl_port_touch.h 里
     *     `#if __has_include("esp_lcd_touch.h")` 会定义
     *     ESP_LVGL_PORT_TOUCH_COMPONENT，本工程有 esp_lcd_touch 依赖，必然命中。
     *
     * ★ **坐标映射已核对正确，不要"顺手改"**：
     *   面板原生是**竖屏 240x320**；`esp_lcd_panel_swap_xy(true)` 把 ST7789 的
     *   MADCTL.MV 置起来，CASET 才能寻址 320 列 ⇒ 逻辑横屏 320x240。
     *   esp_lvgl_port 只把 swap/mirror 透传给面板（sw_rotate=0 时不改 LVGL 的
     *   rotation），所以 LVGL 侧始终是 320x240，indev 要求驱动上报的也是
     *   **逻辑横屏帧** (x∈[0,320), y∈[0,240))。
     *   本函数给 x_max=DISPLAY_HEIGHT(240) / y_max=DISPLAY_WIDTH(320) + flags
     *   swap_xy=1/mirror_x=1/mirror_y=0，经 esp_lcd_touch 的软件调整后上报的
     *   正是这个逻辑帧。✔
     *   ⚠️ 2026-10-20 反例：把 swap_xy 关掉试图"改成横屏"，结果整屏斜切花屏
     *      —— 因为 MV=0 时面板只有 240 列可寻址。方向不是这么改的，见 config.h。
     *
     * ★ **与"屏幕无显示"的依赖关系（重要，别让两个改动打架）**：
     *   下面要用 lv_display_get_default()，它只在**LVGL 的 display 被创建之后**
     *   才非 NULL。之前一直返回 NULL 的根因是显示走了 EmoteDisplay
     *   （esp_emote_gfx 自绘引擎，既不 lv_init() 也不建 LVGL display），
     *   **不是触摸屏驱动的问题**。改回 SpiLcdDisplay 之后它自然有值。
     *    ⇒ 所以：显示修好 ⇒ 这条自动好；显示没修好 ⇒ 这里改到天上去也没用。
     *      本函数只负责**不在这种状态下刷 ERROR、不 assert**，见下面。 */
    void InitializeTouch()
    {
        /* ⚠️ 必须初始化成 nullptr：下面三个 API 只要有一个失败，
         * out_touch **不会被赋值**。原代码写的是未初始化的 `tp` 再 `assert(tp)`，
         * 于是「触摸屏没接 / I2C 通信失败」时读到的是栈上的随机值 ——
         * 运气好 assert 崩掉重启，运气不好 assert 通过、拿着野指针往下走。 */
        esp_lcd_touch_handle_t tp = nullptr;
        esp_lcd_touch_config_t tp_cfg = {
            /* 2026-10-20 第二次修正：显示回到「面板原生竖屏 + swap_xy」后，
             * 触摸也必须回到同一套坐标（与 config.h 的 SWAP_XY/MIRROR_X 一一对应）。
             *
             * 为什么是 x_max=HEIGHT / y_max=WIDTH：esp_lcd_touch 的 x_max/y_max
             * 描述的是**面板原生帧**（240 宽 x 320 高），协议层再按 flags 换成
             * 逻辑横屏 (320x240) 上报给 LVGL。写反了现象是「画面对、点哪儿都偏」。
             *
             * 判断依据（不要再靠猜）：立创官方 BSP 与本文件 InitializeSt7789Display
             * 用的是同一组 esp_lcd_panel_swap_xy(true)+mirror(true,false)。 */
            .x_max = DISPLAY_HEIGHT,
            .y_max = DISPLAY_WIDTH,
            .rst_gpio_num = GPIO_NUM_NC, // Shared with LCD reset
            .int_gpio_num = GPIO_NUM_NC, // NC => 轮询模式，见 lvgl_port_add_touch
            .levels = {
                .reset = 0,
                .interrupt = 0,
            },
            .flags = {
                .swap_xy = 1,
                .mirror_x = 1,
                .mirror_y = 0,
            },
        };
        esp_lcd_panel_io_handle_t tp_io_handle = NULL;
        esp_lcd_panel_io_i2c_config_t tp_io_config = ESP_LCD_TOUCH_IO_I2C_FT5x06_CONFIG();
        tp_io_config.scl_speed_hz = 400000;

        /* 三步各自判返回值，任何一步失败都**优雅降级**：
         * 触摸屏不是核心功能（语音 / 提醒 / 联网都不依赖它），
         * 一块屏没接or 触摸芯片坏了就把整块板初始化打断（原代码的 assert 就是
         * 这个效果）属于把「少一个便利功能」放大成「设备变砖」。 */
        esp_err_t err = esp_lcd_new_panel_io_i2c(i2c_bus_, &tp_io_config, &tp_io_handle);
        if (err != ESP_OK || tp_io_handle == NULL) {
            ESP_LOGW(TAG, "触摸屏 IO 未建起来（err=0x%x）→ 跳过触摸；"
                          "不影响语音 / 提醒 / 联网，只是不能点屏", err);
            return;
        }

        err = esp_lcd_touch_new_i2c_ft5x06(tp_io_handle, &tp_cfg, &tp);
        if (err != ESP_OK || tp == nullptr) {
            ESP_LOGW(TAG, "FT5x06 未初始化（err=0x%x，I2C 地址 0x%02X）→ 跳过触摸；"
                          "常见原因是屏排线没接好", err, ESP_LCD_TOUCH_IO_I2C_FT5x06_ADDRESS);
            return;
        }

        lv_display_t *disp = lv_display_get_default();
        if (disp == nullptr) {
            /* ⚠️ 这条**不是**触摸屏的错。它说的是「LVGL display 还没建出来」，
             * 根因在显示路径（见 InitializeSt7789Display 与上面那段依赖说明）。
             * 所以这里打 WARNING 而不是 ERROR —— ERROR 会让人去查触摸屏，
             * 而真正该看的是屏幕。 */
            ESP_LOGW(TAG, "LVGL display 尚未就绪 → 触摸屏暂不注册"
                          "（待显示路径修好后自动生效，不是触摸屏驱动的问题）");
            return;
        }

        /* Add touch input (for selected screen) */
        const lvgl_port_touch_cfg_t touch_cfg = {
            .disp = disp,
            .handle = tp,
        };
        if (lvgl_port_add_touch(&touch_cfg) == nullptr) {
            ESP_LOGW(TAG, "lvgl_port_add_touch 返回 NULL → 触摸输入未注册");
        }
    }

public:
    LichuangDevBoard() : boot_button_(BOOT_BUTTON_GPIO) {
        InitializeI2c();
        InitializeSpi();
        InitializeSt7789Display();
        InitializeTouch();
        InitializeButtons();

        GetBacklight()->RestoreBrightness();
    }

    virtual AudioCodec* GetAudioCodec() override {
        static CustomAudioCodec audio_codec(
            i2c_bus_, 
            pca9557_);
        return &audio_codec;
    }

    virtual Display* GetDisplay() override {
        return display_;
    }
    
    virtual Backlight* GetBacklight() override {
        static PwmBacklight backlight(DISPLAY_BACKLIGHT_PIN, DISPLAY_BACKLIGHT_OUTPUT_INVERT);
        return &backlight;
    }

};

DECLARE_BOARD(LichuangDevBoard);
