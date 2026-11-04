package com.elder.lx04.light

/**
 * 灯效语义模型 —— 移植自 ESP32 固件的 `firmware/components/elder/include/elder_rgb.h`。
 *
 * ## 为什么是"移植"而不是"重写"
 *
 * LX04 上**没有任何物理 LED**（顶部小板只有 3 个微动键 + 双麦 + 光线传感器），
 * 所以原固件的 WS2812B 灯带无处安放。灯效必须落到屏幕上。
 *
 * 但灯效的**语义**不能变 —— 它是产品的一部分，是老人与家属约定好的信号：
 * 哪个颜色代表"在听"、哪个代表"该吃药了"、闪几次代表"按对了"。
 * 换硬件不等于换语义。所以这里把 `elder_rgb.h` 的枚举、优先级、亮度口径
 * 原样搬过来，让 Android 侧与固件侧说的是同一套话。
 *
 * ## 与固件的对应关系
 *
 * | 固件（elder_rgb.h）           | 本文件                      |
 * |-------------------------------|-----------------------------|
 * | `elder_rgb_prio_t`            | [LedPriority]               |
 * | `elder_rgb_fx_t`              | [LedEffect]                 |
 * | `elder_rgb_ui_t`              | [UiState]                   |
 * | `elder_rgb_cfg_item_t`        | [LedConfigItem]             |
 * | `elder_rgb_set_ui_state()`    | [LedEngine.setUiState]      |
 * | `elder_rgb_med_pending/done`  | [LedEngine.medPending] 等   |
 * | `elder_rgb_button_*`          | [LedEngine] 的按钮方法组     |
 *
 * ## 亮度口径（最容易搞错的一处）
 *
 * 固件里 `level` 是 0~255 的**感知亮度**（人眼看着几成亮），**不是** 0~100，
 * 也不是写进灯珠的线性值 —— 中间隔着一张 gamma 2.2 查表（固件 `rgb.c` 的
 * `GAMMA_LUT`）。加表之前 `scale_color()` 是纯线性乘法，写 30% 人眼看着约 58%，
 * 这正是当年"WS2812B 好像控制不了亮度"的真正原因。
 *
 * Android 侧用屏幕直接绘制，没有灯珠的非线性衰减，**但仍沿用同一套感知亮度口径**，
 * 这样同一个 `level` 在两个平台上的观感一致，灯效配置表也才能原样复用。
 */
object Led {

    /**
     * 优先级仲裁。
     *
     * 与固件完全一致：数值越大越优先，低优先级不能抢占高优先级。
     * 固件里判据落在**优先级**上，而不是另立一张"哪些算事件灯"的名单 ——
     * 名单会漂移，优先级不会。
     */
    enum class Priority(val value: Int) {
        /** 呼吸 / 待机 */
        IDLE(0),

        /** 对话状态（可被提醒打断） */
        EMOTION(10),

        /** 日程提醒中 */
        REMINDER(50),

        /** 服药未确认 —— 除升级外最高 */
        MED_PENDING(90),

        /** 求助 / 告警 */
        ALARM(100);

        companion object {
            fun of(value: Int): Priority =
                entries.firstOrNull { it.value == value } ?: IDLE
        }
    }

    /**
     * 灯效类型。
     *
     * 命名与固件 `elder_rgb_fx_t` 一一对应，语义不变，只是渲染方式从
     * "点亮灯珠"变成"填充色带"。
     */
    enum class LedEffect(val id: Int) {
        /** 常亮 */
        SOLID(0),

        /** 呼吸 */
        BREATH(1),

        /** 快闪 */
        BLINK_FAST(2),

        /** 整条同步缓慢流转（原待机"五彩幻彩"，已被 TWINKLE 替掉，保留备用） */
        RAINBOW(3),

        /** 流水（跑马灯）：全亮，随时间沿色带流动。
         *  · color != 0：同色行波（颜色固定，亮度做行进三角波）
         *  · color == 0：五彩流水（每段一个色相、整体滚动，服务端用 rgb="#000000"
         *    表达"颜色随机"） */
        FLOW(4),

        /** 轮流亮起：同一时刻只有一段亮，亮点从底跑到顶 */
        CHASE(5),

        /** 闪烁星光（待机用）：
         * **全局一个节拍，一次只亮一段**：2 秒一格、每格挑一段亮 300ms 渐隐，
         * 亮起时刻在格内抖 0~1 秒 ⇒ 相邻两闪间隔 1~3 秒。颜色每闪一次换一个。
     *
     * 第一版是"每段各自 1.2~3.0s 的独立周期"，8 段叠加后每秒闪 3~4 次，
     * 用户实报"频率太快、像每秒都有多颗在闪"。星光要的是稀疏，不是热闹。 */
        TWINKLE(6),

        /** 流星：网络传输中用。一个亮头 + 拖尾沿色带跑过去，头部最亮、尾部线性衰减。
     * 每段流星换一个色相（流星本身必须是**一个**颜色，不能是彩虹），
     * 连续几段换色 ⇒ 整体仍是五彩。 */
        METEOR(7);

