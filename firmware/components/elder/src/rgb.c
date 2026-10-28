#include "elder_rgb.h"
#include "elder_board.h"

#include "driver/rmt_tx.h"
#include "driver/rmt_encoder.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include <string.h>

static const char *TAG = "elder.rgb";

/* WS2812 时序（单位 ns @ RMT 10MHz 分辨率） */
#define WS2812_T0H_NS   350
#define WS2812_T0L_NS   1000
#define WS2812_T1H_NS   1000
#define WS2812_T1L_NS   350
#define WS2812_RESET_NS 50000

typedef struct {
    bool            used;
    int             prio;
    elder_rgb_fx_t  fx;
    uint32_t        color;
    uint8_t         level;       /* 0~255 统一亮度，见 elder_rgb_request_ex */
    uint32_t        expire_ms;   /* 0 = 常驻 */
} slot_t;

#define ELDER_RGB_SLOTS 8

/* ---- 需求 8：提醒的闪灯节奏（见 elder_rgb.h 的接口注释） ----
 *
 * 只用 xTaskGetTickCount 的毫秒数，不另起定时器。三个常量都是"用户原话
 * 里的数字"，改它们就等于改产品行为，别顺手调：
 *   每 10 分钟一次 → NAG_PERIOD_MS
 *   每次亮 10 秒   → NAG_BLINK_MS
 *   2 小时后不再亮 → NAG_TOTAL_MS
 */
#define ELDER_NAG_PERIOD_MS (10u * 60u * 1000u)
#define ELDER_NAG_BLINK_MS  (10u * 1000u)
#define ELDER_NAG_TOTAL_MS  (2u * 60u * 60u * 1000u)

/* ---- 对话四态的取值（2026-10-27 定色，2026-10-28 定亮度，改数字 = 改产品观感）
 *
 * ⚠️ 这里的 level 是**感知亮度**（"看起来几成亮"），不是写入灯珠的线性值。
 *    中间隔着一张 gamma 2.2 查表（GAMMA_LUT / level_to_linear）。
 *
 *    为什么必须有这张表：加表之前 scale_color() 是纯线性乘法
 *    （`通道 × level / 255`），而人眼亮度感知近似 gamma 2.2 ——
 *    写 30% 出来人眼看着约 58%，写 55% 看着约 76%。用户 2026-10-28 问
 *    「WS2812B 是不是控制不了亮度」，根因就是这个错位，不是驱动没生效。
 *    加表之后 30% 真的是三成亮、70% 真的是七成亮。
 *
 *    副作用要记住：感知 30% ⇒ 线性只有 7%（18/255），感知 20% ⇒ 线性 2.7%
 *    （7/255）。WS2812B 的低端本来就不可靠（值 1~11 在表里直接是 0，
 *    就是为了避开恒流源最小导通时间造成的闪烁/偏色），所以**低于 20% 的
 *    感知亮度基本等于"看不太清"**，别指望再往下调。 */
#define ELDER_UI_LEVEL_IDLE_BOOST 153       /* 60%：刚进待机的那 1 分钟 */
#define ELDER_UI_LEVEL_IDLE       77        /* 30%：待机超过 1 分钟 */
/* 收听态：**不是**纯蓝 #0000FF 100%（2026-10-27 用户实报"过于刺眼"）。
 *
 * 为什么纯蓝最刺眼：蓝光波长短、在暗环境里散射强，老人晶状体黄化后
 * 对短波更敏感；而 #0000FF 只有蓝通道满驱，另外两个通道为 0，
 * 观感是"发紫的高亮针尖"，眩光强但显色极差。
 * 换成深天蓝 #00BFFF（青味蓝）：加进来的绿通道把峰值能量摊开，
 * 视觉上仍是"蓝色 = 正在听"，且和待机的幻彩靠"单色常亮"区分开。
 * 要再暗一档就调 ELDER_UI_LEVEL_LISTENING，别改色相 —— 色相是语义。 */
#define ELDER_UI_COLOR_LISTENING 0x00BFFFu  /* 深天蓝 */
#define ELDER_UI_LEVEL_LISTENING 140        /* 55% */
#define ELDER_UI_COLOR_SPEAKING  0xFF8C00u  /* 深橙 */
#define ELDER_UI_LEVEL_SPEAKING  178        /* 70%（原 100%，2026-10-28 用户下调） */
#define ELDER_UI_LEVEL_CHASE     51         /* 20%：网络传输中（原 80%） */
#define ELDER_UI_FULL_LEVEL      255        /* 100% */

/* 待机"高亮 1 分钟"的窗口长度：进待机先亮 60%，1 分钟后自动降到 30%。
 * 为什么要有这个窗口：老人刚放下话、抬头看设备时，灯还亮着能确认"它听见了"；
 * 1 分钟后进入真正的待机，太亮会干扰睡眠（夜里尤其）。 */
#define ELDER_IDLE_BOOST_MS       (60u * 1000u)

/* 待机"五彩幻彩"：整条同步缓慢流转，一圈约 20 秒。
 * 为什么这么慢：待机是**长期**状态，颜色变化太快在余光里会一直"动"，
 * 夜里尤其让人睡不踏实。55ms 走 1° ≈ 每秒 18°，肉眼看是"颜色缓缓流动"。 */
#define ELDER_RAINBOW_MS_PER_DEG  55u

/* 网络传输中"从 #2 到 #9 轮流亮起"：同一时刻只有一颗亮，每 150ms 前进一格
 * （8 颗一轮 1.2 秒）。慢到能看清"有一颗在跑"，又不至于像卡住。 */
#define ELDER_CHASE_STEP_MS       150u

/* 流水灯速度（ELDER_FX_FLOW 专用；当前四态不再使用它，
 * 保留是因为"整条每颗不同色相同时流动"和"轮流亮起"是两种观感，
 * 将来想换回来改一行就行）。60°/秒，逐颗偏移 45°。
 * "低速"是用户明确要求的 —— 快速闪烁对老人有光敏性癫痫风险。 */
