#include "anim_emoji_player.h"

#include <esp_log.h>
#include <esp_random.h>
#include <esp_heap_caps.h>

#include <cstring>

#include "anim_emoji_index.h"

#define TAG "AnimEmoji"

/* 由 CMake 的 target_add_binary_data 生成，见 main/CMakeLists.txt。
 * ⚠️ 符号名会把文件名里的 . 和 - 换成 _：anim_frames.bin →
 * _binary_anim_frames_bin_start / _end。 */
extern const uint8_t anim_frames_bin_start[] asm("_binary_anim_frames_bin_start");
extern const uint8_t anim_frames_bin_end[]   asm("_binary_anim_frames_bin_end");

namespace {

/* 帧间隔。Noto 原 GIF 是 30ms/帧、58 帧；我们抽成 8 帧后，
 * 每帧 100ms ⇒ 一轮 0.8 秒，接近原速，又不会把刷屏总线占满。
 * 再快意义不大：320x240 屏一次全屏刷新就是 150KB。 */
constexpr uint32_t kFrameIntervalMs = 100;

/* 只允许在 PSRAM 上分配描述符池：内部 SRAM 在运行期只剩 45KB 上下
 * （见串口 SystemInfo 日志），几十个 dsc 塞进去会挤压音频缓冲。 */
constexpr uint32_t kPoolCaps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;

}  // namespace

AnimEmojiPlayer& AnimEmojiPlayer::Get() {
    static AnimEmojiPlayer instance;
    return instance;
}

void AnimEmojiPlayer::Init() {
    if (inited_) {
        return;
    }
    inited_ = true;

    const size_t blob_len = (size_t)(anim_frames_bin_end - anim_frames_bin_start);
    if (blob_len != kAnimEmojiBlobSize) {
        /* 长度不符说明生成的头文件与 bin 不同步（改过帧数/尺寸却没重新
         * 生成）。这种情况宁可不播，也不能让 dsc 指到越界地址 —— 越界读
         * 在 PSRAM/flash 映射区表现为随机花屏，很难查。 */
        ESP_LOGE(TAG, "帧数据长度不符：bin=%u 期望=%u → 不启用动图",
                 (unsigned)blob_len, (unsigned)kAnimEmojiBlobSize);
        return;
    }
    if (kAnimEmojiCount == 0 || kAnimEmojiFrameSize == 0) {
        ESP_LOGW(TAG, "索引表为空 → 不启用动图");
        return;
    }

    uint16_t max_frames = 0;
    for (size_t i = 0; i < kAnimEmojiCount; ++i) {
        if (kAnimEmojiTable[i].frames > max_frames) {
            max_frames = kAnimEmojiTable[i].frames;
        }
    }
    dsc_stride_ = max_frames;

    const size_t dsc_count = (size_t)kAnimEmojiCount * max_frames;
    dscs_ = (lv_image_dsc_t*)heap_caps_malloc(dsc_count * sizeof(lv_image_dsc_t), kPoolCaps);
    if (dscs_ == nullptr) {
        ESP_LOGE(TAG, "描述符池分配失败（%u 个）→ 不启用动图", (unsigned)dsc_count);
        return;
    }
    memset(dscs_, 0, dsc_count * sizeof(lv_image_dsc_t));

    for (size_t i = 0; i < kAnimEmojiCount; ++i) {
        const AnimEmojiEntry& e = kAnimEmojiTable[i];
        const uint16_t side = e.size;
        for (uint16_t f = 0; f < e.frames; ++f) {
            lv_image_dsc_t* d = &dscs_[i * dsc_stride_ + f];
            d->header.magic = LV_IMAGE_HEADER_MAGIC;
            d->header.cf = LV_COLOR_FORMAT_RGB565A8;
            d->header.flags = 0;
            d->header.w = side;
            d->header.h = side;
            d->header.stride = (uint16_t)(side * 2);   /* RGB565 ⇒ 2 字节/像素 */
            d->data_size = (uint32_t)e.frame_bytes;    /* w*h*2(RGB) + w*h(alpha) */
            d->data = anim_frames_bin_start + e.offset + (uint32_t)f * e.frame_bytes;
            d->reserved = nullptr;
            d->reserved_2 = nullptr;
        }
    }

    available_ = true;
    ESP_LOGI(TAG, "动图就绪：%u 个动画，%ux%u，最多 %u 帧，每帧 %ums，共 %u 字节",
             (unsigned)kAnimEmojiCount, (unsigned)kAnimEmojiFrameSize,
             (unsigned)kAnimEmojiFrameSize, (unsigned)max_frames,
             (unsigned)kFrameIntervalMs, (unsigned)blob_len);
}

