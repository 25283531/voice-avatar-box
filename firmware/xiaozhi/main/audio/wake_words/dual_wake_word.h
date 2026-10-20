#ifndef DUAL_WAKE_WORD_H
#define DUAL_WAKE_WORD_H

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "audio_codec.h"
#include "wake_word.h"

class AfeWakeWord;
class CustomWakeWord;

/*
 * 双引擎唤醒：**WakeNet + MultiNet 同时跑**，两个引擎认同一个唤醒词「你好小安」
 *
 * 分工（2026-10-20 用户拍板：两个都保留）：
 *   · WakeNet（wn9_nihaoxiaoan_tts2）——**主通路**。专用唤醒网络，召回高、
 *     误唤醒低，还走 AFE 的 AEC，喇叭出声时也能听见呼唤（可打断播报）。
 *     代价：唤醒词训练在模型里，**改不了字符串**，只能从 esp-sr 自带的
 *     wn9 模型里挑一个（我们挑的是「你好小安」，理由见
 *     tools/merge_to_xiaozhi.py 的 SDKCONFIG_OVERLAY）。
 *   · MultiNet（mn7_cn）——**兜底 + 命令词**。注册「ni hao xiao an」和
 *     2 音节短名「xiao an」（CONFIG_ELDER_WAKE_ALIASES），并承载 9 条
 *     离线服药命令词。通用命令词模型对自定义短语召回天生弱，
 *     所以不能让 MultiNet 单独扛唤醒，但作为第二双耳朵很划算：
 *     它漏掉的，WakeNet 大概率能接住；反之亦然。
 *
 * 两条通路谁命中都算唤醒，屏显/上报用各自引擎返回的短语。
 *
 * 帧长度对齐：两个引擎的 chunk 不一样（AFE 通常 1024 帧、MultiNet 512 帧），
 * 所以本类内部给两个引擎各留一个**独立的攒帧缓冲**，按各自 chunk 切好再喂
 * —— 直接把一路数据同时丢给两边会让 MultiNet 收到 1024 而它只认 512。
 */
class DualWakeWord : public WakeWord {
public:
    DualWakeWord();
    ~DualWakeWord() override;

    bool Initialize(AudioCodec* codec, srmodel_list_t* models_list) override;
    void Feed(const std::vector<int16_t>& data) override;
    void OnWakeWordDetected(std::function<void(const std::string& wake_word)> callback) override;
    void Start() override;
    void Stop() override;
    size_t GetFeedSize() override;
    void EncodeWakeWordData() override;
    bool GetWakeWordOpus(std::vector<uint8_t>& opus) override;
    const std::string& GetLastDetectedWakeWord() const override;

    /* 供 AudioService::IsAfeWakeWord() 判断「能不能在播报时继续听」 */
    bool HasWakenet() const { return wakenet_ready_; }

private:
    /* 一个引擎的攒帧器：把任意长度的入流切成它要的 chunk */
    struct Framer {
        size_t chunk = 0;                 /* 每声道帧数（0 = 该引擎未启用） */
        std::vector<int16_t> buf;         /* 交织样本（帧 * channels） */
    };

    /* 返回本次实际切出并喂给引擎的帧数。
     * ⚠️ 别再用「缓冲变短了」来推断切了几帧：当缓冲恰好切到 0 时
     * size==0 而 before 也可能刚是 0，`size < before` 恒假 ⇒ 探针恒报 0，
     * 把「数据一直在正常喂」误读成「模型一帧没收到」。这个错判过一次
     * （2026-10-20），代价是整轮排查方向跑偏到麦克风/AEC 上。 */
    size_t FeedOne(WakeWord* engine, Framer& framer, const std::vector<int16_t>& data);

    std::unique_ptr<AfeWakeWord> wakenet_;
    std::unique_ptr<CustomWakeWord> multinet_;
    bool wakenet_ready_ = false;

    size_t channels_ = 1;
    Framer wn_framer_;
    Framer mn_framer_;
    std::vector<int16_t> scratch_;        /* 切帧用的临时缓冲，避免每帧 malloc */

    /* 这次唤醒是哪个引擎报的：1=WakeNet 2=MultiNet。
     * EncodeWakeWordData()/GetWakeWordOpus() 必须**委托给同一个引擎**，
     * 否则 GetWakeWordOpus() 会等在另一个引擎的 condition_variable 上永不返回。 */
    std::atomic<int> fired_{0};
    std::function<void(const std::string&)> wake_word_detected_callback_;
    std::string last_word_;

    /* 音频通路探针（诊断「喊不醒」用，见 .cc 里 Feed() 的注释）。
     * 纯观测，不参与任何判定，也不改变喂数据的行为。 */
    int64_t probe_last_ms_ = 0;
    int16_t probe_mic_peak_ = 0;
    int16_t probe_ref_peak_ = 0;
    uint32_t probe_frames_ = 0;
    uint32_t wn_fed_ = 0;
    uint32_t mn_fed_ = 0;
};

#endif  // DUAL_WAKE_WORD_H