#define ELDER_FLOW_DEG_PER_TICK   3u        /* 每 50ms 走 3° ⇒ 60°/秒 */
#define ELDER_FLOW_TICK_MS        50u
#define ELDER_FLOW_LED_STEP_DEG   45u       /* 8 颗灯铺满一圈色相 */

/* ---- 第 1 颗灯：按键指示灯（取值理由见 elder_rgb.h） ---- */
#define ELDER_RGB_BTN_ATTRACT_COLOR 0xFF8C00u  /* 深橙：有待确认提醒 */
#define ELDER_RGB_BTN_GRAD_FROM     0x90EE90u  /* 浅绿：刚按下 */
#define ELDER_RGB_BTN_GRAD_TO       0x006400u  /* 深绿：按住够 3 秒 */
#define ELDER_RGB_BTN_OK_COLOR      0x00CED1u  /* 深蓝绿：确认完成 */
#define ELDER_RGB_BTN_FAIL_COLOR    0xFF1493u  /* 深粉红：中途松开没确认 */
#define ELDER_RGB_BTN_BREATH_MS     3000u      /* 吸引注意的呼吸周期 */
/* 亮 250ms / 灭 250ms：慢到老人看得清次数，一次结果共 1.25s（3 次）/
 * 2.5s（5 次）。再快就数不清，再慢就以为灯坏了。 */
#define ELDER_RGB_BTN_FLASH_ON_MS   250u
#define ELDER_RGB_BTN_FLASH_OFF_MS  250u
#define ELDER_RGB_BTN_OK_TIMES      3
#define ELDER_RGB_BTN_FAIL_TIMES    5
/* 单次"动画步进"的时间上限（ms）。
 *
 * 闪烁用的是**渲染时钟**而不是墙上时钟，这是 2026-10-27 真机事故的直接产物：
 *   老人长按 3 秒确认 → elder_bridge 立刻把回执入队 → elder_sync 开始
 *   HTTPS 上传，TLS 握手实测占用 1.6 秒；rgb_task 优先级只有 5，在这 1.6 秒里
 *   **一帧都没跑**。而闪烁窗口按墙上时钟算只有 1.2 秒 —— 等 CPU 回来，
 *   窗口早就过期了，老人只看到"闪了一下深蓝绿"（用户原话）。
 *   ⇒ 把每步最多算 50ms：被饿死时动画就地**暂停**，CPU 回来接着放完，
 *     3 次就是 3 次，不会因为系统忙而少闪。 */
#define ELDER_RGB_ANIM_MAX_STEP_MS  50u

/* #1 的四个状态（互斥） */
typedef enum {
    BTN_OFF = 0,     /* 常态：不亮 */
    BTN_ATTRACT,     /* 有待确认提醒：橙色呼吸 */
    BTN_GRADIENT,    /* 按住中：浅绿 → 深绿 */
    BTN_FLASH        /* 结果闪烁（成功 3 次 / 失败 5 次） */
} btn_mode_t;

static struct {
    rmt_channel_handle_t  chan;
    rmt_encoder_handle_t  enc;
    /* 缓冲区按**灯带真实颗数**开（20 颗 = 60 字节），不是受控颗数。
     * #10~#20 每帧显式写 0，否则它们的锁存器会停在开机随机态常亮，
     * 见 elder_board.h 的 ELDER_RGB_CHAIN。 */
    uint8_t               pixels[ELDER_RGB_CHAIN * 3];
    uint8_t               last[ELDER_RGB_CHAIN * 3];  /* 上一帧，用于"没变就不发" */
    bool                  last_valid;
    SemaphoreHandle_t     lock;
    TaskHandle_t          task;
    slot_t                slots[ELDER_RGB_SLOTS];
    bool                  ready;

    /* 提醒闪灯：active 为真时由 rgb_task 自己算"这一帧该不该亮" */
    bool                  nag_active;
    uint32_t              nag_start_ms;

    /* 待机"高亮 1 分钟"窗口（见 ELDER_IDLE_BOOST_MS）。
     * idle_dim 为真表示当前 #2~#9 正在放**待机幻彩**，rgb_task 每帧按
     * idle_since_ms 算"该 60% 还是 30%"。放在这里而不是写死进槽位的 level，
     * 是因为降亮必须**自己发生** —— 让上层 60 秒后再调一次 set_ui_state
     * 就得依赖上层定时器，而待机期间没有任何上层任务在跑（唤醒词才起会话）。 */
    bool                  idle_dim;
    uint32_t              idle_since_ms;

    /* #1 按键指示灯状态机 */
    btn_mode_t            btn_mode;
    bool                  btn_pending;    /* 存在待确认提醒（吸引注意态的真源） */
    bool                  btn_resolved;   /* 本轮按下已经出过结果，别再重复反馈 */
    uint32_t              btn_phase_ms;   /* 呼吸/渐变的相位起点（墙上时钟） */
    uint32_t              btn_flash_start_ms;  /* 闪烁起点（**渲染时钟**） */
    uint32_t              btn_flash_color;
    uint8_t               btn_flash_times;
    uint32_t              btn_anim_ms;    /* 渲染时钟：只在真的渲染时前进 */
    uint32_t              btn_last_step_ms; /* 上次步进时的墙上时钟，用于算 dt */
} g;

