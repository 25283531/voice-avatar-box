package com.elder.lx04

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioManager
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.TextView
import com.elder.lx04.input.ConfirmKeyDetector
import com.elder.lx04.input.TopKeys
import com.elder.lx04.light.Led
import com.elder.lx04.light.LedBandView
import com.elder.lx04.light.LedEngine
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * LX04 设备端主界面。
 *
 * ## 它要满足的四件事（用户 2026-11-04 的要求）
 *
 * 1. **开机直接进入我们的项目，不进小爱桌面**
 *    · Manifest 里注册了 `MAIN + HOME + DEFAULT` ⇒ 可以被设为默认桌面
 *    · 部署脚本执行 `adb shell cmd package set-home-activity` 强制指定，跳过选择框
 *    · [BootReceiver] 兜底自启
 *
 * 2. **可以直接语音唤醒**
 *    · 自己用 `AudioRecord` 采集 + 自带唤醒词检测，**不依赖小爱的唤醒引擎**
 *      （小爱的唤醒是私有实现，拿不到事件也拿不到麦克风流）
 *
 * 3. **顶部三个按键**：音量± 保留原功能，中间键 = 服药确认键
 *    · 见 [dispatchKeyEvent]
 *
 * 4. **事件灯 / 状态灯取消，改为屏幕两侧各 5mm 的色带**
 *    · [LedBandView] 画在内容两侧
 *    · 灯效语义 1:1 移植自 ESP32 固件（见 [LedEngine]）
 *
 * ## 全屏（很重要）
 *
 * 必须全屏且隐藏状态栏/导航栏 —— 否则两侧色带会被系统 UI 压住，
 * 或者系统强制显示状态栏导致布局被挤。
 */
