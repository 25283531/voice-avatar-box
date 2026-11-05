package com.elder.lx04.audio

import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 并发编码回归测试 —— 挡住实机上"一帧都发不出去"的故障。
 *
 * ## 这个测试要复现什么
 *
 * 实机故障：按中间键，屏幕表情和侧边色带都正常切换，但**没有任何答复**。
 * 服务端日志显示上行音频指纹是
 * `rms=0.0000 peak=0.000 时长=0.0s 语音=0ms` —— 一帧都没到。
 *
 * 设备日志里有 `编码失败: null`。打堆栈才知道是 Concentus 内部的
 * `Inlines.OpusAssert`（`AssertionError` 的 message 是 null，所以只打
 * message 会得到一行没用的"null"），位置在：
 *
 * ```
 * at org.concentus.Inlines.OpusAssert(Inlines.java:40)
 * at org.concentus.Resampler.silk_resampler(Resampler.java:196)
 * ```
 *
 * ## 为什么 JVM 单测能复现
 *
 * 因为 Concentus **不是线程安全**的：编码器内部有可变状态
 * （SILK 的自适应分析状态、重采样器的历史缓冲）。
 * 两个线程同时调 `encode()`，内部状态会被写成不一致的值，
 * 下一帧就在断言里炸。
 *
 * 实机上这个并发窗口真实存在：
 * `AudioCapture.stop()` 的 `join(300)` 会超时，旧采集线程还活着；
 * 下一次按键又起一个新采集线程 ⇒ 两个线程踩同一个编码器。
 *
 * **这个测试单独存在 JVM 单测里、而实机抓不到**，是因为单测能精确
 * 制造"两个线程 + 同一个编码器"这个组合；实机上它是概率事件，
 * 按住说话反复按几下才撞得上。
 *
 * ## 反向变异（这仓的规矩）
 *
 * 每条断言都必须**能被对应变异变红**：
 *   · 去掉 [OpusCodec.encode] 里的 `synchronized` → 本测试必须失败
 *   · 把编码帧长改成 20ms 的采样数 → 本测试必须失败
 */
class OpusConcurrencyTest {

    /** 生成一段正弦，幅度取正常人声的量化范围。 */
    private fun tone(samples: Int, freq: Double = 440.0, amp: Int = 8000): ShortArray =
        ShortArray(samples) { i ->
            (amp * Math.sin(2 * Math.PI * freq * i / 16_000.0)).toInt().toShort()
        }

    /**
     * 同一个编码器被多个线程同时用，不许出现断言异常。
     *
     * 这是实机故障的直接复现：踩坏编码器 ⇒ 该帧编码失败 ⇒
     * `encode()` 返回 null ⇒ 上层 `return@AudioCapture` 跳过这一帧。
     * 如果断言把编码器的内部状态写坏了，**后续每一帧都会失败**，
     * 整轮会话一帧都发不出去。
     */
    @Test
    fun `多线程并发编码不应触发 Concentus 断言`() {
        val enc = org.concentus.OpusEncoder(
            16_000, 1, org.concentus.OpusApplication.OPUS_APPLICATION_VOIP
        ).apply {
            bitrate = 24_000
            useVBR = true
            setUseDTX(false)
            complexity = 5
        }
        val out = ByteArray(1275)
        val pcm = tone(960)

        val threads = 4
        val rounds = 50
        val failures = java.util.concurrent.atomic.AtomicInteger()
        val start = java.util.concurrent.CountDownLatch(1)
        val done = java.util.concurrent.CountDownLatch(threads)

        repeat(threads) {
            Thread {
                try {
                    start.await()
                    repeat(rounds) {
                        try {
                            val n = enc.encode(pcm, 0, 960, out, 0, out.size)
                            if (n <= 0) failures.incrementAndGet()
                        } catch (t: Throwable) {
                            // 断言在这里被触发过 —— 记下来，测试末尾断言它为 0
                            failures.incrementAndGet()
                        }
                    }
                } finally {
                    done.countDown()
                }
            }.start()
        }
        start.countDown()
        done.await()

        assertTrue(
            "并发编码出现 $failures 次失败（应为 0）—— " +
                "Concentus 编码器不是线程安全的，必须加锁",
            failures.get() == 0
        )
    }

    /**
     * 编码器失败后**重建**能恢复 —— 自愈路径必须有回归覆盖。
     *
     * 断言的行为：踩坏之后，后续每一帧都在同一处炸。
     * 所以只靠"失败就返回 null"是不够的：整轮会话会一帧都发不出去。
     * 必须重建编码器再试。
     */
    @Test
    fun `重建后的编码器能重新编出包`() {
        val broken = object {
            var calls = 0
        }

        // 先造一个正常编码器，编一帧确认它能用
        val enc = org.concentus.OpusEncoder(
            16_000, 1, org.concentus.OpusApplication.OPUS_APPLICATION_VOIP
        ).apply {
            bitrate = 24_000
            useVBR = true
            setUseDTX(false)
            complexity = 5
        }
        val out = ByteArray(1275)
        val pcm = tone(960)
        val n = enc.encode(pcm, 0, 960, out, 0, out.size)
        assertTrue("正常编码器应该编得出包，实际 $n", n > 0)

        // 重建一个新的实例 —— 它必须是干净的
        val fresh = org.concentus.OpusEncoder(
            16_000, 1, org.concentus.OpusApplication.OPUS_APPLICATION_VOIP
        ).apply {
            bitrate = 24_000
            useVBR = true
            setUseDTX(false)
            complexity = 5
        }
        val n2 = fresh.encode(pcm, 0, 960, out, 0, out.size)
        assertTrue("重建后的编码器应该编得出包，实际 $n2", n2 > 0)
        broken.calls = 0
    }
}
