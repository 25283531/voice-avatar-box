idf_component_register(
    SRCS
        "src/rgb.c"
        "src/input.c"
        "src/reminder.c"
        "src/activation.c"
        "src/ui.c"
        "src/net.c"
        "src/sync.c"
        "src/rtc.c"
        "src/config_report.c"
    INCLUDE_DIRS "include"
    REQUIRES
        driver
        esp_timer
        nvs_flash
        esp_hw_support
        esp_driver_rmt
        esp_driver_gpio
        # i2c_master_get_bus_handle / i2c_master_transmit（新 I2C 主机驱动，rtc.c）
        esp_driver_i2c
        esp_http_client
        # esp_crt_bundle.h（证书包）在 mbedtls 组件里，必须显式声明：
        # 漏了它 net.c 直接 fatal error: esp_crt_bundle.h: No such file。
        mbedtls
        json
)

# 说明：src/elder_bridge.cc **不在这里编译**。
# 它是 C++ 的 xiaozhi 适配层，必须编译进 main 组件才能访问
# Application / Display / AudioService。由 tools/merge_to_xiaozhi.py
# 追加到 xiaozhi/main/CMakeLists.txt 的 target_sources() 中。