static uint32_t now_ms(void)
{
    return (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
}

/* ---------------- 颜色工具 ---------------- */

static uint32_t hsv2rgb(uint16_t h, uint8_t s, uint8_t v)
{
    uint8_t region = h / 43;
    uint8_t rem    = (h % 43) * 6;
    uint8_t p = (v * (255 - s)) >> 8;
    uint8_t q = (v * (255 - ((s * rem) >> 8))) >> 8;
    uint8_t t = (v * (255 - ((s * (255 - rem)) >> 8))) >> 8;
    switch (region) {
    case 0: return ((uint32_t)v << 16) | ((uint32_t)t << 8) | p;
    case 1: return ((uint32_t)q << 16) | ((uint32_t)v << 8) | p;
    case 2: return ((uint32_t)p << 16) | ((uint32_t)v << 8) | t;
    case 3: return ((uint32_t)p << 16) | ((uint32_t)q << 8) | v;
    case 4: return ((uint32_t)t << 16) | ((uint32_t)p << 8) | v;
    default:return ((uint32_t)v << 16) | ((uint32_t)p << 8) | q;
    }
}

/* ---- gamma 2.2 查表：感知亮度 level(0..255) → 线性系数(0..255) ----
 *
 * 生成方式（Python，改表就重跑这一行）：
 *     [round(255 * (i / 255.0) ** 2.2) for i in range(256)]
 *
 * 关键点：**表作用在"亮度系数"上，不作用在每个颜色通道上**。
 *   若对通道值单独做 gamma，三个通道在暗部的非线性不一致，颜色会偏
 *   （暗绿变墨绿、暗蓝发紫）。这里先算出"整体该暗多少"，再等比乘到
 *   RGB 三个通道上 —— 色相不变，只有亮度变。
 *
 * 表里 0~14 全是 0/1：这是**故意的**。WS2812B 用内置恒流源，
 *   值 1~8 时占空比太窄，实际表现是闪烁 + 三色比例失衡；
 *   与其"似亮非亮地抖"，不如干脆黑掉。所以感知亮度低于约 8% 就看不见了，
 *   这是硬件下限，不是 bug。
 *
 * 体积：256 字节常量，放 flash，不进 RAM。查表比 powf() 快得多，
 *   而 rgb_task 每 30ms 要算 8 颗灯 × 3 通道。 */
static const uint8_t GAMMA_LUT[256] = {
      0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   1,
      1,   1,   1,   1,   1,   1,   1,   1,   1,   2,   2,   2,   2,   2,   2,   2,
      3,   3,   3,   3,   3,   4,   4,   4,   4,   5,   5,   5,   5,   6,   6,   6,
      6,   7,   7,   7,   8,   8,   8,   9,   9,   9,  10,  10,  11,  11,  11,  12,
     12,  13,  13,  13,  14,  14,  15,  15,  16,  16,  17,  17,  18,  18,  19,  19,
     20,  20,  21,  22,  22,  23,  23,  24,  25,  25,  26,  26,  27,  28,  28,  29,
     30,  30,  31,  32,  33,  33,  34,  35,  35,  36,  37,  38,  39,  39,  40,  41,
     42,  43,  43,  44,  45,  46,  47,  48,  49,  49,  50,  51,  52,  53,  54,  55,
     56,  57,  58,  59,  60,  61,  62,  63,  64,  65,  66,  67,  68,  69,  70,  71,
     73,  74,  75,  76,  77,  78,  79,  81,  82,  83,  84,  85,  87,  88,  89,  90,
     91,  93,  94,  95,  97,  98,  99, 100, 102, 103, 105, 106, 107, 109, 110, 111,
    113, 114, 116, 117, 119, 120, 121, 123, 124, 126, 127, 129, 130, 132, 133, 135,
    137, 138, 140, 141, 143, 145, 146, 148, 149, 151, 153, 154, 156, 158, 159, 161,
    163, 165, 166, 168, 170, 172, 173, 175, 177, 179, 181, 182, 184, 186, 188, 190,
    192, 194, 196, 197, 199, 201, 203, 205, 207, 209, 211, 213, 215, 217, 219, 221,
    223, 225, 227, 229, 231, 234, 236, 238, 240, 242, 244, 246, 248, 251, 253, 255,
};

/* 感知亮度 → 线性系数 */
static uint32_t level_to_linear(uint32_t lvl)
{
    return (lvl >= 255u) ? 255u : (uint32_t)GAMMA_LUT[lvl];
}

/* 线性衰减：k 是**线性**系数（0..255），不再过 gamma。
 * 灯效自身的调制（呼吸相位、轮流亮起的通断）都走这里 ——
 * 它们本来就是线性波形，再过一次 gamma 会让暗部占比过大、
 * 呼吸看起来像"眨眼"而不是"呼吸"。 */
static uint32_t dim_color(uint32_t c, uint32_t k)
{
    if (k >= 255u) return c;
    uint32_t r  = ((c >> 16) & 0xFFu) * k / 255u;
    uint32_t g2 = ((c >> 8) & 0xFFu) * k / 255u;
    uint32_t b  = (c & 0xFFu) * k / 255u;
    return (r << 16) | (g2 << 8) | b;
}

/* 对外唯一入口：按**感知亮度**衰减一个颜色（见 GAMMA_LUT 的注释） */
static uint32_t scale_color(uint32_t c, uint32_t lvl)
{
    return dim_color(c, level_to_linear(lvl));
}

/* 线性插值：num/den 为进度。den==0 时直接给终点色，避免除零。 */
static uint32_t lerp_color(uint32_t a, uint32_t b, uint32_t num, uint32_t den)
{
    if (den == 0u || num >= den) return b;
    int32_t n = (int32_t)num;
    int32_t d = (int32_t)den;
    int32_t ar = (int32_t)((a >> 16) & 0xFFu), ag = (int32_t)((a >> 8) & 0xFFu), ab = (int32_t)(a & 0xFFu);
    int32_t br = (int32_t)((b >> 16) & 0xFFu), bg = (int32_t)((b >> 8) & 0xFFu), bb = (int32_t)(b & 0xFFu);
    int32_t r  = ar + (br - ar) * n / d;
    int32_t g2 = ag + (bg - ag) * n / d;
    int32_t b2 = ab + (bb - ab) * n / d;
    return ((uint32_t)r << 16) | ((uint32_t)g2 << 8) | (uint32_t)b2;
}

/* ---------------- #2~#9 灯效 ---------------- */

/* 取当前应当生效的最高优先级请求；best_prio 回带该请求的优先级（-1 = 没有） */
static bool active_slot(slot_t *out, uint32_t t_ms, int *best_prio)
{
    slot_t *best = NULL;
    for (int i = 0; i < ELDER_RGB_SLOTS; i++) {
        slot_t *s = &g.slots[i];
        if (!s->used) continue;
        if (s->expire_ms && (int32_t)(s->expire_ms - t_ms) <= 0) {
            s->used = false;              /* TTL 自然到期 */
            continue;
        }
        if (!best || s->prio > best->prio) best = s;
    }
    if (best_prio) *best_prio = best ? best->prio : -1;
    if (!best) return false;
    *out = *best;
    return true;
}

/* 单颗灯的最终颜色。led_idx 是灯带索引（1..ELDER_RGB_LEDS-1），
 * 灯效的相位/位置偏移按 led_idx-1 算 —— #1 不参与灯效，不能让它占掉一格色相。
 *
 * 亮度分两层，别混：
 *   ① 基准亮度 s->level 是**感知亮度**（60% / 30% / 20%…）→ 过 gamma 表；
 *   ② 灯效自身的调制 mod（呼吸相位、轮流亮起的通断）是**线性波形** → 不过 gamma。
 * 合成方式：dim_color(颜色, gamma(level) × mod / 255)。
 * 历史上这里是 `scale_color(color, lvl)` 之后又 `scale_color(c, s->level)`
 * 两层都乘 —— 加了 gamma 之后会变成"gamma 套 gamma"，暗部直接消失。 */
static uint32_t fx_pixel(const slot_t *s, uint32_t t_ms, int led_idx)
{
    uint32_t c = s->color;
    uint32_t mod = 255u;      /* 灯效自身的线性调制，255 = 不做调制 */

    switch (s->fx) {
    case ELDER_FX_BREATH: {
        /* 2.4s 一个呼吸周期（三角波） */
        uint32_t phase = t_ms % 2400u;
        mod = (phase < 1200u) ? (phase * 255u / 1200u)
                              : ((2400u - phase) * 255u / 1200u);
        break;
    }
    case ELDER_FX_BLINK_FAST:
        mod = ((t_ms % 500u) < 250u) ? 255u : 0u;
        break;
    case ELDER_FX_CHASE: {
        /* **从 #2 到 #9 轮流亮起**（2026-10-28 用户指定，用于"网络传输处理中"）：
         * 同一时刻只有一颗亮，位置每 ELDER_CHASE_STEP_MS 前进一格，8 颗循环。
         * 色相按灯位固定分配（#2 红 → #9 紫，每颗 45°），于是"轮流亮起"的
         * 同时颜色也在依次变化，形成五彩循环。
         *
         * 和 ELDER_FX_FLOW 的区别：FLOW 是"8 颗都亮、彩虹沿灯带流动"，
         * CHASE 是"只有一颗亮、亮点在跑"。用户要的是后者（"轮流亮起"）。 */
        const uint32_t span = (uint32_t)(ELDER_RGB_LEDS - 1);   /* 8 格 */
        uint32_t idx  = (uint32_t)(led_idx - 1);                /* 0..7 */
        uint32_t step = (t_ms / ELDER_CHASE_STEP_MS) % span;
        if (idx != step) return 0u;      /* 没轮到它：灭 */
        c = hsv2rgb((uint16_t)(idx * ELDER_FLOW_LED_STEP_DEG), 255, 255);
        break;
    }
    case ELDER_FX_FLOW: {
        uint32_t idx = (led_idx > 0) ? (uint32_t)(led_idx - 1) : 0u;
        uint32_t hue = ((t_ms / ELDER_FLOW_TICK_MS) * ELDER_FLOW_DEG_PER_TICK
                        + idx * ELDER_FLOW_LED_STEP_DEG) % 360u;
        c = hsv2rgb((uint16_t)hue, 255, 255);
        break;
    }
    case ELDER_FX_RAINBOW:
        /* **五彩幻彩**（待机用）：整条同步、缓慢流转，约 20 秒一圈。 */
        c = hsv2rgb((uint16_t)((t_ms / ELDER_RAINBOW_MS_PER_DEG) % 360u), 255, 255);
        break;
    case ELDER_FX_SOLID:
    default:
        break;
    }

    return dim_color(c, level_to_linear(s->level) * mod / 255u);
}

/* ---------------- #1 按键指示灯状态机 ----------------
 *
 * 全部在 g.lock 保护下调用（btn_step 会改 g.btn_mode）。 */

static void btn_enter_flash(uint32_t color, uint8_t times)
{
    g.btn_mode = BTN_FLASH;
    /* 起点用**渲染时钟**（g.btn_anim_ms），不是 now_ms()：
     * 这样"系统忙、rgb_task 被饿死"只会让闪烁暂停，不会让它过期。
     * 详见 ELDER_RGB_ANIM_MAX_STEP_MS 的注释。 */
    g.btn_flash_start_ms = g.btn_anim_ms;
    g.btn_flash_color = color;
    g.btn_flash_times = times;
    g.btn_resolved = true;   /* 这一轮已经有结果了，松开时不能再补一次粉色闪 */
    ESP_LOGI(TAG, "按键结果灯：%s，闪 %u 次（每次 %ums 亮 / %ums 灭）",
             (color == ELDER_RGB_BTN_OK_COLOR) ? "成功（深蓝绿）" : "未完成（深粉红）",
             (unsigned)times,
             (unsigned)ELDER_RGB_BTN_FLASH_ON_MS,
             (unsigned)ELDER_RGB_BTN_FLASH_OFF_MS);
}

/* 推进渲染时钟：dt 取两次步进之间的真实间隔，单步封顶 ELDER_RGB_ANIM_MAX_STEP_MS。
 * 必须**在每帧调用一次**（btn_step 的第一句），且只在 g.lock 内跑。 */
static void btn_anim_advance(uint32_t t_ms)
{
    if (g.btn_last_step_ms == 0u) {   /* 第一次：只记基准，不加时间 */
        g.btn_last_step_ms = t_ms;
        return;
    }
    uint32_t dt = t_ms - g.btn_last_step_ms;
    g.btn_last_step_ms = t_ms;
    if (dt > ELDER_RGB_ANIM_MAX_STEP_MS) dt = ELDER_RGB_ANIM_MAX_STEP_MS;
    g.btn_anim_ms += dt;
}

static uint32_t btn_step(uint32_t t_ms)
{
    btn_anim_advance(t_ms);

    /* 闪烁放完 → 有待确认就继续吸引注意，否则熄灭 */
    if (g.btn_mode == BTN_FLASH) {
        uint32_t period = ELDER_RGB_BTN_FLASH_ON_MS + ELDER_RGB_BTN_FLASH_OFF_MS;
        if ((g.btn_anim_ms - g.btn_flash_start_ms) >= (uint32_t)g.btn_flash_times * period) {
            g.btn_mode = g.btn_pending ? BTN_ATTRACT : BTN_OFF;
            g.btn_phase_ms = t_ms;
            g.btn_resolved = false;
        }
    }

    switch (g.btn_mode) {
    case BTN_ATTRACT: {
        uint32_t half  = ELDER_RGB_BTN_BREATH_MS / 2u;
        uint32_t phase = (t_ms - g.btn_phase_ms) % ELDER_RGB_BTN_BREATH_MS;
        uint32_t lvl = (phase < half) ? (phase * 255u / half)
                                      : ((ELDER_RGB_BTN_BREATH_MS - phase) * 255u / half);
        return scale_color(ELDER_RGB_BTN_ATTRACT_COLOR, lvl);
    }
    case BTN_GRADIENT:
        /* 进度条语义：3 秒走完，走完就是最深绿。
         * 这里**必须**用墙上时钟：它要和 input.c 判"按住够 3 秒"的时长
         * 严格对齐，动画时钟一旦落后，灯还在浅绿、按键却已经算确认了
         * —— 灯在骗人。 */
        return lerp_color(ELDER_RGB_BTN_GRAD_FROM, ELDER_RGB_BTN_GRAD_TO,
                          t_ms - g.btn_phase_ms,
                          (uint32_t)ELDER_BTN_CONFIRM_HOLD_MS);
    case BTN_FLASH: {
        uint32_t period = ELDER_RGB_BTN_FLASH_ON_MS + ELDER_RGB_BTN_FLASH_OFF_MS;
        uint32_t el = g.btn_anim_ms - g.btn_flash_start_ms;
        return ((el % period) < ELDER_RGB_BTN_FLASH_ON_MS) ? g.btn_flash_color : 0u;
    }
    case BTN_OFF:
    default:
        return 0u;
    }
}

/* ---------------- 帧渲染 ---------------- */

/* #1 = 按键指示灯，其余 = 灯效。两路分开写，互不覆盖。
 * WS2812 为 GRB 顺序，别按 RGB 顺序写（写错的表现是红蓝互换）。
 *
 * 循环写满**整条灯带**（ELDER_RGB_CHAIN = 20 颗），其中 #10~#20 一律写 0：
 * 只发前 9 颗的数据时，后面 11 颗收不到任何比特，锁存器停在开机随机态，
 * 真机表现是低亮度白色常亮（2026-10-27 用户实报）。显式写 0 是唯一的办法
 * —— 没被寻址过的灯珠不会自己熄灭。 */
static void render_frame(const slot_t *fx, bool has_nag, uint32_t nag_color,
                         uint32_t t_ms, uint32_t btn)
{
    g.pixels[0] = (uint8_t)((btn >> 8) & 0xFFu);
    g.pixels[1] = (uint8_t)((btn >> 16) & 0xFFu);
    g.pixels[2] = (uint8_t)(btn & 0xFFu);

    for (int i = 1; i < ELDER_RGB_CHAIN; i++) {
        uint32_t c = 0u;
        if (i < ELDER_RGB_LEDS) {          /* 受控段：#2~#9 */
            if (has_nag) {
                c = nag_color;             /* 提醒闪灯整条一起闪 */
            } else if (fx) {
                c = fx_pixel(fx, t_ms, i);
            }
        }                                      /* #10~#20 保持 0 */
        g.pixels[i * 3 + 0] = (uint8_t)((c >> 8) & 0xFFu);
        g.pixels[i * 3 + 1] = (uint8_t)((c >> 16) & 0xFFu);
        g.pixels[i * 3 + 2] = (uint8_t)(c & 0xFFu);
    }

    /* 值不变就不发：呼吸/流水每帧都在变，但"常亮""熄灭"这类静态画面
     * 如果也每 30ms 发一次，RMT 会被白白占满，还会拖慢同核的其他任务。 */
    if (g.last_valid && memcmp(g.pixels, g.last, sizeof(g.pixels)) == 0) {
        return;
    }
    memcpy(g.last, g.pixels, sizeof(g.pixels));
    g.last_valid = true;

    rmt_transmit_config_t tx = {.loop_count = 0};
    rmt_transmit(g.chan, g.enc, g.pixels, sizeof(g.pixels), &tx);
    rmt_tx_wait_all_done(g.chan, portMAX_DELAY);
}

static void rgb_task(void *arg)
{
    (void)arg;

    while (1) {
        uint32_t t_ms = now_ms();
        slot_t   s;
        bool     has_fx = false;
        bool     has_nag = false;
        uint32_t nag_color = 0u;
        uint32_t btn = 0u;

        if (xSemaphoreTake(g.lock, pdMS_TO_TICKS(50)) == pdTRUE) {
            int prio = -1;
            has_fx = active_slot(&s, t_ms, &prio);

            /* 待机降亮：进待机的前 ELDER_IDLE_BOOST_MS 用 60%，之后 30%。
             *
             * 为什么在这里改 s.level 而不是让上层再调一次 set_ui_state：
             *   待机期间**没有任何上层任务在跑**（会话已结束、唤醒词还没来），
             *   没人能来改。放在渲染循环里是唯一的做法，且不引入定时器/新任务。
             *
             * 只认"待机幻彩"这一种：条件里带 s.fx == ELDER_FX_RAINBOW，
             * 这样别人用 RAINBOW 做别的灯效时不会被误降亮。 */
            if (g.idle_dim && has_fx && s.fx == ELDER_FX_RAINBOW
                && (int32_t)(t_ms - g.idle_since_ms) >= (int32_t)ELDER_IDLE_BOOST_MS) {
                s.level = ELDER_UI_LEVEL_IDLE;
            }

            /* 需求 8：提醒闪灯（每 10 分钟的前 10 秒，共 2 小时）。
             *
             * 优先级刻意排在"服药未确认 / 求助告警"之下：老人正按着求助键
             * 或系统在报漏服时，灯不该被提醒节奏抢走。
             * 计时全部用 tick 毫秒数相减 —— 不另起定时器、不加任务，
             * 原因见 elder_rgb.h 里 nag_start 的注释。 */
            if (g.nag_active && prio < (int)ELDER_RGB_PRIO_MED_PEND) {
                uint32_t el = t_ms - g.nag_start_ms;
                if (el >= ELDER_NAG_TOTAL_MS) {
                    /* 2 小时到点：灯自己熄灭。**不依赖上层来关** ——
                     * 上层要记得关，就一定会有忘记关的那一次。 */
                    g.nag_active = false;
                    ESP_LOGI(TAG, "提醒闪灯已满 2 小时，熄灭");
                } else if ((el % ELDER_NAG_PERIOD_MS) < ELDER_NAG_BLINK_MS) {
                    /* 0.5s 亮 / 0.5s 灭，10 秒共 10 个周期 */
                    has_nag = true;
                    nag_color = ((t_ms % 500u) < 250u) ? 0xFF0000u : 0u;
                }
            }

            btn = btn_step(t_ms);
            xSemaphoreGive(g.lock);
        }

        render_frame(has_fx ? &s : NULL, has_nag, nag_color, t_ms, btn);
        vTaskDelay(pdMS_TO_TICKS(30));   /* 灯效刷新 ~33Hz */
    }
}

/* ---------------- 对外接口 ---------------- */

esp_err_t elder_rgb_init(void)
{
    if (g.ready) return ESP_OK;

#if !ELDER_RGB_ENABLED
    /* 原型期未接灯珠（CONFIG_ELDER_ENABLE_RGB=n 或 ELDER_RGB_GPIO=-1）。
     * 返回 ESP_OK 而不是错误：少一颗灯不该让整个启动流程报错，
     * 灯效相关的 request 会因为 g.ready=false 自动变成空操作。 */
    ESP_LOGW(TAG, "未启用 WS2812（RGB 关闭或 GPIO=-1），灯效功能关闭");
    return ESP_OK;
#endif

    rmt_tx_channel_config_t ch = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .gpio_num = ELDER_RGB_GPIO,
        /* 一次申请**整组** RMT 内存（8 通道 × 48 字 = 384 符号）。
         *
         * 为什么要这么多：一帧要发 20 颗 = 60 字节 = 480 个符号，
         * 而单块只有 48 符号。原来 mem_block_symbols=64 时驱动按 48 符号
         * 一块流式发送，480 符号要补块 9 次，每次都必须在 4.8µs 内补上
         * （10MHz 下 48 符号就是 4.8µs）；补块由 RMT 中断完成，只要有一次
         * 被更高优先级中断拖住超过 50µs，灯珠就把断流当成复位，后半段颜色
         * 不再更新。申请 384 之后只剩 1 次补块，风险降到最低。
         * 本工程没有别的东西用 RMT（无红外、无第二路灯带），整组拿走安全。 */
        .mem_block_symbols = 384,
        .resolution_hz = 10 * 1000 * 1000,   /* 10MHz → 100ns/符号 */
        .trans_queue_depth = 4,
        .flags.invert_out = false,
        .flags.with_dma = false,
    };
    esp_err_t ch_err = rmt_new_tx_channel(&ch, &g.chan);
    if (ch_err != ESP_OK) {
        /* 整组申请失败（例如将来有人加了第二路 RMT 设备）不能直接死：
         * 退回 48 符号分块流式发送，功能照旧，只是补块次数多。 */
        ESP_LOGW(TAG, "整组 RMT 内存申请失败(%s)，退回 64 符号分块流式发送",
                 esp_err_to_name(ch_err));
        ch.mem_block_symbols = 64;
        ESP_ERROR_CHECK(rmt_new_tx_channel(&ch, &g.chan));
    }

    rmt_bytes_encoder_config_t enc = {
        .bit0 = {.level0 = 1, .duration0 = WS2812_T0H_NS / 100,
                 .level1 = 0, .duration1 = WS2812_T0L_NS / 100},
        .bit1 = {.level0 = 1, .duration0 = WS2812_T1H_NS / 100,
                 .level1 = 0, .duration1 = WS2812_T1L_NS / 100},
        .flags.msb_first = 1,
    };
    ESP_ERROR_CHECK(rmt_new_bytes_encoder(&enc, &g.enc));
    ESP_ERROR_CHECK(rmt_enable(g.chan));

    g.lock = xSemaphoreCreateMutex();
    memset(g.slots, 0, sizeof(g.slots));
    g.btn_mode = BTN_OFF;
    g.last_valid = false;
    g.ready = true;

    xTaskCreatePinnedToCore(rgb_task, "elder_rgb", 3072, NULL, 5, &g.task, 1);
    ESP_LOGI(TAG, "WS2812 ready on GPIO%d，灯带 %d 颗（受控 %d 颗）："
                  "#1 按键指示灯，#2~#%d 灯效，#%d~#%d 恒灭",
             ELDER_RGB_GPIO, ELDER_RGB_CHAIN, ELDER_RGB_LEDS,
             ELDER_RGB_LEDS, ELDER_RGB_LEDS + 1, ELDER_RGB_CHAIN);
    return ESP_OK;
}

