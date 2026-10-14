idf_component_register(
    SRCS
        "src/rgb.c"
        "src/input.c"
        "src/reminder.c"
        "src/activation.c"
    INCLUDE_DIRS "include"
    REQUIRES
        driver
        esp_timer
        nvs_flash
        esp_driver_rmt
        esp_driver_gpio
)
