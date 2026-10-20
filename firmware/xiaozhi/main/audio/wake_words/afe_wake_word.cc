#include "afe_wake_word.h"
#include "audio_service.h"

#include <esp_log.h>
#include <esp_timer.h>
#include <chrono>
#include <sstream>

#define DETECTION_RUNNING_EVENT 1

#define TAG "AfeWakeWord"

AfeWakeWord::AfeWakeWord()
    : afe_data_(nullptr),
      wake_word_pcm_(),
      wake_word_opus_() {

    event_group_ = xEventGroupCreate();
}

AfeWakeWord::~AfeWakeWord() {
    if (afe_data_ != nullptr) {
        afe_iface_->destroy(afe_data_);
    }

    if (wake_word_encode_task_stack_ != nullptr) {
        heap_caps_free(wake_word_encode_task_stack_);
    }

    if (wake_word_encode_task_buffer_ != nullptr) {
        heap_caps_free(wake_word_encode_task_buffer_);
    }

    if (models_ != nullptr) {
        esp_srmodel_deinit(models_);
    }

    vEventGroupDelete(event_group_);
}

bool AfeWakeWord::Initialize(AudioCodec* codec, srmodel_list_t* models_list) {
    codec_ = codec;
    int ref_num = codec_->input_reference() ? 1 : 0;

    if (models_list == nullptr) {
        models_ = esp_srmodel_init("model");
    } else {
        models_ = models_list;
    }

    if (models_ == nullptr || models_->num == -1) {
        ESP_LOGE(TAG, "Failed to initialize wakenet model");
        return false;
    }
    for (int i = 0; i < models_->num; i++) {
        ESP_LOGI(TAG, "Model %d: %s", i, models_->model_name[i]);
        if (strstr(models_->model_name[i], ESP_WN_PREFIX) != NULL) {
            wakenet_model_ = models_->model_name[i];
            auto words = esp_srmodel_get_wake_words(models_, wakenet_model_);
            // split by ";" to get all wake words
            std::stringstream ss(words);
            std::string word;
            while (std::getline(ss, word, ';')) {
                wake_words_.push_back(word);
            }
        }
    }

    std::string input_format;
    for (int i = 0; i < codec_->input_channels() - ref_num; i++) {
        input_format.push_back('M');
    }
    for (int i = 0; i < ref_num; i++) {
        input_format.push_back('R');
    }
    afe_config_t* afe_config = afe_config_init(input_format.c_str(), models_, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);
    afe_config->aec_init = codec_->input_reference();
    afe_config->aec_mode = AEC_MODE_SR_HIGH_PERF;
    afe_config->afe_perferred_core = 1;
    /* 保持 1，别改成官方默认的 5。
     * 2026-10-20 试过改成 5，结果唤醒后 56 秒里 AFE feed 环形缓冲溢出 1589 次
     * —— 因为 afe_audio_processor.cc 里的 VC 型 AFE 也用默认优先级 5，
     * 两条 AFE 抢同一个核，WakeNet 这条反而被饿住。
     * 真正导致溢出的洞是 Feed() 在 Stop() 之后还在喂，已在 Feed() 里堵上，
     * 优先级不是那里的解药。 */
    afe_config->afe_perferred_priority = 1;
    afe_config->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    
    afe_iface_ = esp_afe_handle_from_config(afe_config);
    afe_data_ = afe_iface_->create_from_config(afe_config);

    /* 2026-10-20 加的保护：本工程**同时**启用了 MultiNet（双引擎唤醒），
     * AFE 与 MultiNet 抢 PSRAM；一旦 AFE 建不起来（返回空），
     * AudioDetectionTask 里第一句 afe_iface_->get_fetch_chunksize(afe_data_)
     * 就是空指针解引用 ⇒ 开机即崩，而崩溃点在 AFE 内部，日志完全指不到配置。
     * 另外 wake_words_ 为空说明模型包里根本没有 wn* 模型，
     * AudioDetectionTask 用 wakenet_model_index-1 索引它同样会越界。
     * ⇒ 这两种情况都必须在这里**返回 false**，让上层降级到只用 MultiNet。 */
    if (afe_iface_ == nullptr || afe_data_ == nullptr || wake_words_.empty()) {
        ESP_LOGE(TAG, "AFE/WakeNet 起不来（iface=%p data=%p 唤醒词数=%u）→ 降级",
                 (void*)afe_iface_, (void*)afe_data_, (unsigned)wake_words_.size());
        if (afe_iface_ != nullptr && afe_data_ != nullptr) {
            afe_iface_->destroy(afe_data_);
        }
        afe_data_ = nullptr;
        return false;
    }

    xTaskCreate([](void* arg) {
        auto this_ = (AfeWakeWord*)arg;
        this_->AudioDetectionTask();
        vTaskDelete(NULL);
    }, "audio_detection", 4096, this, 3, nullptr);

    return true;
}

