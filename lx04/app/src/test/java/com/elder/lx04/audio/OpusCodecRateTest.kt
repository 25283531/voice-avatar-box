package com.elder.lx04.audio

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 下行解码采样率的回归测试 —— 防「改回 24k 又变成 10kHz 噪声」。
 *
 * 2026-11-06 真机事故：Concentus 在 24kHz 输出时解服务端的
 * Hybrid/SILK 语音包会吐出 10kHz 单峰噪声，老人完全听不出内容；
 * 48kHz 输出则是正常语音（谱质心 1587Hz）。
 * 这个错**编译得过、单测全绿、日志无异常**，只能靠钉死常量防回归。
 */
class OpusCodecRateTest {

    @Test
    fun `下行解码固定 48kHz，忽略 hello 的 24k`() {
        val c = OpusCodec()
        c.init(24_000)
        assertEquals(48_000, c.outputSampleRate())
        assertEquals(2_880, c.decodeFrameSamples)
        // 上行编码不受影响
        assertEquals(960, c.encodeFrameSamples)
    }

    @Test
    fun `换成别的 hello 采样率也不改 48k`() {
        val c = OpusCodec()
        c.init(16_000)
        assertEquals(48_000, c.outputSampleRate())
    }
}
