/*
 * DS1302 三线同步串行时钟后端（2026-10-23 新增）。
 *
 * 为什么单独一个文件：DS3231 走 I2C（rtc.c），DS1302 走三线 bit-bang，
 * 两者没有任何可复用的代码，混在一个文件里只会让条件编译更难读。
 * 由 Kconfig 的 ELDER_RTC_DRIVER choice 二选一，对外接口仍是
 * elder_rtc.h 里的四个函数（调用点在 elder_bridge.cc / sync.c，未改）。
 *
 * ⚠️ 接线红线（固件会硬性拒绝，见 pins_guard()）：
 *    CLK / DAT **绝不能**落在 GPIO1 / GPIO2 —— 那是立创板音频 codec
 *    （ES8311/ES7210）与触摸屏（FT5x06）共用的 I2C 总线（SDA=1/SCL=2）。
 *    DS1302 不是 I2C 器件，bit-bang 期间会与触摸轮询的 I2C 事务持续抢
 *    总线，后果是 codec 寄存器被写坏、触摸失灵 —— 用户听到的就是破音/
 *    卡顿/断续。宁可时钟功能不可用，也不能毁音频。
 *    RST 也不能等于老人服药确认键（CONFIG_ELDER_KEY_GPIO，默认 GPIO11）。
 *
 * 协议要点（DS1302 数据手册）：
 *   · 三线：CE(RST) 使能、SCLK(CLK) 时钟、I/O(DAT) 双向数据；LSB first。
 *   · 命令字节：bit7=1 起始位；bit6=0 时钟寄存器；bit5..1 地址；bit0 读写。
 *   · 时钟寄存器：秒 0x80/81、分 0x82/83、时 0x84/85、日 0x86/87、
 *     月 0x88/89、星期 0x8A/8B、年 0x8C/8D、写保护 0x8E/8F。
 *   · 突发模式：写 0xBE、读 0xBF，一次搬 8 字节（7 时间 + 控制）。
 *     突发比逐字节稳：逐字节时两次事务之间芯片仍在走时，秒可能进位。
 *   · 数据是 BCD；年份 00~99 映射 2000~2099。
 *   · 秒寄存器 bit7 = CH（Clock Halt）：1 表示停振过/从未起振（新模块出厂
 *     就是 CH=1），读数是垃圾 —— 语义等价于 DS3231 的 OSF 位。
 */

#include "elder_rtc.h"

#include "sdkconfig.h"

#if CONFIG_ELDER_RTC_ENABLE && defined(CONFIG_ELDER_RTC_DRIVER_DS1302)

#include <stdbool.h>
#include <stdint.h>
#include <time.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_rom_sys.h"

static const char *TAG = "elder.rtc.ds1302";

#define PIN_CLK ((gpio_num_t)CONFIG_ELDER_RTC_CLK_GPIO)
#define PIN_DAT ((gpio_num_t)CONFIG_ELDER_RTC_DAT_GPIO)
#define PIN_RST ((gpio_num_t)CONFIG_ELDER_RTC_RST_GPIO)

/* 命令字节（bit7 固定为 1 = 起始位；bit6=0 = 时钟寄存器组） */
#define CMD_WP_WRITE     0x8E   /* 写保护寄存器：0x00 解锁 / 0x80 加锁 */
#define CMD_BURST_WRITE  0xBE
#define CMD_BURST_READ   0xBF

#define WP_UNLOCK        0x00
#define WP_LOCK          0x80
#define CH_BIT           0x80   /* 秒寄存器 bit7：Clock Halt */

/*
 * 位间延时 5µs。
 *
 * 手册只要求 SCLK 高/低各 ≥250ns，但读数据时主机要先"放掉" DAT 再采样，
 * 模块内部输出级建立时间比手册最小值宽裕得多，5µs 留足余量、又不至于
 * 让一次突发读（8 字节 = 8*3*8*5µs ≈ 1ms）影响任何实时任务。
 *
 * **不能用 vTaskDelay**：FreeRTOS 的 tick 是 1ms 级，1µs 级的等待它给不了；
 * 也不能用 esp_timer，那要走中断。esp_rom_delay_us 是忙等，正合适。
 */
#define BIT_DELAY_US 5

static struct {
    bool ready;
} g;

/* ---------------- 位操作 ---------------- */

