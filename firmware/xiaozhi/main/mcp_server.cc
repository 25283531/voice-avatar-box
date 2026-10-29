/*
 * MCP Server Implementation
 * Reference: https://modelcontextprotocol.io/specification/2024-11-05
 */

#include "mcp_server.h"
#include <esp_log.h>
#include <esp_app_desc.h>
#include <algorithm>
#include <cstring>
#include <esp_pthread.h>

#include "application.h"
#include "display.h"
#include "board.h"
#include "settings.h"
#include "lvgl_theme.h"
#include "lvgl_display.h"

#define TAG "MCP"

// >>> BEGIN elder-companion overlay（由本 fork 维护，勿随上游合并覆盖）>>>
// 老人服药确认：由 elder 组件导出（定义在 components/elder/src/elder_bridge.cc）。
// 项目里 elder 没有对外头文件，沿用 main.cc 的裸 extern "C" 声明模式。
extern "C" int elder_confirm_pending_medicine(void);
/* 需求 7：进入待机（关收音、重开唤醒词）。服务端在念完告别语后调它。 */
extern "C" void elder_enter_standby(void);
/* 2026-10-29：叫设备立刻同步一次（不等下一个心跳周期）。
 * 提醒表在服务端变了之后由服务端调 —— 见下面 self.elder.sync_now 的注释。 */
extern "C" esp_err_t elder_sync_request_now(void);
// <<< END elder-companion overlay <<<

McpServer::McpServer() {
}

McpServer::~McpServer() {
    for (auto tool : tools_) {
        delete tool;
    }
    tools_.clear();
}