esp_err_t elder_rgb_request_ex(elder_rgb_prio_t prio, elder_rgb_fx_t fx,
                               uint32_t color, uint8_t level, uint32_t ttl_ms)
{
    if (!g.ready) return ESP_ERR_INVALID_STATE;
    uint32_t t_ms = now_ms();

    xSemaphoreTake(g.lock, portMAX_DELAY);
    slot_t *dst = NULL;
    for (int i = 0; i < ELDER_RGB_SLOTS; i++) {
        if (g.slots[i].used && g.slots[i].prio == (int)prio) { dst = &g.slots[i]; break; }
        if (!g.slots[i].used && !dst) dst = &g.slots[i];
    }
    if (!dst) { xSemaphoreGive(g.lock); return ESP_ERR_NO_MEM; }

    dst->used = true;
    dst->prio = (int)prio;
    dst->fx = fx;
    dst->color = color;
    dst->level = level;
    dst->expire_ms = ttl_ms ? t_ms + ttl_ms : 0;
    xSemaphoreGive(g.lock);
    return ESP_OK;
}

esp_err_t elder_rgb_request(elder_rgb_prio_t prio, elder_rgb_fx_t fx,
                            uint32_t color, uint32_t ttl_ms)
{
    return elder_rgb_request_ex(prio, fx, color, ELDER_UI_FULL_LEVEL, ttl_ms);
}