static inline void bit_delay(void)
{
    esp_rom_delay_us(BIT_DELAY_US);
}

/* 主机驱动 DAT（写数据时）。 */
static void dat_drive(void)
{
    gpio_set_level(PIN_DAT, 0);
    gpio_set_direction(PIN_DAT, GPIO_MODE_OUTPUT);
}

/* 主机放掉 DAT（读数据时）。
 *
 * DS1302 的 I/O 是双向的：读的时候主机必须停止驱动，否则会把从机的输出
 * 短路。模块板上通常有 10k 上拉，这里再开内部上拉只是兜底 —— 模块上拉
 * 漏装/虚焊时不至于读到浮空值。 */
static void dat_release(void)
{
    gpio_set_direction(PIN_DAT, GPIO_MODE_INPUT);
    gpio_pullup_en(PIN_DAT);
}

/* CE 拉高开始一次事务。要求 SCLK 先为低（手册的时序前提）。 */
static void ce_high(void)
{
    gpio_set_level(PIN_CLK, 0);
    bit_delay();
    gpio_set_level(PIN_RST, 1);
    bit_delay();
}

/* CE 拉低结束事务，并保证 SCLK 回到低电平。 */
static void ce_low(void)
{
    gpio_set_level(PIN_RST, 0);
    bit_delay();
    gpio_set_level(PIN_CLK, 0);
    bit_delay();
}

/* 写一个字节：LSB first，DS1302 在 SCLK 上升沿采样 DAT。 */
static void write_byte(uint8_t v)
{
    for (int i = 0; i < 8; i++) {
        gpio_set_level(PIN_DAT, (v >> i) & 0x01);
        bit_delay();
        gpio_set_level(PIN_CLK, 1);
        bit_delay();
        gpio_set_level(PIN_CLK, 0);
        bit_delay();
    }
}

/* 读一个字节：LSB first，DS1302 在 SCLK 下降沿输出，主机在下降沿之后采样。 */
static uint8_t read_byte(void)
{
    uint8_t v = 0;
    for (int i = 0; i < 8; i++) {
        gpio_set_level(PIN_CLK, 1);
        bit_delay();
        gpio_set_level(PIN_CLK, 0);   /* 下降沿：芯片把这一位摆上 DAT */
        bit_delay();
        if (gpio_get_level(PIN_DAT)) v |= (uint8_t)(1u << i);
    }
    return v;
}

/* ---------------- 事务封装 ---------------- */

static void reg_write(uint8_t cmd, uint8_t val)
{
    dat_drive();
    ce_high();
    write_byte(cmd);
    write_byte(val);
    ce_low();
}

/* 突发写：手册要求一次写满 8 字节（7 个时间寄存器 + 控制寄存器），
 * 中途松手芯片会认为突发不完整。调用方传 8 字节，最后一字节是控制寄存器。 */
static void burst_write(const uint8_t *b, int n)
{
    dat_drive();
    ce_high();
    write_byte(CMD_BURST_WRITE);
    for (int i = 0; i < n; i++) write_byte(b[i]);
    ce_low();
}

/* 突发读 8 字节：秒/分/时/日/月/星期/年/控制。 */
static void burst_read(uint8_t *b, int n)
{
    dat_drive();
    ce_high();
    write_byte(CMD_BURST_READ);
    dat_release();          /* 命令发完立刻放掉总线，芯片才开始输出 */
    for (int i = 0; i < n; i++) b[i] = read_byte();
    ce_low();
    dat_drive();            /* 事务结束把脚收回输出态，避免长期浮空 */
}

/* ---------------- BCD ---------------- */

static bool bcd_valid(uint8_t v)
{
    return ((v >> 4) <= 9) && ((v & 0x0F) <= 9);
}

static uint8_t bcd2bin(uint8_t v) { return (uint8_t)((v >> 4) * 10 + (v & 0x0F)); }

static uint8_t bin2bcd(uint8_t v) { return (uint8_t)(((v / 10) << 4) | (v % 10)); }

/* ---------------- 硬守卫 ---------------- */

/*
 * 初始化前必须过的两道守卫。判定放在 gpio_config 之前 —— 一个脚都不许驱动，
 * 否则"拒绝"就只是嘴上说说，GPIO1/2 上已经开始翻转电平了。
 */
