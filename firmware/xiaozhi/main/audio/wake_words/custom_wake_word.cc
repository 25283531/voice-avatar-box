#include "custom_wake_word.h"
#include "audio_service.h"
#include "system_info.h"
#include "assets.h"

#include <esp_log.h>
#include <esp_mn_iface.h>
#include <esp_mn_models.h>
#include <esp_mn_speech_commands.h>
#include <cJSON.h>

// elder 离线命令词：词表本体 + 异步投递通道（C 链接，见 elder_mn.h）。
// 识别层只负责「听」和「转交」，不做分发 —— 分发要写 NVS，见 elder_mn.c 注释。
#include "elder_cmd.h"
#include "elder_mn.h"

#define TAG "CustomWakeWord"

namespace {

/* elder 命令词的 action 标记，用来把「执行离线命令」与「唤醒」区分开。
 * 不是 command_id：id 由注册顺序决定，唤醒词占前 commands_.size() 个，
 * elder 词紧跟其后（见Initialize）。这里只需要一个可比较的标签。 */
constexpr const char* kElderAction = "elder";

}  // namespace


CustomWakeWord::CustomWakeWord()
    : wake_word_pcm_(), wake_word_opus_() {
}

CustomWakeWord::~CustomWakeWord() {
    if (multinet_model_data_ != nullptr && multinet_ != nullptr) {
        multinet_->destroy(multinet_model_data_);
        multinet_model_data_ = nullptr;
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
}

void CustomWakeWord::ParseWakenetModelConfig() {
    // Read index.json
    auto& assets = Assets::GetInstance();
    void* ptr = nullptr;
    size_t size = 0;
    if (!assets.GetAssetData("index.json", ptr, size)) {
        ESP_LOGE(TAG, "Failed to read index.json");
        return;
    }
    cJSON* root = cJSON_ParseWithLength(static_cast<char*>(ptr), size);
    if (root == nullptr) {
        ESP_LOGE(TAG, "Failed to parse index.json");
        return;
    }
    cJSON* multinet_model = cJSON_GetObjectItem(root, "multinet_model");
    if (cJSON_IsObject(multinet_model)) {
        cJSON* language = cJSON_GetObjectItem(multinet_model, "language");
        cJSON* duration = cJSON_GetObjectItem(multinet_model, "duration");
        cJSON* threshold = cJSON_GetObjectItem(multinet_model, "threshold");
        cJSON* commands = cJSON_GetObjectItem(multinet_model, "commands");
        if (cJSON_IsString(language)) {
            language_ = language->valuestring;
        }
        if (cJSON_IsNumber(duration)) {
            duration_ = duration->valueint;
        }
        if (cJSON_IsNumber(threshold)) {
            threshold_ = threshold->valuedouble;
        }
        if (cJSON_IsArray(commands)) {
            for (int i = 0; i < cJSON_GetArraySize(commands); i++) {
                cJSON* command = cJSON_GetArrayItem(commands, i);
                if (cJSON_IsObject(command)) {
                    cJSON* command_name = cJSON_GetObjectItem(command, "command");
                    cJSON* text = cJSON_GetObjectItem(command, "text");
                    cJSON* action = cJSON_GetObjectItem(command, "action");
                    if (cJSON_IsString(command_name) && cJSON_IsString(text) && cJSON_IsString(action)) {
                        commands_.push_back({command_name->valuestring, text->valuestring, action->valuestring});
                        ESP_LOGI(TAG, "Command: %s, Text: %s, Action: %s", command_name->valuestring, text->valuestring, action->valuestring);
                    }
                }
            }
        }
    }
    cJSON_Delete(root);
}


bool CustomWakeWord::Initialize(AudioCodec* codec, srmodel_list_t* models_list) {
    codec_ = codec;
    commands_.clear();

    if (models_list == nullptr) {
        language_ = "cn";
        models_ = esp_srmodel_init("model");
#ifdef CONFIG_CUSTOM_WAKE_WORD
        threshold_ = CONFIG_CUSTOM_WAKE_WORD_THRESHOLD / 100.0f;
        commands_.push_back({CONFIG_CUSTOM_WAKE_WORD, CONFIG_CUSTOM_WAKE_WORD_DISPLAY, "wake"});
#endif
    } else {
        models_ = models_list;
        ParseWakenetModelConfig();
    }

    if (models_ == nullptr || models_->num == -1) {
        ESP_LOGE(TAG, "Failed to initialize wakenet model");
        return false;
    }

    // 初始化 multinet (命令词识别)
    mn_name_ = esp_srmodel_filter(models_, ESP_MN_PREFIX, language_.c_str());
    if (mn_name_ == nullptr) {
        ESP_LOGW(TAG, "Language '%s' multinet not found, falling back to any multinet model", language_.c_str());
        mn_name_ = esp_srmodel_filter(models_, ESP_MN_PREFIX, NULL);
    }
    if (mn_name_ == nullptr) {
        ESP_LOGE(TAG, "Failed to initialize multinet, mn_name is nullptr");
        ESP_LOGI(TAG, "Please refer to https://pcn7cs20v8cr.feishu.cn/wiki/CpQjwQsCJiQSWSkYEvrcxcbVnwh to add custom wake word");
        return false;
    }

    multinet_ = esp_mn_handle_from_name(mn_name_);
    multinet_model_data_ = multinet_->create(mn_name_, duration_);
    multinet_->set_det_threshold(multinet_model_data_, threshold_);

    // 注册命令词。
    //
    // ⚠️ command_id 必须与 registered_ids_ 严格对齐：esp_mn_commands_add()
    // 对不合法的短语 return ESP_ERR_INVALID_STATE 且**根本不进链表**，
    // 但 command_id 是我们按顺序递增传进去的。若不记录实际注册成功的条目，
    // 一旦有条短语被拒，后面所有 id 都会整体前移，
    // Feed() 里 commands_[command_id - 1] 就会取到**别的短语** ——
    // 对医疗设备来说这是「说 A 执行 B」，比不识别严重得多。
    esp_mn_commands_clear();
    registered_ids_.clear();

    int next_id = 1;
    for (int i = 0; i < (int)commands_.size(); i++) {
        if (esp_mn_commands_add(next_id, commands_[i].command.c_str()) == ESP_OK) {
            registered_ids_.push_back({next_id, i});
            next_id++;
        } else {
            ESP_LOGE(TAG, "命令词注册失败（短语无法被模型解析）: \"%s\"",
                     commands_[i].command.c_str());
        }
    }

    // elder 离线命令词：与唤醒词共用同一个 MultiNet 实例。
    // 老人不需要先说唤醒词，直接说「吃过了」就该生效 —— 所以这里
    // 把 elder 词表也塞进同一个识别网络，action 标成 "elder"
    // 以便 Feed() 区分「唤醒」与「执行命令」。
    int elder_ok = 0;
    for (int i = 0; i < elder_cmd_word_count(); i++) {
        const elder_cmd_word_t* w = elder_cmd_word_at(i);
        if (w == nullptr) {
            continue;
        }
        // commands_ 里存：command=拼音（注册用）、text=汉字短语（分发给 elder_cmd 用）
        commands_.push_back({w->pinyin, w->phrase, kElderAction});
        if (esp_mn_commands_add(next_id, w->pinyin) == ESP_OK) {
            registered_ids_.push_back({next_id, (int)commands_.size() - 1});
            next_id++;
            elder_ok++;
        } else {
            // 注册失败就把它从 commands_ 里摘掉：留着会让 id 与下标错位
            ESP_LOGE(TAG, "elder 命令词注册失败（模型无法解析该拼音）: \"%s\" -> \"%s\"",
                     w->phrase, w->pinyin);
            commands_.pop_back();
        }
    }
    ESP_LOGI(TAG, "elder 离线命令词注册 %d/%d 条成功", elder_ok, elder_cmd_word_count());

    esp_mn_commands_update();

    multinet_->print_active_speech_commands(multinet_model_data_);
    return true;
}

void CustomWakeWord::OnWakeWordDetected(std::function<void(const std::string& wake_word)> callback) {
    wake_word_detected_callback_ = callback;
}

void CustomWakeWord::Start() {
    running_ = true;
}

void CustomWakeWord::Stop() {
    running_ = false;
}

void CustomWakeWord::Feed(const std::vector<int16_t>& data) {
    if (multinet_model_data_ == nullptr || !running_) {
        return;
    }

    esp_mn_state_t mn_state;
    // If input channels is 2, we need to fetch the left channel data
    if (codec_->input_channels() == 2) {
        auto mono_data = std::vector<int16_t>(data.size() / 2);
        for (size_t i = 0, j = 0; i < mono_data.size(); ++i, j += 2) {
            mono_data[i] = data[j];
        }

        StoreWakeWordData(mono_data);
        mn_state = multinet_->detect(multinet_model_data_, const_cast<int16_t*>(mono_data.data()));
    } else {
        StoreWakeWordData(data);
        mn_state = multinet_->detect(multinet_model_data_, const_cast<int16_t*>(data.data()));
    }
    
    if (mn_state == ESP_MN_STATE_DETECTING) {
        return;
    } else if (mn_state == ESP_MN_STATE_DETECTED) {
        esp_mn_results_t *mn_result = multinet_->get_results(multinet_model_data_);
        for (int i = 0; i < mn_result->num && running_; i++) {
            int id = mn_result->command_id[i];
            ESP_LOGI(TAG, "Custom wake word detected: command_id=%d, string=%s, prob=%f",
                    id, mn_result->string, mn_result->prob[i]);

            // command_id → commands_ 下标必须查表，不能直接用 id-1：
            // 见 Initialize() 里关于 registered_ids_ 的注释。
            const Command* command = nullptr;
            for (const auto& kv : registered_ids_) {
                if (kv.first == id) {
                    command = &commands_[kv.second];
                    break;
                }
            }
            if (command == nullptr) {
                ESP_LOGW(TAG, "command_id=%d 不在注册表里，忽略（不应发生）", id);
                continue;
            }

            if (command->action == kElderAction) {
                // 离线命令词命中。非阻塞投递：elder_mn_post() 只做
                // memcpy + xQueueSend，真正写 NVS 的活儿在独立任务里。
                // 音频任务里绝不能等 —— 见 elder_mn.c 文件头注释。
                // text 是汉字短语，正好是 elder_cmd_dispatch() 的匹配键。
                if (!elder_mn_post(command->text.c_str())) {
                    ESP_LOGW(TAG, "elder 命令词投递失败（队列满或未初始化），"
                                  "本次识别作废，老人可改用按键: \"%s\"",
                             command->text.c_str());
                }
                continue;
            }

            if (command->action == "wake") {
                last_detected_wake_word_ = command->text;
                running_ = false;

                if (wake_word_detected_callback_) {
                    wake_word_detected_callback_(last_detected_wake_word_);
                }
            }
        }
        multinet_->clean(multinet_model_data_);
    } else if (mn_state == ESP_MN_STATE_TIMEOUT) {
        ESP_LOGD(TAG, "Command word detection timeout, cleaning state");
        multinet_->clean(multinet_model_data_);
    }
}

size_t CustomWakeWord::GetFeedSize() {
    if (multinet_model_data_ == nullptr) {
        return 0;
    }
    return multinet_->get_samp_chunksize(multinet_model_data_);
}

void CustomWakeWord::StoreWakeWordData(const std::vector<int16_t>& data) {
    // store audio data to wake_word_pcm_
    wake_word_pcm_.push_back(data);
    // keep about 2 seconds of data, detect duration is 30ms (sample_rate == 16000, chunksize == 512)
    while (wake_word_pcm_.size() > 2000 / 30) {
        wake_word_pcm_.pop_front();
    }
}

void CustomWakeWord::EncodeWakeWordData() {
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
        auto this_ = (CustomWakeWord*)arg;
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

bool CustomWakeWord::GetWakeWordOpus(std::vector<uint8_t>& opus) {
    std::unique_lock<std::mutex> lock(wake_word_mutex_);
    wake_word_cv_.wait(lock, [this]() {
        return !wake_word_opus_.empty();
    });
    opus.swap(wake_word_opus_.front());
    wake_word_opus_.pop_front();
    return !opus.empty();
}
