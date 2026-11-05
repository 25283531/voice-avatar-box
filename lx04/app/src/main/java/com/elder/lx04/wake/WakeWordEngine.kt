package com.elder.lx04.wake

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.elder.lx04.audio.AudioCapture
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig

/**
 * 离线语音唤醒 —— 对齐 ESP32-S3 固件的 ESP-SR「你好小智」。
 *
 * ## 为什么这样设计
 *
 * 固件侧：`EnableWakeWordDetection(true)` 后 WakeNet 常驻跑在**同一条**音频链路上，
 * 检出唤醒词才切到 ASR。LX04 上我们没有板级音频框架可以共用句柄，而
 * `AudioRecord` 在这台 HAL 上**不允许两个实例同时打开**（实机日志
 * `startInputDevice(), input device already opened`）。所以这里必须做成
 * 「唤醒采集」与「对话采集」**互斥、分时复用同一个麦克风**：
 *
 * ```
 *   待机 ──(常驻 KWS 采集)──► 检出「你好小智」
 *                              │
 *                              ├─ 立刻停掉 KWS 采集（释放麦克风）
 *                              └─ onDetected() → 服务端 detect → 对话采集
 *   对话结束 ──► resume() 重新打开 KWS 采集
 * ```
 *
 * ⚠️ 顺序不能反：先开对话采集再停 KWS 采集，会撞上 `input device already
 *    opened`，症状是唤醒后一句话都录不进去（而日志里 KWS 明明检出了）。
 *
 * ## 与固件一致的两处语义
 *
 * 1. **唤醒词检出要发 `listen detect`**：服务端 `standby` 之后有 180 秒冷却，
 *    期间丢弃上行音频、忽略 listen start；只有 `detect` 能解除。固件
 *    `application.cc` 正是靠 `Idle → Listening` 这次跃迁发 detect。
 *    不发 detect 的症状是「喊了没反应」，而且 180 秒内一直没反应。
 * 2. **播报时不跑唤醒**：半双工，麦克风会把 TTS 自己录进去（LX04 的 HAL
 *    在录音路径上有回声消除，实机结论是「设备扬声器的声音麦克风采不到」，
 *    但采集仍会占用句柄并产生近零帧）。由 MainActivity 在 speaking 期间
 *    调 [pause]，播报结束再 [resume]。
 *
 * ## 版本锁
 *
 * `app/src/main/java/com/k2fsa/sherpa/onnx/` 里的 Kotlin API 与
 * `tools/fetch_kws_assets.py` 里的 `.so`/模型**必须同版本**（当前 1.12.22）。
 * 详见 fetch_kws_assets.py 的说明。
 */