void elder_rgb_release(elder_rgb_prio_t prio)
{
    if (!g.ready) return;
    xSemaphoreTake(g.lock, portMAX_DELAY);
    for (int i = 0; i < ELDER_RGB_SLOTS; i++) {
        if (g.slots[i].used && g.slots[i].prio == (int)prio) {
            memset(&g.slots[i], 0, sizeof(slot_t));
        }
    }
    xSemaphoreGive(g.lock);
}

/* ---- 对话四态（2026-10-28 用户最终定义，见 elder_rgb.h） ---- */
void elder_rgb_set_ui_state(elder_rgb_ui_t st)
{
    /* 四个状态共用一个槽位（ELDER_RGB_PRIO_EMOTION）⇒ 切换就是替换。
     * 若各用各的优先级，从"说话"回"待机"时旧的 speaking 槽位不会被清掉，
     * 只要它的优先级更高就会一直压着待机呼吸 —— 表现为"说完话灯还亮着橙"。
     *
     * ttl 一律 0（常驻）：原来给 8000 会让待机呼吸 8 秒后灯自己灭掉，
     * 而待机是**长期**状态，老人看到灯灭了只会以为设备坏了。 */
    if (g.ready) {
        xSemaphoreTake(g.lock, portMAX_DELAY);
        /* 待机降亮的计时基准：**每次从别的状态回到待机都重新起算**。
         * 这样"老人刚说完话、放下设备"时灯是亮的（60%），他能确认设备
         * 已经回到待机；1 分钟后才悄悄降到 30%，夜里不刺眼。 */
        if (st == ELDER_RGB_UI_IDLE) {
            if (!g.idle_dim) g.idle_since_ms = now_ms();
            g.idle_dim = true;
        } else {
            g.idle_dim = false;
        }
        xSemaphoreGive(g.lock);
    }

    switch (st) {
    case ELDER_RGB_UI_LISTENING:
        /* 深天蓝 #00BFFF，55%，常亮。色相是语义（蓝 = 正在听），
         * 觉得亮就调 ELDER_UI_LEVEL_LISTENING，别改颜色。 */
        elder_rgb_request_ex(ELDER_RGB_PRIO_EMOTION, ELDER_FX_SOLID,
                             ELDER_UI_COLOR_LISTENING, ELDER_UI_LEVEL_LISTENING, 0);
        break;
    case ELDER_RGB_UI_CONNECTING:
        /* #2~#9 轮流亮起五彩，20%。颜色由 CHASE 自己按灯位算，
         * 这里的 color 参数不用（传 0 明确表示"灯效自定色"）。 */
        elder_rgb_request_ex(ELDER_RGB_PRIO_EMOTION, ELDER_FX_CHASE,
                             0u, ELDER_UI_LEVEL_CHASE, 0);
        break;
    case ELDER_RGB_UI_SPEAKING:
        /* 深橙 #FF8C00，70%，常亮。 */
        elder_rgb_request_ex(ELDER_RGB_PRIO_EMOTION, ELDER_FX_SOLID,
                             ELDER_UI_COLOR_SPEAKING, ELDER_UI_LEVEL_SPEAKING, 0);
        break;
    case ELDER_RGB_UI_IDLE:
    default:
        /* 五彩幻彩，整条同步缓慢流转。
         * level 先给 60%（刚进待机）；1 分钟后由 rgb_task 自动降到 30%
         * （见 g.idle_dim / ELDER_IDLE_BOOST_MS），上层不需要再调一次。 */
        elder_rgb_request_ex(ELDER_RGB_PRIO_EMOTION, ELDER_FX_RAINBOW,
                             0u, ELDER_UI_LEVEL_IDLE_BOOST, 0);
        break;
    }
}

