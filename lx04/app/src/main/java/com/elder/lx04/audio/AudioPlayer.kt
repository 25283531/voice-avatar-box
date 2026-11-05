package com.elder.lx04.audio

import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.util.Log
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * TTS 播放 —— 24kHz / 单声道 / PCM16。
 *
 * ## 为什么用 AudioTrack 直写 PCM，而不是 MediaPlayer
 *
 * 服务端下发的是**裸 Opus 包**（每帧 60ms），不是文件、也不是流容器。
 * MediaPlayer 要文件/URL，MediaCodec 要自己喂码流还得处理时间戳 ——
 * 而我们已经有 PCM 了（Concentus 解出来的），直接 AudioTrack 写是最短路径，
 * 也是唯一能做到"边收边播、延迟 < 200ms"的方式。
 *
 * ## 缓冲策略
 *
 * AudioTrack 内部缓冲设成 2~3 帧（120~180ms）：
 *   · 太小（< 60ms）：网络抖一下就是"咔咔"的断音
 *   · 太大（> 500ms）：老人说完话要等半秒才听到回应，交互会显得迟钝
 *
 * 队列深度上限 [MAX_QUEUED_FRAMES]：服务端是匀速下发（有 AudioPacer），
 * 正常情况下队列几乎空。队列堆满说明播放卡住了，这时**丢新帧**比无限堆积好 ——
 * 堆到几秒之后再播，听起来就是"它怎么突然自己说起话来"。
 */
class AudioPlayer {

    private var track: AudioTrack? = null
    private var worker: Thread? = null

    @Volatile
    private var running = false

    /** 待播帧队列。存的是解码后的 PCM。 */
    private val queue = ArrayBlockingQueue<ShortArray>(MAX_QUEUED_FRAMES)

    private var sampleRate = 24_000

    /** 已丢弃的帧数（队列满）。用于日志判断链路是否健康。 */
    @Volatile
    var droppedFrames: Int = 0
        private set

    /**
     * 下行 PCM 的**客观电平观测点 + 自适应增益**。
     *
     * 背景（2026-11-05 真机）：服务端日志两次都写着「播报完成（音频已发）」，
     * `dumpsys media.audio_flinger` 也显示 AudioTrack 写了约 5.5 秒数据，
     * ALSA `DL1_AWB_Record` 回采也录到了信号 —— 但老人**听不到声音**。
     * 最后量出来真因是电平：TTS 解码后 rms 只有 223~435（满幅 32767），
     * 比 440Hz 自检音（rms≈7071）低约 20 倍，叠加系统音量 9~12/20 后
     * 功放输入只剩几十的幅度。**不是没播，是轻到听不见**。
     *
     * 这里既量电平也做归一化，详见 [AudioLevel] 的注释。
     */
    private var levelFrames = 0
    private var levelSumSq = 0.0
    private var levelPeak = 0

    /** 本帧实际用的增益，由 [targetGain] 逐帧 ramp 过来，绝不跳变。 */
    @Volatile
    private var gain = AudioLevel.INITIAL_GAIN

    /** 窗口统计得出的目标增益；[gain] 每帧向它靠近 [AudioLevel.RAMP_STEP]。 */
    private var targetGain = AudioLevel.INITIAL_GAIN

    /** 同一窗口里**放大前**的平方和与样本数，用来算下一轮增益。 */
    private var rawSumSq = 0.0
    private var rawSamples = 0

    /**
     * 是否已 `play()`。
     *
     * ⚠️ 不能再像以前那样在 `start()` 里就 `play()`：那一刻 AudioTrack
     * 内部缓冲是**空的**，HAL 立刻 underrun 并把 track disable
     * （真机日志反复出现 `releaseBuffer() track disabled due to previous
     * underrun, restarting`）。之后每 60ms 才补一帧，缓冲一直填不满，
     * 大量 `write()` 静默失败 ⇒ 老人只听到零星碎片。
     *
     * 正确顺序：**先 write 预填充 → 攒够 [PREBUFFER_SAMPLES] → 再 play()**。
     * MODE_STREAM 允许在 STOPPED 状态写入，数据会留在内部缓冲里。
     */
    @Volatile
    private var played = false

    /** `write()` 返回负值的次数。以前被 `runCatching` 整个吞掉，丢帧无日志。 */
    @Volatile
    var writeErrors: Int = 0
        private set

    /**
     * 开始播放（可重复调用以切换采样率）。
     *
     * @param sampleRate 服务端 hello 里声明的采样率
     */
    @Synchronized
    fun start(sampleRate: Int = 24_000) {
        if (running && this.sampleRate == sampleRate) return
        stop()
        this.sampleRate = sampleRate

        val minBuf = AudioTrack.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) {
            Log.e(TAG, "getMinBufferSize 失败: $minBuf")
            return
        }
        // ⚠️ 1 秒内部缓冲（原来只有 3 帧 / 180ms）。
        //    服务端 TTS 帧经 AudioPacer 匀速下发，但 WS 抖动一次就够让
        //    180ms 的缓冲见底。缓冲是吸收抖动的唯一余量，不能省。
        val bufBytes = maxOf(minBuf, sampleRate * 2)

        val t = runCatching {
            AudioTrack(
                AudioManager.STREAM_MUSIC,
                sampleRate,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufBytes,
                AudioTrack.MODE_STREAM
            )
        }.getOrNull()

        if (t == null || t.state != AudioTrack.STATE_INITIALIZED) {
            Log.e(TAG, "AudioTrack 初始化失败")
            runCatching { t?.release() }
            return
        }