        companion object {
            /** 服务端下发的 "关闭" */
            const val OFF = 0xFF

            fun of(id: Int): LedEffect? = entries.firstOrNull { it.id == id }
        }
    }

    /**
     * 对话四态。
     *
     * 2026-10-28 用户最终定义（两轮，覆盖 10-27 的版本）：
     *
     *   待机（进入后 0~1 分钟）  闪烁星光 Twinkle 60%  随机彩色，偶尔闪一下
     *   待机（超过 1 分钟）      闪烁星光 Twinkle 30%  同上，自动降亮
     *   唤醒后收听               #00BFFF 深天蓝 55%   常亮
     *   TTS 输出                 #FF8C00 深橙 70%     常亮
     *   网络传输处理中           流星 Meteor 20%      亮头 + 拖尾跑过色带
     *
     * 四个状态**互斥**，共用 Priority.EMOTION 一个槽位 —— 切换即替换，
     * 不会留下过期槽位把新状态顶掉。
     */
    enum class UiState {
        /** 待机：闪烁星光（TWINKLE），60% → 1 分钟后 30% */
        IDLE,

        /** 唤醒后收听：深天蓝 #00BFFF，55%，常亮 */
        LISTENING,

        /** 数据上传/等回复：流星（METEOR），20% */
        CONNECTING,

        /** TTS 输出：深橙 #FF8C00，70%，常亮 */
        SPEAKING
    }

    /**
     * 灯效配置项 —— 对应固件的 `elder_rgb_cfg_item_t`。
     *
     * 真源在服务端 `server/app/avatar_style.py` 的 `LIGHT_PRESETS`。
     * 设备只认下面的 effect 数值，不认预置 id —— 预置是"给人挑的"，
     * 下发时已经展开成 rgb/effect/亮度/间隔，设备不需要知道它叫什么名字。
     */
    data class LedConfigItem(
        /** [LedEffect] 的 id，或 [LedEffect.OFF] */
        val effect: Int,
        /** 0xRRGGBB；0 = 灯效自定色（星光随机色） */
        val color: Int,
        /** 0~255 **感知亮度**，不是 0~100 */
        val level: Int,
        /** 呼吸/闪烁/星光的周期（ms）；0 = 用内置默认 */
        val periodMs: Int
    )

    /**
     * 对话情绪 —— 对应固件 `elder_emotion_t`。
     *
     * NEUTRAL/LISTENING/SPEAKING 三个转成对应的 UI 四态；
     * HAPPY/SAD/ANGRY 走"状态灯效表"里的同名项。
     */
    enum class Emotion {
        NEUTRAL, HAPPY, SAD, ANGRY, LISTENING, SPEAKING
    }

    // ---------------------------------------------------------------- 颜色常量

    /**
     * 灯效颜色。
     *
     * 注意「深天蓝」与「天蓝」是两个不同的蓝，语义也不同：
     *   · #00BFFF 天蓝 = **确认成功**（按钮结果闪烁）
     *   · #00E5FF 天青 = **按住期间的渐变**（进度条）
     *   · #00BFFF 在 UI 四态里是"唤醒聆听"，在按钮语义里是"成功" ——
     *     两处都是"深天蓝"，这是固件里的既定选择，不要擅自改色。
     */
    object Color {
        /** 待机星光：随机色 */
        const val TWINKLE_RANDOM = 0

        /** 唤醒聆听：深天蓝 */
        const val LISTENING = 0x00BFFF

        /** TTS 说话：深橙 */
        const val SPEAKING = 0xFF8C00

        /** 按住渐变：天青 #00E5FF（同一色相由暗变亮） */
        const val BUTTON_RAMP = 0x00E5FF

        /** 确认成功：天蓝 #00BFFF 全亮闪 3 次 */
        const val BUTTON_OK = 0x00BFFF

        /** 中途松开失败：粉红 #FF69B4 全亮闪 5 次 */
        const val BUTTON_FAIL = 0xFF69B4

        /** 服药未确认：红 */
        const val MED_PENDING = 0xFF0000

        /** 服药完成：绿（默认 60s 后回落） */
        const val MED_DONE = 0x00FF00

        /** 提醒警示：橙 */
        const val REMINDER = 0xFF8C00
    }

    // ---------------------------------------------------------------- 亮度常量

    /**
     * 感知亮度常量（0~255）。
     *
     * ⚠️ 这些是**感知亮度**，不是百分比。写代码时看到 55 要理解成
     * "人眼看着五成半亮"，而不是"55%"。
     */
    object Level {
        /** 待机 0~1 分钟：闪烁星光 60% */
        const val IDLE_NEAR = 153

        /** 待机超过 1 分钟：闪烁星光 30%（自动降亮，夜里不刺眼） */
        const val IDLE_FAR = 77

        /** 唤醒聆听：55% */
        const val LISTENING = 140

        /** TTS 说话：70% */
        const val SPEAKING = 179

        /** 网络传输中（流星）：20% */
        const val CONNECTING = 51

        /** 按键渐变起点（不是全黑 —— 按下瞬间就要有反馈） */
        const val BUTTON_RAMP_MIN = 60

        /** 满亮 */
        const val FULL = 255
    }
}
