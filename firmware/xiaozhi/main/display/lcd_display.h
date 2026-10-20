#ifndef LCD_DISPLAY_H
#define LCD_DISPLAY_H

#include "lvgl_display.h"
#include "gif/lvgl_gif.h"

#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <font_emoji.h>

#include <atomic>
#include <memory>
#include <string>

#define PREVIEW_IMAGE_DURATION_MS 5000

/* 防烧屏轮播（2026-10-20 用户要求：屏幕常亮固定一张图，担心烧屏）。
 *
 * 做法：空闲时每隔一段时间把表情图换一张。为什么是"轮换表情"而不是
 * "整屏平移"：这是 320x240 IPS，真正会留下残影的是**长期不动的高对比
 * 区域**（中央的表情图正是这种）；整屏平移一两个像素在 LVGL 里要动
 * 所有对象、每帧重排，代价远大于收益，而且用户看不懂"界面在抖"。
 *
 * 判据用「距上次 UI 变化多久」而不是「当前设备状态」：设备状态要问
 * Application，显示层不该反向依赖它；而"界面 60 秒没变过"本身就是
 * 烧屏风险的定义。
 */
#define ANTI_BURN_CHECK_MS  20000   /* 每 20s 检查一次 */
#define ANTI_BURN_IDLE_MS   60000   /* 界面 60s 无变化才轮换 */


class LcdDisplay : public LvglDisplay {
protected:
    esp_lcd_panel_io_handle_t panel_io_ = nullptr;
    esp_lcd_panel_handle_t panel_ = nullptr;
    
    lv_draw_buf_t draw_buf_;
    lv_obj_t* top_bar_ = nullptr;
    lv_obj_t* status_bar_ = nullptr;
    lv_obj_t* content_ = nullptr;
    lv_obj_t* container_ = nullptr;
    lv_obj_t* side_bar_ = nullptr;
    lv_obj_t* bottom_bar_ = nullptr;
    lv_obj_t* preview_image_ = nullptr;
    lv_obj_t* emoji_label_ = nullptr;
    lv_obj_t* emoji_image_ = nullptr;
    std::unique_ptr<LvglGif> gif_controller_ = nullptr;
    lv_obj_t* emoji_box_ = nullptr;
    lv_obj_t* chat_message_label_ = nullptr;
    esp_timer_handle_t preview_timer_ = nullptr;
    std::unique_ptr<LvglImage> preview_image_cached_ = nullptr;
    bool hide_subtitle_ = false;  // Control whether to hide chat messages/subtitles

    void InitializeLcdThemes();
    void SetupUI();
    virtual bool Lock(int timeout_ms = 0) override;
    virtual void Unlock() override;

    /* 防烧屏：在 SetupUI 末尾启动，空闲时随机换表情（见文件顶部宏注释） */
    void StartAntiBurnInTimer();
    void AntiBurnInStep();
    lv_timer_t* anti_burn_timer_ = nullptr;
    uint32_t last_ui_change_tick_ = 0;      /* lv_tick 毫秒 */
    std::string anti_burn_last_emoji_;      /* 上一次轮播选中的表情，避免连选同一张 */

protected:
    // Add protected constructor
    LcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel, int width, int height);
    
public:
    ~LcdDisplay();
    virtual void SetEmotion(const char* emotion) override;
    virtual void SetChatMessage(const char* role, const char* content) override; 
    virtual void SetPreviewImage(std::unique_ptr<LvglImage> image) override;

    // Add theme switching function
    virtual void SetTheme(Theme* theme) override;
    
    // Set whether to hide chat messages/subtitles
    void SetHideSubtitle(bool hide);
};

// SPI LCD display
class SpiLcdDisplay : public LcdDisplay {
public:
    SpiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                  int width, int height, int offset_x, int offset_y,
                  bool mirror_x, bool mirror_y, bool swap_xy);
};

// RGB LCD display
class RgbLcdDisplay : public LcdDisplay {
public:
    RgbLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                  int width, int height, int offset_x, int offset_y,
                  bool mirror_x, bool mirror_y, bool swap_xy);
};

// MIPI LCD display
class MipiLcdDisplay : public LcdDisplay {
public:
    MipiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                   int width, int height, int offset_x, int offset_y,
                   bool mirror_x, bool mirror_y, bool swap_xy);
};

#endif // LCD_DISPLAY_H