static uint32_t emotion_color(elder_emotion_t emo)
{
    switch (emo) {
    case ELDER_EMO_HAPPY:     return 0x00C853;   /* 绿 */
    case ELDER_EMO_SAD:       return 0x2962FF;   /* 蓝 */
    case ELDER_EMO_ANGRY:     return 0xD50000;   /* 红 */
    default:                  return 0x8E24AA;   /* 紫 */
    }
}

void elder_rgb_set_emotion(elder_emotion_t emo)
{
    /* 三个"有明确灯效定义"的情绪直接走四态，避免两处颜色各写一份 */
    switch (emo) {
    case ELDER_EMO_NEUTRAL:   elder_rgb_set_ui_state(ELDER_RGB_UI_IDLE);      return;
    case ELDER_EMO_LISTENING: elder_rgb_set_ui_state(ELDER_RGB_UI_LISTENING); return;
    case ELDER_EMO_SPEAKING:  elder_rgb_set_ui_state(ELDER_RGB_UI_SPEAKING);  return;
    default: break;
    }
    elder_rgb_request(ELDER_RGB_PRIO_EMOTION, ELDER_FX_SOLID, emotion_color(emo), 0);
}

/* ---- 需求 11：服药状态灯 ---- */
void elder_rgb_med_pending(void)
{
    elder_rgb_request(ELDER_RGB_PRIO_MED_PEND, ELDER_FX_SOLID, 0xFF0000, 0);
}
void elder_rgb_med_done(void)
{
    elder_rgb_reminder_nag_stop();          /* 已确认 ⇒ 闪灯节奏立刻停 */
    elder_rgb_release(ELDER_RGB_PRIO_MED_PEND);
    elder_rgb_release(ELDER_RGB_PRIO_ALARM);
    elder_rgb_request(ELDER_RGB_PRIO_MED_PEND, ELDER_FX_SOLID, 0x00FF00, 60000);
}
void elder_rgb_med_escalate(void)
{
    elder_rgb_request(ELDER_RGB_PRIO_ALARM, ELDER_FX_BLINK_FAST, 0xFF0000, 0);
}

