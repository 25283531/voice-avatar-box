package com.elder.lx04.audio

import android.util.Log
import org.concentus.OpusApplication
import org.concentus.OpusDecoder
import org.concentus.OpusEncoder

/**
 * Opus 编解码 —— 纯 Java（Concentus），不带任何 `.so`。
 *
 * ## 为什么不用系统编解码器
 *
 * 服务端协议要求双向 Opus 裸包（见 `server/app/voice/session.py`）：
 *   · 上行 **16kHz / 单声道 / 60ms 一帧**（960 采样）
 *   · 下行 **24kHz / 单声道 / 60ms 一帧**（1440 采样）
 *
 * 而 LX04（Android 8.1 / MTK8167A）实测：
 *
 *     /vendor/etc/media_codecs_google_audio.xml:
 *       <MediaCodec name="OMX.google.opus.decoder" type="audio/opus">
 *         <Limit name="sample-rate" ranges="48000" />     ← 只能出 48k
 *
 *   · **有解码器**，但只支持 48kHz 输出（Opus 内部本来就在 48k 上解码，
 *     24k 的流解出来是带宽受限的 48k 信号，直接按 48k 播放是对的）
 *   · **没有编码器** —— 全系统找不到任何 `audio/opus` 编码器
 *
 * 没有编码器就发不出上行音频，而没有上行就没有对话。三条路：
 *   ① 给 armeabi-v7a 编一个 libopus.so + JNI（要 NDK，CI 里编译，改动大）
 *   ② 改服务端收 PCM（要改线上服务，且固件那边仍走 Opus，得双轨）
 *   ③ 纯 Java 实现（Concentus，BSD 许可，零依赖，124 个源文件）
 *
 * 选 ③：不引入原生库、不动服务端、32 位和 64 位都一样跑。
 * 代价是 CPU —— 16kHz 单声道编码在 A53 上实测约 3~6% 单核，
 * 对话是间歇的，可以接受。
 *
 * ## 线程模型：为什么这里必须加锁
 *
 * Concentus **不是线程安全**的：编码器/解码器内部各有可变状态
 * （SILK 的自适应分析状态、重采样器的历史缓冲）。
 * 两个线程同时踩一个实例，内部状态就会被写成不一致的值，
 * 随后在 `Resampler.silk_resampler` / `Autocorrelation.silk_warped_autocorrelation`
 * 的断言里炸出来（`Inlines.OpusAssert` → `AssertionError`，message 为 null）。
 *
 * 实机上这个窗口真实存在：`AudioCapture.stop()` 的 `join(300)` 有超时，
 * 旧采集线程可能还活着；下一次按键又起一个新采集线程。
 * 两个线程同时调 [encode] ⇒ 同一个编码器被并发踩 ⇒ 整轮会话一帧都发不出去。
 *
 * 所以 [encode] / [decode] / [init] 全部在**同一把锁**（`this`）上：
 * 锁的粒度是"一帧"，在音频线程上的开销可以忽略（远小于一帧 60ms）。
 */
class OpusCodec {

    /** 上行采样率：与服务端解码端一致（写死 16k）。 */
    private val encRate = 16_000

    /** 下行采样率：取服务端 hello 里声明的值，默认 24k。 */
    @Volatile
    private var decRate = 24_000

    /** 每帧时长（ms），协议固定 60。 */
    private val frameMs = 60

    /** 上行每帧采样数：16000 × 60ms = 960。 */
    val encodeFrameSamples: Int = encRate * frameMs / 1000

    /** 下行每帧采样数（随 decRate 变）。 */
    val decodeFrameSamples: Int get() = decRate * frameMs / 1000

    @Volatile
    private var encoder: OpusEncoder? = null

    @Volatile
    private var decoder: OpusDecoder? = null

    /** 复用的输出缓冲，避免每帧 new（每秒 16 帧 × 2 方向）。 */
    private var encBuf = ByteArray(MAX_OPUS_BYTES)

    // ---------------------------------------------------------------- 可观测计数
    //
    // 为什么必须有：症状是"按了没反应"，但链路上有 6 个环节可能断
    // （麦克风 → 前处理 → 编码 → 上行 WS → 服务端待机 → ASR/TTS）。
    // 光看"没声音"分不清是哪一环。装机后跑一次 opus_selftest，
    // 看这几行就能定位到具体环节 —— 不用再靠猜。

    /** 成功编码并交给上层的帧数。 */
    @Volatile
    var encodedFrames: Int = 0
        private set

    /** 编码彻底失败（重建后重试仍失败）的帧数。 */
    @Volatile
    var encodeFailures: Int = 0
        private set

    /** 编码失败后**重建编码器并重试成功**的次数 —— 自愈命中数。 */
    @Volatile
    var encodeRecoveries: Int = 0
        private set

    /** 成功解码的帧数。 */
    @Volatile
    var decodedFrames: Int = 0
        private set

