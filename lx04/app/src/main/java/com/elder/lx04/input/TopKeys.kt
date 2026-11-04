package com.elder.lx04.input

import android.view.KeyEvent

/**
 * 顶部按键的语义映射 —— LX04 设备端。
 *
 * ## 硬件事实
 *
 * LX04 顶部三个微动键，官方规格页标注为 **音量- / 屏幕开关 / 音量+**：
 * https://www.mi.com/aispeaker-touch/specs
 *
 * 用户的要求：
 *   · 音量+ / 音量- **保留原功能**（调音量）
 *   · 中间键（原"屏幕开关"）**改为服药确认键**
 *
 * ## ✅ 中间键的 KeyCode 已实机确认（2026-11-04）
 *
 * 结论先行：**原厂是 `KEY_POWER`，已改 keylayout 重映射为 `F1`**。
 *
 * 实机 `getevent -lt /dev/input/event1`（设备 `mtk-pmic-keys`）抓到的是
 * `KEY_POWER`，系统当场熄屏：
 *
 *     PowerManagerService: Going to sleep due to power button (uid 1000)
 *
 * 社区资料里 `KEY_POWER` / `KEY_HOME` 两种说法，这台机器是 **POWER**。
 *
 * 直接后果：应用层**拦不住** POWER（它在 `interceptKeyBeforeQueueing`
 * 阶段就被系统吃掉），所以只能在 keylayout 层改：
 *
 *     /system/usr/keylayout/mtk-pmic-keys.kl
 *         key 114   VOLUME_DOWN
 *         key 116   F1
 *
 * 改完重启，`dumpsys input` 确认 `KeyLayoutFile` 指向该文件；
 * 连按 5 次实测：`getevent` 照常上报 116，但 logcat 零熄屏日志。
 *
 * 完整验证过程见 docs/lx04/实机验证结果.md 第三节。
 */
object TopKeys {

    /** 音量加。 */
    const val VOLUME_UP = KeyEvent.KEYCODE_VOLUME_UP

    /** 音量减。 */
    const val VOLUME_DOWN = KeyEvent.KEYCODE_VOLUME_DOWN

    /**
     * 中间键的 KeyCode —— **实机确认，已收敛为单一值 F1**。
     *
     * ## 为什么是 F1 而不是 POWER
     *
     * 2026-11-04 实机抓取（`getevent -lt /dev/input/event1`），中间键原始扫描码是
     * `KEY_POWER`，系统当场熄屏：
     *
     *     PowerManagerService: Going to sleep due to power button (uid 1000)
     *
     * 更糟的是长按 POWER 会弹关机菜单 —— 老人长按"服药确认键"就能把音箱关了。
     *
     * 解法是改 keylayout 重映射（`/system/usr/keylayout/mtk-pmic-keys.kl`）：
     *
     *     key 114   VOLUME_DOWN
     *     key 116   F1
     *
     * 实测连按 5 次：`getevent` 照常上报 `KEY_POWER`（内核层不变），
     * 但 logcat **零**熄屏日志、`mWakefulness=Awake` —— 系统已把它当 F1 处理。
     *
     * ## ⚠️ 这里刻意**只认 F1**，不放 POWER
     *
     *   · 放 POWER 会双重触发：触摸屏 event2/3 也声明了 `KEY_POWER`，
     *     万一某条路径上报 POWER，我们当确认键、系统同时熄屏
     *   · 若 keylayout 没刷（中间键仍是 POWER），我们的 App 就不响应 ——
     *     这是**故意的**：宁可"按了没反应"，也不能"按了就熄屏"
     *
     * 详见 docs/lx04/实机验证结果.md 第三节。
     */
    val CONFIRM_KEY_CODES: Set<Int> = setOf(
        KeyEvent.KEYCODE_F1
    )

    /** 是否是中间键（服药确认键）。 */
    fun isConfirmKey(keyCode: Int): Boolean = keyCode in CONFIRM_KEY_CODES

    /** 是否是音量键。 */
    fun isVolumeKey(keyCode: Int): Boolean =
        keyCode == VOLUME_UP || keyCode == VOLUME_DOWN
}