/* ---- 需求 8：提醒的闪灯节奏 ---- */
void elder_rgb_reminder_nag_start(void)
{
    if (!g.ready) return;
    xSemaphoreTake(g.lock, portMAX_DELAY);
    g.nag_active = true;
    g.nag_start_ms = now_ms();
    xSemaphoreGive(g.lock);
    ESP_LOGI(TAG, "提醒闪灯开始：每 %u 分钟闪 %u 秒，共 %u 小时",
             (unsigned)(ELDER_NAG_PERIOD_MS / 60000),
             (unsigned)(ELDER_NAG_BLINK_MS / 1000),
             (unsigned)(ELDER_NAG_TOTAL_MS / 3600000));
}

void elder_rgb_reminder_nag_stop(void)
{
    if (!g.ready) return;
    xSemaphoreTake(g.lock, portMAX_DELAY);
    if (g.nag_active) ESP_LOGI(TAG, "提醒闪灯停止");
    g.nag_active = false;
    xSemaphoreGive(g.lock);
}

/* ---- 第 1 颗灯：按键指示灯（语义与取值理由见 elder_rgb.h） ---- */

void elder_rgb_button_press(void)
{
    if (!g.ready) return;
    uint32_t t_ms = now_ms();

    xSemaphoreTake(g.lock, portMAX_DELAY);
    /* 没有待确认提醒 ⇒ **不进渐变**，#1 保持熄灭。
     * 正在放结果闪烁时也不打断：一次按下的反馈必须放完，
     * 否则老人按得快一点就永远看不到"成了还是没成"。 */
    if (g.btn_pending && g.btn_mode != BTN_FLASH) {
        g.btn_mode = BTN_GRADIENT;
        g.btn_phase_ms = t_ms;
        g.btn_resolved = false;
    }
    xSemaphoreGive(g.lock);
}

