package com.elder.lx04.audio

/**
 * 录音前处理：直流阻断 → 高通 → 噪声底跟踪 → 自动增益 → 语音活动判定。
 *
 * ## 2026-11-04 实机更正（这条结论推翻过之前的判断，留痕）
 *
 * 之前从 `dumpsys media.audio_flinger` 里看到
 *
 *     XML effect configuration failed to load.
 *     Input thread AudioIn_1E ... 0 Effect Chains
 *
 * 就判定"平台效果挂不上，必须纯软件兜底"。**这个判断是错的** ——
 * `AudioIn_1E` 是 `MiVpmService`（小爱唤醒）的线程，那 0 条链是**它的**，
 * 不是我们的。
 *
 * 我们自己的录音会开一条**独立的** input thread。实测（设备 10.0.1.184，
 * 录 5 秒时抓 `dumpsys`）：
 *
 *     Input thread AudioIn_3E, tid 3989, type 3 (RECORD):
 *       Audio source: 6 (voice recognition)
 *       Input device: 0x80000004 (AUDIO_DEVICE_IN_BUILTIN_MIC)
 *       Active Client Session 89
 *       1 Effect Chains
 *         3 effects for session 89
 *           Effect ID 83: Noise Suppression  (AOSP)
 *           Effect ID 91: Acoustic Echo Canceler
 *           Effect ID 9x: Automatic Gain Control
 *
 * 也就是说 **NS / AEC / AGC 三件套是真挂上并且真在跑的**。
 * `NoiseSuppressor.create()` 返回非 null 是可信的，不是空壳。
 *
 * ## 所以增益不能无条件做
 *
 * 平台 AGC 已经在我们前面动过增益了。如果软件再叠一层最高 8 倍的放大，
 * 轻则"抽气"，重则直接削顶失真 —— 而且是在平台 AGC 已经把弱音提上来的
 * 基础上再提，噪声会被放大到 ASR 无法忍受。
 *
 * 因此 [softwareAgc] 是一个**开关**：
 *   · 平台 AGC 挂上了  → 关掉软件增益，本类只做噪声底跟踪 + VAD
 *   · 平台 AGC 挂不上  → 打开软件增益兜底
 *
 * 开关由 [AudioCapture] 在挂效果之后设置，日志里会打清楚走的是哪条。
 *
 * ## 每一级在解决什么
 *
 * 1. **直流阻断**：MTK 的 ADC 有几十个 LSB 的直流偏置。不除掉的话
 *    下面算 RMS 会被这个恒定分量抬高，VAD 就永远判"有声音"。
 *
 * 2. **120Hz 高通**：音箱放在桌上/柜子上，空调、冰箱、变压器的低频轰鸣
 *    全在 100Hz 以下。老人说话的声音基频在 100~300Hz，切 120Hz 会伤到
 *    一点低频，但换来的是"没有嗡嗡声"—— 服务端 ASR 对低频噪声非常敏感。
 *
 * 3. **噪声底跟踪**：持续估计"环境有多吵"。用**快降慢升**的方式跟踪
 *    （安静时立刻降下来，变吵时慢慢升上去），这样老人一开口，
 *    噪声底还停在安静时的值，信噪比才算得准。
 *
 * 4. **自动增益**（见上，可能被关掉）：老人对着音箱说话，距离从 30cm
 *    到 3m 都有可能，音量能差 20dB。目标是把语音段拉到 -26dBFS 附近。
 *    **只对语音帧提增益**，静音帧不动 —— 否则安静时会去放大底噪，
 *    服务端 ASR 听到的是一片嘶嘶声，识别率反而更低。
 *    增益变化做平滑（每帧最多变 1.5dB），突变会产生"抽气"声。
 *
 * 5. **VAD**：能量判据 + 滞回。用于两件事：判断"说完了没有"（自动结束
 *    一轮聆听），以及给增益决定"这一帧要不要提"。
 *    **平台效果不提供 VAD**，所以这一级无论走哪条路都必须自己算。
 *
 * ## 关于回声消除
 *
 * 平台 AEC 已经挂上了，但**不要指望它能救半双工**：AEC 的效果取决于
 * 参考信号与声学路径的对齐，设备外放音量大的时候残留回声仍然会进 ASR。
 * 所以上层仍然是半双工（说话不播、播不说话），AEC 只是多一层保险。
 */
