package com.elder.lx04.audio

import org.concentus.OpusApplication
import org.concentus.OpusEncoder
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
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
 * 设备日志里有 `编码失败: null`。打堆栈后才知道是 Concentus 内部的
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
 * 两个线程同时 `encode()`，内部状态会被写成不一致的值，
 * 下一帧就在断言里炸。
 *
 * 实机上这个并发窗口真实存在：
 * `AudioCapture.stop()` 原本先 `join(300)` 再 `record.stop()` ——
 * 而采集线程正阻塞在 `rec.read()` 里，**不会**因为 `running=false`
 * 立刻醒 ⇒ join 常常超时 ⇒ 旧采集线程还活着；
 * 下一次按键又起一个新采集线程 ⇒ 两个线程踩同一个编码器。
 *
 * ## ⚠️ 为什么测的是 [OpusCodec] 而不是裸 Concentus
 *
 * 第一版这个测试直接拿 `org.concentus.OpusEncoder` 并发调 —— 结果
 * **必然失败**（裸 Concentus 就是非线程安全的，不加锁当然炸）。
 * 那种测试测的是"Concentrus 非线程安全"这个**已知事实**，
 * 与生产代码是否正确无关 —— 它红了不代表代码有 bug，绿了也不代表代码没问题。
 *
 * 真正要锁住的是：**[OpusCodec] 在并发下不许失败**。
 * 所以这里直接测 [OpusCodec]（它内部有 `synchronized`）。
 * 为此 `build.gradle.kts` 开了 `unitTests.isReturnDefaultValues = true`，
 * 否则 `android.util.Log` 会抛 "not mocked"，[OpusCodec] 根本跑不起来。
 *
 * ## 反向变异（这仓的规矩）
 *
 * 每条断言都必须**能被对应变异变红**：
 *   · 去掉 [OpusCodec.encode] 里的 `synchronized` → 第 1 条必须失败
 *   · 去掉 [OpusCodec.encode] 里的自愈重建 → 第 1 条必须失败
 */
class OpusConcurrencyTest {

    /** 生成一段正弦，幅度取正常人声的量化范围。 */
    private fun tone(samples: Int, freq: Double = 440.0, amp: Int = 8000): ShortArray =
        ShortArray(samples) { i ->
            (amp * Math.sin(2 * Math.PI * freq * i / 16_000.0)).toInt().toShort()
        }

