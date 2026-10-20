// 自动生成，勿手改。由 tools/anim_emoji_pack.py 产出。
//
// 素材：Noto Animated Emoji (https://fonts.google.com/noto/specimen/Noto+Emoji)
//       版权所有 Google，许可 CC BY 4.0。
//       512x512 GIF 均匀抽 8 帧，缩放到 64x64，
//       转 LVGL LV_COLOR_FORMAT_RGB565A8（前 w*h*2 的 RGB565，后 w*h 的 alpha）。
//
// 二进制由 CMake 的 target_add_binary_data 嵌入，符号名见 .cc 里的 extern 声明。
#ifndef ANIM_EMOJI_INDEX_H
#define ANIM_EMOJI_INDEX_H

#include <cstdint>
#include <cstddef>

struct AnimEmojiEntry {
    const char* name;      // 轮播用的 key
    uint32_t offset;       // 在 anim_frames_bin 里的字节偏移
    uint16_t frames;       // 帧数
    uint16_t frame_bytes;  // 单帧字节数（w*h*3）
    uint16_t size;         // 边长
};

static constexpr uint16_t kAnimEmojiFrameSize = 64;

static const AnimEmojiEntry kAnimEmojiTable[] = {
    {"wave", 0u, 8u, 12288u, 64u},
    {"smile", 98304u, 8u, 12288u, 64u},
    {"heart_eyes", 196608u, 8u, 12288u, 64u},
    {"thumbs_up", 294912u, 8u, 12288u, 64u},
    {"heart", 393216u, 8u, 12288u, 64u},
    {"sleepy", 491520u, 8u, 12288u, 64u},
};

static constexpr size_t kAnimEmojiCount = sizeof(kAnimEmojiTable) / sizeof(kAnimEmojiTable[0]);

static constexpr size_t kAnimEmojiBlobSize = 589824u;

#endif  // ANIM_EMOJI_INDEX_H
