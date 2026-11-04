package com.elder.lx04.audio

/**
 * 录音前处理：直流阻断 → 高通 → 噪声底跟踪 → 自动增益 → 语音活动判定。
 *
 * ## 为什么要自己写，而不是直接开系统的 NS/AGC
 *
 * LX04 原厂的小爱唤醒（`MiVpmService`）走的也是 **AOSP 的音频预处理**，
 * 也就是 `/vendor/lib/soundfx/libaudiopreprocessing.so` 里的 WebRTC
 * AEC / NS / AGC 三件套 —— 这台机器上并没有小米自己另写的降噪库。
 *
 * 但实测（2026-11-04，设备 10.0.1.184）发现：
 *
 *     $ dumpsys media.audio_flinger | head
 *     XML effect configuration failed to load.
 *     ...
 *     Input thread 0xb0183d40, name AudioIn_1E, tid 488, type 3 (RECORD):
 *     Active Client Session S  Flags  Format  Chn mask  SRate
 *     0 Effect Chains            ← 一条效果链都没有
 *
 * 也就是说**效果配置没加载成功**，录音会话上挂不到任何效果链。
 * `NoiseSuppressor.create()` / `AutomaticGainControl.create()` 在这种机器上
 * 会返回 null（框架拿不到效果句柄）。所以：
 *
 *   1. [AudioCapture] 仍然**先尝试**挂平台效果 —— 挂上就白赚，零 CPU；
 *   2. 挂不上就由本类兜底，纯 Kotlin 自己算。
 *
 * 两条路的开关状态会在日志里打出来，装机时一眼能看出走的是哪条。
 *
 * ## 每一级在解决什么（都是实机听感问题，不是教科书凑数）
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
 * 4. **自动增益**：老人对着音箱说话，距离从 30cm 到 3m 都有可能，
 *    音量能差 20dB。目标是把语音段拉到 -26dBFS 附近。
 *    **只对语音帧提增益**，静音帧不动 —— 否则安静时会去放大底噪，
 *    服务端 ASR 听到的是一片嘶嘶声，识别率反而更低。
 *    增益变化做平滑（每帧最多变 1.5dB），突变会产生"抽气"声。
 *
 * 5. **VAD**：能量判据 + 滞回。用于两件事：判断"说完了没有"（自动结束
 *    一轮聆听），以及给增益决定"这一帧要不要提"。
 *
 * ## 不做的事
 *
 * **不做回声消除**。AEC 需要参考信号（正在播放的 TTS 音频），
 * 我们的播放和录音在同一个进程里，理论上能拿到，但要做到不跑偏
 * 得对齐延迟、估计声学路径 —— 那是几百行的自适应滤波器。
 * 现阶段用**半双工**回避：说话时不播放，播放时不录音。
 * 这也是对讲机时代的成熟做法，老人不会觉得别扭。
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

        // ---- 5. 自动增益（只对语音帧） ----
        if (speaking && rms > 1f) {
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