static esp_err_t pins_guard(void)
{
    const int rst = CONFIG_ELDER_RTC_RST_GPIO;

#if !CONFIG_ELDER_RTC_ALLOW_SHARED_I2C_BUS
    const int clk = CONFIG_ELDER_RTC_CLK_GPIO;
    const int dat = CONFIG_ELDER_RTC_DAT_GPIO;

    /* 守卫一：GPIO1 / GPIO2 是音频 codec 与触摸屏的 I2C 总线。
     *
     * 这是本次任务的核心：用户把 DS1302 的 CLK/DAT 插到了板上的
     * 「I2C 外拓口」（就是 GPIO1/GPIO2）。DS1302 不是 I2C 器件，
     * bit-bang 会与触摸轮询的 I2C 事务持续抢总线 ⇒ codec 寄存器被写坏、
     * 触摸失灵，听感上就是破音/卡顿/断续。用户的硬约束是「声音现在正常，
     * 千万不要再修的破音、卡顿」，所以这里必须硬拒绝。 */
    if (clk == 1 || clk == 2 || dat == 1 || dat == 2 || rst == 1 || rst == 2) {
        ESP_LOGE(TAG, "拒绝启动 DS1302：CLK=%d DAT=%d RST=%d，其中 GPIO1/GPIO2 是"
                      "板载音频 codec(ES8311/ES7210) 与触摸屏(FT5x06) 共用的 I2C 总线"
                      "（SDA=GPIO1, SCL=GPIO2）。DS1302 是三线同步串行、不是 I2C，"
                      "软件 bit-bang 会与触摸轮询的 I2C 事务持续抢总线，导致 codec "
                      "寄存器被写坏、触摸失灵，听感上就是破音/卡顿/断续。"
                      "请改线：CLK→GPIO4、DAT→GPIO5（RST→GPIO6），"
                      "对应 Kconfig 的 ELDER_RTC_CLK_GPIO / ELDER_RTC_DAT_GPIO。"
                      "确实要在该总线上硬跑，请显式打开 ELDER_RTC_ALLOW_SHARED_I2C_BUS。",
                 clk, dat, rst);
        return ESP_ERR_INVALID_STATE;
    }
#endif

    /* 守卫二：CE(RST) 与老人服药确认键抢同一个脚。
     *
     * 用户原来把 RST 接到 GPIO11，而那是 CONFIG_ELDER_KEY_GPIO。一个脚不能
     * 既做按键输入（内部上拉 + 中断）又做 CE 输出：按键按下会把 CE 拉低，
     * 三线时序直接断在半路；CE 拉高又会让按键逻辑误判为"一直没按"。 */
#if defined(CONFIG_ELDER_KEY_GPIO) && (CONFIG_ELDER_KEY_GPIO >= 0)
    if (rst == CONFIG_ELDER_KEY_GPIO) {
        ESP_LOGE(TAG, "拒绝启动 DS1302：CE(RST) 脚 GPIO%d 与老人服药确认键 "
                      "ELDER_KEY_GPIO 相同。同一个脚不能既做按键输入又做 CE 输出 —— "
                      "按键会把 CE 拉低导致三线时序全乱，CE 输出又会让按键误触发。"
                      "请把 RST 改到 GPIO6（Kconfig: ELDER_RTC_RST_GPIO）。", rst);
        return ESP_ERR_INVALID_STATE;
    }
#endif

    return ESP_OK;
}

/* ---------------- 对外接口 ---------------- */