void AfeWakeWord::OnWakeWordDetected(std::function<void(const std::string& wake_word)> callback) {
    wake_word_detected_callback_ = callback;
}

void AfeWakeWord::Start() {
    xEventGroupSetBits(event_group_, DETECTION_RUNNING_EVENT);
}

void AfeWakeWord::Stop() {
    xEventGroupClearBits(event_group_, DETECTION_RUNNING_EVENT);
    if (afe_data_ != nullptr) {
        afe_iface_->reset_buffer(afe_data_);
    }
}

void AfeWakeWord::Feed(const std::vector<int16_t>& data) {
    if (afe_data_ == nullptr) {
        return;
    }
    /* 没在检测就别喂。
     *
     * AudioDetectionTask 只在 DETECTION_RUNNING_EVENT 置位时 fetch；
     * 而唤醒命中后 Stop() 会清掉这一位（AEC 模式下唤醒后要让 AudioProcessor
     * 接管音频），此后本类再 feed 就是「只进不出」——AFE 的 feed 环形缓冲
     * 一圈圈被覆盖，日志刷屏：
     *   W AFE: Ringbuffer of AFE(FEED) is full, Please use fetch() to read data
     * 2026-10-20 实测一次唤醒后 56 秒内刷了 **1589 条**这种警告，同时
     * AFE 还在无谓地跑 AEC/WakeNet 推理，白烧 CPU 挤占真正干活的
     * AudioProcessor。这一行就是那个洞。 */
    if ((xEventGroupGetBits(event_group_) & DETECTION_RUNNING_EVENT) == 0) {
        return;
    }
    afe_iface_->feed(afe_data_, data.data());
}

size_t AfeWakeWord::GetFeedSize() {
    if (afe_data_ == nullptr) {
        return 0;
    }
    return afe_iface_->get_feed_chunksize(afe_data_);
}

void AfeWakeWord::AudioDetectionTask() {
    auto fetch_size = afe_iface_->get_fetch_chunksize(afe_data_);
    auto feed_size = afe_iface_->get_feed_chunksize(afe_data_);
    ESP_LOGI(TAG, "Audio detection task started, feed size: %d fetch size: %d",
        feed_size, fetch_size);

    while (true) {
        xEventGroupWaitBits(event_group_, DETECTION_RUNNING_EVENT, pdFALSE, pdTRUE, portMAX_DELAY);

        auto res = afe_iface_->fetch_with_delay(afe_data_, portMAX_DELAY);
        if (res == nullptr || res->ret_value == ESP_FAIL) {
            continue;;
        }

        /* 探针（2026-10-20 诊断「喊不醒」）：证明 AFE 出口**真的在出数据**。
         * fetch 出现即说明 AEC/VAD/WakeNet 整条 pipeline 在跑；
         * 若这里恒 0（且伴随 "Ringbuffer of AFE(FEED) is full"），
         * 那就是喂进去的音频在 AFE 出口被吞掉了，与唤醒词模型无关。 */
        {
            static uint32_t fetch_cnt = 0;
            static int64_t fetch_probe_ms = 0;
            fetch_cnt++;
            int64_t now_ms = esp_timer_get_time() / 1000;
            if (now_ms - fetch_probe_ms >= 2000) {
                ESP_LOGI(TAG, "WakeNet 探针：AFE fetch=%u 次/2s（每次 %d 帧），最近状态=%d",
                         (unsigned)fetch_cnt, (int)fetch_size, (int)res->wakeup_state);
                fetch_cnt = 0;
                fetch_probe_ms = now_ms;
            }
        }

        // Store the wake word data for voice recognition, like who is speaking
        StoreWakeWordData(res->data, res->data_size / sizeof(int16_t));

        if (res->wakeup_state == WAKENET_DETECTED) {
            Stop();
            last_detected_wake_word_ = wake_words_[res->wakenet_model_index - 1];

            if (wake_word_detected_callback_) {
                wake_word_detected_callback_(last_detected_wake_word_);
            }
        }
    }
}

