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

    /* 空闲屏保：在 SetupUI 末尾启动，静止 60s 后循环播放熊猫动图，
     * 一有语境（SetEmotion / SetChatMessage）就停掉、回到静态表情图标。 */
    void StartAntiBurnInTimer();
    void AntiBurnInStep();
    lv_timer_t* anti_burn_timer_ = nullptr;
    uint32_t last_ui_change_tick_ = 0;      /* lv_tick 毫秒 */
    std::string anti_burn_last_emoji_;      /* 兜底轮播用：避免连选同一张 */

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

    // >>> BEGIN elder-reminder-fullscreen (2026-10-22，需求 8) >>>
    /* 全屏显示一条提醒内容（图片区不再显示，用整屏大字）。
     *
     * 为什么放在 LcdDisplay 而不是基类 Display：
     *   基类 display.h 是上游文件，加虚函数会牵动所有子类（OLED / Emote /
     *   RGB……全部要跟着实现一遍）。而"全屏大字提醒"只对这块 320x240 的
     *   屏有意义，所以只在本类暴露，由 elder_bridge 用 dynamic_cast 取到
     *   （本工程 -frtti 已开）。代价：没有屏/不是这台机时拿不到 overlay，
     *   但那条路径本来就不该显示全屏提醒。
     *
     * 显隐由调用方控制（elder_ui 负责 4 小时倒计时），本类不管计时。
     */
    void ShowReminder(const char* text);
    void HideReminder();
    lv_obj_t* reminder_overlay_ = nullptr;   /* 懒创建，全屏不透明 */
    lv_obj_t* reminder_label_ = nullptr;
    // <<< END elder-reminder-fullscreen <<<
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
