#ifndef ANIM_EMOJI_PLAYER_H
#define ANIM_EMOJI_PLAYER_H

#include <cstdint>

#include "lvgl.h"

/*
 * 表情动图播放器
 *
 * 为什么不用 lv_animimg 也不用 GIF 解码器：见 tools/anim_emoji_pack.py 文件头。
 * 一句话：`lv_animimg` 内部就是「一个 lv_timer 反复 lv_image_set_src」，
 * 我们自己写这 20 行，既省掉 CONFIG_LV_USE_ANIMIMG 这个开关（当前是 n，
 * 开了要多链一份组件），也不用碰 GIF 的逐帧 LZW 解码。
 * 播放时 CPU 只花在「换一张已经解好的位图」上。
 *
 * 数据来自 CMake 的 target_add_binary_data 嵌入的 anim_frames.bin，
 * 索引表是同目录 anim/ 下的 anim_emoji_index.h（由 tools/anim_emoji_pack.py 生成）。
 *
 * 现在的产品形态只有一张动画（熊猫），用途是「空闲屏保」：
 *   · 界面 60 秒没变化 ⇒ 开始播放，**无限循环**，直到有人说话
 *   · 有语境（在听/在想/在说/有提醒）⇒ 由 LcdDisplay::SetEmotion() 调 Stop()，
 *     回到静态表情图标
 * 所以 Play() 没有「播一轮就停」的语义，Stop() 只由外部触发。
 */
class AnimEmojiPlayer {
public:
    static AnimEmojiPlayer& Get();

    /* 构造帧描述符。幂等；blob 缺失或索引为空时 Available() 返回 false，
     * 上层的空闲轮播会退回静态表情，不影响其他功能。 */
    void Init();
    bool Available() const { return available_; }

    /* name 必须在索引表里，否则返回 false（调用方别当成错误，退回静态图即可） */
    bool Has(const char* name) const;

    /* 在 img 上循环播放 name。会先停掉当前正在播的。
     * img 必须是 lv_image；返回 false 表示没播（名字不存在/未初始化）。 */
    bool Play(lv_obj_t* img, const char* name);

    /* 播索引表里第一个（也是目前唯一的）动画。名字不必硬编码到调用方，
     * 以后换动物只改 tools/anim_emoji_pack.py 的一行。 */
    bool PlayDefault(lv_obj_t* img);

    /* 第一个动画的名字，没有可用动画时返回 nullptr */
    const char* DefaultName() const;

    /* 停掉当前动画。**必须**在 SetEmotion 等会改写 img 的地方之前调用，
     * 否则 timer 会把一张已经换掉的图又切回动画帧（花屏）。 */
    void Stop();

    bool Playing() const { return timer_ != nullptr; }

private:
    AnimEmojiPlayer() = default;
    AnimEmojiPlayer(const AnimEmojiPlayer&) = delete;
    AnimEmojiPlayer& operator=(const AnimEmojiPlayer&) = delete;

    void Step();
    uint16_t FrameMs(uint16_t idx) const;

    bool available_ = false;
    bool inited_ = false;

    lv_obj_t* target_ = nullptr;
    lv_timer_t* timer_ = nullptr;

    const struct AnimEmojiEntry* current_ = nullptr;
    size_t current_index_ = 0;
    uint16_t frame_idx_ = 0;

    /* 帧描述符池：每个动画每一帧一个 lv_image_dsc_t。
     * 数量在编译期由索引表决定，运行时按需 malloc 一次。 */
    lv_image_dsc_t* dscs_ = nullptr;
    uint16_t dsc_stride_ = 0;   /* 每个动画占多少个 dsc（= 最大帧数） */
};

#endif  // ANIM_EMOJI_PLAYER_H
