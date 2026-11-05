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
     * 下行 PCM 的**客观电平观测点**。
     *
     * 背景（2026-11-05 真机）：服务端日志两次都写着「播报完成（音频已发）」，
     * `dumpsys media.audio_flinger` 也显示 AudioTrack 写了约 5.5 秒数据，
     * 但老人**听不到声音**。此时读代码分不清两件事：
     *   · 写进 AudioTrack 的 PCM 本来就是静音（解码 / TTS 侧问题）
     *   · PCM 正常，但 HAL / 音量 / 路由没把声音放出来
     * 两者日志都是全绿。所以必须在**入队前**量一次电平，把这两种情况分开。
     *
     * 单位是 PCM16 的 short 幅度（满幅 32767）。正常语音 rms 约 300~3000；
     * 全零/近零（< 20）就是静音帧。每 25 帧（1.5 秒）打一条，不会刷屏。
     */
    private var levelFrames = 0
    private var levelSumSq = 0.0
    private var levelPeak = 0

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
        // 3 帧的内部缓冲，约 180ms
        val bufBytes = maxOf(minBuf, sampleRate * 2 * 3 * OpusCodec.FRAME_MS / 1000)

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
        t.play()

        worker = thread(name = "elder-speaker", priority = Thread.MAX_PRIORITY - 2) {
            playLoop(t)
        }
        Log.i(TAG, "播放已启动：${sampleRate}Hz mono，缓冲 ${bufBytes / 2} 采样")
    }

    private fun playLoop(t: AudioTrack) {
        while (running) {
            val frame = queue.poll(200, TimeUnit.MILLISECONDS) ?: continue
            if (!running) break
            runCatching { t.write(frame, 0, frame.size) }
        }
    }

    /** 送一帧去播（在 WS 线程调用，不阻塞）。 */
    fun enqueue(pcm: ShortArray) {
        if (!running) return
        observeLevel(pcm)
        if (!queue.offer(pcm)) {
            droppedFrames++
            if (droppedFrames % 20 == 1) {
                Log.w(TAG, "播放队列满，已丢弃 $droppedFrames 帧（链路卡顿）")
            }
        }
    }

    /** 累计一帧的电平，满 25 帧打一条日志。 */
    private fun observeLevel(pcm: ShortArray) {
        var peak = 0
        var sum = 0.0
        for (v in pcm) {
            val a = v.toInt()
            val abs = if (a < 0) -a else a
            if (abs > peak) peak = abs
            sum += (a.toDouble() * a)
        }
        levelFrames++
        levelSumSq += sum
        if (peak > levelPeak) levelPeak = peak
        if (levelFrames >= 25) {
            val total = levelFrames * pcm.size
            val rms = kotlin.math.sqrt(levelSumSq / maxOf(1, total))
            Log.i(
                TAG,
                "下行 PCM 电平：帧=$levelFrames rms=${"%.1f".format(rms)} " +
                    "peak=$levelPeak（${if (rms < 20) "疑似静音 ⚠️" else "有信号"}）"
            )
            levelFrames = 0
            levelSumSq = 0.0
            levelPeak = 0
        }
    }

    /** 立刻停下并清空队列（用户打断 / 新的一轮开始）。 */
    fun flush() {
        queue.clear()
        runCatching { track?.pause() }
        runCatching { track?.flush() }
        runCatching { track?.play() }
    }

    @Synchronized
    fun stop() {
        running = false
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
    }
}