class MainActivity : Activity() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    private lateinit var ledEngine: LedEngine
    private lateinit var bandView: LedBandView
    private lateinit var subtitleView: TextView
    private lateinit var statusView: TextView

    private val keyDetector = ConfirmKeyDetector()

    /** 确认窗口的到期时刻（elapsedRealtime）；0 = 没有开着的窗口 */
    private var confirmWindowUntil = 0L

    /** 是否有待确认的提醒（由服务端事件驱动） */
    private var hasPendingReminder = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // ⚠️ 必须全屏：色带是画在屏幕边缘的，系统 UI 会压住它
        window.addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        hideSystemUi()

        ledEngine = LedEngine()
        ledEngine.defaultConfig()

        setContentView(buildUi())

        // 调试广播（默认不生效，见 DebugBridge）
        runCatching {
            registerReceiver(debugReceiver, android.content.IntentFilter(DebugBridge.ACTION))
        }.onFailure { Log.w(TAG, "注册调试广播失败: ${it.message}") }

        requestMicPermission()

        // 启动时先亮一下"待命"色带，给老人一个"设备活着"的信号
        ledEngine.setUiState(Led.UiState.IDLE)
    }

    // ------------------------------------------------------------------ UI

    private fun buildUi(): View {
        val root = FrameLayout(this)

        // 内容区（两侧留出色带空间）
        //
        // ⚠️ 每侧留 **1 个**色带宽度，不是 2 个。
        // 色带是画在 root 的最上层（左 0~w，右 width-w~width），
        // 内容区必须正好让开这两条，否则字幕/头像会压在色带下面。
        val bandW = bandViewWidthPx()
        val content = FrameLayout(this)
        content.setPadding(bandW, 0, bandW, 0)

        subtitleView = TextView(this).apply {
            textSize = 30f
            setTextColor(0xFFFFFFFF.toInt())
            gravity = android.view.Gravity.CENTER
            text = "小爱已就位"
        }
        content.addView(
            subtitleView,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            ).apply { gravity = android.view.Gravity.CENTER }
        )

        statusView = TextView(this).apply {
            textSize = 16f
            setTextColor(0xFF9AA0A6.toInt())
            gravity = android.view.Gravity.CENTER
            text = ""
        }
        content.addView(
            statusView,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT
            ).apply { gravity = android.view.Gravity.BOTTOM }
        )

        root.addView(
            content,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        )

        // 色带：左右各一条，叠在内容之上（内容已留出 padding，不会重叠）
        bandView = LedBandView(this)
        root.addView(
            bandView,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        )

        // 每帧把引擎算出的结果交给色带
        scope.launch {
            while (true) {
                val now = SystemClock.elapsedRealtime()
                // ⚠️ render() 必须**只调一次**：它内部会推进过期清理与状态机，
                // 调两次会算出两套可能不一致的结果（第二套的时间基准更晚）。
                val frame = ledEngine.render(now)
                bandView.leftBand = frame.left
                bandView.rightBand = frame.right
                delay(33)   // ≈30fps，够用且省电（对齐固件 rgb_task 的节奏量级）
            }
        }

        return root
    }

    private fun bandViewWidthPx(): Int =
        LedBandView.defaultBandWidthPx(this)

    private fun hideSystemUi() {
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            )
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUi()
    }

    // ------------------------------------------------------------------ 按键

    /**
     * 按键分发。
     *
     * ⚠️ 音量键**必须返回 false**（让系统继续处理），
     * 否则音量就调不动了 —— 这正是用户要的"保留音量加减功能"。
     *
     * 中间键返回 true 吃掉事件，避免触发系统默认行为。
     *
     * ⚠️ 中间键的键值**已经不是 POWER 了**：实机验证后我们用 keylayout
     * 把它重映射成了 `F1`（见 TopKeys.CONFIRM_KEY_CODES 与
     * docs/lx04/实机验证结果.md）。所以这里"吃掉事件"更多是保险，
     * 真正挡住熄屏/关机的是 keylayout 那一层 —— 应用层是拦不住 POWER 的。
     */
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        val now = SystemClock.elapsedRealtime()

        when (event.keyCode) {
            TopKeys.VOLUME_UP, TopKeys.VOLUME_DOWN -> {
                // 交给系统处理音量（返回 false = 不消费）
                return super.dispatchKeyEvent(event)
            }
        }

        if (!TopKeys.isConfirmKey(event.keyCode)) {
            return super.dispatchKeyEvent(event)
        }

        // 实机排查时这条日志很关键：它能区分"按键根本没到应用"和
        // "到了但判定/灯效不对" —— 两者修的地方完全不同。
        Log.i(
            TAG,
            "确认键 ${if (event.action == KeyEvent.ACTION_DOWN) "DOWN" else "UP"} " +
                "code=${event.keyCode} repeat=${event.repeatCount}"
        )

        when (event.action) {
            KeyEvent.ACTION_DOWN -> {
                if (keyDetector.onDown(now)) {
                    // 新的按压开始 ⇒ 灯效开始渐变
                    ledEngine.buttonPress()
                }
                return true
            }

            KeyEvent.ACTION_UP -> {
                when (keyDetector.onUp(now)) {
                    ConfirmKeyDetector.ReleaseResult.Confirmed -> {
                        // ⚠️ 顺序要紧：**先 release 归位，再 confirmed 出结果**。
                        // release() 负责清掉"按住中"的状态（pressAt）并标记本轮已判定；
                        // confirmed() 负责出结果闪烁（天蓝闪 3 次）。
                        // 少调 release() 的话，灯效引擎里 pressAt 残留，
                        // 色带会卡在天青渐变态不熄 —— 老人看着就是"灯坏了"。
                        ledEngine.buttonRelease()
                        // 长按达标 ⇒ 天蓝闪 3 次
                        ledEngine.buttonConfirmed()
                        onConfirmPressed()
                    }

                    ConfirmKeyDetector.ReleaseResult.ReleasedEarly -> {
                        // 中途松开 ⇒ 粉红闪 5 次
                        ledEngine.buttonRelease()
                    }

                    ConfirmKeyDetector.ReleaseResult.Debounced -> {
                        // 抖动：静默（固件的 300ms 兜底）
                        ledEngine.buttonRelease()
                    }

                    ConfirmKeyDetector.ReleaseResult.None -> Unit
                }
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    /**
     * 长按拦截。
     *
     * 历史上这一段是**为了防关机**：中间键原本上报 `KEYCODE_POWER`，
     * 系统长按 1 秒会弹关机菜单，老人按服药键就能把音箱关了。
     *
     * 现在中间键已被 keylayout 重映射为 `F1`（实机验证过，见
     * docs/lx04/实机验证结果.md），长按 F1 本来就没有系统语义。
     * 这里仍然拦一下，是为了兜住"keylayout 没刷成功"的退化情况 ——
     * 那种情况下中间键还是 POWER，至少别让它弹关机菜单。
     */
    override fun onKeyLongPress(keyCode: Int, event: KeyEvent): Boolean {
        if (TopKeys.isConfirmKey(keyCode)) {
            // 自己处理长按（进度条已由 ACTION_DOWN 起的计时驱动）
            return true
        }
        return super.onKeyLongPress(keyCode, event)
    }

    /**
     * 长按达标的回调。
     *
     * 语义（固件 `elder_input.c`）：
     *   · 提醒响铃期间长按 ≥3s ⇒ 直接确认「已服药」
     *   · 不在响铃时 ⇒ 开一个 30 秒确认窗口，老人再短按一次确认
     */
    private fun onConfirmPressed() {
        if (hasPendingReminder) {
            // 有待确认提醒 ⇒ 直接确认
            sendConfirmToServer()
        } else {
            // 没有 ⇒ 开 30 秒窗口
            confirmWindowUntil = SystemClock.elapsedRealtime() + ConfirmKeyDetector.CONFIRM_WINDOW_MS
            showStatus("已长按，请在 30 秒内再按一下确认")
        }
    }

    /** 把「已服药」的确认事件发给服务端。 */
    private fun sendConfirmToServer() {
        // TODO(阶段1): 通过 WebSocket 发送确认帧
        hasPendingReminder = false
        ledEngine.medDone()
        ledEngine.buttonPending(false)
        showStatus("已确认服药")
    }

    /**
     * 服务端下发提醒时调用。
     */
    fun onReminderPending(pending: Boolean) {
        hasPendingReminder = pending
        ledEngine.buttonPending(pending)
        if (pending) {
            ledEngine.medPending()
        }
    }

    // ------------------------------------------------------------------ 权限

    private fun requestMicPermission() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            onMicPermissionGranted()
            return
        }
        requestPermissions(
            arrayOf(Manifest.permission.RECORD_AUDIO),
            REQ_MIC
        )
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_MIC) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                onMicPermissionGranted()
            } else {
                // 没有麦克风权限就做不了唤醒 —— 必须让用户知道
                showStatus("未授予麦克风权限，语音唤醒不可用")
            }
        }
    }

    private fun onMicPermissionGranted() {
        // TODO(阶段1): 启动 AudioRecord 采集 + 唤醒词检测 + WebSocket
        showStatus("")
    }

    // ------------------------------------------------------------------ 其他

    private fun showStatus(text: String) {
        statusView.text = text
    }

    /** 设置字幕（服务端的 TTS / STT 文本）。 */
    fun setSubtitle(text: String) {
        subtitleView.text = text
    }

    /** 设置语音链路的 UI 态（对应固件的 `elder_rgb_set_ui_state`）。 */
    fun setVoiceState(state: Led.UiState) {
        ledEngine.setUiState(state)
    }

    override fun onDestroy() {
        super.onDestroy()
        runCatching { unregisterReceiver(debugReceiver) }
        scope.cancel()
    }

    // ------------------------------------------------------------------ 调试入口

    /**
     * 调试广播（见 [DebugBridge]）。
     *
     * 默认不响应：只有 `settings put global elder_debug 1` 之后才生效。
     */
    private val debugReceiver = object : android.content.BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (!DebugBridge.enabled(this@MainActivity)) {
                Log.w(TAG, "调试命令被忽略 —— 先执行 settings put global elder_debug 1")
                return
            }
            val cmd = intent.getStringExtra("cmd").orEmpty()
            Log.i(TAG, "调试命令: $cmd")
            handleDebugCommand(cmd)
        }
    }

    /**
     * 执行一条调试命令。
     *
     * 存在的理由：这些状态平时到不了 —— 服药提醒要等服务端下发，
     * 「按住 3 秒」用 `input keyevent` 模拟不出来（它同一毫秒就发 UP，
     * 只会走抖动分支）。没有这个入口，灯效就只能靠等真实事件来验。
     */
    private fun handleDebugCommand(cmd: String) {
        when (cmd) {
            // ---- 服药语义 ----
            "med_pending" -> {
                onReminderPending(true)
                showStatus("调试：服药未确认")
            }
            "med_done" -> {
                sendConfirmToServer()
            }
            "clear" -> {
                onReminderPending(false)
                showStatus("调试：已清空提醒")
            }
            "escalate" -> {
                ledEngine.medEscalate()
                showStatus("调试：漏服升级（快闪）")
            }

            // ---- 按键灯效（不需要真的按住 3 秒）----
            "press" -> {
                // 开始按住：右带进入天青渐变
                ledEngine.buttonPending(true)
                ledEngine.buttonPress()
                showStatus("调试：按住中（渐变）")
            }
            "confirmed" -> {
                // 达标：天蓝闪 3 次
                ledEngine.buttonPending(true)
                ledEngine.buttonConfirmed()
                showStatus("调试：确认成功（天蓝闪 3 次）")
            }
            "fail" -> {
                // 中途松开：粉红闪 5 次。用显式时间戳模拟"按了 1.5 秒"，
                // 不需要真的等 —— buttonPress/Release 都收时间戳。
                val t = SystemClock.elapsedRealtime()
                ledEngine.buttonPending(true)
                ledEngine.buttonPress(t)
                ledEngine.buttonRelease(t + 1500L)
                showStatus("调试：中途松开（粉红闪 5 次）")
            }
            "ack" -> {
                ledEngine.buttonAcknowledged()
                showStatus("调试：按压已受理")
            }

            // ---- 提醒 nag 节奏 ----
            "nag_start" -> {
                ledEngine.nagStart()
                showStatus("调试：提醒 nag 开始（每 10 分钟前 10 秒红闪）")
            }
            "nag_stop" -> {
                ledEngine.nagStop()
                showStatus("调试：提醒 nag 停止")
            }

            // ---- 状态灯开关 ----
            "status_off" -> {
                ledEngine.setStatusLight(false)
                showStatus("调试：状态灯已关（事件灯不受影响）")
            }
            "status_on" -> {
                ledEngine.setStatusLight(true)
                showStatus("调试：状态灯已开")
            }

            // ---- 对话四态 / 表情 ----
            "idle" -> setVoiceState(Led.UiState.IDLE)
            "listening" -> setVoiceState(Led.UiState.LISTENING)
            "speaking" -> setVoiceState(Led.UiState.SPEAKING)
            "connecting" -> setVoiceState(Led.UiState.CONNECTING)
            "happy" -> ledEngine.setEmotion(Led.Emotion.HAPPY)
            "sad" -> ledEngine.setEmotion(Led.Emotion.SAD)
            "angry" -> ledEngine.setEmotion(Led.Emotion.ANGRY)

            else -> {
                Log.w(TAG, "未知调试命令: $cmd")
                showStatus("未知调试命令：$cmd")
            }
        }
    }

    companion object {
        private const val REQ_MIC = 1001
        private const val TAG = "ElderLX04"
    }
}
