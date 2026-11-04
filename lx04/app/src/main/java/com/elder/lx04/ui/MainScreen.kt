package com.elder.lx04.ui

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import com.elder.lx04.R

/**
 * 主界面 —— 与 ESP32 固件的屏显一一对应。
 *
 * ## 界面结构（固件 `main/display/lcd_display.cc` + `elder_bridge.cc`）
 *
 * ```
 * ┌────────────────────────────────────────────┐
 * │ 12:34                        WiFi  87%     │  ← 状态栏
 * │                                            │
 * │              ( 表情 128px )                │  ← 表情图标
 * │                                            │
 * │           老人说的话（灰）                   │  ← 用户字幕
 * │           设备说的话（白）                   │  ← 设备字幕
 * └────────────────────────────────────────────┘
 * ```
 *
 * 提醒时**整屏覆盖**（固件是 `elder_ui` 的 reminder 状态，最长 4 小时）：
 * 表情换成 shocked，中间是事项和时间，底部提示怎么确认。
 *
 * ## 表情与固件同一套图
 *
 * 用的是 `firmware/xiaozhi/main/assets/emoji/fluent3d_128/` 里的 21 张 PNG，
 * 原样拷到 `res/drawable-nodpi/`。固件里情绪名到图的映射在
 * `components/elder/src/ui.c` 的 `EMOTION[]` 表，这里 [STATE_EMOJI] 与它逐项对齐 ——
 * 两边不一致的话，同一个"正在听"在两块屏上是两张脸，家属会以为换了产品。
 *
 * ## 为什么全用代码建 View，不写 XML
 *
 * 这个界面只有一层：状态栏一行、一张图、两行字、两个覆盖层。
 * 写成 XML 要多三个文件，而且尺寸要按 800×480 单独调（别的屏用不上）。
 * 代码里放着，尺寸常量就在用它的地方，改起来一眼能看全。
 */
class MainScreen(private val context: Context) {

    /** 整屏根 View。 */
    val root: FrameLayout = FrameLayout(context).apply {
        setBackgroundColor(Color.BLACK)
    }

    private val timeText: TextView
    private val wifiText: TextView
    private val batteryText: TextView
    private val emojiView: ImageView
    private val userText: TextView
    private val deviceText: TextView

    /**
     * 底部状态提示（小字）。
     *
     * ⚠️ 它**不能**和激活页的说明文字共用同一个 TextView。
     *    原先 `setHint()` 直接写 `activationHint`，而激活覆盖层在绑定成功后
     *    会被隐藏 —— 于是"已连接"、"连不上服务器：xxx"、"麦克风打不开"
     *    这些最需要看到的提示，全写进了一个看不见的 View 里。
     *    调试时表现为"日志有、屏幕上什么都没有"。
     */
    private val statusText: TextView

    private val reminderOverlay: FrameLayout
    private val reminderEmoji: ImageView
    private val reminderTitle: TextView
    private val reminderDetail: TextView

    private val activationOverlay: FrameLayout
    private val activationCode: TextView
    private val activationHint: TextView

    /** 当前表情名，避免重复加载同一张图。 */
    private var currentEmoji: String = ""

