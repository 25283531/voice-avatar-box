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
 * ## 平台效果：实测**挂得上**，而且真的在跑
 *
 * `NoiseSuppressor` / `AutomaticGainControl` / `AcousticEchoCanceler`
 * 都是对 `AudioRecord` 的 session 挂 `libaudiopreprocessing.so` 里的效果。
 *
 * 2026-11-04 实机验证（录音进行中抓 `dumpsys media.audio_flinger`）：
 *
 *     Input thread AudioIn_3E, tid 3989, type 3 (RECORD):
 *       Audio source: 6 (voice recognition)
 *       Active Client Session 89
 *       1 Effect Chains
 *         3 effects for session 89
 *           Effect ID 83: Noise Suppression
 *           Effect ID 91: Acoustic Echo Canceler
 *           Effect ID 9x: Automatic Gain Control
 *
 * 三条全在，`create()` 返回的对象是实的。
 *
 * ⚠️ 之前一度判断"效果配置加载失败、挂不上"，那是因为看错了线程：
 *    `AudioIn_1E` 是 `MiVpmService`（小爱唤醒）的线程，它的 0 条链
 *    跟我们无关。我们自己会开一条**独立的** `AudioIn_3E`。
 *
 * ## 软件链仍然要跑，但增益不重复做
 *
 * 平台效果提供 NS / AEC / AGC，**但不提供 VAD**，也不给我们噪声底估计。
 * 所以 [AudioPreProcess] 无论如何都要跑 —— 它是"说完了没有"的唯一判据。
 *
 * 唯一要避免的是**两层 AGC 叠加**：平台 AGC 已经动过增益了，
 * 软件再放大最高 8 倍会削顶失真。所以挂上平台 AGC 时
 * 把 [AudioPreProcess.softwareAgc] 关掉，只保留噪声底 + VAD。
 */