esp_err_t elder_rtc_init(void)
{
    if (g.ready) return ESP_OK;

    esp_err_t err = pins_guard();
    if (err != ESP_OK) return err;   /* 守卫失败：一个脚都没驱动过 */

    /* CLK / RST 只做输出；DAT 初始为输入（读事务开始前的安全态），
     * 真正需要驱动时由 dat_drive() 切换方向。 */
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << PIN_CLK) | (1ULL << PIN_DAT) | (1ULL << PIN_RST),
        .mode         = GPIO_MODE_INPUT_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    gpio_set_level(PIN_CLK, 0);
    gpio_set_level(PIN_RST, 0);
    gpio_set_level(PIN_DAT, 0);
    gpio_set_direction(PIN_DAT, GPIO_MODE_INPUT);
    gpio_pullup_en(PIN_DAT);

    /* 探一次：能读出 8 字节就说明线接对了、芯片在线。
     *
     * 注意这里**不要求 CH=0**。新模块出厂就是 CH=1（振荡器停着），如果因此
     * 判成"芯片不存在"，sync.c 的 `if (elder_rtc_present()) elder_rtc_write()`
     * 永远不会执行，CH 就永远清不掉 —— 芯片装了也等于没装。
     * CH=1 由 elder_rtc_read() 严格拦下（返回 INVALID_STATE → 降级 NVS），
     * 联网校时后一次 write 就把它清掉，这才是可用的路径。 */
    uint8_t b[8] = {0};
    burst_read(b, sizeof(b));

    /* 全 0 / 全 0xFF 是 DAT 悬空（模块没插、线断、没共地）的典型图案：
     * DS1302 的寄存器不可能整片都是这两种值。 */
    bool all_zero = true, all_ff = true;
    for (int i = 0; i < (int)sizeof(b); i++) {
        if (b[i] != 0x00) all_zero = false;
        if (b[i] != 0xFF) all_ff = false;
    }
    if (all_zero || all_ff) {
        ESP_LOGW(TAG, "未检测到 DS1302（CLK=%d DAT=%d RST=%d，读回全 %s），"
                      "降级为 NVS 时间兜底",
                 CONFIG_ELDER_RTC_CLK_GPIO, CONFIG_ELDER_RTC_DAT_GPIO,
                 CONFIG_ELDER_RTC_RST_GPIO, all_zero ? "0" : "1");
        return ESP_ERR_NOT_FOUND;
    }

    g.ready = true;
    ESP_LOGI(TAG, "DS1302 就绪（CLK=%d DAT=%d RST=%d, CH=%d）",
             CONFIG_ELDER_RTC_CLK_GPIO, CONFIG_ELDER_RTC_DAT_GPIO,
             CONFIG_ELDER_RTC_RST_GPIO, (b[0] & CH_BIT) ? 1 : 0);
    if (b[0] & CH_BIT) {
        ESP_LOGW(TAG, "DS1302 的 CH=1（停振过或首次上电），本次开机不采信其时间；"
                      "联网校时成功后会自动写入并清 CH");
    }
    return ESP_OK;
}

bool elder_rtc_present(void)
{
    return g.ready;
}

esp_err_t elder_rtc_read(time_t *out)
{
    if (out == NULL) return ESP_ERR_INVALID_ARG;
    if (!g.ready)    return ESP_ERR_INVALID_STATE;

    uint8_t b[8] = {0};
    burst_read(b, sizeof(b));

    /* CH 必须先判：停振过的时间是"停振那一刻"的垃圾，直接采信会让提醒在
     * 错误时刻响 —— 比不响更危险。语义与 DS3231 的 OSF 完全一致。 */
    if (b[0] & CH_BIT) {
        ESP_LOGW(TAG, "CH=1（时钟停振过），DS1302 读数不可信，不采信");
        return ESP_ERR_INVALID_STATE;
    }

    /* 非法 BCD（例如 0x1A）先拦掉：bcd2bin 对它会算出 26，蒙混过范围检查。 */
    if (!bcd_valid(b[0] & 0x7F) || !bcd_valid(b[1] & 0x7F) ||
        !bcd_valid(b[2] & 0x3F) || !bcd_valid(b[4] & 0x3F) ||
        !bcd_valid(b[5] & 0x1F) || !bcd_valid(b[6])) {
        ESP_LOGW(TAG, "DS1302 寄存器不是合法 BCD，判为无效");
        return ESP_ERR_INVALID_ARG;
    }

    int sec  = bcd2bin(b[0] & 0x7F);
    int min  = bcd2bin(b[1] & 0x7F);
    int hour;
    if (b[2] & 0x80) {
        /* 12 小时制：bit7=1 表示 12h，bit5=PM，bit4..0 = 1~12 */
        hour = bcd2bin(b[2] & 0x1F);
        if (hour < 1 || hour > 12) return ESP_ERR_INVALID_ARG;
        if (hour == 12) hour = 0;
        if (b[2] & 0x20) hour += 12;
    } else {
        hour = bcd2bin(b[2] & 0x3F);
    }
    int mday = bcd2bin(b[4] & 0x3F);
    int mon  = bcd2bin(b[5] & 0x1F);
    int year = 2000 + bcd2bin(b[6]);

    /* 合理性校验：越界一律判无效，**不做任何"修正"**。
     * 修正会把"芯片里是垃圾"伪装成"时间是好的"，提醒照样在错的时间响。 */
    if (year < 2000 || year > 2099 || mon < 1 || mon > 12 ||
        mday < 1 || mday > 31 || hour > 23 || min > 59 || sec > 59) {
        ESP_LOGW(TAG, "DS1302 读数越界（%04d-%02d-%02d %02d:%02d:%02d），判为无效",
                 year, mon, mday, hour, min, sec);
        return ESP_ERR_INVALID_ARG;
    }

    /* 星期寄存器不参与换算：从日期反推更可靠，也不会因为手动改时间后忘了
     * 改星期而错一天（与 DS3231 路径的取舍一致）。 */
    struct tm tmv = {0};
    tmv.tm_year  = year - 1900;
    tmv.tm_mon   = mon - 1;
    tmv.tm_mday  = mday;
    tmv.tm_hour  = hour;
    tmv.tm_min   = min;
    tmv.tm_sec   = sec;
    tmv.tm_isdst = 0;

    time_t t = mktime(&tmv);
    if (t <= 0) return ESP_ERR_INVALID_ARG;

    *out = t;
    return ESP_OK;
}

