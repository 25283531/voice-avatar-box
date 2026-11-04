package com.elder.lx04.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.util.Log
import kotlin.concurrent.thread

/**
 * 麦克风采集 —— 16kHz / 单声道 / 60ms 一帧。
 *
 * ## 音频源为什么选 VOICE_RECOGNITION
 *
 * LX04 原厂的小爱唤醒（`MiVpmService`）用的就是它 —— 实测
 * `dumpsys media.audio_flinger` 里它的录音线程挂在 `AudioIn_1E` 上。
 *
 * 与 `MIC` 的区别很实际：
 *   · `MIC` 是**原始信号**，空调声、桌子震动、电路噪声全在
 *   · `VOICE_RECOGNITION` 在 HAL 层就带了为语音识别调过的处理
 *     （部分平台自动开降噪 + 固定增益），并且**不做 AGC 抽气**
 *
 * 老人对着音箱说话，距离从半米到三米，原始信号的信噪比会差很多。
 * 用和小爱同一个音频源，至少起点是一样的。
 *
 * ## 平台效果：先试，试不到就自己算
 *
 * `NoiseSuppressor` / `AutomaticGainControl` / `AcousticEchoCanceler`
 * 都是对 `AudioRecord` 的 session 挂 `libaudiopreprocessing.so` 里的效果。
 * 但这台机器实测 **效果配置加载失败**（`XML effect configuration failed
 * to load.`，录音线程 0 Effect Chains），`create()` 很可能返回 null。
 *
 * 所以这里的设计是：
 *   1. 逐个尝试挂载，**挂上几个算几个**，日志里打清楚；
 *   2. 无论挂没挂上，[AudioPreProcess] 都会跑（软件兜底）。
 *
 * 为什么不"挂了平台效果就不跑软件链"：两条链的语义不一样 ——
 * 平台效果是 NS/AGC，而软件链还负责 **VAD**（判断说完了没有）和
 * 噪声底估计，这些是平台效果不提供的。软件链的 AGC 在信噪比已经
 * 很好的时候会自动停手（`gain` 停在 1.0 附近），不会叠加放大。
 */
class AudioCapture(
    /** 每 60ms 回调一次。回调在**采集线程**上执行，不要在里面做重活。 */
    private val onFrame: (pcm: ShortArray, info: AudioPreProcess.Frame) -> Unit
) {

    private var record: AudioRecord? = null
    private var worker: Thread? = null

    @Volatile
    private var running = false

    private val pre = AudioPreProcess(SAMPLE_RATE)

    private var noiseSuppressor: NoiseSuppressor? = null
    private var autoGain: AutomaticGainControl? = null
    private var echoCancel: AcousticEchoCanceler? = null

    val isRunning: Boolean get() = running

    /**
     * 开始采集。
     *
     * @return true = 已启动；false = 麦克风被占用或参数不支持
     */
    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        if (running) return true

        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) {
            Log.e(TAG, "getMinBufferSize 失败: $minBuf")
            return false
        }

        // 缓冲给 4 帧，留够抖动余量：只给 1 帧的话，一次 GC 停顿就会丢采样，
        // 丢采样在 Opus 里表现为"咔"一声，服务端 ASR 会听到半个字。
        val bufBytes = maxOf(minBuf, FRAME_SAMPLES * 2 * 4)

        val rec = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufBytes
            )
        } catch (e: Exception) {
            Log.e(TAG, "创建 AudioRecord 失败: ${e.message}", e)
            return false
        }

        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord 未初始化（麦克风多半被别的进程占着）state=${rec.state}")
            rec.release()
            return false
        }

        attachEffects(rec.audioSessionId)

        record = rec
        pre.reset()
        running = true

        rec.startRecording()
        if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            Log.e(TAG, "startRecording 失败，麦克风被占用")
            running = false
            rec.release()
            record = null
            return false
        }

        worker = thread(name = "elder-mic", priority = Thread.MAX_PRIORITY - 2) {
            captureLoop(rec)
        }
        Log.i(TAG, "采集已启动：${SAMPLE_RATE}Hz mono，${FRAME_MS}ms/帧")
        return true
    }

    private fun captureLoop(rec: AudioRecord) {
        val buf = ShortArray(FRAME_SAMPLES)
        while (running) {
            // ⚠️ 必须读满一整帧：Opus 编码器要求输入长度正好是 960 采样，
            //    少一个都会导致时间轴错位（表现为服务端听到的声音越来越慢）。
            var off = 0
            while (off < FRAME_SAMPLES && running) {
                val n = rec.read(buf, off, FRAME_SAMPLES - off)
                if (n <= 0) {
                    if (n == AudioRecord.ERROR_INVALID_OPERATION ||
                        n == AudioRecord.ERROR_BAD_VALUE
                    ) {
                        Log.e(TAG, "read 返回 $n，采集链路已断")
                        running = false
                        return
                    }
                    // ERROR_DEAD_OBJECT / 暂时没数据：小睡再试，不要空转
                    Thread.sleep(5)
                    continue
                }
                off += n
            }
            if (!running) break

            val info = pre.process(buf, FRAME_SAMPLES)
            onFrame(buf, info)
        }
    }

    /** 挂平台效果。挂不上是**正常情况**（本机效果配置加载失败），只记日志。 */
    private fun attachEffects(sessionId: Int) {
        noiseSuppressor = runCatching { NoiseSuppressor.create(sessionId) }.getOrNull()
            ?.also { it.enabled = true }
        autoGain = runCatching { AutomaticGainControl.create(sessionId) }.getOrNull()
            ?.also { it.enabled = true }
        echoCancel = runCatching { AcousticEchoCanceler.create(sessionId) }.getOrNull()
            ?.also { it.enabled = true }

        Log.i(
            TAG,
            "平台效果：NS=${noiseSuppressor != null} AGC=${autoGain != null} " +
                "AEC=${echoCancel != null}（全 false 属正常，走软件前处理兜底）"
        )
    }

    /** 暂停采集但保留 AudioRecord（快速恢复用）。 */
    fun pause() {
        runCatching { record?.stop() }
    }

    /** 停止并释放。 */
    fun stop() {
        running = false
        worker?.let { runCatching { it.join(300) } }
        worker = null

        runCatching { record?.stop() }
        runCatching { record?.release() }
        record = null

        runCatching { noiseSuppressor?.release() }
        runCatching { autoGain?.release() }
        runCatching { echoCancel?.release() }
        noiseSuppressor = null
        autoGain = null
        echoCancel = null

        Log.i(TAG, "采集已停止")
    }

    companion object {
        private const val TAG = "ElderMic"

        /** 采样率：与服务端解码端一致，不能改。 */
        const val SAMPLE_RATE = 16_000

        /** 每帧时长：协议固定 60ms。 */
        const val FRAME_MS = 60

        /** 每帧采样数：16000 × 0.06 = 960。 */
        const val FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS / 1000
    }
}