void McpServer::AddCommonTools() {
    // *Important* To speed up the response time, we add the common tools to the beginning of
    // the tools list to utilize the prompt cache.
    // **重要** 为了提升响应速度，我们把常用的工具放在前面，利用 prompt cache 的特性。

    // Backup the original tools list and restore it after adding the common tools.
    auto original_tools = std::move(tools_);
    auto& board = Board::GetInstance();

    // Do not add custom tools here.
    // Custom tools must be added in the board's InitializeTools function.

    AddTool("self.get_device_status",
        "Provides the real-time information of the device, including the current status of the audio speaker, screen, battery, network, etc.\n"
        "Use this tool for: \n"
        "1. Answering questions about current condition (e.g. what is the current volume of the audio speaker?)\n"
        "2. As the first step to control the device (e.g. turn up / down the volume of the audio speaker, etc.)",
        PropertyList(),
        [&board](const PropertyList& properties) -> ReturnValue {
            return board.GetDeviceStatusJson();
        });

    AddTool("self.audio_speaker.set_volume", 
        "Set the volume of the audio speaker. If the current volume is unknown, you must call `self.get_device_status` tool first and then call this tool.",
        PropertyList({
            Property("volume", kPropertyTypeInteger, 0, 100)
        }), 
        [&board](const PropertyList& properties) -> ReturnValue {
            auto codec = board.GetAudioCodec();
            if (!codec) {
                /* 没有 codec 的板子：如实报错，别让 LLM 以为调成了 */
                throw std::runtime_error("no audio codec on this board");
            }
            /* 夹紧范围：模型给的数不总是 0~100（见过 150、"50%"）。
             * Property 的 set_value 会挡掉越界，但这里是二次兜底 ——
             * 真调到头/尾也只是音量到顶，不该变成一次异常。 */
            int volume = properties["volume"].value<int>();
            if (volume < 0) volume = 0;
            if (volume > 100) volume = 100;
            /* 这条日志是"语音调音量到底有没有走到固件"的唯一证据。
             * 待机/收听时输出是关着的，音量会在下次 EnableOutput(true)
             * 时由 BoxAudioCodec 补写（见那里的注释）。 */
            ESP_LOGI(TAG, "set_volume -> %d（输出%s）", volume,
                     codec->output_enabled() ? "已打开，立即生效" : "未打开，下次播放生效");
            codec->SetOutputVolume(volume);
            return true;
        });
    
    auto backlight = board.GetBacklight();
    if (backlight) {
        AddTool("self.screen.set_brightness",
            "Set the brightness of the screen.",
            PropertyList({
                Property("brightness", kPropertyTypeInteger, 0, 100)
            }),
            [backlight](const PropertyList& properties) -> ReturnValue {
                int brightness = properties["brightness"].value<int>();
                if (brightness < 0) brightness = 0;
                if (brightness > 100) brightness = 100;
                /* 同样留一行证据：语音调亮度到底有没有走到固件。
                 * SetBrightness 内部用 LEDC 渐变（每 5ms 一步），
                 * permanent=true 会落 NVS，重启后仍然记得。 */
                ESP_LOGI(TAG, "set_brightness -> %d", brightness);
                backlight->SetBrightness(static_cast<uint8_t>(brightness), true);
                return true;
            });
    }

#ifdef HAVE_LVGL
    auto display = board.GetDisplay();
    if (display && display->GetTheme() != nullptr) {
        AddTool("self.screen.set_theme",
            "Set the theme of the screen. The theme can be `light` or `dark`.",
            PropertyList({
                Property("theme", kPropertyTypeString)
            }),
            [display](const PropertyList& properties) -> ReturnValue {
                auto theme_name = properties["theme"].value<std::string>();
                auto& theme_manager = LvglThemeManager::GetInstance();
                auto theme = theme_manager.GetTheme(theme_name);
                if (theme != nullptr) {
                    display->SetTheme(theme);
                    return true;
                }
                return false;
            });
    }
#endif

    // >>> BEGIN elder-companion overlay >>>
    // 老人服药确认。放在**最后**注册：前面那些工具的 JSON 顺序保持不变，
    // 服务端 prompt 的前缀缓存不会被这次新增打掉。
    //
    // 为什么用 MCP 工具而不是本地命令词：离线命令词已随上游回归删除
    // （2026-10-20 决策），而按键迁到 GPIO11 后原型期没接线 —— 不加这条，
    // 老人听到提醒后**没有任何确认手段**，15 分钟后固件会升级成 ESCALATED
    // 并回执 MISSED，子女端每条提醒都收到「漏服」假告警。
    //
    // 返回 false 不是错误：老人可能只是顺口说「吃过了」，此时没有待确认
    // 提醒，工具如实返回 false，让 LLM 用一句话圆过去即可。
    AddTool("self.elder.confirm_reminder",
        "Confirm that the elderly user has just completed the current reminder "
        "(for example, has taken their medicine).\n"
        "Call this tool when the user says things like \"我吃过了\", \"吃完了\", "
        "\"已经吃了\", \"药吃了\" in response to a medicine reminder.\n"
        "Return:\n"
        "  true if a pending reminder was confirmed; false if there is nothing "
        "pending (in that case just reply naturally, do not report an error).",
        PropertyList(),
        [](const PropertyList& properties) -> ReturnValue {
            (void)properties;
            return elder_confirm_pending_medicine() == 0;
        });

    /* 需求 7：待机。
     *
     * 服务端在「退下吧 / 待机吧 / 晚安吧 / 我睡了」这类**明确要求结束对话**
     * 的话之后，念完告别语、再调这个工具把设备切到 idle：
     * 停掉麦克风推流、把 WakeNet 重新打开。
     *
     * 为什么必须有一块固件侧的配合（而不是服务端单方面停响应）：realtime
     * （连续对话）模式下，设备播报完会回 kDeviceStateListening，而那一态
     * **WakeNet 是关的**。服务端只"不再应答"的话，老人再喊唤醒词设备根本
     * 听不见 —— 一次实测聋了 164 秒（见 elder_bridge.cc 的 elder_enter_standby）。
     *
     * 也注册成工具（而不只是服务端内部命令）的好处：老人说「让我安静会儿」
     * 这种不在正则表里的说法，LLM 也能自己判断并调用它。
     * 描述刻意写窄，避免模型在正常对话里随手调。 */
    AddTool("self.elder.standby",
        "Put the device into standby: stop listening through the microphone and "
        "wait for the wake word again.\n"
        "Call this only when the user explicitly wants to END the conversation "
        "(for example \"待机吧\", \"退下吧\", \"晚安吧\", \"我睡了\", \"别说话了\").\n"
        "Do NOT call it to answer a question or when the user is still talking.",
        PropertyList(),
        [](const PropertyList& properties) -> ReturnValue {
            (void)properties;
            elder_enter_standby();
            return true;
        });

    /* 2026-10-29：叫设备**立刻**同步一次。
     *
     * 为什么需要（真机事故）：提醒由设备本地响（断网也能响，刻意设计），
     * 但服务端新加的提醒要等设备**下次心跳**才拿到 —— 心跳周期由服务端
     * 下发的 sync_interval_sec 决定，默认 300 秒。老人 12:43:41 说
     * 「十二点四十五提醒我下楼」，12:45 到点只有 79 秒 ⇒ 即使参数全对，
     * 设备手里也还没有这条提醒，必然漏。
     *
     * 固件早有现成钩子 elder_sync_request_now()（把同步任务从"等到超时"
     * 改成"立刻醒一次"），但全仓没有任何外部调用者。服务端在提醒表变更
     * 成功之后调这个工具即可，**精度到秒级**，且不像"把心跳调成 60 秒"
     * 那样付出 5 倍的 TLS 握手代价（那个会拖累音频）。
     *
     * 描述刻意写窄：这是服务端内部用的，不该让模型在对话里随手调。
     * 返回 true 只表示"叫醒信号已发出"，不代表同步已经成功 —— 同步结果
     * 照旧走周期心跳与回执，别在这里等。 */
    AddTool("self.elder.sync_now",
        "Ask the device to run its reminder/config sync immediately instead of "
        "waiting for the next heartbeat.\n"
        "The server calls this right after the reminder list changes.\n"
        "Do NOT call it on your own during a conversation.",
        PropertyList(),
        [](const PropertyList& properties) -> ReturnValue {
            (void)properties;
            (void)elder_sync_request_now();
            return true;
        });
    // <<< END elder-companion overlay <<<

    // Restore the original tools list to the end of the tools list
    tools_.insert(tools_.end(), original_tools.begin(), original_tools.end());
}

