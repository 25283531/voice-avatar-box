package com.elder.lx04.audio

/**
 * 下行语音电平归一化 —— 纯逻辑，不依赖 Android，便于 JVM 单测。
 *
 * ## 为什么必须做（2026-11-05 真机实测）
 *
 * 用户反馈「唤醒成功了，但听不到播放的声音」。逐层查下来链路全通：
 *  · 服务端两次 `播报完成（音频已发）`
 *  · 设备解码出的 PCM `rms=223.8 / 435.2, peak=2792 / 9191`
 *  · AudioTrack 写了约 5.5 秒，`dumpsys media.audio_flinger` 有 active 记录
 *  · ALSA `DL1_AWB_Record` 回采确实录到 440Hz 自检音（rms 575~7517）
 *  · `STREAM_MUSIC` 未静音、路由 speaker
 *
 * 断点是**电平**：TTS 的 rms 只有 223~435（满幅 32767），比自检正弦
 * （amp=10000 ⇒ rms≈7071）低约 20 倍（-26dB）。叠加系统音量 9~12/20
 * 之后，功放输入只有几十的幅度 —— 不是没播，是轻到听不见。
 * 音量对照实验：同一段正弦，音量 12 时 AWB 回采 rms=1500，
 * 音量 20 时 rms=7517（5 倍），说明音量链路本身正常，源头电平才是短板。
 *
 * ## 为什么不改服务端
 *
 * TTS 由多个供应商（Edge / 其它）产出，电平不统一；在服务端统一放大
 * 会影响**所有设备**（含已经调好音量的 ESP32-S3 固件板）。放在设备侧，
 * 每台设备各自按自己听到的结果归一化，风险最小。
 *
 * ## 为什么用滑动窗口而不是逐帧
 *
 * 逐帧归一化会让背景噪声被拉到和语音一样响，听起来像「沙沙」的泵噪。
 * 每 25 帧（1.5 秒，约一句话）统计一次，再平滑过渡，既跟得上不同
 * TTS 的电平差异，又不会一帧一变。
 */
object AudioLevel {

    /**
     * 目标 rms。**按平均响度对齐 440Hz 自检音**（amp=10000 ⇒ rms≈7071）。
     *
     * 这里踩过一次：最初设 2500，结果真机反馈「正弦很大、语音很小很小」。
     * 原因是语音的峰值因子高达 12~21（实测 `rms=223 / peak=2792`），
     * 把 rms 拉到 2500 时峰值就会顶到天花板；人耳听的是**平均能量**，
     * 不是峰值。所以目标必须看齐正弦自检音，并配合下面的软限幅。
     */
    const val TARGET_RMS = 5_500f

    /** 增益上限。配合 tanh 软限幅，允许更高而不硬削波。 */
    const val MAX_GAIN = 16f

    /** 开局增益：第一句话前半段还没攒够窗口，先给一个够用的放大。 */
    const val INITIAL_GAIN = 8f

    /** 输出天花板，留 2767 的余量给功放，避免硬削波失真。 */
    const val CEILING = 30_000

    /**
     * 按一个窗口的原始 rms 更新增益。
     *
     * 静音窗口（rms < 1）**保持原增益**：不能因为一句停顿就把增益顶到
     * 8 倍，否则下一句开头会突然炸响。
     */
    fun nextGain(rawRms: Double, previous: Float): Float {
        if (!rawRms.isFinite() || rawRms < 1.0) return previous
        val target = (TARGET_RMS / rawRms).toFloat().coerceIn(1f, MAX_GAIN)
        // ⚠️ 升得慢、降得快：
        //   · 上升平滑 —— 避免句子之间增益跳变造成「忽大忽小」；
        //   · 下降立即生效 —— 上一句很轻、这一句突然很响时，若还按平滑
        //     留着 2.2 倍增益，会把本来正常的一句推到刺耳甚至顶到天花板。
        //     这是单测抓出来的：响音频一次更新后仍被放大 2.2 倍。
        return if (target < previous) target
        else (previous * 0.4f + target * 0.6f).coerceIn(1f, MAX_GAIN)
    }

    /**
     * 放大一帧，返回**新数组**（不改动解码器给的缓冲）。
     *
     * 用 tanh 软限幅：尖峰平滑压到 [CEILING]，其余样本照常放大。
     * 逐样本硬截断会把波形削平，等比缩回又会把整帧平均能量压回去。
     */
    fun amplify(src: ShortArray, gain: Float): ShortArray {
        if (src.isEmpty()) return src
        // tanh 软限幅：小信号近似线性放大 gain 倍，大信号平滑压到天花板。
        //
        // 为什么不能再用「按帧峰值等比缩回」：语音的峰值因子高达 12~21，
        // 一旦某帧有个尖峰，整帧增益就被压回去，平均能量仍上不去 ——
        // 真机表现就是「峰值够了、人耳还是几乎听不见」。
        // 让尖峰单独被压、其余样本照常放大，才能把**整句平均响度**抬起来。
        val k = gain / CEILING.toDouble()
        val out = ShortArray(src.size)
        for (i in src.indices) {
            val y = CEILING * kotlin.math.tanh(src[i].toDouble() * k)
            out[i] = y.toInt().coerceIn(-CEILING, CEILING).toShort()
        }
        return out
    }

    /** 一帧的 rms 与峰值（用 double 累加，避免平方和溢出）。 */
    fun stats(src: ShortArray): Pair<Double, Int> {
        if (src.isEmpty()) return 0.0 to 0
        var sum = 0.0
        var peak = 0
        for (v in src) {
            val a = v.toInt()
            val abs = if (a < 0) -a else a
            if (abs > peak) peak = abs
            sum += a.toDouble() * a
        }
        return kotlin.math.sqrt(sum / src.size) to peak
    }
}
