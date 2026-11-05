package com.elder.lx04.audio

import org.concentus.OpusApplication
import org.concentus.OpusEncoder
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Concentus 编码器能不能真的编出包 —— 纯 JVM 单测，不需要设备。
 *
 * ## 为什么要单独测这一层
 *
 * 实机日志只有一行 `编码失败: null`，打堆栈之后才看到是
 * `Inlines.OpusAssert` 抛的 `AssertionError`，位置在
 * `Resampler.silk_resampler` 和 `Autocorrelation.silk_warped_autocorrelation`。
 *
 * 这两个断言都在 SILK 内部状态上。可能的原因有三类：
 *   ① 我们的调用参数不对（帧长 / 采样率 / 码率）
 *   ② Concentus 这份源码本身有 bug
 *   ③ Android 特有（ART / 32 位）—— 纯 Java 数学运算，理论上不该有
 *
 * ①②在 JVM 上就能复现，③不能。所以先跑这个测试把范围缩小：
 * 这里失败 ⇒ 库或用法问题，改代码即可，与设备无关；
 * 这里通过 ⇒ 得回头看设备侧。
 *
 * 注意：这里**不能**测 [OpusCodec] 本身 —— 它调了 `android.util.Log`，
 * 在 JVM 单测里没有实现，会抛 "not mocked"。
 */
class OpusEncodeTest {

    /** 生成一段 440Hz 正弦，幅度取正常人声的量化范围。 */
    private fun tone(samples: Int, freq: Double = 440.0, amp: Int = 8000): ShortArray =
        ShortArray(samples) { i ->
            (amp * Math.sin(2 * Math.PI * freq * i / 16_000.0)).toInt().toShort()
        }

    private fun newEncoder(): OpusEncoder =
        OpusEncoder(16_000, 1, OpusApplication.OPUS_APPLICATION_VOIP).apply {
            bitrate = 24_000
            useVBR = true
            setUseDTX(false)
            complexity = 5
        }

    @Test
    fun `16k 60ms 单声道可以编码出包`() {
        val enc = newEncoder()
        val out = ByteArray(1275)
        var total = 0
        // 连编 10 帧：第一帧走的是初始化路径，后面几帧才验证状态是否稳定
        repeat(10) {
            val n = enc.encode(tone(960), 0, 960, out, 0, out.size)
            assertTrue("第 $it 帧编码返回 $n，应为正数", n > 0)
            total += n
        }
        println("10 帧共 $total 字节，平均 ${total / 10}")
    }

    @Test
    fun `16k 20ms 单声道可以编码出包`() {
        val enc = newEncoder()
        val out = ByteArray(1275)
        repeat(10) {
            val n = enc.encode(tone(320), 0, 320, out, 0, out.size)
            assertTrue("第 $it 帧编码返回 $n，应为正数", n > 0)
        }
    }

    @Test
    fun `全静音帧不会抛断言`() {
        // 真实场景里老人按下键之后经常先是几帧静音。
        // 如果静音会触发 SILK 内部断言，那按住说话永远起不来。
        val enc = newEncoder()
        val out = ByteArray(1275)
        repeat(10) {
            val n = enc.encode(ShortArray(960), 0, 960, out, 0, out.size)
            assertTrue("第 $it 帧（静音）编码返回 $n，应为非负", n >= 0)
        }
    }
}