void McpServer::AddUserOnlyTools() {
    // System tools
    AddUserOnlyTool("self.get_system_info",
        "Get the system information",
        PropertyList(),
        [this](const PropertyList& properties) -> ReturnValue {
            auto& board = Board::GetInstance();
            return board.GetSystemInfoJson();
        });

    AddUserOnlyTool("self.reboot", "Reboot the system",
        PropertyList(),
        [this](const PropertyList& properties) -> ReturnValue {
            auto& app = Application::GetInstance();
            app.Schedule([&app]() {
                ESP_LOGW(TAG, "User requested reboot");
                vTaskDelay(pdMS_TO_TICKS(1000));

                app.Reboot();
            });
            return true;
        });

    // Firmware upgrade
    AddUserOnlyTool("self.upgrade_firmware", "Upgrade firmware from a specific URL. This will download and install the firmware, then reboot the device.",
        PropertyList({
            Property("url", kPropertyTypeString, "The URL of the firmware binary file to download and install")
        }),
        [this](const PropertyList& properties) -> ReturnValue {
            auto url = properties["url"].value<std::string>();
            ESP_LOGI(TAG, "User requested firmware upgrade from URL: %s", url.c_str());
            
            auto& app = Application::GetInstance();
            app.Schedule([url, &app]() {
                auto ota = std::make_unique<Ota>();
                
                bool success = app.UpgradeFirmware(*ota, url);
                if (!success) {
                    ESP_LOGE(TAG, "Firmware upgrade failed");
                }
            });
            
            return true;
        });

    // Display control
#ifdef HAVE_LVGL
    auto display = dynamic_cast<LvglDisplay*>(Board::GetInstance().GetDisplay());
    if (display) {
        AddUserOnlyTool("self.screen.get_info", "Information about the screen, including width, height, etc.",
            PropertyList(),
            [display](const PropertyList& properties) -> ReturnValue {
                cJSON *json = cJSON_CreateObject();
                cJSON_AddNumberToObject(json, "width", display->width());
                cJSON_AddNumberToObject(json, "height", display->height());
                /* 2026-10-22 oled_display.cc 不再编译（OLED 屏本板不用），
                 * dynamic_cast<OledDisplay*> 需要 typeinfo，链接期会缺符号。
                 * 本板固定彩色屏，monochrome 恒 false（与 board.cc 的
                 * GetSystemInfoJson 同口径）。 */
                cJSON_AddBoolToObject(json, "monochrome", false);
                return json;
            });

#if CONFIG_LV_USE_SNAPSHOT
        /* 2026-10-22 self.screen.snapshot 已移除：SnapshotToJpeg 的 JPEG 编码链路
         * （image_to_jpeg + esp_new_jpeg + esp_image_effects + esp_video）随摄像头一起删了。
         * 产品不需要截图。保留 preview_image（下载图片上屏，与 JPEG 编码无关）。 */

        AddUserOnlyTool("self.screen.preview_image", "Preview an image on the screen",
            PropertyList({
                Property("url", kPropertyTypeString)
            }),
            [display](const PropertyList& properties) -> ReturnValue {
                auto url = properties["url"].value<std::string>();
                auto http = Board::GetInstance().GetNetwork()->CreateHttp(3);

                if (!http->Open("GET", url)) {
                    throw std::runtime_error("Failed to open URL: " + url);
                }
                int status_code = http->GetStatusCode();
                if (status_code != 200) {
                    throw std::runtime_error("Unexpected status code: " + std::to_string(status_code));
                }

                size_t content_length = http->GetBodyLength();
                char* data = (char*)heap_caps_malloc(content_length, MALLOC_CAP_8BIT);
                if (data == nullptr) {
                    throw std::runtime_error("Failed to allocate memory for image: " + url);
                }
                size_t total_read = 0;
                while (total_read < content_length) {
                    int ret = http->Read(data + total_read, content_length - total_read);
                    if (ret < 0) {
                        heap_caps_free(data);
                        throw std::runtime_error("Failed to download image: " + url);
                    }
                    if (ret == 0) {
                        break;
                    }
                    total_read += ret;
                }
                http->Close();

                auto image = std::make_unique<LvglAllocatedImage>(data, content_length);
                display->SetPreviewImage(std::move(image));
                return true;
            });
#endif // CONFIG_LV_USE_SNAPSHOT
    }
#endif // HAVE_LVGL

    // Assets download url
    auto& assets = Assets::GetInstance();
    if (assets.partition_valid()) {
        AddUserOnlyTool("self.assets.set_download_url", "Set the download url for the assets",
            PropertyList({
                Property("url", kPropertyTypeString)
            }),
            [](const PropertyList& properties) -> ReturnValue {
                auto url = properties["url"].value<std::string>();
                Settings settings("assets", true);
                settings.SetString("download_url", url);
                return true;
            });
    }
}

