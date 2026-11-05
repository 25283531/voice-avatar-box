package com.elder.lx04

import android.Manifest
import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
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

    /**
     * 本轮是"按住说话"（PTT）。
     *
     * PTT 下**不用** VAD 自动收尾 —— 按多久说多久，松手才结束。
     * 唤醒词/自由聆听那种场景才靠静音判"说完了"。
     */
    @Volatile
    private var pttMode = false

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
    /**
     * 开始聆听。
     *
     * @param ptt true = 按住说话（松手才结束）。
     *
     * 为什么必须区分：VAD 的静音自动收尾是给"唤醒后自由说"用的。
     * 按住说话时如果也套 1.2 秒静音判据，老人从按下键到真正开口
     * （清嗓子、想说什么）稍慢一点，话还没出口就被切断了 ——
     * 表现为"按了没反应"。PTT 语义就是"按多久说多久"。
     */
    private fun beginListening(ptt: Boolean = false) {
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
        pttMode = ptt
        silentFrames = 0
        screen.setUserSaid("")
        screen.setDeviceSaid("")
        setUiState("listening")

        // ⚠️⚠️ 必须**先发 detect 再发 listen start** —— 这是整条链路能不能
        //    活下来的关键一步，顺序反了等于没发。
        //
        //    服务端 `_on_listen` 的两个分支是这样配合的：
        //
        //      · "detect" → 清 `_dismissed_until`（解除 180 秒待机冷却）
        //                   + 刷新 `_last_active_at`（会话空闲锚点）
        //      · "start"  → 先查 `_dismissed_until`，**还在冷却里就直接
        //                   return**（这一轮 listen 被整个丢掉）
        //
        //    而 `_last_active_at` 只有两个刷新点：**有效识别**和 **detect**。
        //    本客户端此前只发 listen start、从不发 detect，于是：
        //
        //      ① 会话空闲超过 120s（VOICE_IDLE_STANDBY_SEC）后，
        //         静音看门狗在 listen start 后 0.2s 内就判"连续 120s 没有
        //         有效内容"→ 进待机 → state 变成 idle；
        //      ② 随后的 listen stop 走 `_finalize()`，那里第一句就是
        //         `if self.state != "listening": return` ⇒ **不做 ASR、
        //         不出 TTS、老人什么都听不到**；
        //      ③ 待机还会拉起 180 秒冷却，期间连 listen start 都被忽略
        //         （现场日志：「退下吧冷却中（还剩 149s），忽略 listen start」）。
        //
        //    实机生产日志（2026-11-05）实测就是这个时序：
        //
        //        10:21:21,280 listen start（mode=auto）
        //        10:21:21,479 进入待机：连续 120s 没有有效内容   ← 199ms
        //        10:21:26,156 listen stop                            ← 无 ASR
        //
        //    固件那边 2026-11-03 就加了同一处理（application.cc:1007-1013），
        //    判据是 **Idle → Listening** 这一次跃迁：播报完自动回 listening
        //    是 Speaking → Listening，不在此列 —— 那条正是冷却要挡的。
        //
        //    本客户端没有"自动回 listening"这条路（只有按键/调试命令会
        //    调 beginListening），所以每次 beginListening 都对应"有人明确
        //    让设备开始听"，发 detect 是正确的。
        voice?.listenDetect("按键唤醒", greet = false)

        voice?.listenStart()

        val cap = AudioCapture { pcm, info ->
            if (!sending) return@AudioCapture

            // 上行编码（16k / 60ms / 960 采样）
            val enc = opus.encode(pcm) ?: return@AudioCapture
            voice?.sendAudio(enc)

            // VAD 自动收尾：连续静音 1.2 秒算说完。
            // 只在**非按住**模式下生效（见上面的 ptt 说明）。
            if (!pttMode) {
                if (info.isSpeech) silentFrames = 0 else silentFrames++
                if (silentFrames * AudioCapture.FRAME_MS >= SILENCE_STOP_MS) {
                    endListening()
                }
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
        pttMode = false
        capture?.stop()
        capture = null
        voice?.listenStop()
        setUiState("idle")
        screen.setHint("")
    }

    // ------------------------------------------------------------------ 按键

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        val now = SystemClock.elapsedRealtime()

        // 调试模式下把**每一个**按下的键原样打出来。
        //
        // 为什么需要这个：实机排查"按住中间键没反应"时，RecentQueue 里只有
        // 音量减（scanCode=114），完全没有中间键的记录。可能是键没上报、
        // 也可能是上报成了别的 keyCode。只打"确认键"的日志在那种情况下
        // 永远是空的，分不清"没按到"还是"映射错了"。
        if (event.action == KeyEvent.ACTION_DOWN && DebugBridge.enabled(this)) {
            Log.i(
                TAG,
                "按键 code=${event.keyCode} scan=${event.scanCode} " +
                    "dev=${event.deviceId} repeat=${event.repeatCount}"
            )
        }

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
                    beginListening(ptt = true)
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
    /**
     * Opus 编码器自检 —— 完全不碰麦克风和网络。
     *
     * ## 为什么需要它
     *
     * 实机上"按住说话"没反应，抓到的日志是 `编码失败: null`，打堆栈后
     * 看到是 Concentus 内部的 `OpusAssert`。但同样的代码在 JVM 单测里
     * 连编 10 帧毫无问题 —— 说明不是库本身，是**设备上的运行时状态**。
     *
     * 分叉只有两种，修法完全不同：
     *
     * ```
     * 正弦也能编出来  → 编码器是好的，坏的是喂进去的麦克风数据
     *                    （数组长度、并发踩踏、采集线程提前退出）
     * 正弦也编不出来  → 编码器实例本身坏了
     *                    （初始化没生效 / 被并发重置 / 参数没设进去）
     * ```
     *
     * 用固定正弦而不是麦克风，就是把"输入"这个变量钉死。
     *
     * 用法：`adb shell am broadcast -a com.elder.lx04.DEBUG --es cmd opus_selftest`
     */
    private fun runOpusSelfTest() {
        if (sending) {
            Log.w(TAG, "opus_selftest 忽略 —— 正在对话中")
            return
        }
        screen.setHint("Opus 自检中…")
        opus.resetDiag()

        scope.launch(Dispatchers.Default) {
            val sb = StringBuilder()
            sb.append("参数 ").append(opus.describe()).append('\n')

            // ---- 1. 固定 440Hz 正弦，连编 5 帧 ----
            val tone = ShortArray(opus.encodeFrameSamples) { i ->
                (8000 * Math.sin(2 * Math.PI * 440 * i / 16_000.0)).toInt().toShort()
            }
            var okTone = 0
            var bytesTone = 0
            repeat(5) {
                val b = opus.encode(tone)
                if (b != null) {
                    okTone++
                    bytesTone += b.size
                }
            }
            sb.append("正弦 5 帧：成功 $okTone，共 $bytesTone 字节\n")
            sb.append("计数：${opus.stats()}\n")

            // ---- 2. 全静音 3 帧（按住键还没开口时就是这个） ----
            var okSilent = 0
            repeat(3) { if (opus.encode(ShortArray(opus.encodeFrameSamples)) != null) okSilent++ }
            sb.append("静音 3 帧：成功 $okSilent\n")

            val verdict = when {
                okTone == 0 -> "编码器坏了（正弦都编不出）"
                okTone < 5 -> "编码器不稳定（正弦 $okTone/5）"
                okSilent < 3 -> "静音帧异常（$okSilent/3），可能是 DTX 相关"
                else -> "编码器正常 —— 问题在麦克风数据"
            }
            sb.append("结论：").append(verdict)

            Log.i(TAG, "opus_selftest\n$sb")
            screen.setHint("Opus：正弦 $okTone/5 静音 $okSilent/3 → $verdict")
        }
    }

    /**
     * 语音链路自检 —— 判定"帧到底出没出客户端"。
     *
     * ## 为什么需要它
     *
     * 症状是"按了没反应"，但链路上有 6 个环节可能断：
     *
     * ```
     * 麦克风 → 前处理 → Opus 编码 → WS 上行 → 服务端待机 → ASR/TTS
     * ```
     *
     * 光看"没声音"分不清是哪一环断了。这个自检**不发网络包**，
     * 只在本地跑完整条上行链：采集 → 前处理 → 编码 → 统计。
     *
     * 怎么读结果：
     *
     * ```
     * 读到 0 帧          → 麦克风/采集这一环（多半被 mivpm 占着）
     * 读到帧但编码 0 帧  → Opus 编码这一环（编码器坏了）
     * 编码 0 帧但失败 0  → 编码器不可用（init 没生效）
     * 全部正常          → 客户端没问题，断点在 WS 上行或服务端
     * ```
     *
     * 用法：`adb shell am broadcast -a com.elder.lx04.DEBUG --es cmd link_test`
     */
    private fun runLinkTest() {
        if (sending) {
            Log.w(TAG, "link_test 忽略 —— 正在对话中")
            return
        }
        screen.setHint("链路自检中（5 秒）…")

        val frames = java.util.concurrent.atomic.AtomicInteger(0)
        val speech = java.util.concurrent.atomic.AtomicInteger(0)
        val encOk = java.util.concurrent.atomic.AtomicInteger(0)
        val encNull = java.util.concurrent.atomic.AtomicInteger(0)
        val encBytes = java.util.concurrent.atomic.AtomicInteger(0)

        val cap = AudioCapture { pcm, _ ->
            frames.incrementAndGet()
            val enc = opus.encode(pcm)
            if (enc == null) encNull.incrementAndGet()
            else {
                encOk.incrementAndGet()
                encBytes.addAndGet(enc.size)
            }
        }
        if (!cap.start()) {
            Log.e(TAG, "link_test: 采集启动失败 —— 麦克风被占用（多半是 MiVpmService）")
            screen.setHint("链路自检失败：麦克风打不开")
            return
        }

        scope.launch {
            delay(5_000)
            cap.stop()

            val f = frames.get()
            val ok = encOk.get()
            val bad = encNull.get()
            val verdict = when {
                f == 0 -> "一帧都没采到 —— 断点在麦克风/采集"
                ok == 0 -> "采到 $f 帧但一帧都没编出来 —— 断点在 Opus 编码"
                bad > 0 -> "编出 $ok/$f 帧，有 $bad 帧失败 —— 编码器不稳定"
                else -> "客户端上行链完好：采 $f 帧 / 编 $ok 帧 / 共 ${encBytes.get()} 字节"
            }
            Log.i(TAG, "link_test：$verdict | ${opus.stats()}")
            screen.setHint(verdict)
        }
    }

    /**
     * Opus 编码探针 —— 在**真机**上把"为什么一帧都编不出来"钉死。
     *
     * ## 为什么需要它
     *
     * 2026-11-05 实机事实（三条互相矛盾，指向真机运行时）：
     *
     *   · `opus_selftest`：合成正弦 5/5、全零静音 3/3 **成功** ⇒ 编码器没坏
     *   · `link_test` / `talk`：真实麦克风帧 **165/165 失败**（Inlines.OpusAssert）
     *   · 本机 JVM 上用**同一份 Concentus** 编码真实录音（含各种电平、
     *     削顶、双声道交错）**全部成功**
     *
     * 光看日志分不清是"效果链把数据改了"还是"ART 上的某种差异"，
     * 所以这个探针把变量一个个拆开：
     *
     *   ① 平台效果链（NS/AGC/AEC）开 / 关 —— 同样的麦克风，两种处理
     *   ② 真实帧 / 归一化后的真实帧 / 全零 / 合成正弦
     *   ③ 直连 Concentus（拿得到真实断言位置）**和** 生产 OpusCodec
     *      （拿得到生产路径的真实行为）
     *
     * 另外把真实帧落盘到 app 私有目录，可以拉回本机 JVM 复现。
     *
     * 用法：`adb shell am broadcast -a com.elder.lx04.DEBUG --es cmd enc_probe`
     */
    private fun runEncProbe() {
        if (sending) {
            Log.w(TAG, "enc_probe 忽略 —— 正在对话中")
            return
        }
        screen.setHint("编码探针中…")

        scope.launch(Dispatchers.Default) {
            val sb = StringBuilder()
            for (fx in listOf(true, false)) {
                val frames = captureProbeFrames(enableEffects = fx, want = 40, timeoutMs = 4000)
                sb.append("── 平台效果链 ${if (fx) "开" else "关"} ──\n")
                if (frames.isEmpty()) {
                    sb.append("  采不到帧（麦克风被占）\n")
                    continue
                }
                val st = frameStats(frames)
                sb.append("  帧数=${frames.size} min=${st.first} max=${st.second} rms=${st.third}\n")
                sb.append(probeConcentus("真实帧", frames))
                sb.append(probeConcentus("归一化×8", frames.map { scaleFrame(it, 8.0) }))
                sb.append(probeConcentus("全零对照", frames.map { ShortArray(it.size) }))
                sb.append(probeConcentus("正弦对照", frames.map { toneFrame(it.size) }))
                sb.append(probeCodec("真实帧(生产 OpusCodec)", frames))
                val name = if (fx) "elder_pcm_fx.bin" else "elder_pcm_nofx.bin"
                sb.append("  落盘：${dumpFrames(frames, name)}\n")
            }
            Log.i(TAG, "enc_probe\n$sb")
            screen.setHint("探针完成，见 logcat")
        }
    }

    /** 采集若干帧真实数据（可选择是否挂平台效果链）。 */
    private fun captureProbeFrames(
        enableEffects: Boolean,
        want: Int,
        timeoutMs: Long
    ): List<ShortArray> {
        val frames = java.util.Collections.synchronizedList(ArrayList<ShortArray>())
        // ⚠️ 第一个参数是 enablePlatformEffects，第二个才是回调 lambda ——
        //    Kotlin 的尾随 lambda 只能绑最后一个参数。
        val cap = AudioCapture(enableEffects) { pcm, _ ->
            if (frames.size < want) frames.add(pcm.copyOf())
        }
        if (!cap.start()) return emptyList()
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (frames.size < want && SystemClock.elapsedRealtime() < deadline) {
            Thread.sleep(50)
        }
        cap.stop()
        return frames.toList()
    }

    /** 帧的数值特征：min、max、rms。 */
    private fun frameStats(frames: List<ShortArray>): Triple<Int, Int, String> {
        var mn = Int.MAX_VALUE
        var mx = Int.MIN_VALUE
        var sum = 0.0
        var n = 0
        for (f in frames) {
            for (v in f) {
                val iv = v.toInt()
                if (iv < mn) mn = iv
                if (iv > mx) mx = iv
                sum += iv.toDouble() * iv
                n++
            }
        }
        return Triple(mn, mx, String.format(Locale.US, "%.1f", Math.sqrt(sum / n)))
    }

    /** 放大并饱和（模拟 AGC 拉满）。 */
    private fun scaleFrame(f: ShortArray, k: Double): ShortArray {
        val out = ShortArray(f.size)
        for (i in f.indices) {
            val v = f[i] * k
            val clipped = if (v > 32767.0) 32767 else if (v < -32768.0) -32768 else v
            out[i] = clipped.toInt().toShort()
        }
        return out
    }

    /** 合成 440Hz 正弦（对照用）。 */
    private fun toneFrame(n: Int): ShortArray {
        val out = ShortArray(n)
        for (i in 0 until n) {
            out[i] = (8000 * Math.sin(2 * Math.PI * 440 * i / 16_000.0)).toInt().toShort()
        }
        return out
    }

    /**
     * 直连 Concentus 编码。
     *
     * 目的是拿到**真实异常**：生产 [OpusCodec] 会把异常兜住（重建 → 重试 →
     * 返回 null），日志里只看得到"失败 165 帧"，看不到断言发生在哪一行。
     */
    private fun probeConcentus(label: String, frames: List<ShortArray>): String {
        val encoder = try {
            org.concentus.OpusEncoder(
                16_000,
                1,
                org.concentus.OpusApplication.OPUS_APPLICATION_VOIP
            ).apply {
                setBitrate(24_000)
                setUseVBR(true)
                setUseDTX(false)
                setComplexity(5)
            }
        } catch (e: Throwable) {
            return "  $label：编码器构造失败 ${e.javaClass.simpleName}: ${e.message}\n"
        }
        val buf = ByteArray(1275)
        var ok = 0
        for (f in frames) {
            try {
                val n = encoder.encode(f, 0, f.size, buf, 0, buf.size)
                if (n > 0) ok++
            } catch (t: Throwable) {
                val top = t.stackTrace.take(3).joinToString(" | ") { it.toString() }
                return "  $label：成功 $ok/${frames.size} 后失败 ${t.javaClass.simpleName}: ${t.message}\n" +
                    "      $top\n"
            }
        }
        return "  $label：成功 $ok/${frames.size}\n"
    }

    /** 走生产 [OpusCodec] —— 看生产路径的真实行为（成功/失败计数）。 */
    private fun probeCodec(label: String, frames: List<ShortArray>): String {
        val codec = OpusCodec()
        codec.init(24_000)
        var ok = 0
        for (f in frames) {
            if (codec.encode(f) != null) ok++
        }
        return "  $label：成功 $ok/${frames.size} | ${codec.stats()}\n"
    }

    /** 把真实帧落盘（app 私有目录），便于拉回本机 JVM 复现。 */
    private fun dumpFrames(frames: List<ShortArray>, name: String): String {
        return try {
            val f = java.io.File(filesDir, name)
            java.io.FileOutputStream(f).use { os ->
                for (fr in frames) {
                    val b = ByteArray(fr.size * 2)
                    for (i in fr.indices) {
                        val v = fr[i].toInt()
                        b[i * 2] = (v and 0xff).toByte()
                        b[i * 2 + 1] = ((v shr 8) and 0xff).toByte()
                    }
                    os.write(b)
                }
            }
            "${f.absolutePath}（${frames.size} 帧）"
        } catch (e: Throwable) {
            "落盘失败：${e.javaClass.simpleName}: ${e.message}"
        }
    }

    /**
     * 麦克风参数矩阵 —— 找出这台设备上**真的能拿到数据**的采集参数。
     *
     * ## 为什么需要它
     *
     * 2026-11-05 实机事实（三条互相咬合的证据）：
     *
     *   · `tinycap` 直连 ALSA（pcm1c）：rms 2270，**有真实信号**
     *   · App 走 AudioFlinger：ALSA `state: PREPARED`（**流没跑起来**），
     *     读到的帧**全是 0**，4 秒只有 4 帧
     *   · AudioFlinger 日志：`startInputDevice(), input device already opened`
     *
     * ⇒ 问题不在 ALSA 层，而在「AudioFlinger 用什么参数打开设备」。
     *   所以把 (音频源 × 采样率 × 通道数) 全试一遍，
     *   看哪一组能拿到**非零**数据；拿到的原始样本会落盘，
     *   拉回本机 JVM 就能复现 Opus 断言。
     *
     * 每组之间**必须等设备释放**（MTK 的 HAL 释放有延迟，
     * 下一组立刻开会撞上 "input device already opened"）。
     *
     * 用法：`adb shell am broadcast -a com.elder.lx04.DEBUG --es cmd mic_matrix`
     */
    private fun runMicMatrix() {
        if (sending) {
            Log.w(TAG, "mic_matrix 忽略 —— 正在对话中")
            return
        }
        screen.setHint("麦克风参数矩阵…")

        scope.launch(Dispatchers.Default) {
            val sb = StringBuilder()
            for (c in MIC_CASES) {
                val r = tryMicCase(c)
                sb.append(
                    String.format(
                        Locale.US,
                        "%-40s frames=%-5d min=%-7d max=%-7d rms=%-10.1f %s%n",
                        c.label, r.frames, r.min, r.max, r.rms, r.note,
                    )
                )
                if (r.rms > 1.0 && r.samples != null) {
                    sb.append("    落盘：${dumpSamples(r.samples, "mic_matrix_${c.label.replace(' ', '_')}.bin")}\n")
                }
            }
            Log.i(TAG, "mic_matrix\n$sb")
            screen.setHint("矩阵完成，见 logcat")
        }
    }

    /** 矩阵里的一组参数。 */
    private data class MicCase(
        val label: String,
        val source: Int,
        val rate: Int,
        val channels: Int,
    )

    /** 矩阵里一组的测量结果。 */
    private data class MicResult(
        val frames: Int,
        val min: Int,
        val max: Int,
        val rms: Double,
        val note: String,
        val samples: ShortArray?,
    )

    /** 直连 AudioRecord 试一组参数，采 2.5 秒。 */
    private fun tryMicCase(c: MicCase): MicResult {
        val mask =
            if (c.channels == 2) AudioFormat.CHANNEL_IN_STEREO else AudioFormat.CHANNEL_IN_MONO
        var rec: AudioRecord? = null
        return try {
            val minBuf = AudioRecord.getMinBufferSize(
                c.rate, mask, AudioFormat.ENCODING_PCM_16BIT
            )
            if (minBuf <= 0) {
                return MicResult(0, 0, 0, 0.0, "getMinBufferSize=$minBuf", null)
            }
            rec = AudioRecord(
                c.source, c.rate, mask, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, c.rate / 5 * 2 * c.channels),
            )
            if (rec.state != AudioRecord.STATE_INITIALIZED) {
                return MicResult(0, 0, 0, 0.0, "未初始化 state=${rec.state}", null)
            }
            rec.startRecording()
            if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                return MicResult(0, 0, 0, 0.0, "startRecording 失败", null)
            }

            val chunk = ShortArray(c.rate / 20)          // 50ms
            val all = java.io.ByteArrayOutputStream()
            var frames = 0
            var mn = Int.MAX_VALUE
            var mx = Int.MIN_VALUE
            var sum = 0.0
            var n = 0
            val deadline = SystemClock.elapsedRealtime() + 2500
            while (SystemClock.elapsedRealtime() < deadline) {
                val got = rec.read(chunk, 0, chunk.size)
                if (got > 0) {
                    frames++
                    val b = ByteArray(got * 2)
                    for (i in 0 until got) {
                        val v = chunk[i].toInt()
                        b[i * 2] = (v and 0xff).toByte()
                        b[i * 2 + 1] = ((v shr 8) and 0xff).toByte()
                        if (v < mn) mn = v
                        if (v > mx) mx = v
                        sum += v.toDouble() * v
                        n++
                    }
                    all.write(b)
                } else {
                    Thread.sleep(20)
                }
            }
            val rms = if (n > 0) Math.sqrt(sum / n) else 0.0
            val samples = ShortArray(n)
            val raw = all.toByteArray()
            for (i in 0 until n) {
                samples[i] = ((raw[i * 2].toInt() and 0xff) or (raw[i * 2 + 1].toInt() shl 8)).toShort()
            }
            MicResult(
                frames,
                if (mn == Int.MAX_VALUE) 0 else mn,
                if (mx == Int.MIN_VALUE) 0 else mx,
                rms,
                "readFrames=$frames",
                samples,
            )
        } catch (t: Throwable) {
            MicResult(0, 0, 0, 0.0, "异常 ${t.javaClass.simpleName}: ${t.message}", null)
        } finally {
            runCatching { rec?.stop() }
            runCatching { rec?.release() }
            // ⚠️ 必须等：MTK 的 HAL 释放有延迟，立刻开下一组会撞上
            //    "input device already opened"，那一组就必然全零。
            Thread.sleep(1500)
        }
    }

    /** 落盘原始样本（app 私有目录），供拉回本机 JVM 复现。 */
    private fun dumpSamples(samples: ShortArray, name: String): String {
        return try {
            val f = java.io.File(filesDir, name)
            java.io.FileOutputStream(f).use { os ->
                val b = ByteArray(samples.size * 2)
                for (i in samples.indices) {
                    val v = samples[i].toInt()
                    b[i * 2] = (v and 0xff).toByte()
                    b[i * 2 + 1] = ((v shr 8) and 0xff).toByte()
                }
                os.write(b)
            }
            "${f.absolutePath}（${samples.size} 样本）"
        } catch (e: Throwable) {
            "落盘失败：${e.javaClass.simpleName}: ${e.message}"
        }
    }

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
            // ⚠️ 前 10 帧（0.6 秒）不计入峰值：麦克风上电有一声爆音，
            //    实测 rms 能到 5000+，会把"这台机器到底能不能录到人声"
            //    这个问题糊成"能" —— 而实际上它只是电源瞬态。
            if (frames > 10 && info.rms > maxRms) maxRms = info.rms
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

    /**
     * 扬声器 → 麦克风 **回环自检**。
     *
     * ## 解决什么问题
     *
     * [runMicTest] 只能证明"能读到数据"。但实测稳态 rms 只有 6~12，
     * 而峰值出现在开录的头 0.5 秒（7056）—— 这有两种完全不同的解释：
     *
     *   a) 房间真的安静，那点峰值是麦克风上电的爆音；
     *   b) 平台的 NS（Noise Suppression）把一切非人声都压掉了。
     *
     * 两者对产品的影响天差地别：a 正常，b 意味着老人说话也可能被压没。
     * 光看数字分不出来，**必须制造一个已知的声音**去听。
     *
     * ## 怎么造声音
     *
     * 用设备自己的扬声器放 1kHz 正弦（幅度 8000 ≈ -12dBFS），
     * 同时用麦克风录。音箱的喇叭和麦克风在同一个腔体里，
     * 耦合足够强，一定能收到。
     *
     * ⚠️ 这次**刻意不挂平台效果**（`enablePlatformEffects = false`）：
     *    平台 AEC 的参考信号正是我们正在播的内容，它会把这段声音
     *    当成回声消掉 —— 挂着 AEC 测回环，等于自己把答案抹掉。
     *
     * ## 怎么读结果
     *
     * ```
     * 静默段 rms ≈ 10   放音段 rms ≈ 几百~几千   → 通路完好
     * 静默段 rms ≈ 10   放音段 rms ≈ 10          → 麦克风通路有问题
     * ```
     *
     * ⚠️ 第一版这里踩了个坑：静默段算的是"开录后第 0~1 秒"的峰值，
     *    而麦克风上电时有一声爆音（实测 rms 5542），于是基线被抬到天上，
     *    放音段的 1159 反而被判成"收不到"。现在分成三段，
     *    **前 600ms 直接丢弃**（上电瞬态），再取基线。
     *
     * 用法：`adb shell am broadcast -a com.elder.lx04.DEBUG --es cmd tone_test`
     */
    private fun runToneTest() {
        if (sending) {
            Log.w(TAG, "tone_test 忽略 —— 正在对话中")
            return
        }
        screen.setHint("回环自检：上电 0.6s → 静音 0.6s → 放音 1.5s…")

        // ⚠️ 不能用 `@Volatile var`：Kotlin 的 @Volatile 只作用于属性（有 backing
        //    field 的），局部变量上写它会直接编译报错。这里跨线程读，用原子量。
        val phase = java.util.concurrent.atomic.AtomicInteger(PHASE_WARMUP)
        var peakQuiet = 0f
        var peakTone = 0f
        var framesQuiet = 0
        var framesTone = 0

        val cap = AudioCapture(
            onFrame = { _, info ->
                when (phase.get()) {
                    PHASE_QUIET -> {
                        framesQuiet++
                        if (info.rms > peakQuiet) peakQuiet = info.rms
                    }
                    PHASE_TONE -> {
                        framesTone++
                        if (info.rms > peakTone) peakTone = info.rms
                    }
                    // PHASE_WARMUP：上电瞬态，**不计入任何统计**
                }
            },
            enablePlatformEffects = false
        )

        if (!cap.start()) {
            screen.setHint("回环自检失败：麦克风打不开")
            return
        }

        scope.launch {
            // ---- 1. 上电瞬态，丢弃 ----
            delay(WARMUP_MS)
            phase.set(PHASE_QUIET)

            // ---- 2. 静音基线 ----
            delay(QUIET_MS)
            val baseline = peakQuiet
            phase.set(PHASE_TONE)

            // ---- 3. 放 1.5 秒 1kHz 正弦，同时录 ----
            val tone = buildTone(SAMPLE_RATE_OUT, ms = TONE_MS, freqHz = 1000.0, amp = 8000)
            val track = createTrack(SAMPLE_RATE_OUT)
            if (track == null) {
                cap.stop()
                screen.setHint("回环自检失败：AudioTrack 打不开")
                return@launch
            }

            withContext(Dispatchers.IO) {
                runCatching {
                    track.play()
                    track.write(tone, 0, tone.size)
                    // 等缓冲放完，否则立刻 release 会截断
                    Thread.sleep(300)
                }
            }
            phase.set(PHASE_DONE)
            runCatching { track.stop() }
            runCatching { track.release() }

            delay(300)
            cap.stop()

            val verdict = when {
                framesQuiet == 0 || framesTone == 0 ->
                    "没读到帧（静音段 $framesQuiet / 放音段 $framesTone）"
                peakTone < baseline * 3 ->
                    "麦克风收不到扬声器 —— 通路有问题（静默峰值 ${f(baseline)}，" +
                        "放音峰值 ${f(peakTone)}）"
                else -> "通路完好：静默峰值 ${f(baseline)} → 放音峰值 ${f(peakTone)}" +
                    "（提升 ${"%.0f".format(peakTone / maxOf(baseline, 1f))} 倍）"
            }
            Log.i(TAG, "tone_test 结果：$verdict")
            screen.setHint("回环：$verdict")
        }
    }

    /** 生成一段正弦 PCM16。 */
    private fun buildTone(sampleRate: Int, ms: Int, freqHz: Double, amp: Int): ShortArray {
        val n = sampleRate * ms / 1000
        val out = ShortArray(n)
        val w = 2.0 * Math.PI * freqHz / sampleRate
        for (i in 0 until n) {
            out[i] = (Math.sin(w * i) * amp).toInt().toShort()
        }
        return out
    }

    /** 建一个 24kHz 单声道流式 AudioTrack（与 TTS 播放同参数）。 */
    private fun createTrack(sampleRate: Int): android.media.AudioTrack? {
        val minBuf = android.media.AudioTrack.getMinBufferSize(
            sampleRate,
            android.media.AudioFormat.CHANNEL_OUT_MONO,
            android.media.AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) return null
        val t = runCatching {
            @Suppress("DEPRECATION")
            android.media.AudioTrack(
                android.media.AudioManager.STREAM_MUSIC,
                sampleRate,
                android.media.AudioFormat.CHANNEL_OUT_MONO,
                android.media.AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, sampleRate), // 1 秒缓冲，够放 1.5 秒的余量
                android.media.AudioTrack.MODE_STREAM
            )
        }.getOrNull()
        return t?.takeIf { it.state == android.media.AudioTrack.STATE_INITIALIZED }
    }

    private fun f(v: Float): String = "%.0f".format(v)

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
            "link_test" -> runLinkTest()
            "enc_probe" -> runEncProbe()
            "mic_matrix" -> runMicMatrix()
            "tone_test" -> runToneTest()
            "opus_selftest" -> runOpusSelfTest()

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

        /**
         * 麦克风参数矩阵 —— (音频源 × 采样率 × 通道数) 全扫一遍。
         *
         * 见 [runMicMatrix] 的说明：ALSA 层是好的（tinycap 直连有信号），
         * 坏的是 AudioFlinger 打开设备后的启动过程。
         */
        private val MIC_CASES = listOf(
            MicCase("VOICE_RECOGNITION 16000 2ch", MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000, 2),
            MicCase("VOICE_RECOGNITION 16000 1ch", MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000, 1),
            MicCase("VOICE_RECOGNITION 48000 2ch", MediaRecorder.AudioSource.VOICE_RECOGNITION, 48000, 2),
            MicCase("VOICE_RECOGNITION 48000 1ch", MediaRecorder.AudioSource.VOICE_RECOGNITION, 48000, 1),
            MicCase("MIC 16000 1ch", MediaRecorder.AudioSource.MIC, 16000, 1),
            MicCase("MIC 16000 2ch", MediaRecorder.AudioSource.MIC, 16000, 2),
            MicCase("MIC 48000 2ch", MediaRecorder.AudioSource.MIC, 48000, 2),
            MicCase("DEFAULT 48000 2ch", MediaRecorder.AudioSource.DEFAULT, 48000, 2),
            MicCase("VOICE_COMMUNICATION 16000 1ch", MediaRecorder.AudioSource.VOICE_COMMUNICATION, 16000, 1),
            MicCase("CAMCORDER 48000 2ch", MediaRecorder.AudioSource.CAMCORDER, 48000, 2),
        )

        /** 服务端根地址（生产环境，IP 直连）。 */
        const val SERVER_BASE_URL = "https://152.136.230.132"

        /**
         * 静音多久算"说完了"。
         *
         * 1.2 秒是权衡：太短会把老人换气的停顿当成说完了（一句话被切成两半，
         * ASR 两半都识别不出来）；太长则每轮都要多等一截，交互变迟钝。
         */
        const val SILENCE_STOP_MS = 1200L

        /**
         * 下行播放采样率（自检用）。
         *
         * 正常对话里这个值由服务端 hello 下发（`opus.init(sampleRate)`），
         * 不能写死；这里只是回环自检没有服务端可问，用一个与 TTS 同参数的默认值。
         */
        private const val SAMPLE_RATE_OUT = 24_000

        /** 回环自检的三段时间。 */
        private const val WARMUP_MS = 600L
        private const val QUIET_MS = 600L
        private const val TONE_MS = 1_500

        /** 回环自检的阶段标记。 */
        private const val PHASE_WARMUP = 0
        private const val PHASE_QUIET = 1
        private const val PHASE_TONE = 2
        private const val PHASE_DONE = 3
    }
}