void AfeWakeWord::StoreWakeWordData(const int16_t* data, size_t samples) {
    // store audio data to wake_word_pcm_
    wake_word_pcm_.emplace_back(std::vector<int16_t>(data, data + samples));
    // keep about 2 seconds of data, detect duration is 30ms (sample_rate == 16000, chunksize == 512)
    while (wake_word_pcm_.size() > 2000 / 30) {
        wake_word_pcm_.pop_front();
    }
}

void AfeWakeWord::EncodeWakeWordData() {
    const size_t stack_size = 4096 * 7;
    wake_word_opus_.clear();
    if (wake_word_encode_task_stack_ == nullptr) {
        wake_word_encode_task_stack_ = (StackType_t*)heap_caps_malloc(stack_size, MALLOC_CAP_SPIRAM);
        assert(wake_word_encode_task_stack_ != nullptr);
    }
    if (wake_word_encode_task_buffer_ == nullptr) {
        wake_word_encode_task_buffer_ = (StaticTask_t*)heap_caps_malloc(sizeof(StaticTask_t), MALLOC_CAP_INTERNAL);
        assert(wake_word_encode_task_buffer_ != nullptr);
    }

    wake_word_encode_task_ = xTaskCreateStatic([](void* arg) {
        auto this_ = (AfeWakeWord*)arg;
        {
            auto start_time = esp_timer_get_time();
            auto encoder = std::make_unique<OpusEncoderWrapper>(16000, 1, OPUS_FRAME_DURATION_MS);
            encoder->SetComplexity(0); // 0 is the fastest

            int packets = 0;
            for (auto& pcm: this_->wake_word_pcm_) {
                encoder->Encode(std::move(pcm), [this_](std::vector<uint8_t>&& opus) {
                    std::lock_guard<std::mutex> lock(this_->wake_word_mutex_);
                    this_->wake_word_opus_.emplace_back(std::move(opus));
                    this_->wake_word_cv_.notify_all();
                });
                packets++;
            }
            this_->wake_word_pcm_.clear();

            auto end_time = esp_timer_get_time();
            ESP_LOGI(TAG, "Encode wake word opus %d packets in %ld ms", packets, (long)((end_time - start_time) / 1000));

            std::lock_guard<std::mutex> lock(this_->wake_word_mutex_);
            this_->wake_word_opus_.push_back(std::vector<uint8_t>());
            this_->wake_word_cv_.notify_all();
        }
        vTaskDelete(NULL);
    }, "encode_wake_word", stack_size, this, 2, wake_word_encode_task_stack_, wake_word_encode_task_buffer_);
}

bool AfeWakeWord::GetWakeWordOpus(std::vector<uint8_t>& opus) {
    std::unique_lock<std::mutex> lock(wake_word_mutex_);
    /* 必须带超时。这里是**主任务**在调用（Application::OnWakeWordDetected
     * 里同步取包），无超时的 wait 一旦踩到「队列其实是空的」就永久挂住整个
     * 会话状态机 —— 症状是设备能唤醒、WS 也连上，但永远不说话，
     * 而且之后连唤醒都不再响应（状态机已死）。宁可丢这段唤醒音频，
     * 也不能把主任务赔进去。 */
    bool got = wake_word_cv_.wait_for(lock, std::chrono::milliseconds(1500),
                                      [this]() { return !wake_word_opus_.empty(); });
    if (!got) {
        ESP_LOGW(TAG, "取唤醒音频超时（1.5s 无数据）→ 本次跳过，会话继续");
        return false;
    }
    opus.swap(wake_word_opus_.front());
    wake_word_opus_.pop_front();
    return !opus.empty();
}
