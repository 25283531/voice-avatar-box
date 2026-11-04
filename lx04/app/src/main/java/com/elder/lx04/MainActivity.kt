package com.elder.lx04

import android.Manifest
import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.BatteryManager
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import com.elder.lx04.audio.AudioCapture
import com.elder.lx04.audio.AudioPlayer
import com.elder.lx04.audio.OpusCodec
import com.elder.lx04.input.ConfirmKeyDetector
import com.elder.lx04.input.TopKeys
import com.elder.lx04.light.Led
import com.elder.lx04.light.LedBandView
import com.elder.lx04.light.LedEngine
import com.elder.lx04.net.DeviceIdentity
import com.elder.lx04.net.OtaClient
import com.elder.lx04.net.VoiceClient
import com.elder.lx04.ui.MainScreen
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * LX04 设备端主界面 —— 与服务端对接的完整客户端。
 *
 * ## 一屏之内要完成的事
 *
 * 1. **和服务端对接**：OTA 拿 token → WebSocket → 语音上行/下行
 * 2. **界面与固件一致**：状态栏 / 表情 / 字幕 / 提醒页 / 激活页
 * 3. **顶部三键**：音量± 保留，中间键 = 按住说话（也兼服药确认）
 * 4. **两侧色带**：状态灯与事件灯搬到屏幕上（[LedEngine] 1:1 移植自固件）
 *
 * ## 一轮对话的完整时序（这是整份代码的主线）
 *
 * ```
 * 按下中间键
 *   → voice.listenStart()         服务端进入 listening，起静音看门狗
 *   → capture.start()             AudioRecord 16k + 前处理（降噪/增益/VAD）
 *   → opus.encode() 每 60ms 一帧 → WS 二进制帧（4 字节头 + Opus）
 * 松手 / VAD 判到静音 1.2 秒
 *   → voice.listenStop()          服务端开始 ASR → LLM → TTS
 *   → capture.stop()
 * 服务端回：
 *   → {"type":"stt"}                          显示"老人说了什么"
 *   → {"type":"tts","state":"sentence_start"} 显示字幕 + 表情切 happy
 *   → 二进制 Opus 帧                          opus.decode() → AudioTrack
 *   → {"type":"tts","state":"stop"}           回到待机
 * ```
 *
 * ## 为什么是"按一下说话"而不是一直听
 *
 * 唤醒词（sherpa-onnx，已确认有 armeabi-v7a 的 .so）还没接。
 * 在它接上之前，用中间键触发是最稳的 —— 而且对老人来说
 * "按一下再说"比"记住一句唤醒词"更不容易失败。
 * 唤醒词接上之后这条路径仍然保留：按键永远可用，不依赖模型。
 */