bool AnimEmojiPlayer::Has(const char* name) const {
    if (!available_ || name == nullptr) {
        return false;
    }
    for (size_t i = 0; i < kAnimEmojiCount; ++i) {
        if (strcmp(kAnimEmojiTable[i].name, name) == 0) {
            return true;
        }
    }
    return false;
}

const char* AnimEmojiPlayer::RandomName(const char* avoid) const {
    if (!available_ || kAnimEmojiCount == 0) {
        return nullptr;
    }
    /* 最多试 8 次避开上一次；全撞上就随便给一个（总比不换好） */
    for (int attempt = 0; attempt < 8; ++attempt) {
        const AnimEmojiEntry& e = kAnimEmojiTable[esp_random() % kAnimEmojiCount];
        if (avoid == nullptr || strcmp(e.name, avoid) != 0) {
            return e.name;
        }
    }
    return kAnimEmojiTable[0].name;
}

bool AnimEmojiPlayer::Play(lv_obj_t* img, const char* name) {
    Stop();
    if (!available_ || img == nullptr || name == nullptr) {
        return false;
    }

    const AnimEmojiEntry* found = nullptr;
    size_t index = 0;
    for (size_t i = 0; i < kAnimEmojiCount; ++i) {
        if (strcmp(kAnimEmojiTable[i].name, name) == 0) {
            found = &kAnimEmojiTable[i];
            index = i;
            break;
        }
    }
    if (found == nullptr || found->frames == 0) {
        ESP_LOGW(TAG, "没有这个动画：%s", name);
        return false;
    }

    target_ = img;
    current_ = found;
    frame_idx_ = 0;

    lv_image_set_src(img, &dscs_[index * dsc_stride_]);
    timer_ = lv_timer_create([](lv_timer_t* t) {
        static_cast<AnimEmojiPlayer*>(lv_timer_get_user_data(t))->Step();
    }, kFrameIntervalMs, this);
    if (timer_ == nullptr) {
        ESP_LOGW(TAG, "帧定时器创建失败，退回静态帧");
        target_ = nullptr;
        current_ = nullptr;
        return false;
    }
    ESP_LOGI(TAG, "播放动画：%s（%u 帧）", found->name, (unsigned)found->frames);
    return true;
}

void AnimEmojiPlayer::Step() {
    if (current_ == nullptr || target_ == nullptr) {
        Stop();
        return;
    }
    /* 目标对象被删了（切主题会重建 UI）⇒ 立刻停，别对野指针 set_src */
    if (!lv_obj_is_valid(target_)) {
        target_ = nullptr;
        current_ = nullptr;
        Stop();
        return;
    }

    frame_idx_ = (uint16_t)((frame_idx_ + 1) % current_->frames);
    /* 用当前动画在表里的下标算 dsc 位置 */
    size_t index = 0;
    for (size_t i = 0; i < kAnimEmojiCount; ++i) {
        if (&kAnimEmojiTable[i] == current_) {
            index = i;
            break;
        }
    }
    lv_image_set_src(target_, &dscs_[index * dsc_stride_ + frame_idx_]);
}

void AnimEmojiPlayer::Stop() {
    if (timer_ != nullptr) {
        lv_timer_delete(timer_);
        timer_ = nullptr;
    }
    target_ = nullptr;
    current_ = nullptr;
    frame_idx_ = 0;
}
