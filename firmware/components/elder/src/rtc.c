#include "elder_rtc.h"

#include "sdkconfig.h"

#include <string.h>

#include "driver/i2c_master.h"
#include "esp_log.h"

static const char *TAG = "elder.rtc";

/* 本文件是 **DS3231（I2C）** 后端。
 *
 * 2026-10-23 起支持 DS1302（三线同步串行，不是 I2C），实现在 rtc_ds1302.c，
 * 由 Kconfig 的 ELDER_RTC_DRIVER choice 二选一。两者没有可复用代码，所以
 * 各自独立成文件，对外接口仍是 elder_rtc.h 的四个函数。
 *
 * 三种编译形态：
 *   · RTC 未启用            → 本文件提供"一律 NOT_SUPPORTED"的桩
 *   · 启用 + DS3231（默认） → 本文件提供 I2C 实现
 *   · 启用 + DS1302         → 本文件不定义任何函数（rtc_ds1302.c 负责） */

#if CONFIG_ELDER_RTC_ENABLE && !defined(CONFIG_ELDER_RTC_DRIVER_DS1302)

#define DS3231_REG_TIME     0x00    /* 秒/分/时/周/日/月/年，共 7 字节 */
#define DS3231_REG_STATUS   0x0F
#define DS3231_OSF_BIT      0x80    /* bit7：振荡器停振标志 */
#define RTC_TIMEOUT_MS      100

static struct {
    i2c_master_bus_handle_t bus;
    i2c_master_dev_handle_t dev;
    bool                    ready;
} g;

static uint8_t bcd2bin(uint8_t v) { return (uint8_t)((v >> 4) * 10 + (v & 0x0F)); }
static uint8_t bin2bcd(uint8_t v) { return (uint8_t)(((v / 10) << 4) | (v % 10)); }

static esp_err_t reg_read(uint8_t reg, uint8_t *buf, size_t len)
{
    return i2c_master_transmit_receive(g.dev, &reg, 1, buf, len, RTC_TIMEOUT_MS);
}

static esp_err_t reg_write(uint8_t reg, const uint8_t *buf, size_t len)
{
    uint8_t tmp[8];
    if (len + 1 > sizeof(tmp)) return ESP_ERR_INVALID_SIZE;
    tmp[0] = reg;
    memcpy(tmp + 1, buf, len);
    return i2c_master_transmit(g.dev, tmp, len + 1, RTC_TIMEOUT_MS);
}

esp_err_t elder_rtc_init(void)
{
    if (g.ready) return ESP_OK;

    /* 总线句柄由板级创建（lichuang_dev_board.cc 的 i2c_new_master_bus），
     * 本组件不重复建总线 —— 同一组引脚上建第二条会直接失败。
     * 端口号优先用配置值，探不到再回退另一个（板级历史上有 0/1 两种写法）。 */
    i2c_port_num_t ports[2] = { (i2c_port_num_t)CONFIG_ELDER_RTC_I2C_PORT, I2C_NUM_0 };
    if (ports[0] == I2C_NUM_0) ports[1] = I2C_NUM_1;

    for (int i = 0; i < 2; i++) {
        i2c_master_bus_handle_t bus = NULL;
        if (i2c_master_get_bus_handle(ports[i], &bus) != ESP_OK || bus == NULL) {
            continue;
        }

        i2c_device_config_t dev_cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address  = CONFIG_ELDER_RTC_I2C_ADDR,
            .scl_speed_hz    = 100 * 1000,
        };
        i2c_master_dev_handle_t dev = NULL;
        if (i2c_master_bus_add_device(bus, &dev_cfg, &dev) != ESP_OK || dev == NULL) {
            continue;
        }

        /* 真探一次：没有模块时 add_device 不会失败，只有 probe 才知道在不在。 */
        if (i2c_master_probe(bus, CONFIG_ELDER_RTC_I2C_ADDR, 50) != ESP_OK) {
            i2c_master_bus_rm_device(dev);
            continue;
        }

        g.bus = bus;
        g.dev = dev;
        g.ready = true;
        ESP_LOGI(TAG, "DS3231 就绪（I2C%d, addr 0x%02X）", (int)ports[i],
                 (unsigned)CONFIG_ELDER_RTC_I2C_ADDR);
        return ESP_OK;
    }

    /* 打 WARN 不打 ERROR：原型期本来就没接时钟，这是预期状态而非故障。 */
    ESP_LOGW(TAG, "未检测到 DS3231，降级为 NVS 时间兜底");
    return ESP_ERR_NOT_FOUND;
}

bool elder_rtc_present(void)
{
    return g.ready;
}