        track = t
        running = true
        played = false
        writeErrors = 0
        gain = AudioLevel.INITIAL_GAIN
        targetGain = AudioLevel.INITIAL_GAIN
        // ⚠️ 这里**不 play()**：由 playLoop 在缓冲预填充完成后开播。
        worker = thread(name = "elder-speaker", priority = Thread.MAX_PRIORITY - 2) {
            playLoop(t)
        }
        Log.i(TAG, "播放已启动：${sampleRate}Hz mono，缓冲 ${bufBytes / 2} 采样（预填充后开播）")
    }

    private fun playLoop(t: AudioTrack) {
        var prefilled = 0
        while (running) {
            val frame = queue.poll(100, TimeUnit.MILLISECONDS)
            if (frame == null) {
                // 队列短暂空但已经攒了一点音频：别让老人一直等，直接开播。
                // 正常情况下预填充会在 100ms 内攒够，不会走到这里。
                if (!played && prefilled > 0) {
                    runCatching { t.play() }
                    played = true
                    Log.i(TAG, "预填充超时，提前开播（$prefilled 采样）")
                }
                continue
            }
            if (!running) break

            // ⚠️ 必须检查返回值：AudioTrack 被 HAL disable 时 write() 会返回
            //    负值，以前的 `runCatching { t.write(...) }` 把失败整个吞掉，
            //    于是丢帧**不报错、不崩溃、日志全绿**，只表现为「隐隐约约」。
            val n = runCatching { t.write(frame, 0, frame.size) }.getOrDefault(-1)
            if (n < 0) {
                writeErrors++
                if (writeErrors == 1 || writeErrors % 20 == 0) {
                    Log.w(TAG, "AudioTrack.write 失败($n)，累计丢帧 $writeErrors")
                }
                continue
            }
            if (n < frame.size) {
                // 部分写入：把尾巴补上，别丢半个帧
                runCatching { t.write(frame, n, frame.size - n) }
            }

            if (!played) {
                prefilled += frame.size
                if (prefilled >= PREBUFFER_SAMPLES) {
                    runCatching { t.play() }
                    played = true
                    Log.i(TAG, "预填充完成（$prefilled 采样），开始播放")
                }
            }
        }
    }

    /** 送一帧去播（在 WS 线程调用，不阻塞）。 */
    fun enqueue(pcm: ShortArray) {
        if (!running) return
        val (rawRms, _) = AudioLevel.stats(pcm)
        rawSumSq += rawRms * rawRms * pcm.size
        rawSamples += pcm.size

        // ⚠️ 返回新数组，不改动解码器给的缓冲（OpusCodec 内部可能复用它）。
        val boosted = AudioLevel.amplify(pcm, gain)
        // 逐帧平滑：窗口边界不许出现增益跳变（真机「中途变频率」的根因）。
        gain = AudioLevel.ramp(gain, targetGain)
        observeLevel(boosted)

        if (!queue.offer(boosted)) {
            droppedFrames++
            if (droppedFrames % 20 == 1) {
                Log.w(TAG, "播放队列满，已丢弃 $droppedFrames 帧（链路卡顿）")
            }
        }
    }

    /** 累计一帧的电平，满 25 帧（1.5 秒）打一条日志并更新增益。 */
    private fun observeLevel(pcm: ShortArray) {
        val (rms, peak) = AudioLevel.stats(pcm)
        levelFrames++
        levelSumSq += rms * rms * pcm.size
        if (peak > levelPeak) levelPeak = peak
        if (levelFrames >= 25) {
            val outRms = kotlin.math.sqrt(levelSumSq / maxOf(1, levelFrames * pcm.size))
            val rawRms = kotlin.math.sqrt(rawSumSq / maxOf(1, rawSamples))
            targetGain = AudioLevel.nextGain(rawRms, targetGain)
            Log.i(
                TAG,
                "下行 PCM 电平：帧=$levelFrames 原始rms=${"%.1f".format(rawRms)} → " +
                    "放大后rms=${"%.1f".format(outRms)} peak=$levelPeak " +
                    "增益=${"%.2f".format(gain)}x → 目标${"%.2f".format(targetGain)}x" +
                    "（${if (rawRms < 20) "疑似静音 ⚠️" else "有信号"}）"
            )
            levelFrames = 0
            levelSumSq = 0.0
            levelPeak = 0
            rawSumSq = 0.0
            rawSamples = 0
        }
    }

    /** 立刻停下并清空队列（用户打断 / 新的一轮开始）。 */
    fun flush() {
        queue.clear()
        runCatching { track?.pause() }
        runCatching { track?.flush() }
        runCatching { track?.play() }
        // flush() 已经把 track 推到 PLAYING；playLoop 不要再重复预填充。
        played = true
    }

    @Synchronized
    fun stop() {
        running = false
        played = false
        queue.clear()
        worker?.let { runCatching { it.join(300) } }
        worker = null

        runCatching { track?.stop() }
        runCatching { track?.release() }
        track = null
        droppedFrames = 0
    }

    companion object {
        private const val TAG = "ElderSpeaker"

        /** 队列上限：20 帧 = 1.2 秒。 */
        private const val MAX_QUEUED_FRAMES = 20

        /**
         * 开播前预填充的采样数（48kHz 下约 0.33 秒）。
         *
         * 必须**小于** AudioTrack 内部缓冲（1 秒），否则 STOPPED 状态下的
         * write() 会在缓冲填满时阻塞，playLoop 卡死、永远不开播。
         */
        private const val PREBUFFER_SAMPLES = 16_000
    }
}