class MainActivity : Activity() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    // ---- 界面与灯效 ----
    private lateinit var screen: MainScreen
    private lateinit var bandView: LedBandView
    private lateinit var ledEngine: LedEngine

    // ---- 语音链路 ----
    private var voice: VoiceClient? = null
    private val opus = OpusCodec()
    private var capture: AudioCapture? = null
    private val player = AudioPlayer()

    /** 正在把麦克风数据往上送。 */
    @Volatile
    private var sending = false

    /** 连续静音帧数（60ms 一帧）。 */
    @Volatile
    private var silentFrames = 0

    /** 正在播报（此时不开麦克风，半双工）。 */
    @Volatile
    private var speaking = false

    private val keyDetector = ConfirmKeyDetector()

    /** 是否有待确认的提醒（由服务端事件驱动）。 */
    @Volatile
    private var hasPendingReminder = false

    /** 确认窗口到期时刻（elapsedRealtime）；0 = 没有开着的窗口。 */
    private var confirmWindowUntil = 0L

    /** 是否已经绑定（OTA 拿到 websocket 段）。 */
    @Volatile
    private var bound = false

    private var batteryPercent = 0
    private var batteryCharging = false
    private var wifiOk = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // ⚠️ 必须全屏：色带画在屏幕最边缘，系统 UI 会压住它
        window.addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        hideSystemUi()

        // ⚠️ 顺序不能动：ledEngine 必须在 setContentView 之前建好。
        //    buildRoot() 里那个 scope.launch 跑在 Dispatchers.Main.immediate 上，
        //    也就是**当场同步执行**，第一句就是 ledEngine.render(now)。
        //    如果还按"先 setContentView 再 new LedEngine"的顺序，
        //    这里会抛 UninitializedPropertyAccessException，一开机就崩。
        ledEngine = LedEngine()
        ledEngine.defaultConfig()
        ledEngine.setUiState(Led.UiState.IDLE)

        screen = MainScreen(this)
        setContentView(buildRoot())

        registerReceiver(batteryReceiver, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        registerReceiver(debugReceiver, IntentFilter(DebugBridge.ACTION))

        startClock()
        requestMicPermission()
    }

    // ------------------------------------------------------------------ 视图

    private fun buildRoot(): View {
        val root = FrameLayout(this)
        root.addView(
            screen.root,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        )

        // 色带叠在最上层（左右各 5mm）
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
                // ⚠️ render() 只能调一次：它内部推进过期清理与状态机
                val frame = ledEngine.render(now)
                bandView.leftBand = frame.left
                bandView.rightBand = frame.right
                delay(33)   // ≈30fps，对齐固件 rgb_task 的节奏
            }
        }
        return root
    }

    private fun startClock() {
        scope.launch {
            val fmt = SimpleDateFormat("HH:mm", Locale.CHINA)
            while (true) {
                screen.setClock(fmt.format(Date()))
                screen.setStatus(wifiOk, batteryPercent, batteryCharging)
                delay(10_000)
            }
        }
    }

    private val batteryReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
            val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
            if (level >= 0 && scale > 0) batteryPercent = level * 100 / scale
            val st = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
            batteryCharging = st == BatteryManager.BATTERY_STATUS_CHARGING ||
                st == BatteryManager.BATTERY_STATUS_FULL
            wifiOk = DeviceIdentity.wifiConnected(this@MainActivity)
        }
    }

    // ------------------------------------------------------------------ 连接

    /**
     * OTA → WebSocket。
     *
     * 未绑定时不是错误：把激活码显示出来，每 5 秒重试一次 ——
     * 家属在手机 App 里输完码，设备这边**自己**就接上了，不用重启。
     */
    private fun startConnect() {
        val deviceId = DeviceIdentity.mac(this)
        val clientId = DeviceIdentity.clientId(this)
        val ota = OtaClient(SERVER_BASE_URL, deviceId, clientId)

        scope.launch {
            while (true) {
                val result = withContext(Dispatchers.IO) { ota.check() }
                when (result) {
                    is OtaClient.Result.Bound -> {
                        bound = true
                        screen.hideActivation()
                        screen.setHint("")
                        connectWs(result.wsUrl, result.token, deviceId, clientId)
                        return@launch
                    }
                    is OtaClient.Result.NeedActivation -> {
                        bound = false
                        setUiState("activating")
                        screen.showActivation(result.code)
                        delay(5_000)
                    }
                    is OtaClient.Result.Failed -> {
                        bound = false
                        setUiState("offline")
                        screen.setHint("连不上服务器：${result.message}")
                        delay(5_000)
                    }
                }
            }
        }
    }

    private fun connectWs(wsUrl: String, token: String, deviceId: String, clientId: String) {
        val client = VoiceClient(object : VoiceClient.Listener {

            override fun onConnected() {
                runOnUiThread {
                    setUiState("idle")
                    screen.setHint("已连接")
                }
            }

            override fun onDisconnected(reason: String) {
                runOnUiThread {
                    setUiState("offline")
                    screen.setHint(reason)
                }
                // 断线自动重连：老人不会来点"重试"
                scope.launch {
                    delay(3_000)
                    if (!isFinishing) startConnect()
                }
            }

            override fun onHello(sampleRate: Int, frameMs: Int) {
                // ⚠️ 采样率必须读 hello，不能写死 24k —— 服务端可用环境变量改
                opus.init(sampleRate)
                player.start(sampleRate)
            }

            override fun onStt(text: String) {
                runOnUiThread { screen.setUserSaid(text) }
            }

            override fun onTtsState(state: String, text: String?) {
                runOnUiThread {
                    when (state) {
                        "start" -> {
                            speaking = true
                            setUiState("speaking")
                        }
                        "sentence_start" -> {
                            if (!text.isNullOrBlank()) screen.setDeviceSaid(text)
                        }
                        "stop" -> {
                            speaking = false
                            setUiState("idle")
                        }
                    }
                }
            }

            override fun onEmotion(emotion: String) {
                runOnUiThread {
                    // 表情与灯效是两条独立通道（固件也是这么分的）：
                    // 表情走 21 个词，灯效只认四态。
                    screen.setEmoji(emotion)
                }
            }

            override fun onAlert(message: String) {
                runOnUiThread { screen.setHint(message) }
            }

            override fun onAudio(opusPacket: ByteArray) {
                val pcm = ShortArray(opus.decodeFrameSamples)
                val n = opus.decode(opusPacket, pcm)
                if (n > 0) player.enqueue(pcm.copyOf(n))
            }
        })

        if (client.connect(wsUrl, token, deviceId, clientId)) {
            voice = client
        } else {
            screen.setHint("WebSocket 建立失败")
        }
    }

    // ------------------------------------------------------------------ 一轮对话

    /** 开始说话（按下中间键 / 调试命令触发）。 */
    private fun beginListening() {
        if (sending) return
        if (!bound || voice?.isConnected != true) {
            screen.setHint("还没连上服务器，稍等一下")
            return
        }
        // 半双工：正在播报就先打断，否则麦克风会把 TTS 自己录进去
        if (speaking) {
            voice?.abort()
            player.flush()
            speaking = false
        }

        sending = true
        silentFrames = 0
        screen.setUserSaid("")
        screen.setDeviceSaid("")
        setUiState("listening")

        voice?.listenStart()

        val cap = AudioCapture { pcm, info ->
            if (!sending) return@AudioCapture

            // 上行编码（16k / 60ms / 960 采样）
            val enc = opus.encode(pcm) ?: return@AudioCapture
            voice?.sendAudio(enc)

            // VAD 自动收尾：连续静音 1.2 秒算说完。
            // 不"一静音就停"是因为老人说话中间会换气，
            // 一停就掐断会把一句话切成两半，服务端 ASR 两半都识别不出来。
            if (info.isSpeech) silentFrames = 0 else silentFrames++
            if (silentFrames * AudioCapture.FRAME_MS >= SILENCE_STOP_MS) {
                endListening()
            }
        }
        capture = cap
        if (!cap.start()) {
            sending = false
            setUiState("idle")
            screen.setHint("麦克风打不开（可能被小爱占用）")
        }
    }

    /** 说完了。 */
    private fun endListening() {
        if (!sending) return
        sending = false
        capture?.stop()
        capture = null
        voice?.listenStop()
        setUiState("idle")
        screen.setHint("")
    }

    // ------------------------------------------------------------------ 按键

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

        Log.i(
            TAG,
            "确认键 ${if (event.action == KeyEvent.ACTION_DOWN) "DOWN" else "UP"} " +
                "code=${event.keyCode} repeat=${event.repeatCount}"
        )

        when (event.action) {
            KeyEvent.ACTION_DOWN -> {
                if (keyDetector.onDown(now)) {
                    ledEngine.buttonPress()
                    // 按下即开始说话 —— 对老人来说"按住说话"最直觉
                    beginListening()
                }
                return true
            }

            KeyEvent.ACTION_UP -> {
                when (keyDetector.onUp(now)) {
                    ConfirmKeyDetector.ReleaseResult.Confirmed -> {
                        ledEngine.buttonRelease()
                        ledEngine.buttonConfirmed()
                        onConfirmPressed()
                    }
                    ConfirmKeyDetector.ReleaseResult.ReleasedEarly,
                    ConfirmKeyDetector.ReleaseResult.Debounced -> ledEngine.buttonRelease()
                    ConfirmKeyDetector.ReleaseResult.None -> Unit
                }
                endListening()
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onKeyLongPress(keyCode: Int, event: KeyEvent): Boolean {
        if (TopKeys.isConfirmKey(keyCode)) return true
        return super.onKeyLongPress(keyCode, event)
    }

    /** 长按达标（≥3 秒）—— 服药确认。 */
    private fun onConfirmPressed() {
        if (hasPendingReminder) {
            confirmMedication()
        } else {
            confirmWindowUntil = SystemClock.elapsedRealtime() + ConfirmKeyDetector.CONFIRM_WINDOW_MS
        }
    }

    /** 把「已服药」的确认落地。 */
    private fun confirmMedication() {
        hasPendingReminder = false
        ledEngine.medDone()
        ledEngine.buttonPending(false)
        screen.hideReminder()
        setUiState("confirmed")
        scope.launch {
            delay(2_000)
            setUiState("idle")
        }
        // ⚠️ 服务端目前把"服药确认"建模成设备侧 MCP 工具回包
        //    （session.py 的 call_device_tool）。我们还没实现设备工具通道，
        //    所以这里**只做本地反馈**，不回假的"成功"给服务端 ——
        //    回了假的，家属端会看到"已确认"而实际没记录，这是最坏的一类欺骗。
        screen.setHint("已确认（服务端回写待接入）")
    }

    /** 服务端下发提醒时调用。 */
    fun onReminderPending(pending: Boolean) {
        hasPendingReminder = pending
        ledEngine.buttonPending(pending)
        if (pending) ledEngine.medPending()
    }

    // ------------------------------------------------------------------ 状态

    /** 切界面状态。名字与固件 `elder_ui_state_t` 对齐。 */
    private fun setUiState(state: String) {
        screen.setEmoji(MainScreen.STATE_EMOJI[state] ?: "neutral")

        // 灯效只认四态，与表情是两套独立枚举（固件也是分开的）
        val ledState = when (state) {
            "listening" -> Led.UiState.LISTENING
            "speaking" -> Led.UiState.SPEAKING
            "offline" -> Led.UiState.CONNECTING
            else -> Led.UiState.IDLE
        }
        ledEngine.setUiState(ledState)
    }

    // ------------------------------------------------------------------ 权限

    private fun requestMicPermission() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            startConnect()
            return
        }
        requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_MIC) {
            if (grantResults.isEmpty() || grantResults[0] != PackageManager.PERMISSION_GRANTED) {
                screen.setHint("没有麦克风权限，无法对话")
            }
            // 没麦克风也把连接建起来 —— 至少能收到提醒、能显示字幕
            startConnect()
        }
    }

    // ------------------------------------------------------------------ 麦克风自检

    /**
     * 麦克风自检 —— 不开 WebSocket，纯本地验证"能不能录到声音"。
     *
     * ## 为什么需要它
     *
     * LX04 上常驻着 `MiVpmService`（小爱唤醒，init 服务 `mivpm`），
     * 它一直挂在 `AudioIn_1E` 上录音（`dumpsys media.audio_flinger` 里
     * `Standby: no`）。我们的 AudioRecord 能不能和它**同时**打开，
     * 取决于 MTK HAL 允不允许同源共享 —— 这是整条语音链路上
     * 唯一一个"猜不出来、只能试"的点。
     *
     * 如果打不开，表现为 `AudioRecord` state 不是 INITIALIZED、
     * 或 `startRecording` 后读回来的全是 0。这两种情况在这里都能看出来。
     *
     * ## 怎么读日志
     *
     * 每 10 帧（0.6 秒）打一行：
     *   · `rms` 恒为 0        → 录到的是静音，麦克风没真给我们
     *   · `rms` 有值但很小     → 通路通了，只是环境安静（正常）
     *   · `floor` 跟着 `rms` 走 → 噪声底跟踪在工作
     *   · `speech=true`        → VAD 判到人声（对着音箱说话时）
     *
     * 用法：`adb shell am broadcast -a com.elder.lx04.DEBUG --es cmd mic_test`
     */
    private fun runMicTest() {
        if (sending) {
            Log.w(TAG, "mic_test 忽略 —— 正在对话中")
            return
        }
        screen.setHint("麦克风自检中（5 秒）…")
        var frames = 0
        var speechFrames = 0
        var maxRms = 0f

        val cap = AudioCapture { _, info ->
            frames++
            if (info.isSpeech) speechFrames++
            if (info.rms > maxRms) maxRms = info.rms
            if (frames % 10 == 0) {
                Log.i(
                    TAG,
                    "mic[$frames] rms=${"%.0f".format(info.rms)} " +
                        "floor=${"%.0f".format(info.noiseFloor)} " +
                        "gain=${"%.2f".format(info.gain)} speech=${info.isSpeech}"
                )
            }
        }
        if (!cap.start()) {
            Log.e(TAG, "mic_test: 采集启动失败 —— 麦克风被占用（多半是 MiVpmService）")
            screen.setHint("麦克风打不开：被小爱占用")
            return
        }
        scope.launch {
            delay(5_000)
            cap.stop()
            val verdict = when {
                frames == 0 -> "一帧都没读到 —— 采集链路断了"
                maxRms < 1f -> "读到了 $frames 帧但全是 0 —— 麦克风没真给我们"
                else -> "正常：$frames 帧，语音帧 $speechFrames，峰值 rms=${"%.0f".format(maxRms)}"
            }
            Log.i(TAG, "mic_test 结果：$verdict")
            screen.setHint("自检：$verdict")
        }
    }

    // ------------------------------------------------------------------ 调试入口

    private val debugReceiver = object : BroadcastReceiver() {
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

    private fun handleDebugCommand(cmd: String) {
        when (cmd) {
            "talk" -> beginListening()
            "stop_talk" -> endListening()
            "mic_test" -> runMicTest()

            "med_pending" -> {
                onReminderPending(true)
                setUiState("reminder")
                screen.showReminder("该吃药了", "08:00  高血压药 1 片")
            }
            "med_done" -> confirmMedication()
            "clear" -> {
                onReminderPending(false)
                screen.hideReminder()
                setUiState("idle")
            }
            "escalate" -> ledEngine.medEscalate()

            "press" -> {
                ledEngine.buttonPending(true)
                ledEngine.buttonPress()
            }
            "confirmed" -> {
                ledEngine.buttonPending(true)
                ledEngine.buttonConfirmed()
            }
            "fail" -> {
                val t = SystemClock.elapsedRealtime()
                ledEngine.buttonPending(true)
                ledEngine.buttonPress(t)
                ledEngine.buttonRelease(t + 1500L)
            }
            "ack" -> ledEngine.buttonAcknowledged()

            "nag_start" -> ledEngine.nagStart()
            "nag_stop" -> ledEngine.nagStop()
            "status_off" -> ledEngine.setStatusLight(false)
            "status_on" -> ledEngine.setStatusLight(true)

            "idle" -> setUiState("idle")
            "listening" -> setUiState("listening")
            "speaking" -> setUiState("speaking")
            "happy" -> screen.setEmoji("happy")
            "sad" -> screen.setEmoji("sad")
            "angry" -> screen.setEmoji("angry")

            "reconnect" -> startConnect()

            else -> Log.w(TAG, "未知调试命令: $cmd")
        }
    }

    // ------------------------------------------------------------------ 生命周期

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUi()
    }

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

    override fun onDestroy() {
        super.onDestroy()
        runCatching { unregisterReceiver(debugReceiver) }
        runCatching { unregisterReceiver(batteryReceiver) }
        capture?.stop()
        player.stop()
        opus.release()
        voice?.disconnect()
        scope.cancel()
    }

    companion object {
        private const val TAG = "ElderLX04"
        private const val REQ_MIC = 1001

        /** 服务端根地址（生产环境，IP 直连）。 */
        const val SERVER_BASE_URL = "https://152.136.230.132"

        /**
         * 静音多久算"说完了"。
         *
         * 1.2 秒是权衡：太短会把老人换气的停顿当成说完了（一句话被切成两半，
         * ASR 两半都识别不出来）；太长则每轮都要多等一截，交互变迟钝。
         */
        const val SILENCE_STOP_MS = 1200L
    }
}