    init {
        // ---------------- 状态栏 ----------------
        val statusBar = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(20), dp(6), dp(20), dp(6))
        }

        timeText = label(18f, Color.WHITE).apply { text = "--:--" }
        wifiText = label(13f, COLOR_DIM).apply { text = "WiFi" }
        batteryText = label(13f, COLOR_DIM).apply { text = "--%" }

        statusBar.addView(timeText)
        statusBar.addView(View(context), LinearLayout.LayoutParams(0, 1, 1f))
        statusBar.addView(wifiText)
        statusBar.addView(spacer(dp(14)))
        statusBar.addView(batteryText)

        root.addView(
            statusBar,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT
            ).apply { gravity = Gravity.TOP }
        )

        // ---------------- 表情 ----------------
        emojiView = ImageView(context).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            setImageResource(R.drawable.emoji_neutral)
        }
        root.addView(
            emojiView,
            FrameLayout.LayoutParams(dp(150), dp(150)).apply {
                gravity = Gravity.CENTER_HORIZONTAL or Gravity.TOP
                topMargin = dp(40)
            }
        )

        // ---------------- 字幕 ----------------
        val subs = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(24), 0, dp(24), dp(16))
        }

        userText = label(15f, COLOR_DIM).apply {
            gravity = Gravity.CENTER
            maxLines = 2
            text = ""
        }
        deviceText = label(22f, Color.WHITE).apply {
            gravity = Gravity.CENTER
            maxLines = 3
            setPadding(0, dp(6), 0, 0)
            text = ""
        }
        statusText = label(12f, COLOR_DIM).apply {
            gravity = Gravity.CENTER
            maxLines = 2
            setPadding(0, dp(8), 0, 0)
            text = ""
        }
        subs.addView(userText)
        subs.addView(deviceText)
        subs.addView(statusText)

        root.addView(
            subs,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT
            ).apply { gravity = Gravity.BOTTOM }
        )

        // ---------------- 提醒覆盖层 ----------------
        reminderOverlay = FrameLayout(context).apply {
            setBackgroundColor(COLOR_OVERLAY)
            visibility = View.GONE
        }
        val remBox = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(dp(32), dp(24), dp(32), dp(24))
        }
        reminderEmoji = ImageView(context).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            setImageResource(R.drawable.emoji_shocked)
        }
        reminderTitle = label(30f, Color.WHITE).apply {
            gravity = Gravity.CENTER
            setPadding(0, dp(10), 0, 0)
        }
        reminderDetail = label(18f, COLOR_ACCENT).apply {
            gravity = Gravity.CENTER
            setPadding(0, dp(8), 0, 0)
        }
        remBox.addView(reminderEmoji, LinearLayout.LayoutParams(dp(110), dp(110)))
        remBox.addView(reminderTitle)
        remBox.addView(reminderDetail)
        reminderOverlay.addView(
            remBox,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT
            ).apply { gravity = Gravity.CENTER }
        )
        root.addView(
            reminderOverlay,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        )

        // ---------------- 激活码覆盖层 ----------------
        activationOverlay = FrameLayout(context).apply {
            setBackgroundColor(Color.BLACK)
            visibility = View.GONE
        }
        val actBox = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
        }
        actBox.addView(
            ImageView(context).apply {
                scaleType = ImageView.ScaleType.FIT_CENTER
                setImageResource(R.drawable.emoji_winking)
            },
            LinearLayout.LayoutParams(dp(100), dp(100))
        )
        activationCode = label(52f, COLOR_ACCENT).apply {
            gravity = Gravity.CENTER
            typeface = Typeface.MONOSPACE
            setPadding(0, dp(8), 0, 0)
            text = "-------"
        }
        activationHint = label(15f, COLOR_DIM).apply {
            gravity = Gravity.CENTER
            setPadding(0, dp(8), 0, 0)
            text = "请在手机 App 里输入这串数字"
        }
        actBox.addView(activationCode)
        actBox.addView(activationHint)
        activationOverlay.addView(actBox)
        root.addView(
            activationOverlay,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        )
    }

    // ---------------------------------------------------------------- 对外

    /** 状态栏时间（HH:mm）。 */
    fun setClock(hhmm: String) {
        timeText.text = hhmm
    }

    /** 状态栏网络与电量。 */
    fun setStatus(wifiOk: Boolean, batteryPercent: Int, charging: Boolean) {
        wifiText.text = if (wifiOk) "WiFi" else "离线"
        wifiText.setTextColor(if (wifiOk) COLOR_DIM else COLOR_WARN)
        batteryText.text = if (charging) "${batteryPercent}% ⚡" else "${batteryPercent}%"
    }

    /**
     * 换表情。
     *
     * @param name 固件里那 21 个词之一：happy / thinking / shocked / angry / sleepy ...
     */
    fun setEmoji(name: String) {
        if (name == currentEmoji) return
        val res = context.resources.getIdentifier("emoji_$name", "drawable", context.packageName)
        if (res == 0) {
            // 名字写错就是"静默换了张图"，很难发现 —— 记日志而不是静默回落
            android.util.Log.w("ElderUi", "没有这个表情: $name（回落到 neutral）")
            if (currentEmoji == "neutral") return
            currentEmoji = "neutral"
            emojiView.setImageResource(R.drawable.emoji_neutral)
            return
        }
        currentEmoji = name
        emojiView.setImageResource(res)
    }

    /** 用户说的话（灰字，在下面一行字的上面）。 */
    fun setUserSaid(text: String) {
        userText.text = text
    }

    /** 设备说的话（白字，主字幕）。 */
    fun setDeviceSaid(text: String) {
        deviceText.text = text
    }

    /** 显示全屏提醒。 */
    fun showReminder(title: String, detail: String) {
        reminderTitle.text = title
        reminderDetail.text = detail
        reminderOverlay.visibility = View.VISIBLE
    }

    fun hideReminder() {
        reminderOverlay.visibility = View.GONE
    }

    /** 显示激活码（未绑定设备）。 */
    fun showActivation(code: String) {
        activationCode.text = code
        activationOverlay.visibility = View.VISIBLE
    }

    fun hideActivation() {
        activationOverlay.visibility = View.GONE
    }

    /** 底部状态提示一行小字（连接状态 / 错误 / 自检结果）。 */
    fun setHint(text: String) {
        statusText.text = text
    }

    // ---------------------------------------------------------------- 工具

    private fun label(sp: Float, color: Int): TextView = TextView(context).apply {
        setTextSize(TypedValue.COMPLEX_UNIT_SP, sp)
        setTextColor(color)
        includeFontPadding = false
    }

    private fun spacer(px: Int): View = View(context).apply {
        layoutParams = LinearLayout.LayoutParams(px, 1)
    }

    private fun dp(v: Int): Int =
        (v * context.resources.displayMetrics.density + 0.5f).toInt()

    companion object {
        /** 次要文字/图标的灰。 */
        private const val COLOR_DIM = 0xFF9AA0A6.toInt()

        /** 强调色（天蓝，与灯效里的"确认成功"同色）。 */
        private const val COLOR_ACCENT = 0xFF00BFFF.toInt()

        /** 离线/异常。 */
        private const val COLOR_WARN = 0xFFFF7043.toInt()

        /** 提醒覆盖层的半透明底（让下面的界面还在，但看不清 —— 焦点在提醒上）。 */
        private const val COLOR_OVERLAY = 0xF2000000.toInt()

        /**
         * 固件 `elder_ui` 的状态 → 表情名。
         *
         * 逐项对齐 `firmware/components/elder/src/ui.c` 的 `EMOTION[]` 表。
         * 顺序不能改：改了就是"待机时显示生气"这种看起来像坏了的 bug。
         */
        val STATE_EMOJI = mapOf(
            "idle" to "neutral",
            "listening" to "thinking",
            "speaking" to "happy",
            "reminder" to "shocked",
            "confirmed" to "happy",
            "missed" to "angry",
            "alarm" to "angry",
            "offline" to "sleepy",
            "activating" to "winking"
        )
    }
}