void McpServer::AddTool(McpTool* tool) {
    // Prevent adding duplicate tools
    if (std::find_if(tools_.begin(), tools_.end(), [tool](const McpTool* t) { return t->name() == tool->name(); }) != tools_.end()) {
        ESP_LOGW(TAG, "Tool %s already added", tool->name().c_str());
        return;
    }

    ESP_LOGI(TAG, "Add tool: %s%s", tool->name().c_str(), tool->user_only() ? " [user]" : "");
    tools_.push_back(tool);
}

void McpServer::AddTool(const std::string& name, const std::string& description, const PropertyList& properties, std::function<ReturnValue(const PropertyList&)> callback) {
    AddTool(new McpTool(name, description, properties, callback));
}

void McpServer::AddUserOnlyTool(const std::string& name, const std::string& description, const PropertyList& properties, std::function<ReturnValue(const PropertyList&)> callback) {
    auto tool = new McpTool(name, description, properties, callback);
    tool->set_user_only(true);
    AddTool(tool);
}

void McpServer::ParseMessage(const std::string& message) {
    cJSON* json = cJSON_Parse(message.c_str());
    if (json == nullptr) {
        ESP_LOGE(TAG, "Failed to parse MCP message: %s", message.c_str());
        return;
    }
    ParseMessage(json);
    cJSON_Delete(json);
}

