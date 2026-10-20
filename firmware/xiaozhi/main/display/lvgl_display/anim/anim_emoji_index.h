// 自动生成，勿手改。由 tools/anim_emoji_pack.py 产出。
//
// 素材：Noto Animated Emoji (https://fonts.google.com/noto/specimen/Noto+Emoji)
//       版权所有 Google，许可 CC BY 4.0。
//       512x512 GIF 按运动量自适应抽帧，缩放到 64x64，
//       转 LVGL LV_COLOR_FORMAT_RGB565A8（前 w*h*2 的 RGB565，后 w*h 的 alpha）。
//
// frame_ms 是这个动画每一帧该停多久（ms），时间轴与原 GIF 一致。
// 二进制由 CMake 的 target_add_binary_data 嵌入，符号名见 .cc 里的 extern 声明。
#ifndef ANIM_EMOJI_INDEX_H
#define ANIM_EMOJI_INDEX_H

#include <cstdint>
#include <cstddef>

struct AnimEmojiEntry {
    const char* name;      // 动画名，作为 key 用
    uint32_t offset;       // 在 anim_frames_bin 里的字节偏移
    uint16_t frames;       // 帧数
    uint16_t frame_bytes;  // 单帧字节数（w*h*3）
    uint16_t ms_off;       // 本动画第一帧的时长在 kAnimEmojiFrameMs 里的下标
    uint16_t size;         // 边长
};

static constexpr uint16_t kAnimEmojiFrameSize = 64;

static const AnimEmojiEntry kAnimEmojiTable[] = {
    {"panda", 0u, 59u, 12288u, 0u, 64u},
};

static constexpr size_t kAnimEmojiCount = sizeof(kAnimEmojiTable) / sizeof(kAnimEmojiTable[0]);

static constexpr size_t kAnimEmojiBlobSize = 724992u;

// 各帧停留时长（ms），下标以 AnimEmojiEntry::ms_off 为基
static const uint16_t kAnimEmojiFrameMs[] = {
    240u, 30u, 30u, 30u, 30u, 60u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u,
    30u, 30u, 60u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u,
    30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u, 30u,
    60u, 30u, 30u, 30u, 360u,
};

static constexpr size_t kAnimEmojiFrameMsCount = sizeof(kAnimEmojiFrameMs) / sizeof(kAnimEmojiFrameMs[0]);

#endif  // ANIM_EMOJI_INDEX_H