    /**
     * 初始化。可以重复调用（比如 hello 里声明的采样率和默认值不同）。
     *
     * @param outputSampleRate 服务端 hello 里的 `audio_params.sample_rate`
     */
    @Synchronized
    fun init(outputSampleRate: Int = 24_000) {
        val newDecRate = if (outputSampleRate in VALID_RATES) outputSampleRate else 24_000

        // ⚠️ 编码器与解码器**分别**构造，别塞进同一个 runCatching。
        //    曾经它们共用一个：解码器一抛异常（采样率不被支持之类），
        //    编码器也被一起置 null —— 上行音频就此全灭，而日志上只看得见
        //    "解码失败"，完全看不出是它连累了编码器。
        //    两个方向是两条独立的链路，必须各自成败。
        if (encoder == null) {
            encoder = createEncoder()
        }
        if (decoder == null || newDecRate != decRate) {
            decRate = newDecRate
            decoder = createDecoder()
        }

        Log.i(
            TAG,
            "Opus 初始化完成：上行 ${encRate}Hz/${frameMs}ms，下行 ${decRate}Hz/${frameMs}ms（纯 Java 实现）" +
                " 编码器=${if (encoder != null) "就绪" else "不可用"}" +
                " 解码器=${if (decoder != null) "就绪" else "不可用"}"
        )
    }

    /** 造一个编码器。失败返回 null，不影响解码器。 */
    private fun createEncoder(): OpusEncoder? = try {
        OpusEncoder(encRate, 1, OpusApplication.OPUS_APPLICATION_VOIP).apply {
            // 语音用 VOIP 模式（不是 AUDIO）—— 它会开 DTX/VAD 相关的优化，
            // 对老人这种"慢速、有停顿"的说话节奏更友好。
            //
            // 码率 24kbps：16kHz 单声道语音的甜点。再低 ASR 掉字，
            // 再高在 1M 内存的设备上没必要（而且上行带宽也是成本）。
            //
            // ⚠️ 这里必须注意 Concentus 的 API 风格：它是 C# 移植过来的，
            //    全部用 Java 的 getXxx/setXxx，**不是** Kotlin 属性。
            //    Kotlin 只在「getter + setter 成对存在」时才合成属性：
            //      · bitrate    ✅ getBitrate/setBitrate 都在
            //      · complexity ✅ getComplexity/setComplexity 都在
            //      · useVBR     ✅ 注意是 getUseVBR/setUseVBR（大写 VBR）
            //      · setUseDTX  ❌ 只有 setter 没有 getter，只能当方法调
            //    写成 `useVbr` / `useDtx` 会直接 Unresolved reference。
            bitrate = 24_000
            useVBR = true
            // 关掉 DTX：服务端的静音看门狗按"有没有收到帧"判断，
            // 我们这边用 VAD 决定发不发，不需要编码器再吞帧。
            setUseDTX(false)
            complexity = 5
        }
    } catch (t: Throwable) {
        Log.e(TAG, "编码器构造失败: ${t.javaClass.name}: ${t.message}", t)
        null
    }

    /** 造一个解码器。失败返回 null，不影响编码器。 */
    private fun createDecoder(): OpusDecoder? = try {
        OpusDecoder(decRate, 1)
    } catch (t: Throwable) {
        Log.e(TAG, "解码器构造失败（${decRate}Hz）: ${t.javaClass.name}: ${t.message}", t)
        null
    }

    /**
     * 编码一帧。
     *
     * @param pcm 长度必须正好是 [encodeFrameSamples]
     * @return Opus 裸包；编码器不可用时返回 null（上层跳过这一帧，不要崩）
     */
    fun encode(pcm: ShortArray): ByteArray? {
        // ⚠️ 必须同步（理由见类注释的「线程模型」）。
        //    实机堆栈就是 Concentus 断言在 Resampler / Autocorrelation 上炸，
        //    而它的成因就是两个线程同时踩同一个编码器。
        //
        // ⚠️ 返回值用**局部变量**承接、末尾统一 return，不用
        //    "try/catch 作为 synchronized 块的最后表达式"那种写法 ——
        //    那种写法依赖 Kotlin 对非局部 return 的类型推断，
        //    在 1.9 下会报 "A 'return' expression required in a function
        //    with a block body"（CI run 41972940902 实测）。
        //    本机没有 Android SDK、编译不了 Kotlin，这类坑只能靠 CI 兜。
        return synchronized(this) {
            val enc = encoder ?: return@synchronized null
            if (pcm.size < encodeFrameSamples) return@synchronized null

            var out: ByteArray? = null
            try {
                val n = enc.encode(pcm, 0, encodeFrameSamples, encBuf, 0, encBuf.size)
                if (n <= 0) {
                    // DTX 或"这一帧没有可发的数据"都会返回 0。不是错误，
                    // 但如果是负数就说明编码器出问题了 —— 分开记，别混成一个 null。
                    logOnce("编码返回 $n（非正值，跳过该帧）", null)
                } else {
                    encodedFrames++
                    out = encBuf.copyOf(n)
                }
            } catch (t: Throwable) {
                // ⚠️ 必须打**类名 + 堆栈**。OpusException 的 message 是有内容的，
                //    而 NPE / AssertionError 的 message 是 null ——
                //    只打 message 会得到一行无用的"编码失败: null"。
                //
                // ⚠️ 自愈：一次失败**不等于**这轮对话完了。
                //    Concentus 编码器一旦踩坏（断言触发后内部状态已经不一致），
                //    后续每一帧都会在同一处炸 —— 表现是"整个会话一帧都发不出去"，
                //    而日志上只有最初那 3 行（logOnce 用完了），后面全是静默。
                //    所以失败后**重建编码器再试一次**：断言的含义是"这一次的状态
                //    坏了"，换一个新实例就干净了。
                logOnce("编码失败 ${t.javaClass.name}: ${t.message} —— 重建编码器后重试一次", t)

                val fresh = rebuildEncoder()
                if (fresh == null) {
                    // 连重建都失败：编码器这条路彻底断了，如实计数。
                    encodeFailures++
                } else {
                    encodeRecoveries++
                    try {
                        val n = fresh.encode(pcm, 0, encodeFrameSamples, encBuf, 0, encBuf.size)
                        if (n <= 0) {
                            encodeFailures++
                            logOnce("重建后编码返回 $n（非正值，跳过该帧）", null)
                        } else {
                            encodedFrames++
                            out = encBuf.copyOf(n)
                        }
                    } catch (t2: Throwable) {
                        encodeFailures++
                        logOnce("重建后仍编码失败 ${t2.javaClass.name}: ${t2.message}", t2)
                    }
                }
            }
            out
        }
    }