/**
 * 长按确认的判定器 —— 移植自 ESP32 固件的 `firmware/components/elder/src/input.c`。
 *
 * ## 语义（固件里用户拍板的）
 *
 *   1) 服药确认（主用途）
 *        · 提醒响铃期间短按        → 直接确认「已服药」
 *        · 长按 >=3s 后松开       → 30s 窗口内再短按 → 确认（不在响铃时也能用）
 *   2) 没有待确认提醒时，短按复用为「开始/结束对话」
 *
 * ## 三条时间常数（都刻意比固件宽松一点点）
 *
 * 固件用 GPIO 中断 + 定时器判长按；Android 用 KeyEvent 的重复回调
 * （`onKeyDown` 每约 500ms 一次重复），时间粒度更粗，所以：
 *
 *   · [HOLD_MS] = 3000ms —— 与灯效进度条**同源**，两边都用它
 *   · [RELEASE_DEBOUNCE_MS] = 80ms —— 松手消抖。固件里是 80ms，这里一致。
 *     Android 的 KeyEvent 在物理松手时立刻给 ACTION_UP，一般不需要消抖，
 *     但保留这个概念是为了对付个别 ROM 上报的抖动。
 *   · [DEBOUNCE_MIN_MS] = 300ms —— **抖动兜底**。固件 2026-11-04 加的：
 *     老人按住不动，机械按键中途抖几十毫秒就被当成松手，
 *     渐变被掐掉、灯报"你没按够"。用户实报过这个故障。
 *
 * ⚠️ 这三处时间**必须与 [com.elder.lx04.light.LedEngine] 的一致**。
 * 各写一份的话，只要有人改了一处，就会出现
 * "灯已经亮满天青、按键却还没算确认"（或反过来）—— 灯在骗人，比没有灯更糟。
 */
class ConfirmKeyDetector {

    /** 本轮按压的状态。 */
    private enum class State {
        /** 没在按 */
        IDLE,

        /** 按住中，还不够 3 秒 */
        PRESSING,

        /** 长按已达标（仍按住） */
        HOLD_REACHED,

        /** 本轮已经出过结果 */
        RESOLVED
    }

    private var state = State.IDLE
    private var pressAt = 0L

    /** 本轮是否已达标（用于 UI 显示）。 */
    var holdReached: Boolean = false
        private set

    /** 本轮按住的时长（ms），用于驱动进度条。 */
    var heldMs: Long = 0L
        private set

    /**
     * 按下。
     *
     * @return true 表示这是一个**新的**按压开始（重复的 ACTION_DOWN 不算）
     */
    fun onDown(t: Long): Boolean {
        if (state == State.PRESSING || state == State.HOLD_REACHED) {
            // 系统重复上报的 ACTION_DOWN —— 不是新的按压
            heldMs = t - pressAt
            return false
        }
        state = State.PRESSING
        pressAt = t
        holdReached = false
        heldMs = 0L
        return true
    }

    /**
     * 按住期间（系统重复回调）。
     *
     * @return true 表示**此刻刚跨过 3 秒门槛**（用于弹"天蓝闪 3 次"）
     */
    fun onRepeat(t: Long): Boolean {
        if (state != State.PRESSING && state != State.HOLD_REACHED) return false
        heldMs = t - pressAt
        if (state == State.PRESSING && heldMs >= HOLD_MS) {
            state = State.HOLD_REACHED
            holdReached = true
            return true
        }
        return false
    }

    /**
     * 松开。
     *
     * @return [ReleaseResult] 描述这一轮的判定
     */
    fun onUp(t: Long): ReleaseResult {
        if (state == State.IDLE || state == State.RESOLVED) {
            state = State.IDLE
            return ReleaseResult.None
        }
        val held = t - pressAt
        heldMs = held
        val wasHold = state == State.HOLD_REACHED
        state = State.RESOLVED

        return when {
            // 达标 ⇒ 成功
            wasHold -> ReleaseResult.Confirmed
            // 抖动兜底：<300ms 不算失败
            held < DEBOUNCE_MIN_MS -> ReleaseResult.Debounced
            // 中途松开 ⇒ 失败
            else -> ReleaseResult.ReleasedEarly
        }
    }

    /**
     * 询问是否刚达到长按门槛（给 UI 播"确认"动画用）。
     */
    fun consumeHoldReached(): Boolean {
        if (!holdReached) return false
        holdReached = false
        return true
    }

    /** 重置状态（收到服务端的确认回执后调用）。 */
    fun reset() {
        state = State.IDLE
        pressAt = 0L
        holdReached = false
        heldMs = 0L
    }

    /** 一次按压的判定结果。 */
    enum class ReleaseResult {
        /** 本轮无有效按压 */
        None,

        /** 长按达标（≥3s）⇒ 确认成功 */
        Confirmed,

        /** 抖动（<300ms）⇒ 静默，不给任何反馈 */
        Debounced,

        /** 中途松开 ⇒ 失败 */
        ReleasedEarly
    }

    companion object {
        /**
         * 长按达标时长：3 秒。
         *
         * ⚠️ 与 [com.elder.lx04.light.LedEngine.CONFIRM_HOLD_MS] 必须一致。
         */
        const val HOLD_MS = 3000L

        /** 松手消抖：80ms（与固件一致） */
        const val RELEASE_DEBOUNCE_MS = 80L

        /** 抖动兜底：300ms 以内的松手不算"中途放弃" */
        const val DEBOUNCE_MIN_MS = 300L

        /**
         * 长按达标后的确认窗口：30 秒。
         *
         * 语义：不在提醒响铃时，长按 3 秒确认后，30 秒内再短按一次也算确认。
         * 这让老人可以先长按"我看到了"，再短按"我吃完了"。
         */
        const val CONFIRM_WINDOW_MS = 30_000L
    }
}
