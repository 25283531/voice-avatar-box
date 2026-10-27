#include "settings.h"

#include <esp_log.h>
#include <nvs_flash.h>

#define TAG "Settings"

Settings::Settings(const std::string& ns, bool read_write) : ns_(ns), read_write_(read_write) {
    /* nvs_open 的返回值**必须**检查（2026-10-27 真机事故）。
     *
     * 原实现直接丢弃返回值：只要 nvs_open 失败，nvs_handle_ 就停在 0，
     * 之后 SetInt/SetString 里的 ESP_ERROR_CHECK(nvs_set_i32(0, ...))
     * 必然拿到 ESP_ERR_NVS_INVALID_HANDLE(0x1107) → abort() → 设备重启。
     * 真机现场（用户说「声音大一点」必现）：
     *     MCP: set_volume -> 90
     *     AudioCodec: Set output volume to 90
     *     ESP_ERROR_CHECK failed: esp_err_t 0x1107 (ESP_ERR_NVS_INVALID_HANDLE)
     *     file: "./main/settings.cc" line 63  func: Settings::SetInt
     *     abort() was called at PC 0x40384527 on core 1
     * 对一个"提醒吃药"的设备，为了保存一个音量值而重启，代价完全不成比例：
     * 重启期间既听不见提醒，正在上报的确认回执也可能丢。
     * ⇒ 打不开就当"这次运行不持久化"，读写退化成默认值，绝不 abort。 */
    esp_err_t err = nvs_open(ns_.c_str(),
                             read_write_ ? NVS_READWRITE : NVS_READONLY,
                             &nvs_handle_);
    if (err != ESP_OK) {
        nvs_handle_ = 0;
        ESP_LOGE(TAG, "nvs_open(%s, %s) 失败: %s —— 本次运行该命名空间不持久化"
                      "（读写走默认值，不影响其他功能）",
                 ns_.c_str(), read_write_ ? "RW" : "RO", esp_err_to_name(err));
    }
}

Settings::~Settings() {
    if (nvs_handle_ != 0) {
        if (read_write_ && dirty_) {
            esp_err_t err = nvs_commit(nvs_handle_);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "nvs_commit(%s) 失败: %s（值可能没落盘）",
                         ns_.c_str(), esp_err_to_name(err));
            }
        }
        nvs_close(nvs_handle_);
    }
}

std::string Settings::GetString(const std::string& key, const std::string& default_value) {
    if (nvs_handle_ == 0) {
        return default_value;
    }

    size_t length = 0;
    if (nvs_get_str(nvs_handle_, key.c_str(), nullptr, &length) != ESP_OK) {
        return default_value;
    }

    std::string value;
    value.resize(length);
    ESP_ERROR_CHECK(nvs_get_str(nvs_handle_, key.c_str(), value.data(), &length));
    while (!value.empty() && value.back() == '\0') {
        value.pop_back();
    }
    return value;
}

void Settings::SetString(const std::string& key, const std::string& value) {
    if (read_write_) {
        if (nvs_handle_ == 0) {          /* 见构造函数：打不开就当不持久化 */
            ESP_LOGW(TAG, "%s.%s 未保存：命名空间未打开", ns_.c_str(), key.c_str());
            return;
        }
        esp_err_t err = nvs_set_str(nvs_handle_, key.c_str(), value.c_str());
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "%s.%s 写入失败: %s", ns_.c_str(), key.c_str(),
                     esp_err_to_name(err));
            return;
        }
        dirty_ = true;
    } else {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
    }
}

int32_t Settings::GetInt(const std::string& key, int32_t default_value) {
    if (nvs_handle_ == 0) {
        return default_value;
    }

    int32_t value;
    if (nvs_get_i32(nvs_handle_, key.c_str(), &value) != ESP_OK) {
        return default_value;
    }
    return value;
}

void Settings::SetInt(const std::string& key, int32_t value) {
    if (read_write_) {
        if (nvs_handle_ == 0) {          /* 见构造函数：打不开就当不持久化 */
            ESP_LOGW(TAG, "%s.%s=%d 未保存：命名空间未打开",
                     ns_.c_str(), key.c_str(), (int)value);
            return;
        }
        esp_err_t err = nvs_set_i32(nvs_handle_, key.c_str(), value);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "%s.%s=%d 写入失败: %s", ns_.c_str(), key.c_str(),
                     (int)value, esp_err_to_name(err));
            return;
        }
        dirty_ = true;
    } else {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
    }
}

bool Settings::GetBool(const std::string& key, bool default_value) {
    if (nvs_handle_ == 0) {
        return default_value;
    }

    uint8_t value;
    if (nvs_get_u8(nvs_handle_, key.c_str(), &value) != ESP_OK) {
        return default_value;
    }
    return value != 0;
}

void Settings::SetBool(const std::string& key, bool value) {
    if (read_write_) {
        if (nvs_handle_ == 0) {          /* 见构造函数：打不开就当不持久化 */
            ESP_LOGW(TAG, "%s.%s 未保存：命名空间未打开", ns_.c_str(), key.c_str());
            return;
        }
        esp_err_t err = nvs_set_u8(nvs_handle_, key.c_str(), value ? 1 : 0);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "%s.%s 写入失败: %s", ns_.c_str(), key.c_str(),
                     esp_err_to_name(err));
            return;
        }
        dirty_ = true;
    } else {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
    }
}

void Settings::EraseKey(const std::string& key) {
    if (read_write_) {
        auto ret = nvs_erase_key(nvs_handle_, key.c_str());
        if (ret != ESP_ERR_NVS_NOT_FOUND) {
            ESP_ERROR_CHECK(ret);
        }
    } else {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
    }
}

void Settings::EraseAll() {
    if (read_write_) {
        ESP_ERROR_CHECK(nvs_erase_all(nvs_handle_));
    } else {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
    }
}