void McpServer::ParseCapabilities(const cJSON* capabilities) {
    /* vision 能力已随摄像头删除（2026-10-22）—— 这里留空壳保持接口不变。 */
}

void McpServer::ParseMessage(const cJSON* json) {
    // Check JSONRPC version
    auto version = cJSON_GetObjectItem(json, "jsonrpc");
    if (version == nullptr || !cJSON_IsString(version) || strcmp(version->valuestring, "2.0") != 0) {
        ESP_LOGE(TAG, "Invalid JSONRPC version: %s", version ? version->valuestring : "null");
        return;
    }
    
    // Check method
    auto method = cJSON_GetObjectItem(json, "method");
    if (method == nullptr || !cJSON_IsString(method)) {
        ESP_LOGE(TAG, "Missing method");
        return;
    }
    
    auto method_str = std::string(method->valuestring);
    if (method_str.find("notifications") == 0) {
        return;
    }
    
    // Check params
    auto params = cJSON_GetObjectItem(json, "params");
    if (params != nullptr && !cJSON_IsObject(params)) {
        ESP_LOGE(TAG, "Invalid params for method: %s", method_str.c_str());
        return;
    }

    auto id = cJSON_GetObjectItem(json, "id");
    if (id == nullptr || !cJSON_IsNumber(id)) {
        ESP_LOGE(TAG, "Invalid id for method: %s", method_str.c_str());
        return;
    }
    auto id_int = id->valueint;
    
    if (method_str == "initialize") {
        if (cJSON_IsObject(params)) {
            auto capabilities = cJSON_GetObjectItem(params, "capabilities");
            if (cJSON_IsObject(capabilities)) {
                ParseCapabilities(capabilities);
            }
        }
        auto app_desc = esp_app_get_description();
        std::string message = "{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"" BOARD_NAME "\",\"version\":\"";
        message += app_desc->version;
        message += "\"}}";
        ReplyResult(id_int, message);
    } else if (method_str == "tools/list") {
        std::string cursor_str = "";
        bool list_user_only_tools = false;
        if (params != nullptr) {
            auto cursor = cJSON_GetObjectItem(params, "cursor");
            if (cJSON_IsString(cursor)) {
                cursor_str = std::string(cursor->valuestring);
            }
            auto with_user_tools = cJSON_GetObjectItem(params, "withUserTools");
            if (cJSON_IsBool(with_user_tools)) {
                list_user_only_tools = with_user_tools->valueint == 1;
            }
        }
        GetToolsList(id_int, cursor_str, list_user_only_tools);
    } else if (method_str == "tools/call") {
        if (!cJSON_IsObject(params)) {
            ESP_LOGE(TAG, "tools/call: Missing params");
            ReplyError(id_int, "Missing params");
            return;
        }
        auto tool_name = cJSON_GetObjectItem(params, "name");
        if (!cJSON_IsString(tool_name)) {
            ESP_LOGE(TAG, "tools/call: Missing name");
            ReplyError(id_int, "Missing name");
            return;
        }
        auto tool_arguments = cJSON_GetObjectItem(params, "arguments");
        if (tool_arguments != nullptr && !cJSON_IsObject(tool_arguments)) {
            ESP_LOGE(TAG, "tools/call: Invalid arguments");
            ReplyError(id_int, "Invalid arguments");
            return;
        }
        DoToolCall(id_int, std::string(tool_name->valuestring), tool_arguments);
    } else {
        ESP_LOGE(TAG, "Method not implemented: %s", method_str.c_str());
        ReplyError(id_int, "Method not implemented: " + method_str);
    }
}

void McpServer::ReplyResult(int id, const std::string& result) {
    std::string payload = "{\"jsonrpc\":\"2.0\",\"id\":";
    payload += std::to_string(id) + ",\"result\":";
    payload += result;
    payload += "}";
    Application::GetInstance().SendMcpMessage(payload);
}