esp_err_t elder_rtc_write(time_t t)
{
    if (!g.ready) return ESP_ERR_NOT_SUPPORTED;

    /* 与 DS3231 路径一致：RTC 里存的是**本地时间**（板级把 TZ 设为 CST-8），
     * 读侧用 mktime、写侧用 localtime_r 互为逆运算。 */
    struct tm tmv;
    localtime_r(&t, &tmv);

    uint8_t b[8];
    b[0] = (uint8_t)(bin2bcd((uint8_t)tmv.tm_sec) & 0x7F);  /* 清 CH：写时间即视为振荡器可用 */
    b[1] = bin2bcd((uint8_t)tmv.tm_min);
    b[2] = bin2bcd((uint8_t)tmv.tm_hour);                   /* bit7=0 → 24 小时制 */
    b[3] = (uint8_t)(tmv.tm_wday + 1);                      /* 1~7，周日=1 */
    b[4] = bin2bcd((uint8_t)tmv.tm_mday);
    b[5] = bin2bcd((uint8_t)(tmv.tm_mon + 1));
    b[6] = bin2bcd((uint8_t)(tmv.tm_year - 100));           /* 2000 起算 */
    b[7] = 0x00;                                            /* 控制寄存器（突发第 8 字节） */

    /* DS1302 上电默认写保护位 WP=1（0x80），不解锁什么都写不进去。
     * 顺序：解锁 → 突发写 8 字节 → 加锁。加锁放最后，中途掉电最多是
     * "保持解锁"（下次写入仍能成功），不会出现"写了一半又锁死"。 */
    reg_write(CMD_WP_WRITE, WP_UNLOCK);
    burst_write(b, sizeof(b));
    reg_write(CMD_WP_WRITE, WP_LOCK);

    /* 回读校验：三线 bit-bang 没有 ACK，写没写进去只能读回来看。
     * 只比时/日/月/年 —— 分和秒在"写"与"读回"之间可能已经进位，
     * 严格比对会误报失败。多花约 1ms，换来"写失败能报出来"而不是静默。 */
    uint8_t rb[8] = {0};
    burst_read(rb, sizeof(rb));
    if (rb[2] != b[2] || rb[4] != b[4] || rb[5] != b[5] || rb[6] != b[6]) {
        ESP_LOGW(TAG, "DS1302 回读校验失败（时/日/月/年不符），写入可能未生效");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "DS1302 已写入 %04d-%02d-%02d %02d:%02d:%02d（本地时间）",
             tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
             tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
    return ESP_OK;
}

#else  /* 未启用 RTC，或选的是 DS3231 —— 本文件不提供任何函数 */

/* 避免空编译单元（ISO C 要求 translation unit 至少有一个外部声明）。 */
typedef int elder_rtc_ds1302_not_compiled;

#endif /* CONFIG_ELDER_RTC_ENABLE && CONFIG_ELDER_RTC_DRIVER_DS1302 */