    /**
     * 重建编码器 —— 自愈路径。
     *
     * 必须在 [encode] 的锁内调用（它改写 [encoder] 字段）。
     */
    private fun rebuildEncoder(): OpusEncoder? {
        val fresh = createEncoder()
        if (fresh == null) {
            // 重建也失败：把旧的也丢掉，避免继续用一个已知坏掉的实例。
            encoder = null
            return null
        }
        encoder = fresh
        return fresh
    }

    /** 编码器当前参数 —— 自检时打出来，一眼看出是不是初始化没生效。 */
    @Synchronized
    fun describe(): String {
        val enc = encoder ?: return "encoder=null（初始化失败）"
        return "Fs=${enc.sampleRate} bitrate=${enc.bitrate} " +
            "complexity=${enc.complexity} vbr=${enc.useVBR} dtx=${enc.useDTX} " +
            "lookahead=${enc.lookahead} | 上行 ${encodeFrameSamples}@${encRate}Hz " +
            "下行 ${decodeFrameSamples}@${decRate}Hz"
    }

    /** 计数快照 —— 自检/日志用，一行看清"到底有没有帧流出去"。 */
    @Synchronized
    fun stats(): String =
        "编码成功 $encodedFrames 帧 / 彻底失败 $encodeFailures 帧 / 自愈 $encodeRecoveries 次 / 解码 $decodedFrames 帧"

    /** 重新允许打失败日志（自检前调用，否则前面用完了就看不到堆栈）。 */
    @Synchronized
    fun resetDiag() {
        failLogged = 0
    }

    /** 失败日志只打前几次，避免每 60ms 刷一行把 logcat 冲掉。 */
    private var failLogged = 0

    private fun logOnce(msg: String, t: Throwable?) {
        if (failLogged >= 3) return
        failLogged++
        if (t != null) Log.e(TAG, msg, t) else Log.w(TAG, msg)
    }

    /**
     * 解码一帧。
     *
     * @param data Opus 裸包
     * @param out  输出缓冲，长度至少 [decodeFrameSamples]
     * @return 实际解出的采样数；失败返回 0
     */
    fun decode(data: ByteArray, out: ShortArray): Int {
        // ⚠️ 同样必须加锁：解码器也是 Concentus 的非线程安全实例，
        //    而 [init] 会在另一个线程（OkHttp）上替换它。
        //
        // ⚠️ 与 [encode] 同一套写法：返回值用局部变量承接、末尾统一返回，
        //    不依赖非局部 return 的类型推断（见 [encode] 里的注释）。
        return synchronized(this) {
            val dec = decoder ?: return@synchronized 0
            try {
                val n = dec.decode(data, 0, data.size, out, 0, decodeFrameSamples, false)
                if (n > 0) decodedFrames++
                n
            } catch (t: Throwable) {
                // 丢一帧不会影响后续 —— Opus 有丢包隐藏（PLC），
                // 这里直接按"这一帧没有"处理，不要因为一帧坏掉就重启整条链路。
                Log.w(TAG, "解码失败（丢弃该帧）: ${t.javaClass.name}: ${t.message}")
                0
            }
        }
        }
    }

    @Synchronized
    fun release() {
        encoder = null
        decoder = null
    }

    /** 解码器当前采样率（播放端要用它开 AudioTrack）。 */
    fun outputSampleRate(): Int = decRate

    companion object {
        private const val TAG = "ElderOpus"

        /** Opus 单包上限（协议规定 1275 字节）。 */
        private const val MAX_OPUS_BYTES = 1275

        /** Opus 支持的采样率。 */
        private val VALID_RATES = setOf(8000, 12000, 16000, 24000, 48000)

        /** 一帧 60ms 的毫秒数，给上层算时间用。 */
        const val FRAME_MS = 60
    }
}