esp_err_t elder_rtc_read(time_t *out)
{
    if (out == NULL) return ESP_ERR_INVALID_ARG;
    if (!g.ready)    return ESP_ERR_INVALID_STATE;

    /* OSF 必须先查：电池没电/首次上电时，RTC 里存的是"停振那一刻"的垃圾，
     * 直接采信会让提醒在错误时刻响 —— 比不响更危险。 */
    uint8_t status = 0;
    esp_err_t err = reg_read(DS3231_REG_STATUS, &status, 1);
    if (err != ESP_OK) return err;
    if (status & DS3231_OSF_BIT) {
        ESP_LOGW(TAG, "OSF=1（振荡器停振过），RTC 时间不可信，不采信");
        return ESP_ERR_INVALID_STATE;
    }

    uint8_t b[7] = {0};
    err = reg_read(DS3231_REG_TIME, b, sizeof(b));
    if (err != ESP_OK) return err;

    struct tm tmv = {0};
    tmv.tm_sec  = bcd2bin(b[0] & 0x7F);
    tmv.tm_min  = bcd2bin(b[1] & 0x7F);

    /* 小时寄存器 bit6=12/24h 模式，bit5=AM/PM（仅 12h 模式有效） */
    uint8_t h = b[2];
    if (h & 0x40) {
        int hour = bcd2bin(h & 0x1F);
        if (h & 0x20) hour = (hour % 12) + 12;   /* PM */
        tmv.tm_hour = hour;
    } else {
        tmv.tm_hour = bcd2bin(h & 0x3F);
    }

    tmv.tm_mday = bcd2bin(b[4] & 0x3F);
    tmv.tm_mon  = bcd2bin(b[5] & 0x1F) - 1;
    tmv.tm_year = 2000 + bcd2bin(b[6]) - 1900;
    tmv.tm_isdst = 0;

    /* 星期寄存器（b[3]）不参与换算：从日期反推更可靠，也不会因为
     * 手动改时间后忘了改星期而错一天。 */
    time_t t = mktime(&tmv);
    if (t <= 0) return ESP_ERR_INVALID_ARG;

    *out = t;
    return ESP_OK;
}

esp_err_t elder_rtc_write(time_t t)
{
    if (!g.ready) return ESP_ERR_NOT_SUPPORTED;

    struct tm tmv;
    localtime_r(&t, &tmv);

    uint8_t b[7];
    b[0] = bin2bcd((uint8_t)tmv.tm_sec);
    b[1] = bin2bcd((uint8_t)tmv.tm_min);
    b[2] = bin2bcd((uint8_t)tmv.tm_hour);          /* bit6=0 → 24h 模式 */
    b[3] = (uint8_t)(tmv.tm_wday + 1);             /* DS3231 的 1~7，从周日算 */
    b[4] = bin2bcd((uint8_t)tmv.tm_mday);
    b[5] = bin2bcd((uint8_t)(tmv.tm_mon + 1));
    b[6] = bin2bcd((uint8_t)(tmv.tm_year - 100));  /* 2000 起算 */

    esp_err_t err = reg_write(DS3231_REG_TIME, b, sizeof(b));
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "写 RTC 失败: %s", esp_err_to_name(err));
        return err;
    }

    /* 写成功即说明时间可信 → 清 OSF，否则下次开机仍会被判为不可信。 */
    uint8_t status = 0;
    if (reg_read(DS3231_REG_STATUS, &status, 1) == ESP_OK && (status & DS3231_OSF_BIT)) {
        status &= (uint8_t)~DS3231_OSF_BIT;
        reg_write(DS3231_REG_STATUS, &status, 1);
    }
    return ESP_OK;
}

#elif !CONFIG_ELDER_RTC_ENABLE  /* 原型期未接时钟芯片：一律返回 NOT_SUPPORTED */

esp_err_t elder_rtc_init(void)
{
    ESP_LOGI(TAG, "未启用外部时钟（CONFIG_ELDER_RTC_ENABLE=n）");
    return ESP_ERR_NOT_SUPPORTED;
}

bool elder_rtc_present(void)
{
    return false;
}

esp_err_t elder_rtc_read(time_t *out)
{
    (void)out;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t elder_rtc_write(time_t t)
{
    (void)t;
    return ESP_ERR_NOT_SUPPORTED;
}

#else  /* 启用 + DS1302：实现全在 rtc_ds1302.c，本文件不定义任何函数 */

/* 避免空编译单元（ISO C 要求 translation unit 至少有一个外部声明）。 */
typedef int elder_rtc_ds3231_not_compiled;

#endif /* CONFIG_ELDER_RTC_ENABLE && !CONFIG_ELDER_RTC_DRIVER_DS1302 */
