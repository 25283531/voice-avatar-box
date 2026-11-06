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
 * ## 音频源为什么选 VOICE_COMMUNICATION（2026-11-06 真机定案）
 *
 * ⚠️ **这一节推翻了 2026-11-05 的结论** —— 那次写的是 `VOICE_RECOGNITION`，
 *    而且当时的判据把「上电瞬态」当成了「有人声」（见下）。
 *
 * 2026-11-06 真机矩阵（同一台设备、同一说话人、同一次运行内十组参数扫完，
 * 每组采 2.5 秒、组间等 1.5 秒释放），**逐组落盘原始样本**再拉回本机分析：
 *
 *   ```
 *   VOICE_RECOGNITION 16000 1ch → 78400 样本 / 2.45s = 32000 样本/s
 *        ⇒ HAL 回的是 2ch 交错；分段 rms = [8187.6, 53.5, 98, 133, 138, 130,
 *           118, ... 13, 7.9, 6.5, 3.9, 5.6, ...]
 *        ⇒ **第一个 800 样本是上电爆音（rms 8187），此后一路衰减到 ~10**
 *        ⇒ 整个窗口里**没有持续语音**。上行的对话语音全在那个衰减尾巴里，
 *          能量极低 ⇒ 服务端 ASR 恒空。
 *
 *   VOICE_COMMUNICATION 16000 1ch → 39200 样本 / 2.45s = 16000 样本/s
 *        ⇒ HAL 回的是**真单声道**（不需要抽通道）；
 *        分段 rms = [0×21, 2204, 4161, 3838, 3136, 2659, ... 持续 900~1300]
 *        ⇒ 前面 21 段是**麦克风启动延迟**（约 1.05 秒），之后是**持续人声**；
 *          频谱 100/300/700/1500Hz 都有明显能量、3kHz 以上快速衰减
 *          —— 典型语音频谱。
 * ```
 *
 * ⚠️ **2026-11-05 那次「VOICE_RECOGNITION 16000 1ch → rms=553.7 有信号」
 *    是假绿**：553 与今天的 827 同量级，而今天的 827 已被分段证明是
 *    「上电爆音 + 衰减尾巴」。当时的判据只看整窗 rms、没排除瞬态，
 *    于是把「一记爆音」认成了「有人说话」。
 *    ⇒ 任何「麦克风能录到人声」的结论，都必须先**分段**看能量形态，
 *      并把上电瞬态单独剔掉（见 [AudioCapture] 里 [WARMUP_FRAMES]）。
 *
 * ⚠️ **劣化模型被排除**：矩阵里 VOICE_COMMUNICATION 排在**第 9 组（最晚）**、
 *    VOICE_RECOGNITION 排在**第 2 组（最早）**。若 HAL 是「反复 open/close 后
 *    随时间劣化」，那越靠后越该坏 —— 实测恰恰相反（最晚的那组最好）。
 *    ⇒ 差异在**音频源本身**，不是 HAL 状态。
 *
 * 老人对着音箱说话，距离从半米到三米，原始信号的信噪比会差很多。
 * `VOICE_COMMUNICATION` 在这台 MTK HAL 上是唯一能持续拿到人声的源。
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
     * ⚠️ **默认 false，而且生产也必须关掉**（2026-11-05 实机结论）：
     *
     *    挂了平台效果链之后，麦克风信号会被压到几乎为零 —— 同一台设备、
     *    同一套代码实测：
     *
     *        挂 NS/AGC/AEC（老代码） → mic_test：rms 5~80（几乎听不见）
     *        不挂（矩阵实测）        → rms 553~1005（正常说话的电平）
     *
     *    而 Opus 编码器吃的就是这份数据：**被效果链污染过的帧在真机上
     *    100% 触发 Concentus 的 Inlines.OpusAssert**（日志里就是"编码失败: null"），
     *    整轮会话一帧都发不出去。
     *    把同样但**没挂效果链**的真实麦克风数据拉回本机 JVM 编码：
     *    **40/40 全部成功** ⇒ 断言的元凶是效果链，不是数据本身、也不是并发。
     *
     *    降噪/回声抑制改由 [AudioPreProcess] 的软件链路兜底
     *    （直流阻断 + 120Hz 高通 + 软件 AGC + VAD）。
     *
     * ⚠️ 这个参数刻意放在 lambda **前面**：Kotlin 的尾随 lambda 语法
     *    只认最后一个参数，如果 lambda 不在最后，`AudioCapture { ... }`
     *    这种写法就编译不过。放前面还能用默认值，调用处照样只写 lambda。
     */
    private val enablePlatformEffects: Boolean = false,

    /** 每 60ms 回调一次。回调在**采集线程**上执行，不要在里面做重活。 */
    private val onFrame: (pcm: ShortArray, info: AudioPreProcess.Frame) -> Unit
) {

    private var record: AudioRecord? = null
    private var worker: Thread? = null

    /**
     * HAL 实际给的通道数（每帧读多少个 short）。
     *
     * ⚠️ **不是我们请求的** —— 请求的是 mono，HAL 回的是交错双声道，
     *    所以每帧读 1920 个 short 再抽左声道（见 [start] 的说明）。
     */
    @Volatile
    private var channels = HAL_CHANNELS_FALLBACK

    @Volatile
    private var running = false

    private val pre = AudioPreProcess(SAMPLE_RATE)

    /**
     * 当前帧消费者。
     *
     * ⚠️ 唤醒检出后要从 KWS 的 `feed` 切成「上行编码 + sendAudio」而
     *    **不重启 AudioRecord**：LX04 的 HAL 释放有延迟，一 stop/start 就撞
     *    `startInputDevice(), input device already opened` + `[readThread]
     *    failed`，新采集读到全零（项目 tryMicCase 自测记录：必须等 1500ms）。
     *
     * 构造签名刻意不变（lambda 仍是最后一个参数），保住所有
     * `AudioCapture { pcm, info -> ... }` 尾随 lambda 调用点和
     * `return@AudioCapture` 标签 —— 改签名会让 Kotlin 编译不过。
     * [captureLoop] 每帧取一次快照，切换是原子的、不会半帧生效。
     */
    @Volatile
    private var consumer: (ShortArray, AudioPreProcess.Frame) -> Unit = onFrame

    /** 切换帧消费者（采集线程安全）。 */
    fun setConsumer(c: (ShortArray, AudioPreProcess.Frame) -> Unit) {
        consumer = c
    }


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

        // ⚠️⚠️ 采集参数是**实测定死**的，不是猜的。
        //
        //    实机矩阵（`adb shell am broadcast -a com.elder.lx04.DEBUG --es cmd mic_matrix`，
        //    2026-11-06）把 10 组 (音频源 × 采样率 × 通道数) 全试了一遍，
        //    **每组把原始样本落盘**、拉回本机逐段分析（见类注释）：
        //
        //        VOICE_RECOGNITION 16000 2ch → frames=7   rms=0.0     ← 全零
        //        VOICE_RECOGNITION 16000 1ch → frames=98  rms=827.8   ← ★只有上电爆音
        //        VOICE_RECOGNITION 48000 2ch → frames=7   rms=0.0     ← 全零
        //        VOICE_RECOGNITION 48000 1ch → frames=226 rms=734.1   ← ★同上
        //        MIC 16000 1ch              → frames=99  rms=1139.8  ← ★同上
        //        MIC 16000 2ch              → frames=7   rms=0.0     ← 全零
        //        MIC 48000 2ch              → frames=7   rms=0.0     ← 全零
        //        DEFAULT 48000 2ch          → frames=7   rms=0.0     ← 全零
        //        VOICE_COMMUNICATION 16000 1ch → frames=49 rms=1380.8 ← ★真实人声
        //        CAMCORDER 48000 2ch        → frames=7   rms=0.0     ← 全零
        //
        //    ⇒ **2ch 组合全是 0**：ALSA `state: PREPARED`（流没跑起来）、
        //      AudioFlinger 日志 `startInputDevice(), input device already opened`、
        //      HAL 反复打 `[readThread] failed` / `Not enough data`。
        //      所以**必须请求 mono**（请求 stereo 在这台设备上等于没有麦克风）。
        //
        //    ⇒ ★ 标记的那几组「rms 看起来很大」是**假绿**：分段一看，
        //      能量全在**第一段上电爆音**（rms 8187）里，此后一路衰减到 ~10，
        //      整个窗口**没有持续语音**。详见类注释。
        //
        //    ⇒ 唯一能持续拿到人声的是 **VOICE_COMMUNICATION**。
        //
        // ⚠️⚠️ **HAL 通道数不写死，运行时自适应**：
        //
        //    同一次请求 `CHANNEL_IN_MONO`，两个源回来的东西不一样：
        //
        //        VOICE_RECOGNITION  → 78400 样本 / 2.45s = **32000 样本/s**
        //                              ⇒ HAL 回的是 2ch 交错（framework 不 downmix），
        //                                且**右通道恒为数字零**（实测奇位 rms=0.7）
        //        VOICE_COMMUNICATION → 39200 样本 / 2.45s = **16000 样本/s**
        //                              ⇒ HAL 回的是**真单声道**
        //
        //    ⇒ 写死 `HAL_CHANNELS=2` 会在换源后**读错**：
        //      每帧读 960×2=1920 short 会跨过 120ms 真实音频，
        //      再抽「左声道」= 每隔一个取一个 ⇒ 采样率腰斩到 8kHz，
        //      服务端按 16kHz 播 ⇒ **声音慢一倍、音调减半**。
        //      这正是 11-05 修过的那个老坑，换源后必须重新面对。
        //
        //    ⇒ 做法：**先按 mono 读，用实测帧率反推 HAL 通道数**，
        //      确认是 2ch 才切到「读 2 倍 + 抽左声道」。
        //      判据在 [detectHalChannels]，纯函数，可单测。
        //
        //    ⇒ 换设备时先用 `mic_matrix` 复核音频源；通道数不用手动改，
        //      运行时自会测出来。
        val rec = openRecord() ?: return false
        channels = HAL_CHANNELS_FALLBACK

        if (enablePlatformEffects) attachEffects(rec.audioSessionId) else {
            // ⚠️ 不挂平台效果链时，软件增益必须自己顶上（见类注释）。
            pre.softwareAgc = true
            Log.i(TAG, "平台效果：本次**不挂**（NS/AGC/AEC 会把麦克风信号压到近零）")
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
            "采集已启动：${SAMPLE_RATE}Hz mono，源=$AUDIO_SOURCE，" +
                "HAL 通道数运行时探测中，${FRAME_MS}ms/帧"
        )
        return true
    }

    /**
     * 打开采集设备。
     *
     * ⚠️ **只请求 mono** —— 实测本机 HAL 不支持立体声录音（请求 stereo 读到的
     *    全是 0，见 [start] 里的矩阵表）。
     *
     * ⚠️ **音频源必须是 `VOICE_COMMUNICATION`** —— 见类注释里 2026-11-06 的
     *    实测矩阵：本机只有这个源能**持续**拿到人声，其余源只有一记上电爆音。
     *
     * @return 打开成功的 [AudioRecord]；失败返回 null
     */
    private fun openRecord(): AudioRecord? {
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) {
            Log.e(TAG, "getMinBufferSize 失败: $minBuf")
            return null
        }

        // 缓冲给 4 帧，留够抖动余量：只给 1 帧的话，一次 GC 停顿就会丢采样，
        // 丢采样在 Opus 里表现为"咔"一声，服务端 ASR 会听到半个字。
        val bufBytes = maxOf(minBuf, FRAME_SAMPLES * 2 * HAL_CHANNELS_FALLBACK * 4)

        val rec = try {
            AudioRecord(
                AUDIO_SOURCE,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufBytes
            )
        } catch (e: Exception) {
            Log.e(TAG, "创建 AudioRecord 失败: ${e.message}", e)
            return null
        }

        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord 未初始化（麦克风多半被别的进程占着）state=${rec.state}")
            rec.release()
            return null
        }
        Log.i(TAG, "采集通道：请求 mono，源=$AUDIO_SOURCE（HAL 通道数运行时探测）")
        return rec
    }

    private fun captureLoop(rec: AudioRecord) {
        // ⚠️⚠️ **HAL 通道数必须运行时探测，不能写死**（2026-11-06 实机）。
        //
        //    同一个 `CHANNEL_IN_MONO` 请求，不同音频源回来的数据不同：
        //
        //        VOICE_RECOGNITION  → 32000 样本/s ⇒ 2ch 交错
        //        VOICE_COMMUNICATION → 16000 样本/s ⇒ 真单声道
        //
        //    写死 2ch 的话，在真单声道源上会**每帧读 120ms 的音频**、
        //    再隔一个取一个 ⇒ 采样率腰斩 ⇒ 声音慢一倍、音调减半。
        //
        //    探测办法：**先固定按 mono 读**（每帧 960 short），用实测帧率反推：
        //      · 实测帧率 ≈ 16.7 帧/s ⇒ HAL 是 mono，直接用；
        //      · 实测帧率 ≈ 33.3 帧/s ⇒ HAL 是 2ch 交错，
        //        正式读时每帧要读 2×960 个 short 再抽左声道。
        //
        //    ⚠️ **探测阶段的读帧长度必须固定**，否则判据自相矛盾：
        //      若一开始就读 1920 short，mono 源上每帧要等 120ms、
        //      帧率会变成 8.3 帧/s ⇒ 判反。
        //
        //    判据写成纯函数 [detectHalChannels]，可单测（见 JVM 测试）。
        val detected = probeHalChannels(rec)
        val ch = detected
        channels = detected
        Log.i(
            TAG,
            "HAL 通道数探测完成：${ch}ch（实测 ${"%.1f".format(probeFrameRate)} 帧/s）" +
                if (ch == 1) " ⇒ 真单声道，直接用" else " ⇒ 2ch 交错，读 ${FRAME_SAMPLES * ch} short/帧 → 抽左声道"
        )

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
            // 每帧取快照：KWS ↔ 上行链路切换时不重启 AudioRecord，
            // 也不会出现「半帧喂给旧消费者」。
            val c = consumer
            c(mono, info)
        }
    }

    /**
     * 探测 HAL 实际给的通道数。
     *
     * ⚠️ **探测阶段的读帧长度固定为 mono**（每帧 960 short）——
     *    否则判据自相矛盾：若按 2ch 读 1920 short，mono 源上每帧要等
     *    120ms、实测帧率变成 8.3 帧/s，会被误判成 2ch。
     *
     * 做法：连续读若干帧，量出**每秒能读满多少帧**。
     *   · mono 源     ⇒ 每帧覆盖 60ms 真实音频 ⇒ ≈16.7 帧/s
     *   · 2ch 交错源  ⇒ 每帧覆盖 30ms 真实音频 ⇒ ≈33.3 帧/s
     *
     * 探测期间的数据直接丢弃（上电爆音 + 启动延迟本来也不要）。
     *
     * @return 1 = mono；2 = 2ch 交错
     */
    private fun probeHalChannels(rec: AudioRecord): Int {
        val buf = ShortArray(FRAME_SAMPLES)
        var frames = 0
        val t0 = android.os.SystemClock.elapsedRealtime()
        // 至少测 400ms、至少 4 帧，否则帧率抖动大到没法判
        while (running && android.os.SystemClock.elapsedRealtime() - t0 < PROBE_MIN_MS) {
            var off = 0
            var failed = false
            while (off < buf.size && running) {
                val n = rec.read(buf, off, buf.size - off)
                if (n <= 0) {
                    if (n == AudioRecord.ERROR_INVALID_OPERATION ||
                        n == AudioRecord.ERROR_BAD_VALUE
                    ) {
                        failed = true
                        break
                    }
                    Thread.sleep(5)
                    continue
                }
                off += n
            }
            if (failed) break
            frames++
        }
        val dt = android.os.SystemClock.elapsedRealtime() - t0
        probeFrameRate = if (dt > 0) frames * 1000.0 / dt else 0.0
        return detectHalChannels(probeFrameRate)
    }

    /**
     * 实测帧率（帧/秒），只在探测阶段有意义。
     */
    @Volatile
    private var probeFrameRate: Double = 0.0

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
        //
        // ⚠️ **自调用不能 join 自己**（2026-11-06 真机定位）：
        //    VAD 收尾 `endListening()` / KWS `fire()` 都在**采集线程**里调
        //    stop()，此时 `t === Thread.currentThread()`。`join(500)` 必然
        //    等满 500ms 超时，于是
        //      · 打出「采集线程 500ms 内没退出」**假警报**（日志里反复出现，
        //        被误读成"旧线程还活着、并发踩编码器"）；
        //      · `record?.release()` 排在 join 之后 ⇒ 麦克风句柄白占 500ms，
        //        紧接着新建采集就撞 HAL `input device already opened`。
        //    自调用时 `rec.stop()` 已让阻塞的 read 返回错误码，本函数在
        //    返回后线程自然退出，不需要也不应该 join。
        var joined = true
        if (t != null && t !== Thread.currentThread()) {
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

        /**
         * **HAL 实际给的通道数**（不是我们请求的）。
         *
         * ⚠️ **不再写死** —— 2026-11-06 实机证明同一个 `CHANNEL_IN_MONO` 请求，
         * 不同音频源回来的通道结构不同：
         *
         * | 音频源 | 实测样本率 | HAL 实际 |
         * | --- | --- | --- |
         * | `VOICE_RECOGNITION` | 32000/s | 2ch 交错（右通道恒为数字零） |
         * | `VOICE_COMMUNICATION` | 16000/s | **真单声道** |
         *
         * 写死 2ch 在真单声道源上会「每帧读 120ms + 隔一个取一个」
         * ⇒ 采样率腰斩 ⇒ 声音慢一倍、音调减半。
         *
         * ⇒ 现在**运行时探测**（见 [detectHalChannels]）。
         * 这个常量只是**探测失败时的兜底**（取 2 是更保守的一侧：
         * 交错流按 mono 读会快一倍，但不会把音频内容搞乱；
         * 而真 mono 按 2ch 读会慢一倍且抽错通道）。
         */
        const val HAL_CHANNELS_FALLBACK = 2

        /**
         * 采集用的音频源。
         *
         * ⚠️ **必须是 `VOICE_COMMUNICATION`**（2026-11-06 实机定案）。
         *
         * 同一台设备、同一说话人、同一次矩阵运行内逐组落盘分析：
         *   · `VOICE_RECOGNITION` → 能量全在**第一记上电爆音**里，
         *     之后一路衰减到 rms≈10，整个窗口**没有持续语音**；
         *   · `VOICE_COMMUNICATION` → 启动延迟约 1.05 秒，之后是
         *     **持续人声**（rms 900~1400，频谱是典型语音形状）。
         *
         * ⚠️ 别用「整窗 rms 大」判断「能录到人声」—— 上电爆音的 rms
         *    比人声还大（实测 8187 vs 1380）。必须**分段**看能量形态。
         */
        const val AUDIO_SOURCE = MediaRecorder.AudioSource.VOICE_COMMUNICATION

        /**
         * 探测 HAL 通道数的最短时长（毫秒）。
         *
         * 400ms ≈ 7 帧（mono）或 13 帧（2ch），帧率抖动不足以翻转判据。
         */
        private const val PROBE_MIN_MS = 400L

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

        /**
         * 由**实测帧率**判定 HAL 实际给的通道数。
         *
         * 探测阶段固定按 mono 读（每帧 960 short @16kHz = 60ms）：
         *   · HAL 给 mono     ⇒ 每秒 1000/60 ≈ 16.7 帧
         *   · HAL 给 2ch 交错 ⇒ 每秒 1000/30 ≈ 33.3 帧
         *
         * 判据用**中点**（25 帧/s）分开，两侧都留了 50% 余量。
         *
         * ⚠️ 这个判据的前提是「探测阶段的读帧长度固定为 mono」。
         *    若探测时按 2ch 读 1920 short，mono 源上每帧要等 120ms、
         *    帧率变成 8.3 帧/s ⇒ 判反成 2ch ⇒ 之后每帧读 120ms、
         *    声音慢一倍。**判据与前提必须一起改。**
         *
         * @param framesPerSec 实测帧率（帧/秒）
         * @return 1 = mono；2 = 2ch 交错
         */
        internal fun detectHalChannels(framesPerSec: Double): Int {
            if (framesPerSec <= 0.0) return 1
            val monoFrameRate = 1000.0 / 60.0          // ≈16.7
            val stereoFrameRate = 1000.0 / 30.0        // ≈33.3
            val midpoint = (monoFrameRate + stereoFrameRate) / 2.0   // ≈25
            return if (framesPerSec > midpoint) 2 else 1
        }
    }
}
