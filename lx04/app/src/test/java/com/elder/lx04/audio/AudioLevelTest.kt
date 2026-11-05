package com.elder.lx04.audio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.sqrt

/**
 * 下行语音电平归一化的回归测试。
 *
 * 对应 2026-11-05 真机事故：唤醒成功、服务端播了、AudioTrack 也写了，
 * 但老人听不到 —— 因为 TTS 的 rms 只有 223~435。这类故障**不报错、
 * 不崩溃、所有链路日志全绿**，只能靠"量电平"发现，所以增益逻辑必须
 * 有能打红的单测守着。
 */
class AudioLevelTest {

    /** 造一段指定 rms 的正弦，模拟服务端下发的安静 TTS。 */
    private fun sine(n: Int = 1440, amp: Int): ShortArray =
        ShortArray(n) { i ->
            (amp * kotlin.math.sin(2.0 * kotlin.math.PI * 220.0 * i / 24_000.0)).toInt().toShort()
        }

    private fun rms(a: ShortArray): Double {
        var s = 0.0
        for (v in a) s += v.toDouble() * v
        return sqrt(s / a.size)
    }

    @Test
    fun `安静的 TTS 会被放大到可闻电平`() {
        val quiet = sine(amp = 420)          // rms≈300，和真机测到的 223~435 同量级
        val raw = rms(quiet)
        assertTrue("样本应该是安静语音", raw in 200.0..500.0)

        // 用显式 8 倍验证放大能力（开局增益现在刻意设为 1 倍）
        val boosted = AudioLevel.amplify(quiet, 8f)
        val out = rms(boosted)
        assertTrue(
            "给定增益没把安静 TTS 抬起来：raw=${raw.toInt()} out=${out.toInt()}",
            out > raw * 3.5
        )
        // 开局必须不放大：解码修好后正常语音不该被削爆
        assertEquals(
            "开局增益应保持 1 倍",
            rms(quiet), rms(AudioLevel.amplify(quiet, AudioLevel.INITIAL_GAIN)), 1.0
        )
        // 一个窗口之后增益应继续向上收敛（目标 2500）
        var g = AudioLevel.INITIAL_GAIN
        repeat(6) { g = AudioLevel.nextGain(raw, g) }
        assertTrue("增益应向目标收敛，实际 $g", g > 3.5f)
        assertTrue(
            "稳态后 rms 应接近目标 ${AudioLevel.TARGET_RMS}，实际 ${rms(AudioLevel.amplify(quiet, g)).toInt()}",
            rms(AudioLevel.amplify(quiet, g)) > 1_000
        )
    }

    @Test
    fun `全零静音帧不产生 NaN 也不把增益顶到最大`() {
        val silent = ShortArray(1440)
        assertEquals(0.0, AudioLevel.stats(silent).first, 0.0)
        val g = AudioLevel.nextGain(AudioLevel.stats(silent).first, 3.0f)
        assertEquals(3.0f, g, 0.0001f)
        val out = AudioLevel.amplify(silent, g)
        assertEquals(0.0, rms(out), 0.0)
    }

    @Test
    fun `分段线性：线性区完全不失真，只有尖峰被限幅`() {
        // gain=8 ⇒ knee=30000/8=3750；amp=1000 的正弦全在线性区。
        val small = sine(amp = 1_000)
        val out = AudioLevel.amplify(small, 8f)
        for (i in small.indices) {
            assertEquals("线性区不该产生任何失真 @$i", small[i].toInt() * 8, out[i].toInt())
        }

        // 满幅帧：只有超出 knee 的尖峰被压，其余不越界/不翻号/不被压小
        val loud = sine(amp = 20_000)
        val clipped = AudioLevel.amplify(loud, 8f)
        val peak = clipped.maxOf { kotlin.math.abs(it.toInt()) }
        assertTrue("输出峰值不得超过天花板，实际 $peak", peak <= AudioLevel.CEILING)
        for (i in loud.indices) {
            val a = loud[i].toInt(); val b = clipped[i].toInt()
            if (a > 0) assertTrue("正样本被翻号或压没了: $a -> $b", b > 0)
            if (a < 0) assertTrue("负样本被翻号或压没了: $a -> $b", b < 0)
            assertTrue("输出不应比输入还小: $a -> $b", kotlin.math.abs(b) >= kotlin.math.abs(a))
        }
    }

    @Test
    fun `增益逐帧平滑，绝不从 8 一步跳到 1`() {
        var g = 8f
        val steps = mutableListOf<Float>()
        repeat(40) { g = AudioLevel.ramp(g, 1f); steps.add(g) }
        assertEquals(1f, g, 0.001f)
        for (i in 1 until steps.size) {
            val jump = kotlin.math.abs(steps[i] - steps[i - 1])
            assertTrue("第 $i 帧增益跳变了 $jump", jump <= AudioLevel.RAMP_STEP + 0.0001f)
        }
        assertTrue("应当花很多帧过渡，实际第一步就到 1", steps.first() > 7f)
        // 反向（升增益）同样平滑
        var up = 1f
        repeat(3) { up = AudioLevel.ramp(up, 16f) }
        assertTrue(up < 2f)
    }

    @Test
    fun `软限幅能把语音平均能量抬到正弦自检音的量级`() {
        // 真机 TTS：rms 223~435、peak 2792~9191（峰值因子 12~21）。
        // 等比缩回会被尖峰拖累；tanh 让尖峰单独饱和、其余照常放大。
        val quiet = sine(amp = 420)
        val out = AudioLevel.amplify(quiet, 8f)
        val ratio = rms(out) / rms(quiet)
        assertTrue("平均响度提升不足，只放大了 ${"%.1f".format(ratio)} 倍", ratio > 6.0)
        assertTrue("输出峰值仍受控", out.maxOf { kotlin.math.abs(it.toInt()) } <= AudioLevel.CEILING)
    }

    @Test
    fun `本来就响的音频不会被继续放大`() {
        val g = AudioLevel.nextGain(9_000.0, AudioLevel.INITIAL_GAIN)
        assertTrue("响的音频不该加增益，实际 $g", g < 2.0f)
        val g2 = (1..6).fold(AudioLevel.INITIAL_GAIN) { acc, _ -> AudioLevel.nextGain(9_000.0, acc) }
        assertEquals("应收敛到 1 倍", 1.0f, g2, 0.001f)
    }

    @Test
    fun `amplify 不改动解码器给的源数组`() {
        val src = sine(amp = 1_000)
        val copy = src.copyOf()
        AudioLevel.amplify(src, 8f)
        assertTrue("源缓冲被改写了", src.contentEquals(copy))
    }

    @Test
    fun `增益上限封顶，底噪不会被无限放大`() {
        val g = (1..20).fold(1f) { acc, _ -> AudioLevel.nextGain(2.0, acc) }
        assertEquals(AudioLevel.MAX_GAIN, g, 0.001f)
        val noise = ShortArray(1440) { i -> if (i % 2 == 0) 2 else -2 }
        assertTrue(
            "底噪放大后仍在天花板内",
            AudioLevel.amplify(noise, g).all { kotlin.math.abs(it.toInt()) <= AudioLevel.CEILING }
        )
    }
}
