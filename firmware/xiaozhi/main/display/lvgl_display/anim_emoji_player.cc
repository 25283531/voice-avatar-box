#include "anim_emoji_player.h"

#include <esp_log.h>
#include <esp_heap_caps.h>

#include <cstring>

#include "anim/anim_emoji_index.h"

#define TAG "AnimEmoji"

/* 由 CMake 的 target_add_binary_data 生成，见 main/CMakeLists.txt。
 * ⚠️ 符号名会把文件名里的 . 和 - 换成 _：anim_frames.bin →
 * _binary_anim_frames_bin_start / _end。 */
extern const uint8_t anim_frames_bin_start[] asm("_binary_anim_frames_bin_start");
extern const uint8_t anim_frames_bin_end[]   asm("_binary_anim_frames_bin_end");

namespace {

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
    ESP_LOGI(TAG, "动图就绪：%u 个动画，%ux%u，最多 %u 帧，共 %u 字节",
             (unsigned)kAnimEmojiCount, (unsigned)kAnimEmojiFrameSize,
             (unsigned)kAnimEmojiFrameSize, (unsigned)max_frames,
             (unsigned)blob_len);
}

uint16_t AnimEmojiPlayer::FrameMs(uint16_t idx) const {
    if (current_ == nullptr) {
        return 40;
    }
    const size_t at = (size_t)current_->ms_off + idx;
    if (at >= kAnimEmojiFrameMsCount) {
        return 40;   /* 索引头与时长表不同步时的兜底，别让定时器周期变 0 */
    }
    return kAnimEmojiFrameMs[at];
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

const char* AnimEmojiPlayer::DefaultName() const {
    if (!available_ || kAnimEmojiCount == 0) {
        return nullptr;
    }
    return kAnimEmojiTable[0].name;
}

bool AnimEmojiPlayer::PlayDefault(lv_obj_t* img) {
    const char* name = DefaultName();
    return name != nullptr && Play(img, name);
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
    current_index_ = index;
    frame_idx_ = 0;

    lv_image_set_src(img, &dscs_[index * dsc_stride_]);
    /* 周期用第一帧的时长；之后每切一帧按该帧的时长重设（见 Step）。
     * 这样「动作快的地方 30ms、停顿处 300ms」能原样还原，
     * 而不用把定时器固定成最短周期去空转。 */
    timer_ = lv_timer_create([](lv_timer_t* t) {
        static_cast<AnimEmojiPlayer*>(lv_timer_get_user_data(t))->Step();
    }, FrameMs(0), this);
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
    lv_image_set_src(target_, &dscs_[current_index_ * dsc_stride_ + frame_idx_]);

    /* 下一帧该等多久：lv_timer 的周期就是「这一帧停留时间」。
     * 先在回调里改周期再 reset，等价于「从现在起等 ms 毫秒」，
     * 不会因为回调本身耗时把时间轴越拖越慢。 */
    uint16_t ms = FrameMs(frame_idx_);
    if (ms == 0) {
        ms = 40;
    }
    if (timer_ != nullptr) {
        lv_timer_set_period(timer_, ms);
        lv_timer_reset(timer_);
    }
}

void AnimEmojiPlayer::Stop() {
    if (timer_ != nullptr) {
        lv_timer_delete(timer_);
        timer_ = nullptr;
    }
    target_ = nullptr;
    current_ = nullptr;
    current_index_ = 0;
    frame_idx_ = 0;
}
