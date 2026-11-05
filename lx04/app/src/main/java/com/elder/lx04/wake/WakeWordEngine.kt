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
 * 检出唤醒词才切到 ASR。LX04 上 `AudioRecord` 在这台 MTK HAL 上**不允许
 * 两个实例同时打开**，而且释放有延迟（项目 tryMicCase 自测：立刻开下一组
 * 必然撞 `input device already opened`，那一组读到全零，须等 1500ms）。
 *
 * ⇒ 所以**复用同一路 AudioRecord**，只切帧消费者，全会话零 stop/start：
 *
 * ```
 *   待机 ──(KWS 采集 / consumer=feed)──► 检出「你好小智」
 *                              │
 *                              ├─ fire(): 只翻 state=PAUSED，不释放麦克风
 *                              └─ onDetected() → detect/start → takeOver(upstream)
 *   对话态 ──(同一路采集 / consumer=上行编码)──► 播报时继续推流
 *   对话结束 ──► giveBack(): consumer=feed + 重建 stream，不重启 AudioRecord
 * ```
 *
 * 2026-11-06 真机实测：旧实现每次唤醒都 stop/start，HAL 报
 * `startInputDevice(), input device already opened` / `[readThread] failed`，
 * 上行 21 帧里 18 帧是静音、ASR 空；改用共享采集后这些全部消失。
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

    /**
     * 帧已被对话链路接管（共享采集模式）。
     *
     * 此标志为真时 [pause] / [resume] **绝不能** stop/start AudioRecord ——
     * 播报期间设备上行还要继续推流（服务端 `_feed_barge_in` 用它判抢话打断），
     * 一掐就回到「唤醒后说话没响应」。
     */
    @Volatile
    private var shared = false


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

    /** 检出处理：**只翻状态，不释放麦克风**（顺序见类注释）。 */
    private fun fire(keyword: String) {
        val now = android.os.SystemClock.elapsedRealtime()
        if (now - lastDetectedAt < REFIRE_GUARD_MS) return
        lastDetectedAt = now

        // ⚠️ 2026-11-06：这里原来 stopCaptureOnly()，MainActivity 再新建
        //    一路对话采集。MTK HAL 释放有延迟，实测每次唤醒都撞
        //      W/AudioALSAHardwareResourceManager: startInputDevice(), input device already opened
        //      E/AudioALSACaptureDataProviderNormal: [readThread] failed, errno: 1
        //    ⇒ 新采集读到全零 ⇒ 服务端 21 帧里 18 帧静音、ASR 空。
        //    现在改为**复用这一路 AudioRecord**：只把 state 翻成 PAUSED 让
        //    feed() 停止 decode，等 MainActivity 调 takeOver() 换消费者。
        state = State.PAUSED

        main.post {
            Log.i(TAG, "唤醒词检出：$keyword（复用采集，未释放麦克风）")
            onDetected(keyword)
        }
    }

    /**
     * 把共享采集的帧消费者交给对话链路（唤醒词路径）。
     *
     * @return false = KWS 采集不在，调用方需降级。
     */
    fun takeOver(upstream: (ShortArray, com.elder.lx04.audio.AudioPreProcess.Frame) -> Unit): Boolean {
        val cap = capture ?: return false
        if (state == State.RELEASED) return false
        cap.setConsumer(upstream)
        shared = true
        state = State.PAUSED       // feed() 会 return；采集线程继续跑
        Log.i(TAG, "共享采集已接管：帧改送上行链路（AudioRecord 未重启）")
        return true
    }

    /**
     * 把消费者切回唤醒词检测，并重置流式解码器。
     *
     * ⚠️ 重置 stream 是必需的：对话期间那些语音还留在 KWS 解码器里，
     *    不重置会在恢复监听的瞬间「再检出一次」，凭空多一轮唤醒应答。
     * ⚠️ 不 stop/start AudioRecord —— 全会话零 HAL 交接。
     */
    fun giveBack() {
        val cap = capture ?: return
        cap.setConsumer { pcm, _ -> feed(pcm) }
        shared = false
        runCatching { stream?.release() }
        stream = runCatching { spotter?.createStream() }.getOrNull()
        // 刚切回时别把上一句的尾巴再检出一次
        lastDetectedAt = android.os.SystemClock.elapsedRealtime()
        state = State.LISTENING
        Log.i(TAG, "共享采集已交还：帧改送唤醒词检测（AudioRecord 未重启）")
    }

    /**
     * 暂停（对话/播报期间）。
     *
     * 与 [start] 的区别：保留 JNI 引擎与模型，只是关掉采集句柄，
     * 恢复时不必重新加载 13 MB 模型。
     */
    fun pause() {
        if (state == State.RELEASED) return
        // ⚠️ 共享采集期间 TTS start 也会调到这里。若照旧 stopCaptureOnly()，
        //    播报一开始就把上行采集掐掉，服务端 _feed_barge_in 收不到帧，
        //    而且播报结束还得重新开麦 —— 又一次 HAL 交接。
        if (shared) {
            state = State.PAUSED
            return
        }
        stopCaptureOnly()
        state = State.PAUSED
    }

    /** 恢复常驻监听（对话结束 / 播报结束 / 待机解除后调用）。 */
    fun resume(): Boolean {
        if (state == State.RELEASED) return false
        if (state == State.LISTENING) return true
        // 共享采集还活着：恢复监听由 giveBack() 统一负责，这里绝不能
        // startCapture() 新建第二个 AudioRecord。
        if (shared) return true
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
