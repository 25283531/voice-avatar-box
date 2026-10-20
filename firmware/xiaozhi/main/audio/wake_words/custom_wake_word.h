#ifndef CUSTOM_WAKE_WORD_H
#define CUSTOM_WAKE_WORD_H

#include <esp_attr.h>
#include <esp_mn_iface.h>
#include <esp_mn_models.h>
#include <model_path.h>

#include <deque>
#include <string>
#include <vector>
#include <functional>
#include <mutex>
#include <condition_variable>
#include <atomic>

#include "audio_codec.h"
#include "wake_word.h"

class CustomWakeWord : public WakeWord {
public:
    CustomWakeWord();
    ~CustomWakeWord();

    bool Initialize(AudioCodec* codec, srmodel_list_t* models_list);
    void Feed(const std::vector<int16_t>& data);
    void OnWakeWordDetected(std::function<void(const std::string& wake_word)> callback);
    void Start();
    void Stop();
    size_t GetFeedSize();
    void EncodeWakeWordData();
    bool GetWakeWordOpus(std::vector<uint8_t>& opus);
    const std::string& GetLastDetectedWakeWord() const { return last_detected_wake_word_; }

    /* 放弃对 models_list 的所有权（析构时不再 esp_srmodel_deinit）。
     * 只有 DualWakeWord 会用到：同一份 srmodel 列表被两个引擎共享，
     * 两边都 deinit 就是双重释放。见 dual_wake_word.cc 析构函数。 */
    void ForgetModels() { models_ = nullptr; }

private:
    struct Command {
        std::string command;
        std::string text;
        std::string action;
    };

    // multinet 相关成员变量
    esp_mn_iface_t* multinet_ = nullptr;
    model_iface_data_t* multinet_model_data_ = nullptr;
    srmodel_list_t *models_ = nullptr;
    char* mn_name_ = nullptr;
    std::string language_ = "cn";
    int duration_ = 3000;
    float threshold_ = 0.2;
    std::deque<Command> commands_;

    // command_id → commands_ 下标。
    // 不用 id-1 直取的原因：esp_mn_commands_add() 遇到模型解析不了的短语会
    // return ESP_ERR_INVALID_STATE 且不把短语挂进链表，但 command_id 是我们
    // 自己递增传进去的。若有短语被拒，id 与 commands_ 下标就会错位，
    // Feed() 里按 id-1 取命令会拿到**另一条短语** —— 对服药提醒来说
    // 这是「说 A 执行 B」，比识别失败严重得多。
    std::vector<std::pair<int, int>> registered_ids_;
 
    std::function<void(const std::string& wake_word)> wake_word_detected_callback_;
    AudioCodec* codec_ = nullptr;
    std::string last_detected_wake_word_;
    std::atomic<bool> running_ = false;

    TaskHandle_t wake_word_encode_task_ = nullptr;
    StaticTask_t* wake_word_encode_task_buffer_ = nullptr;
    StackType_t* wake_word_encode_task_stack_ = nullptr;
    std::deque<std::vector<int16_t>> wake_word_pcm_;
    std::deque<std::vector<uint8_t>> wake_word_opus_;
    std::mutex wake_word_mutex_;
    std::condition_variable wake_word_cv_;

    void StoreWakeWordData(const std::vector<int16_t>& data);
    void ParseWakenetModelConfig();
};

#endif
