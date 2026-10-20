#include "dual_wake_word.h"

#include <esp_log.h>

#include <utility>

#include "afe_wake_word.h"
#include "custom_wake_word.h"

#define TAG "DualWakeWord"

DualWakeWord::DualWakeWord() = default;

DualWakeWord::~DualWakeWord() {
    /* models_list 是 AudioService 传进来的**同一份** srmodel 列表，
     * 两个子引擎的析构都会调 esp_srmodel_deinit(它) ⇒ 双重释放。
     * 让 MultiNet 放弃所有权，WakeNet 保留并负责释放。
     * ⚠️ WakeNet 没起来时（wakenet_ 为空）不能让它放弃，否则没人释放。 */
    if (multinet_ && wakenet_) {
        multinet_->ForgetModels();
    }
}

bool DualWakeWord::Initialize(AudioCodec* codec, srmodel_list_t* models_list) {
    if (codec == nullptr) {
        return false;
    }
    channels_ = (size_t)codec->input_channels();

    /* 1) MultiNet 先起：它承载**离线服药命令词**，是功能刚需，
     *    失败就等于老人说不出话也按不动（只剩按键），必须当致命错误上报。 */
    multinet_ = std::make_unique<CustomWakeWord>();
    if (!multinet_->Initialize(codec, models_list)) {
        ESP_LOGE(TAG, "MultiNet 初始化失败 → 离线命令词不可用");
        multinet_.reset();
        return false;
    }
    multinet_->OnWakeWordDetected([this](const std::string& word) {
        last_word_ = word;
        fired_ = 2;
        ESP_LOGI(TAG, "MultiNet 命中唤醒词：%s", word.c_str());
        if (wake_word_detected_callback_) {
            wake_word_detected_callback_(word);
        }
    });
    mn_framer_.chunk = multinet_->GetFeedSize();

    /* 2) WakeNet 尽力而起：模型没打进 assets / AFE 内存不够时**降级继续**，
     *    不能让整个唤醒功能起不来（MultiNet 仍在，只是召回差一些）。 */
    wakenet_ = std::make_unique<AfeWakeWord>();
    if (wakenet_->Initialize(codec, models_list)) {
        wakenet_ready_ = true;
        wakenet_->OnWakeWordDetected([this](const std::string& word) {
            last_word_ = word;
            fired_ = 1;
            ESP_LOGI(TAG, "WakeNet 命中唤醒词：%s", word.c_str());
            if (wake_word_detected_callback_) {
                wake_word_detected_callback_(word);
            }
        });
        wn_framer_.chunk = wakenet_->GetFeedSize();
    } else {
        ESP_LOGW(TAG, "WakeNet 初始化失败 → 仅用 MultiNet 唤醒（不影响离线命令词）");
        wakenet_.reset();
        wakenet_ready_ = false;
        wn_framer_.chunk = 0;
    }

    ESP_LOGI(TAG, "唤醒双引擎就绪：WakeNet=%s(chunk=%u) MultiNet(chunk=%u) 声道=%u",
             wakenet_ready_ ? "on" : "off",
             (unsigned)wn_framer_.chunk, (unsigned)mn_framer_.chunk,
             (unsigned)channels_);
    return true;
}

void DualWakeWord::FeedOne(WakeWord* engine, Framer& framer, const std::vector<int16_t>& data) {
    if (engine == nullptr || framer.chunk == 0) {
        return;
    }
    const size_t need = framer.chunk * channels_;
    framer.buf.insert(framer.buf.end(), data.begin(), data.end());

    while (framer.buf.size() >= need) {
        scratch_.assign(framer.buf.begin(), framer.buf.begin() + need);
        engine->Feed(scratch_);
        framer.buf.erase(framer.buf.begin(), framer.buf.begin() + need);
    }
}

void DualWakeWord::Feed(const std::vector<int16_t>& data) {
    if (wakenet_ready_) {
        FeedOne(wakenet_.get(), wn_framer_, data);
    }
    FeedOne(multinet_.get(), mn_framer_, data);
}

void DualWakeWord::OnWakeWordDetected(std::function<void(const std::string&)> callback) {
    wake_word_detected_callback_ = std::move(callback);
    /* 子引擎的回调已在 Initialize 里装好，它们只把事件转给本类的回调，
     * 所以这里不需要再往子引擎转发。 */
}

void DualWakeWord::Start() {
    if (wakenet_ready_) {
        wakenet_->Start();
    }
    if (multinet_) {
        multinet_->Start();
    }
}

void DualWakeWord::Stop() {
    if (wakenet_ready_) {
        wakenet_->Stop();
    }
    if (multinet_) {
        multinet_->Stop();
    }
    /* 停下时清空攒帧，避免下次启动时把上一轮的残帧当成本轮开头 */
    wn_framer_.buf.clear();
    mn_framer_.buf.clear();
}

size_t DualWakeWord::GetFeedSize() {
    /* AudioService 按这个数读一帧 PCM，所以必须取两者**较大**值：
     * 小的那个靠 Framer 攒帧补足，大的那个正好一帧一喂。 */
    size_t a = wn_framer_.chunk;
    size_t b = mn_framer_.chunk;
    size_t n = a > b ? a : b;
    /* 兜底：两个都 0（都没起来）时返回 0，AudioService 会跳过喂数据 */
    return n;
}

void DualWakeWord::EncodeWakeWordData() {
    int who = fired_.load();
    if (who == 1 && wakenet_ready_) {
        wakenet_->EncodeWakeWordData();
    } else if (multinet_) {
        multinet_->EncodeWakeWordData();
    }
}

bool DualWakeWord::GetWakeWordOpus(std::vector<uint8_t>& opus) {
    int who = fired_.load();
    if (who == 1 && wakenet_ready_) {
        return wakenet_->GetWakeWordOpus(opus);
    }
    if (multinet_) {
        return multinet_->GetWakeWordOpus(opus);
    }
    return false;
}

const std::string& DualWakeWord::GetLastDetectedWakeWord() const {
    return last_word_;
}
