package com.elder.lx04.audio

import org.junit.Assert.assertArrayEquals
import org.junit.Test

/**
 * 通道抽离的回归测试 —— 挡住"声音慢一倍"这个静默故障。
 *
 * ## 现场事实
 *
 * 2026-11-05 实机读 `/proc/asound/card0/pcm1c/sub0/hw_params`：
 *
 * ```
 * access: RW_INTERLEAVED
 * format: S16_LE
 * channels: 2          ← HAL 给的是双声道
 * rate: 16000
 * ```
 *
 * 而代码原先用 `CHANNEL_IN_MONO` 去读。MTK 的 HAL **没有**把 stereo
 * downmix 成 mono 就把双声道给了，于是"按单声道读"读到的是
 * **左右交错的立体声**：
 *
 *   · 每帧 960 个 short 只覆盖 960/2/16000 = **30ms** 真实音频
 *     （协议要求 60ms）⇒ 服务端按 60ms 播放 ⇒ 声音慢一倍、音调减半；
 *   · 相邻样本其实是 L、R 两路，信号被 2 倍频化。
 *
 * 现场症状对得上：link_test 跑 5 秒采到 **165 帧**
 * （5s/60ms 本该 83 帧，165 ≈ 83×2）。
 *
 * ## 为什么拆成纯函数
 *
 * 通道错在真机上表现为"声音慢一倍"，听感上很容易被误判成"麦克风坏了"；
 * 而抽左声道这件事是纯数组运算，完全可以在 JVM 上钉死。
 *
 * ## 反向变异（这仓的规矩）
 *
 *   · 抽成右声道（`src[i*ch+1]`）→ 第 1 条必须红
 *   · 改成左右平均（downmix）→ 第 4 条必须红
 *   · 单声道也去乘 ch → 第 2 条必须红
 */
class AudioCaptureChannelTest {

    /** 立体声交错数据抽出来必须是**左声道**。 */
    @Test
    fun `立体声交错抽出的是左声道`() {
        val src = ShortArray(8) { it.toShort() } // 0,1,2,3,4,5,6,7 = L0 R0 L1 R1 ...
        val dst = ShortArray(4)
        AudioCapture.extractLeftChannel(src, dst, 4, 2)
        assertArrayEquals(
            "抽出来的必须是 L0 L1 L2 L3（取右声道或取平均都会错）",
            shortArrayOf(0, 2, 4, 6),
            dst,
        )
    }

    /** 单声道是恒等拷贝 —— 不能因为通道数是 1 就把下标乘 1 之外的东西。 */
    @Test
    fun `单声道是恒等拷贝`() {
        val src = shortArrayOf(1, -2, 3, -4)
        val dst = ShortArray(4)
        AudioCapture.extractLeftChannel(src, dst, 4, 1)
        assertArrayEquals("单声道必须原样拷贝", src, dst)
    }

    /** 四声道取第一路（左声道）。 */
    @Test
    fun `多声道取第一路`() {
        val src = shortArrayOf(10, 11, 12, 13, 20, 21, 22, 23)
        val dst = ShortArray(2)
        AudioCapture.extractLeftChannel(src, dst, 2, 4)
        assertArrayEquals(
            "四声道也应取第一路（左声道）",
            shortArrayOf(10, 20),
            dst,
        )
    }

    /**
     * **不能**把左右平均。
     *
     * 平均是"双声道 downmix"，听起来更自然，但会引入相位抵消：
     * 反相的 L/R 平均后是 0，等于把语音抹掉。所以这里必须取左声道。
     */
    @Test
    fun `不能取左右平均`() {
        val src = shortArrayOf(100, -100, 200, -200)
        val dst = ShortArray(2)
        AudioCapture.extractLeftChannel(src, dst, 2, 2)
        assertArrayEquals(
            "必须取左声道；取平均会得到 0,0（相位抵消）",
            shortArrayOf(100, 200),
            dst,
        )
    }

    /** 只取前 samples 个采样点，多余数据不能串到下一帧。 */
    @Test
    fun `只取指定长度的采样点`() {
        val src = shortArrayOf(1, 9, 2, 9, 3, 9, 4, 9)
        val dst = ShortArray(2)
        AudioCapture.extractLeftChannel(src, dst, 2, 2)
        assertArrayEquals(
            "只能取前 2 个采样点，不能多取",
            shortArrayOf(1, 2),
            dst,
        )
    }
}