void elder_rgb_button_release(void)
{
    if (!g.ready) return;
    uint32_t t_ms = now_ms();

    xSemaphoreTake(g.lock, portMAX_DELAY);
    if (g.btn_mode == BTN_GRADIENT) {
        if (!g.btn_resolved) {
            /* 渐变还没走完就松手 ⇒ 没完成确认 */
            btn_enter_flash(ELDER_RGB_BTN_FAIL_COLOR, ELDER_RGB_BTN_FAIL_TIMES);
        } else {
            g.btn_mode = g.btn_pending ? BTN_ATTRACT : BTN_OFF;
            g.btn_phase_ms = t_ms;
        }
    }
    xSemaphoreGive(g.lock);
}

void elder_rgb_button_confirmed(void)
{
    if (!g.ready) return;

    xSemaphoreTake(g.lock, portMAX_DELAY);
    btn_enter_flash(ELDER_RGB_BTN_OK_COLOR, ELDER_RGB_BTN_OK_TIMES);
    xSemaphoreGive(g.lock);
}

void elder_rgb_button_pending(bool pending)
{
    if (!g.ready) return;
    uint32_t t_ms = now_ms();

    xSemaphoreTake(g.lock, portMAX_DELAY);
    if (g.btn_pending == pending) {          /* 比较也要在锁内，否则会漏掉边沿 */
        xSemaphoreGive(g.lock);
        return;
    }
    g.btn_pending = pending;
    /* 只在"熄灭 ↔ 吸引注意"之间切；正在按住或正在放结果时不打断 */
    if (g.btn_mode == BTN_OFF && pending) {
        g.btn_mode = BTN_ATTRACT;
        g.btn_phase_ms = t_ms;
    } else if (g.btn_mode == BTN_ATTRACT && !pending) {
        g.btn_mode = BTN_OFF;
    }
    xSemaphoreGive(g.lock);
}