class WakeWordEngine(
    private val context: Context,
    /** 检出唤醒词。回调在**主线程**，且此时本引擎已释放麦克风。 */
    private val onDetected: (keyword: String) -> Unit
) {

    /** 引擎状态。 */
    enum class State {
        /** 未初始化 / 资源缺失。 */
        IDLE,

        /** 常驻监听唤醒词。 */
        LISTENING,

        /** 已暂停（对话中 / 播报中 / 待机冷却中），麦克风已让出。 */
        PAUSED,

        /** 已释放，不可再用。 */
        RELEASED
    }

    @Volatile
    var state: State = State.IDLE
        private set

    private val main = Handler(Looper.getMainLooper())

    private var spotter: KeywordSpotter? = null
    private var stream: OnlineStream? = null
    private var capture: AudioCapture? = null

    /** 检出后的一小段静默期，避免同一句话连续触发两次。 */
    @Volatile
    private var lastDetectedAt = 0L

    /** 资源是否可用（so / 模型 / JNI 是否加载成功）。 */
    var available: Boolean = false
        private set

    var lastError: String = ""
        private set

    /**
     * 初始化 JNI 引擎并开始常驻监听。
     *
     * @return true = 已开始监听；false = 资源缺失或加载失败（此时设备退化为
     *         「按键唤醒」，[lastError] 有原因，**必须打到日志里** —— 静默
     *         降级会让"没有语音唤醒"变成无法追查的现象）。
     */
    fun start(): Boolean {
        if (state == State.RELEASED) return false
        if (state == State.LISTENING) return true

        if (!ensureSpotter()) return false

        // 每次开始一轮监听都重置 stream：上一轮对话里的语音不该留在这个
        // 流式解码器里，否则恢复监听后可能立刻"再检出一次"。
        runCatching { stream?.release() }
        stream = runCatching { spotter!!.createStream() }.getOrElse {
            lastError = "createStream 失败: ${it.message}"
            Log.e(TAG, lastError, it)
            return false
        }

        return startCapture()
    }

    /** 建 JNI 引擎（只建一次；模型加载较耗时，别每次唤醒都重载）。 */
    private fun ensureSpotter(): Boolean {
        spotter?.let { return true }
        return runCatching {
            val cfg = KeywordSpotterConfig(
                featConfig = FeatureConfig(
                    sampleRate = AudioCapture.SAMPLE_RATE,
                    featureDim = 80
                ),
                modelConfig = OnlineModelConfig(
                    transducer = OnlineTransducerModelConfig(
                        encoder = "$ASSET_DIR/$ENCODER",
                        decoder = "$ASSET_DIR/$DECODER",
                        joiner = "$ASSET_DIR/$JOINER"
                    ),
                    tokens = "$ASSET_DIR/tokens.txt",
                    modelType = "zipformer2",
                    numThreads = 1,
                    debug = false
                ),
                keywordsFile = "$ASSET_DIR/keywords.txt",
                // 固件的唤醒词只有一个，阈值略收紧一点减少电视/收音机误触发
                keywordsThreshold = 0.30f,
                keywordsScore = 1.5f
            )
            spotter = KeywordSpotter(assetManager = context.assets, config = cfg)
            available = true
            lastError = ""
            Log.i(TAG, "离线唤醒引擎就绪：$WAKE_WORD")
            true
        }.getOrElse {
            lastError = "唤醒引擎加载失败（so/模型缺失或版本不匹配）: ${it.message}"
            Log.e(TAG, lastError, it)
            available = false
            false
        }
    }

    /** 启动常驻采集。 */
    private fun startCapture(): Boolean {
        val cap = AudioCapture { pcm, _ ->
            feed(pcm)
        }
        capture = cap
        if (!cap.start()) {
            capture = null
            state = State.IDLE
            lastError = "唤醒采集打不开（麦克风被占用？）"
            Log.e(TAG, lastError)
            return false
        }
        state = State.LISTENING
        return true
    }

    /**
     * 送一帧给 KWS。跑在**采集线程**，不能碰 UI。
     *
     * `AudioCapture` 已替我们处理了「请求 mono、HAL 回交错双声道」这个坑，
     * 拿到的 `pcm` 是抽过左声道的 16k 单声道 960 short（60ms）。
     */
    private fun feed(pcm: ShortArray) {
        if (state != State.LISTENING) return
        val s = stream ?: return
        val kws = spotter ?: return

        // sherpa-onnx 要归一化 float；Short → [-1, 1)
        val samples = FloatArray(pcm.size) { pcm[it] / 32768.0f }
        s.acceptWaveform(samples, AudioCapture.SAMPLE_RATE)

        var keyword: String? = null
        while (kws.isReady(s)) {
            kws.decode(s)
            val text = kws.getResult(s).keyword
            if (text.isNotBlank()) {
                // 官方 Demo 明确要求：检出后立刻 reset stream，
                // 否则同一句话会被反复检出。
                kws.reset(s)
                keyword = text
            }
        }

        if (keyword != null) fire(keyword)
    }

    /** 检出处理：先释放麦克风，再回调（顺序见类注释）。 */
    private fun fire(keyword: String) {
        val now = android.os.SystemClock.elapsedRealtime()
        if (now - lastDetectedAt < REFIRE_GUARD_MS) return
        lastDetectedAt = now

        // ⚠️ 必须在回调之前把 AudioRecord 关掉：
        //    MainActivity 会在 onDetected 里立刻创建对话采集，
        //    两个采集并存会撞上 HAL 的 "input device already opened"。
        stopCaptureOnly()
        state = State.PAUSED

        main.post {
            Log.i(TAG, "唤醒词检出：$keyword（麦克风已让出）")
            onDetected(keyword)
        }
    }

    /**
     * 暂停（对话/播报期间）。
     *
     * 与 [start] 的区别：保留 JNI 引擎与模型，只是关掉采集句柄，
     * 恢复时不必重新加载 13 MB 模型。
     */
    fun pause() {
        if (state == State.RELEASED) return
        stopCaptureOnly()
        state = State.PAUSED
    }

    /** 恢复常驻监听（对话结束 / 播报结束 / 待机解除后调用）。 */
    fun resume(): Boolean {
        if (state == State.RELEASED) return false
        if (state == State.LISTENING) return true
        if (!ensureSpotter()) return false
        runCatching { stream?.release() }
        stream = runCatching { spotter!!.createStream() }.getOrElse {
            lastError = "createStream 失败: ${it.message}"
            Log.e(TAG, lastError)
            return false
        }
        return startCapture()
    }

    private fun stopCaptureOnly() {
        runCatching { capture?.stop() }
        capture = null
    }

    /** 彻底释放（Activity onDestroy）。 */
    fun release() {
        stopCaptureOnly()
        runCatching { stream?.release() }
        runCatching { spotter?.release() }
        stream = null
        spotter = null
        state = State.RELEASED
        available = false
    }

    companion object {
        private const val TAG = "ElderWake"

        /** 与 ESP32-S3 固件逐字一致（`CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS`）。 */
        const val WAKE_WORD = "你好小智"

        /** assets 里的模型目录（由 tools/fetch_kws_assets.py 生成）。 */
        private const val ASSET_DIR = "kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
        private const val ENCODER = "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"
        private const val DECODER = "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"
        private const val JOINER = "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"

        /** 两次唤醒之间的最小间隔。 */
        private const val REFIRE_GUARD_MS = 1_500L
    }
}
