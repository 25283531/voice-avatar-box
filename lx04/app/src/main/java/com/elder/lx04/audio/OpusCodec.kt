package com.elder.lx04.audio

import android.util.Log
import org.concentus.OpusApplication
import org.concentus.OpusDecoder
import org.concentus.OpusEncoder
import org.concentus.OpusException

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
 * ## 线程模型
 *
 * 编码器/解码器**都不是线程安全的**，各自只被一个线程用：
 *   · 编码器：采集线程
 *   · 解码器：播放线程
 * 所以不加锁 —— 加锁反而会在音频线程上制造抖动。
 */
class OpusCodec {

    /** 上行采样率：与服务端解码端一致（写死 16k）。 */
    private val encRate = 16_000

    /** 下行采样率：取服务端 hello 里声明的值，默认 24k。 */
    private var decRate = 24_000

    /** 每帧时长（ms），协议固定 60。 */
    private val frameMs = 60

    /** 上行每帧采样数：16000 × 60ms = 960。 */
    val encodeFrameSamples: Int = encRate * frameMs / 1000

    /** 下行每帧采样数（随 decRate 变）。 */
    val decodeFrameSamples: Int get() = decRate * frameMs / 1000

    private var encoder: OpusEncoder? = null
    private var decoder: OpusDecoder? = null

    /** 复用的输出缓冲，避免每帧 new（每秒 16 帧 × 2 方向）。 */
    private var encBuf = ByteArray(MAX_OPUS_BYTES)

    /**
     * 初始化。可以重复调用（比如 hello 里声明的采样率和默认值不同）。
     *
     * @param outputSampleRate 服务端 hello 里的 `audio_params.sample_rate`
     */
    @Synchronized
    fun init(outputSampleRate: Int = 24_000) {
        decRate = if (outputSampleRate in VALID_RATES) outputSampleRate else 24_000
        runCatching {
            encoder = OpusEncoder(encRate, 1, OpusApplication.OPUS_APPLICATION_VOIP).apply {
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
            decoder = OpusDecoder(decRate, 1)
            Log.i(TAG, "Opus 初始化完成：上行 ${encRate}Hz/${frameMs}ms，" +
                "下行 ${decRate}Hz/${frameMs}ms（纯 Java 实现）")
        }.onFailure {
            Log.e(TAG, "Opus 初始化失败: ${it.message}", it)
            encoder = null
            decoder = null
        }
    }

    /**
     * 编码一帧。
     *
     * @param pcm 长度必须正好是 [encodeFrameSamples]
     * @return Opus 裸包；编码器不可用时返回 null（上层跳过这一帧，不要崩）
     */
    fun encode(pcm: ShortArray): ByteArray? {
        // ⚠️ 必须同步。
        //    Concentus 明确不是线程安全的，而 SILK 的内部状态一旦被两个线程
        //    同时踩，就会在 Resampler / Autocorrelation 的断言里炸出来
        //    （实机堆栈就是这样）。正常情况下采集线程只有一个调用者，
        //    但"旧 capture 还没退干净、新 capture 已经起来"的窗口是存在的 ——
        //    按住说话反复按几下就能撞上。加锁的代价在音频线程上可以忽略。
        synchronized(this) {
            val enc = encoder ?: return null
            if (pcm.size < encodeFrameSamples) return null
            return try {
                val n = enc.encode(pcm, 0, encodeFrameSamples, encBuf, 0, encBuf.size)
                if (n <= 0) {
                    // DTX 或"这一帧没有可发的数据"都会返回 0。不是错误，
                    // 但如果是负数就说明编码器出问题了 —— 分开记，别混成一个 null。
                    logOnce("编码返回 $n（非正值，跳过该帧）", null)
                    return null
                }
                encBuf.copyOf(n)
            } catch (t: Throwable) {
                // ⚠️ 必须打**类名 + 堆栈**。OpusException 的 message 是有内容的，
                //    而 NPE / AssertionError 的 message 是 null ——
                //    只打 message 会得到一行无用的"编码失败: null"。
                logOnce("编码失败 ${t.javaClass.name}: ${t.message}", t)
                null
            }
        }
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
        val dec = decoder ?: return 0
        return runCatching {
            dec.decode(data, 0, data.size, out, 0, decodeFrameSamples, false)
        }.onFailure {
            // 丢一帧不会影响后续 —— Opus 有丢包隐藏（PLC），
            // 这里直接按"这一帧没有"处理，不要因为一帧坏掉就重启整条链路。
            Log.w(TAG, "解码失败（丢弃该帧）: ${it.message}")
        }.getOrDefault(0)
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