void McpServer::ReplyError(int id, const std::string& message) {
    std::string payload = "{\"jsonrpc\":\"2.0\",\"id\":";
    payload += std::to_string(id);
    payload += ",\"error\":{\"message\":\"";
    payload += message;
    payload += "\"}}";
    Application::GetInstance().SendMcpMessage(payload);
}

void McpServer::GetToolsList(int id, const std::string& cursor, bool list_user_only_tools) {
    const int max_payload_size = 8000;
    std::string json = "{\"tools\":[";
    
    bool found_cursor = cursor.empty();
    auto it = tools_.begin();
    std::string next_cursor = "";
    
    while (it != tools_.end()) {
        // 如果我们还没有找到起始位置，继续搜索
        if (!found_cursor) {
            if ((*it)->name() == cursor) {
                found_cursor = true;
            } else {
                ++it;
                continue;
            }
        }

        if (!list_user_only_tools && (*it)->user_only()) {
            ++it;
            continue;
        }
        
        // 添加tool前检查大小
        std::string tool_json = (*it)->to_json() + ",";
        if (json.length() + tool_json.length() + 30 > max_payload_size) {
            // 如果添加这个tool会超出大小限制，设置next_cursor并退出循环
            next_cursor = (*it)->name();
            break;
        }
        
        json += tool_json;
        ++it;
    }
    
    if (json.back() == ',') {
        json.pop_back();
    }
    
    if (json.back() == '[' && !tools_.empty()) {
        // 如果没有添加任何tool，返回错误
        ESP_LOGE(TAG, "tools/list: Failed to add tool %s because of payload size limit", next_cursor.c_str());
        ReplyError(id, "Failed to add tool " + next_cursor + " because of payload size limit");
        return;
    }

    if (next_cursor.empty()) {
        json += "]}";
    } else {
        json += "],\"nextCursor\":\"" + next_cursor + "\"}";
    }
    
    ReplyResult(id, json);
}

void McpServer::DoToolCall(int id, const std::string& tool_name, const cJSON* tool_arguments) {
    auto tool_iter = std::find_if(tools_.begin(), tools_.end(), 
                                 [&tool_name](const McpTool* tool) { 
                                     return tool->name() == tool_name; 
                                 });
    
    if (tool_iter == tools_.end()) {
        ESP_LOGE(TAG, "tools/call: Unknown tool: %s", tool_name.c_str());
        ReplyError(id, "Unknown tool: " + tool_name);
        return;
    }

    PropertyList arguments = (*tool_iter)->properties();
    try {
        for (auto& argument : arguments) {
            bool found = false;
            if (cJSON_IsObject(tool_arguments)) {
                auto value = cJSON_GetObjectItem(tool_arguments, argument.name().c_str());
                if (argument.type() == kPropertyTypeBoolean && cJSON_IsBool(value)) {
                    argument.set_value<bool>(value->valueint == 1);
                    found = true;
                } else if (argument.type() == kPropertyTypeInteger && cJSON_IsNumber(value)) {
                    argument.set_value<int>(value->valueint);
                    found = true;
                } else if (argument.type() == kPropertyTypeString && cJSON_IsString(value)) {
                    argument.set_value<std::string>(value->valuestring);
                    found = true;
                }
            }

            if (!argument.has_default_value() && !found) {
                ESP_LOGE(TAG, "tools/call: Missing valid argument: %s", argument.name().c_str());
                ReplyError(id, "Missing valid argument: " + argument.name());
                return;
            }
        }
    } catch (const std::exception& e) {
        ESP_LOGE(TAG, "tools/call: %s", e.what());
        ReplyError(id, e.what());
        return;
    }

    // Use main thread to call the tool
    auto& app = Application::GetInstance();
    app.Schedule([this, id, tool_iter, arguments = std::move(arguments)]() {
        try {
            ReplyResult(id, (*tool_iter)->Call(arguments));
        } catch (const std::exception& e) {
            ESP_LOGE(TAG, "tools/call: %s", e.what());
            ReplyError(id, e.what());
        }
    });
}