class AudioCapture(
    /**
     * 是否挂平台 NS/AEC/AGC。
     *
     * 正常对话一律 true。只有**自检**时才传 false —— 因为 AEC 会把
     * 我们自己扬声器的声音当成回声消掉，回环测试就测不出东西了。
     *
     * ⚠️ 这个参数刻意放在 lambda **前面**：Kotlin 的尾随 lambda 语法
     *    只认最后一个参数，如果 lambda 不在最后，`AudioCapture { ... }`
     *    这种写法就编译不过。放前面还能用默认值，调用处照样只写 lambda。
     */
    private val enablePlatformEffects: Boolean = true,

    /** 每 60ms 回调一次。回调在**采集线程**上执行，不要在里面做重活。 */
    private val onFrame: (pcm: ShortArray, info: AudioPreProcess.Frame) -> Unit
) {

    private var record: AudioRecord? = null
    private var worker: Thread? = null

    /**
     * 实际使用的通道数（HAL 协商结果）。
     *
     * 2 = 立体声（读进来的数据要抽左声道）；1 = 单声道（恒等）。
     */
    @Volatile
    private var channels = CHANNELS_STEREO

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

        // ⚠️⚠️ 通道数必须**按 HAL 实际给的来**，不能想当然写 mono。
        //
        //    实机证据（2026-11-05，录音期间读 /proc/asound/card0/pcm1c/sub0/hw_params）：
        //
        //        access: RW_INTERLEAVED
        //        format: S16_LE
        //        channels: 2          ← HAL 给的是**双声道**
        //        rate: 16000
        //
        //    而这里请求的是 CHANNEL_IN_MONO —— MTK 的 HAL **没有**把 stereo
        //    downmix 成 mono 就直接把双声道给了。于是"按单声道读"实际读到的是
        //    **左右交错的立体声**：
        //
        //        · 每帧 960 个 short 只覆盖 960/2/16000 = **30ms** 真实音频
        //          （协议要求 60ms）⇒ 服务端按 60ms 播放 ⇒ 声音慢一倍、
        //          音调减半，ASR 几乎听不出字；
        //        · 相邻样本其实是 L、R 两个声道，信号被 2 倍频化。
        //
        //    现场症状完全对得上：link_test 跑 5 秒却采到 **165 帧**
        //    （5s/60ms 本该只有 83 帧，165 ≈ 83×2）。
        //
        //    ⇒ 修法：**明确请求立体声**，每帧读 960×2 个样本，**抽左声道**
        //    还原成真正的 60ms 单声道。若某台设备只支持 mono，
        //    stereo 构造会失败 → 自动回退到 mono 读法（对 mono 抽左是恒等）。
        val opened = openRecord() ?: return false
        val rec = opened.first
        channels = opened.second

        if (enablePlatformEffects) attachEffects(rec.audioSessionId) else {
            pre.softwareAgc = true
            Log.i(TAG, "平台效果：本次**刻意不挂**（自检模式，避免 AEC 消掉回环）")
        }

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
        Log.i(
            TAG,
            "采集已启动：${SAMPLE_RATE}Hz mono（HAL ${channels}ch → 抽左声道），${FRAME_MS}ms/帧"
        )
        return true
    }

    /**
     * 打开采集设备。
     *
     * 先试**立体声**（本机 HAL 实测给 2ch），失败再回退单声道。
     *
     * @return (AudioRecord, 使用的通道数)；都打不开时返回 null
     */
    private fun openRecord(): Pair<AudioRecord, Int>? {
        for (ch in intArrayOf(CHANNELS_STEREO, CHANNELS_MONO)) {
            val channelMask =
                if (ch == CHANNELS_STEREO) AudioFormat.CHANNEL_IN_STEREO else AudioFormat.CHANNEL_IN_MONO

            val minBuf = AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                channelMask,
                AudioFormat.ENCODING_PCM_16BIT
            )
            if (minBuf <= 0) {
                Log.w(TAG, "getMinBufferSize 失败（${ch}ch）: $minBuf")
                continue
            }

            // 缓冲给 4 帧，留够抖动余量：只给 1 帧的话，一次 GC 停顿就会丢采样，
            // 丢采样在 Opus 里表现为"咔"一声，服务端 ASR 会听到半个字。
            val bufBytes = maxOf(minBuf, FRAME_SAMPLES * 2 * ch * 4)

            val rec = try {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    SAMPLE_RATE,
                    channelMask,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufBytes
                )
            } catch (e: Exception) {
                Log.w(TAG, "创建 ${ch}ch AudioRecord 失败: ${e.message}")
                null
            } ?: continue

            if (rec.state != AudioRecord.STATE_INITIALIZED) {
                Log.w(TAG, "${ch}ch AudioRecord 未初始化（麦克风多半被别的进程占着）state=${rec.state}")
                rec.release()
                continue
            }
            Log.i(TAG, "采集通道：${ch}ch（HAL 协商结果）")
            return rec to ch
        }
        return null
    }

    private fun captureLoop(rec: AudioRecord) {
        val ch = channels
        // 多声道时按 ch 倍长度读，再抽左声道还原成单声道（见 start() 的说明）。
        val buf = ShortArray(FRAME_SAMPLES * ch)
        val mono = ShortArray(FRAME_SAMPLES)
        while (running) {
            // ⚠️ 必须读满一整帧：Opus 编码器要求输入长度正好是 960 采样，
            //    少一个都会导致时间轴错位（表现为服务端听到的声音越来越慢）。
            var off = 0
            while (off < buf.size && running) {
                val n = rec.read(buf, off, buf.size - off)
                if (n <= 0) {
                    if (n == AudioRecord.ERROR_INVALID_OPERATION ||
                        n == AudioRecord.ERROR_BAD_VALUE
                    ) {
                        // ⚠️ `rec.stop()` 之后阻塞中的 read 就会返回
                        //    ERROR_INVALID_OPERATION —— 那是**我们自己在停**，
                        //    不是"采集链路断了"。两者必须分开：
                        //    混为一谈会让每次正常停止都打一条
                        //    "采集链路已断" 的假警报，真出事时反而看不见。
                        if (running) {
                            Log.e(TAG, "read 返回 $n，采集链路已断")
                        }
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

            // 立体声 → 左声道（单声道时是恒等拷贝）。
            extractLeftChannel(buf, mono, FRAME_SAMPLES, ch)

            val info = pre.process(mono, FRAME_SAMPLES)
            onFrame(mono, info)
        }
    }

    /** 挂平台效果。挂不上的情况仍要能跑（只记日志，不阻断）。 */
    private fun attachEffects(sessionId: Int) {
        noiseSuppressor = runCatching { NoiseSuppressor.create(sessionId) }.getOrNull()
            ?.also { it.enabled = true }
        autoGain = runCatching { AutomaticGainControl.create(sessionId) }.getOrNull()
            ?.also { it.enabled = true }
        echoCancel = runCatching { AcousticEchoCanceler.create(sessionId) }.getOrNull()
            ?.also { it.enabled = true }

        // ⚠️ 平台 AGC 挂上了就关掉软件增益 —— 两层 AGC 叠加会削顶失真。
        //    注意这里用 autoGain != null 判断，而不是"create 没抛异常"：
        //    create 返回 null 才是"这台机器没有 AGC"，实测本机是非 null。
        pre.softwareAgc = autoGain == null

        Log.i(
            TAG,
            "平台效果：NS=${noiseSuppressor != null} AGC=${autoGain != null} " +
                "AEC=${echoCancel != null} → 软件增益${if (pre.softwareAgc) "开启（兜底）" else "关闭（避免与平台 AGC 叠加）"}"
        )
    }

    /** 暂停采集但保留 AudioRecord（快速恢复用）。 */
    fun pause() {
        runCatching { record?.stop() }
    }

    /** 停止并释放。 */
    fun stop() {
        running = false

        // ⚠️⚠️ 顺序要紧：**先停 AudioRecord，再 join 采集线程**。
        //
        //    反过来（先 join 再 stop）会留下一个真正的竞态窗口：
        //    采集线程正阻塞在 `rec.read()` 里（等满一帧 960 采样），
        //    它**不会**因为 `running=false` 就立刻醒 —— 标志只在 read
        //    返回之后才被检查。于是 join(300) 常常是**超时返回**的，
        //    旧采集线程还活着；而调用方紧接着就起了新采集线程，
        //    两个线程同时调 `opus.encode()` 踩同一个 Concentus 编码器
        //    ⇒ 内部状态写乱 ⇒ 下一帧在 `Resampler.silk_resampler` /
        //    `Autocorrelation.silk_warped_autocorrelation` 的断言里炸
        //    ⇒ `编码失败: null` ⇒ 整轮会话一帧都发不出去。
        //
        //    实机堆栈就是这个形状，而 JVM 单测永远测不到它
        //    （单测里没有"旧线程还活着"这件事）。
        //
        //    `rec.stop()` 会让阻塞中的 read 立刻返回错误码，
        //    采集线程随即看到 running=false 退出 —— 这才是"确定退出"。
        runCatching { record?.stop() }

        val t = worker
        worker = null
        // ⚠️ `Thread.join(long)` 返回 **void**，不是 Boolean ——
        //    想知道"退干净了没有"只能看 `isAlive`。
        //    （第一版写成 `runCatching { it.join(...) }.getOrDefault(false)`，
        //      类型对不上，CI 编译直接报 Unresolved reference: !。）
        var joined = true
        if (t != null) {
            runCatching { t.join(JOIN_TIMEOUT_MS) }
            joined = !t.isAlive
        }
        if (!joined) {
            // ⚠️ 不要静默。旧线程活着 = 两个线程踩同一个编码器 =
            //    下一轮必然"编码失败: null"。这条日志就是现场的证据。
            Log.e(
                TAG,
                "采集线程 ${JOIN_TIMEOUT_MS}ms 内没退出（旧线程可能仍活着，" +
                    "会与新线程并发踩同一个 Opus 编码器）"
            )
        }

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

        /** 立体声通道数。 */
        const val CHANNELS_STEREO = 2

        /** 单声道通道数。 */
        const val CHANNELS_MONO = 1

        /**
         * 从交错的多声道数据里取**左声道**，还原成单声道。
         *
         * @param src 交错数据（每个采样点 ch 个 short：L R L R …）
         * @param dst 单声道输出
         * @param samples 取多少个采样点
         * @param ch 通道数；1 时是恒等拷贝
         *
         * ⚠️ 这段逻辑单独拆成纯函数，是为了能在 **JVM 单测**里覆盖 ——
         *    通道数错了在真机上表现为"声音慢一倍"，很难一眼看出，
         *    而单测里一测就知道抽出来的是不是左声道。
         */
        internal fun extractLeftChannel(src: ShortArray, dst: ShortArray, samples: Int, ch: Int) {
            if (ch <= 1) {
                System.arraycopy(src, 0, dst, 0, samples)
                return
            }
            for (i in 0 until samples) dst[i] = src[i * ch]
        }

        /**
         * 等待采集线程退出的超时（毫秒）。
         *
         * 一帧是 60ms，`rec.stop()` 之后阻塞中的 read 最多再等一帧就该返回，
         * 所以 500ms 是"确定能退出"的上限余量。超时了要报警而不是静默
         * —— 见 [stop] 里的说明。
         */
        private const val JOIN_TIMEOUT_MS = 500L
    }
}
