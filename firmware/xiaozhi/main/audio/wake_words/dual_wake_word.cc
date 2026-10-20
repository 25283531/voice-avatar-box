#include "dual_wake_word.h"

#include <esp_log.h>
#include <esp_timer.h>

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

size_t DualWakeWord::FeedOne(WakeWord* engine, Framer& framer, const std::vector<int16_t>& data) {
    if (engine == nullptr || framer.chunk == 0) {
        return 0;
    }
    const size_t need = framer.chunk * channels_;
    framer.buf.insert(framer.buf.end(), data.begin(), data.end());

    size_t fed = 0;
    while (framer.buf.size() >= need) {
        scratch_.assign(framer.buf.begin(), framer.buf.begin() + need);
        engine->Feed(scratch_);
        framer.buf.erase(framer.buf.begin(), framer.buf.begin() + need);
        ++fed;
    }
    return fed;
}

void DualWakeWord::Feed(const std::vector<int16_t>& data) {
    /* ── 音频通路探针（2026-10-20）────────────────────────────────
     * 症状是「喊不醒、但初始化全绿」，这种题只能在数据通路上取证：
     * 初始化日志能证明模型加载了、pipeline 建好了，却证明不了
     * **麦克风里到底有没有声音**、以及 **AEC 有没有把语音当回声消掉**。
     *
     * 每 2 秒打一行：mic 通道与 ref（回采）通道的峰值、各自喂了多少帧。
     * 判据：
     *   · mic 峰值恒 0        → I2S/麦克风没数据（硬件或 codec 配置）
     *   · mic 正常 ref 也正常 → AEC 配置可能把语音消掉了（ref 应是"播放出去的"，
     *                           静止时也该有噪声底，安静时两路都接近 0 才正常）
     *   · 两路都正常          → 问题在模型侧（阈值/命令表/引擎竞争）
     * 只在 debug 级别以下不输出会把日志撑爆，所以固定 2s 一次、单行。 */
    if (!data.empty()) {
        size_t n = data.size();
        int16_t mic_peak = 0, ref_peak = 0;
        for (size_t i = 0; i < n; ++i) {
            int16_t v = data[i] < 0 ? (int16_t)(-data[i]) : data[i];
            if (channels_ >= 2) {
                if ((i & 1) == 0) { if (v > mic_peak) mic_peak = v; }
                else              { if (v > ref_peak) ref_peak = v; }
            } else if (v > mic_peak) {
                mic_peak = v;
            }
        }
        if (mic_peak > probe_mic_peak_) probe_mic_peak_ = mic_peak;
        if (ref_peak > probe_ref_peak_) probe_ref_peak_ = ref_peak;
        probe_frames_++;

        int64_t now_ms = esp_timer_get_time() / 1000;
        if (now_ms - probe_last_ms_ >= 2000) {
            ESP_LOGI(TAG, "音频探针：mic峰值=%d ref峰值=%d 本周期帧=%u "
                          "(wn帧=%u mn帧=%u 唤醒检测=%s/%s)",
                     (int)probe_mic_peak_, (int)probe_ref_peak_,
                     (unsigned)probe_frames_,
                     (unsigned)wn_fed_, (unsigned)mn_fed_,
                     wakenet_ready_ ? "on" : "off",
                     mn_framer_.chunk ? "on" : "off");
            probe_mic_peak_ = 0;
            probe_ref_peak_ = 0;
            probe_frames_ = 0;
            wn_fed_ = 0;
            mn_fed_ = 0;
            probe_last_ms_ = now_ms;
        }
    }

    if (wakenet_ready_) {
        wn_fed_ += (uint32_t)FeedOne(wakenet_.get(), wn_framer_, data);
    }
    mn_fed_ += (uint32_t)FeedOne(multinet_.get(), mn_framer_, data);
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
    /* 先把「这次编码属于哪个引擎」锁死，后面取包只认这个快照。
     * 详见头文件里 encode_who_ 的注释：直接读 fired_ 会踩到
     * 「编码进 A、却去 B 取」⇒ 永久阻塞。 */
    int who = fired_.load();
    encode_who_ = who;
    if (who == 1 && wakenet_ready_) {
        wakenet_->EncodeWakeWordData();
    } else if (multinet_) {
        multinet_->EncodeWakeWordData();
    }
}

bool DualWakeWord::GetWakeWordOpus(std::vector<uint8_t>& opus) {
    int who = encode_who_.load();
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
