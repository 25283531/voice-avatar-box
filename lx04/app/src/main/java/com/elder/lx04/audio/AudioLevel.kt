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
     * 目标 rms。**保守补底，不主动放大**。
     *
     * 这一串参数是 2026-11-06 根因定位后回退的：先前以为「语音太小」，
     * 一路把目标抬到 5500、开局 8 倍、上限 16 倍；但真因是 Concentus
     * 24kHz 解码吐出 10kHz **噪声**，放大它只会更刺耳。
     * 解码修成 48kHz 后，服务端 libopus 解出的正常语音 RMS≈2600，
     * 已经足够响 ⇒ 开局不放大、上限 4 倍只作兜底。
     */
    const val TARGET_RMS = 2_500f

    /** 增益上限。配合 tanh 软限幅，允许更高而不硬削波。 */
    const val MAX_GAIN = 4f

    /** 开局增益：第一句话前半段还没攒够窗口，先给一个够用的放大。 */
    const val INITIAL_GAIN = 1f

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
        // 只算**目标**；平滑交给 [ramp] 逐帧做。
        // 早先在这里做窗口级平滑，结果增益在窗口边界一步从 8 跳到 1，
        // 同一段正弦前半段近方波、后半段恢复正弦 ⇒ 真机听到「中途变频率」。
        return (TARGET_RMS / rawRms).toFloat().coerceIn(1f, MAX_GAIN)
    }

    /** 每帧允许的最大增益变化。24kHz/60ms 下，8→1 约需 28 帧（1.7 秒）。 */
    const val RAMP_STEP = 0.25f

    /** 逐帧把 current 往 target 靠近，绝不一步跳变。 */
    fun ramp(current: Float, target: Float): Float {
        val d = target - current
        return if (kotlin.math.abs(d) <= RAMP_STEP) target
        else current + kotlin.math.sign(d) * RAMP_STEP
    }

    /**
     * 放大一帧，返回**新数组**（不改动解码器给的缓冲）。
     *
     * 分段线性：线性区完全不失真，只有超出 knee 的尖峰被压到 [CEILING]。
     * 等比缩回会把整帧平均能量压回去，tanh 又会在整个波形上产生谐波。
     */
    fun amplify(src: ShortArray, gain: Float): ShortArray {
        if (src.isEmpty()) return src
        // **分段线性**软限幅：
        //   |x| <= knee  → 完全线性放大 gain 倍（不产生任何谐波）
        //   |x| >  knee  → 只有尖峰被压到天花板
        // knee 取 CEILING/gain，保证线性区末端刚好到天花板。
        //
        // 为什么换掉 tanh：tanh 在整个波形上都非线性，深饱和时会产生
        // 大量谐波，真机听感是「中途变频率 / 声音发破」。语音峰值因子
        // 12~21，绝大多数样本都在线性区，只有零星尖峰需要压 ——
        // 分段线性既把整句平均响度抬起来，又保住音色。
        val knee = CEILING / gain.toDouble()
        val out = ShortArray(src.size)
        for (i in src.indices) {
            val x = src[i].toDouble()
            val y = if (kotlin.math.abs(x) <= knee) x * gain
            else kotlin.math.sign(x) * CEILING
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