class AudioPreProcess(
    /** 采样率，必须与采集一致（16kHz）。 */
    private val sampleRate: Int = 16_000
) {

    /** 直流阻断的记忆项。 */
    private var dcX1 = 0f
    private var dcY1 = 0f

    /** 高通滤波器的记忆项。 */
    private var hpX1 = 0f
    private var hpY1 = 0f

    /** 当前估计的噪声底（线性 RMS，0~32767 量纲）。 */
    private var noiseFloor = 200f

    /** 当前增益（线性倍数）。1.0 = 不放大。 */
    private var gain = 1f

    /** VAD 滞回：上一帧是否判定为语音。 */
    private var speaking = false

    /** 连续静音帧数 —— 用来判断"说完了"。 */
    private var silentFrames = 0

    /**
     * 是否启用**软件**增益。
     *
     * 默认 true（最坏情况的兜底）。[AudioCapture] 挂完平台效果后会按
     * 实际情况改写：平台 AGC 挂上了就设 false，避免两层 AGC 叠加。
     */
    @Volatile
    var softwareAgc: Boolean = true

    /** 高通系数：一阶高通，截止 120Hz。 */
    private val hpAlpha = run {
        val rc = 1.0 / (2 * Math.PI * 120.0)
        val dt = 1.0 / sampleRate
        (rc / (rc + dt)).toFloat()
    }

    /**
     * 处理一帧。
     *
     * @param pcm  就地修改（省一次分配 —— 设备只有 1GB 内存，每秒 50 帧）
     * @return 本帧的判定结果
     */
    fun process(pcm: ShortArray, length: Int): Frame {
        // ---- 1. 直流阻断 + 2. 高通 ----
        for (i in 0 until length) {
            val x = pcm[i].toFloat()

            // 直流阻断：y = x - x1 + 0.995 * y1
            val dc = x - dcX1 + 0.995f * dcY1
            dcX1 = x
            dcY1 = dc

            // 一阶高通
            val hp = hpAlpha * (hpY1 + dc - hpX1)
            hpX1 = dc
            hpY1 = hp

            pcm[i] = hp.toInt().coerceIn(-32768, 32767).toShort()
        }

        // ---- 3. 噪声底跟踪 ----
        val rms = rmsOf(pcm, length)
        // 快降慢升：安静下来立刻跟上，变吵了慢慢升（0.05 的系数 ≈ 20 帧≈1.2 秒）
        noiseFloor = if (rms < noiseFloor) {
            noiseFloor * 0.7f + rms * 0.3f
        } else {
            noiseFloor * 0.95f + rms * 0.05f
        }
        // 给一个地板，避免绝对安静时把噪声底压到 0，后面除零
        if (noiseFloor < 50f) noiseFloor = 50f

        // ---- 4. VAD ----
        // 滞回：进入要 3 倍噪声底，退出要低于 1.8 倍，避免在阈值附近抖
        val enter = noiseFloor * 3.0f
        val exit = noiseFloor * 1.8f
        val wasSpeaking = speaking
        speaking = if (speaking) rms > exit else rms > enter

        if (speaking) silentFrames = 0 else silentFrames++

        // ---- 5. 自动增益（只对语音帧，且仅当平台 AGC 没挂上时） ----
        if (softwareAgc && speaking && rms > 1f) {
            // 目标 -26dBFS ≈ 32767 * 10^(-26/20) ≈ 1640
            val target = 1640f
            val want = (target / rms).coerceIn(1f, MAX_GAIN)
            // 每帧最多变 1.5dB，避免"抽气"声
            val maxStep = 1.19f      // 10^(1.5/20)
            gain = when {
                want > gain -> minOf(want, gain * maxStep)
                want < gain -> maxOf(want, gain / maxStep)
                else -> want
            }
            if (gain > 1.001f) {
                for (i in 0 until length) {
                    pcm[i] = (pcm[i] * gain).toInt().coerceIn(-32768, 32767).toShort()
                }
            }
        }

        return Frame(
            rms = rms,
            noiseFloor = noiseFloor,
            gain = gain,
            isSpeech = speaking,
            /** 从语音切到静音的瞬间 —— "说完了"的信号。 */
            justStopped = wasSpeaking && !speaking
        )
    }

    /** 重置状态（每轮对话开始时调）。 */
    fun reset() {
        dcX1 = 0f; dcY1 = 0f; hpX1 = 0f; hpY1 = 0f
        gain = 1f
        speaking = false
        silentFrames = 0
        // noiseFloor 刻意**不重置** —— 环境噪声是连续的，每轮重估会让
        // 第一帧误判成语音。
    }

    /** 连续静音了多少帧（[frameMs] 毫秒一帧时，乘以它得到毫秒）。 */
    fun silentFrameCount(): Int = silentFrames

    /** 一帧的处理结果。 */
    data class Frame(
        /** 处理前的 RMS（0~32767）。 */
        val rms: Float,
        /** 当前噪声底估计。 */
        val noiseFloor: Float,
        /** 当前增益倍数。 */
        val gain: Float,
        /** 是否判定为语音。 */
        val isSpeech: Boolean,
        /** 本帧是否刚由"说话"切到"静音"。 */
        val justStopped: Boolean
    )

    private fun rmsOf(pcm: ShortArray, length: Int): Float {
        if (length <= 0) return 0f
        var sum = 0.0
        for (i in 0 until length) {
            val v = pcm[i].toDouble()
            sum += v * v
        }
        return kotlin.math.sqrt(sum / length).toFloat()
    }

    private companion object {
        /**
         * 最大增益 8 倍（≈ +18dB）。
         *
         * 再高就会把底噪也放大到能听出来的程度，服务端 ASR 反而更差。
         * 老人离得远时宁可让他"再说一遍"，也不要喂一坨噪声进去。
         */
        const val MAX_GAIN = 8f
    }
}