    /**
     * [OpusCodec] 被多个线程同时用，不许出现失败。
     *
     * 这是实机故障的直接复现：踩坏编码器 ⇒ 该帧编码失败 ⇒
     * `encode()` 返回 null ⇒ 上层 `return@AudioCapture` 跳过这一帧。
     * 如果断言把编码器的内部状态写坏了，**后续每一帧都会失败**，
     * 整轮会话一帧都发不出去。
     */
    @Test
    fun `OpusCodec 并发编码不许失败`() {
        val codec = OpusCodec()
        codec.init(24_000)

        val threads = 4
        val rounds = 50
        val failures = java.util.concurrent.atomic.AtomicInteger()
        val nulls = java.util.concurrent.atomic.AtomicInteger()
        val start = java.util.concurrent.CountDownLatch(1)
        val done = java.util.concurrent.CountDownLatch(threads)
        val pcm = tone(960)

        repeat(threads) {
            Thread {
                try {
                    start.await()
                    repeat(rounds) {
                        try {
                            val b = codec.encode(pcm)
                            if (b == null) nulls.incrementAndGet()
                            else if (b.isEmpty()) failures.incrementAndGet()
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

        assertEquals(
            "并发编码出现 $failures 次异常（应为 0）—— " +
                "Concentus 编码器不是线程安全的，OpusCodec.encode 必须加锁",
            0, failures.get(),
        )
        assertEquals(
            "并发编码有 $nulls 帧返回 null（应为 0）—— " +
                "编码失败会让上层跳过这一帧，整轮会话一帧都发不出去",
            0, nulls.get(),
        )
        assertEquals(
            "成功编码帧数应为 ${threads * rounds}",
            threads * rounds, codec.encodedFrames,
        )
    }

    /**
     * 单线程正常路径：编码器可用、计数正确。
     *
     * 这条同时守着 `init()` —— 曾经编码器与解码器共用一个 `runCatching`，
     * 解码器一抛异常就把编码器也置 null，上行音频就此全灭。
     */
    @Test
    fun `单线程编码正常工作且计数正确`() {
        val codec = OpusCodec()
        codec.init(24_000)
        assertNotNull("参数里不该出现 encoder=null", codec.describe())

        val pcm = tone(960)
        repeat(10) {
            val b = codec.encode(pcm)
            assertNotNull("第 $it 帧编码返回了 null", b)
            assertTrue("第 $it 帧编码结果为空", b!!.isNotEmpty())
        }
        assertEquals("成功编码 10 帧", 10, codec.encodedFrames)
        assertEquals("不该有失败", 0, codec.encodeFailures)
    }

    /**
     * 帧长不对时**跳过**而不是崩 —— 上层会传各种长度的缓冲。
     */
    @Test
    fun `帧长不足时跳过该帧`() {
        val codec = OpusCodec()
        codec.init(24_000)
        val short = tone(100)
        assertEquals("帧长不足应返回 null", null, codec.encode(short))
        assertEquals("不该计入成功", 0, codec.encodedFrames)
    }

    /**
     * 自愈路径：编码器失败后**重建并重试**，这一帧仍然要发出去。
     *
     * ## 为什么这条必须单独测
     *
     * 上面那条并发测试里，编码器**本来就不会失败** —— 所以自愈路径
     * 在那里永远走不到。去掉自愈逻辑，并发测试照样全绿 ⇒ **假绿**。
     *
     * 真实故障里，"踩坏一次"之后**后续每一帧都会失败**
     * （断言已经把内部状态写坏了），表现是整轮会话一帧都发不出去。
     * 所以"失败后重建"不是锦上添花，是这条链路能不能活的关键。
     *
     * 做法：注入一个"第一次返回坏编码器、之后返回好编码器"的工厂，
     * 让失败**确定性地**发生一次，然后验证：
     *   · 这一帧仍然编出了包（重试成功）
     *   · 自愈计数 +1
     */
    @Test
    fun `编码失败后重建编码器并重试`() {
        val codec = OpusCodec()
        val good = OpusEncoder(
            16_000, 1, OpusApplication.OPUS_APPLICATION_VOIP
        ).apply {
            bitrate = 24_000
            useVBR = true
            setUseDTX(false)
            complexity = 5
        }
        // 第一次返回"坏"的编码器（encode 一定抛），之后返回好的
        var calls = 0
        codec.encoderFactory = {
            calls++
            if (calls == 1) BrokenEncoder() else good
        }
        codec.init(24_000)

        val b = codec.encode(tone(960))
        assertNotNull("重建后重试应该编出包", b)
        assertTrue("编出的包不该为空", b!!.isNotEmpty())
        assertEquals("自愈计数应为 1", 1, codec.encodeRecoveries)
        assertEquals("不该算彻底失败", 0, codec.encodeFailures)
        assertEquals("成功计数应为 1", 1, codec.encodedFrames)
    }

    /**
     * 重建也失败时如实计数，不假装成功。
     */
    @Test
    fun `重建也失败时如实计数`() {
        val codec = OpusCodec()
        codec.encoderFactory = { BrokenEncoder() }
        codec.init(24_000)

        val b = codec.encode(tone(960))
        assertEquals("重建失败该帧应返回 null", null, b)
        assertEquals("应计入彻底失败", 1, codec.encodeFailures)
        assertEquals("不该计入成功", 0, codec.encodedFrames)
    }

    /** 一个"必定抛异常"的编码器 —— 模拟实机上被并发踩坏的编码器。 */
    private class BrokenEncoder : OpusEncoder(16_000, 1, OpusApplication.OPUS_APPLICATION_VOIP) {
        override fun encode(
            in_pcm: ShortArray, pcm_offset: Int, frame_size: Int,
            out_data: ByteArray, out_data_offset: Int, max_data_bytes: Int
        ): Int = throw IllegalStateException("模拟：编码器内部状态坏了")

        override fun encode(
            in_pcm: ByteArray, pcm_offset: Int, frame_size: Int,
            out_data: ByteArray, out_data_offset: Int, max_data_bytes: Int
        ): Int = throw IllegalStateException("模拟：编码器内部状态坏了")
    }
}
