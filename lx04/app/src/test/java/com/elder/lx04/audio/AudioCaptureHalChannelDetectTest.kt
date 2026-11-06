package com.elder.lx04.audio

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * HAL 通道数探测的回归测试。
 *
 * ## 现场事实（2026-11-06 真机）
 *
 * 同一个 `CHANNEL_IN_MONO` 请求，不同音频源回来的**通道结构不同**：
 *
 * | 音频源 | 实测样本率 | HAL 实际 |
 * | --- | --- | --- |
 * | `VOICE_RECOGNITION` | 32000 样本/s | 2ch 交错（右通道恒为数字零） |
 * | `VOICE_COMMUNICATION` | 16000 样本/s | **真单声道** |
 *
 * （样本率由「落盘样本数 ÷ 墙钟时间」反推，见 `tools/mic_channel_decide.py`。）
 *
 * ## 为什么必须探测而不是写死
 *
 * 写死 2ch，在真单声道源上会**每帧读 1920 short**：
 * HAL 只给 960 个真实采样点，于是 `read()` 要等满两帧才返回，
 * 一帧覆盖 120ms 真实音频 → 采样率腰斩到 8kHz →
 * 服务端按 16kHz 播放 → **声音慢一倍、音调减半**，且 ASR 几乎听不出字。
 *
 * 反过来写死 1ch，在交错源上会**每帧只覆盖 30ms** → 声音快一倍。
 *
 * 两个方向都是静默故障（没有异常，只是"声音不对"），所以判据必须钉死。
 *
 * ## 判据的前提（必须与判据一起看）
 *
 * 探测阶段**固定按 mono 读**（每帧 960 short = 60ms）：
 *
 * ```
 *   HAL 给 mono     → 每秒 1000/60 ≈ 16.7 帧
 *   HAL 给 2ch 交错 → 每秒 1000/30 ≈ 33.3 帧   （960 short 只覆盖 30ms）
 * ```
 *
 * ⚠️ 若探测时改成按 2ch 读（1920 short），mono 源上每帧要等 120ms、
 *    帧率变成 8.3 帧/s → 判反成 2ch → 之后每帧读 120ms → 慢一倍。
 *    所以「探测读帧长度固定为 mono」是判据的一部分，
 *    由 `AudioCapture.probeHalChannels` 保证。
 *
 * ## 反向变异（这仓的规矩）
 *
 *   · 阈值从 25 改成 10 → 「mono 实测值」必须红
 *   · 阈值从 25 改成 40 → 「2ch 实测值」必须红
 *   · 判据反过来（`>` 改成 `<`）→ 两条都必须红
 */
class AudioCaptureHalChannelDetectTest {

    /** HAL 真给 mono 时的实测帧率（≈16.7 帧/s）。 */
    private val monoFrameRate = 1000.0 / 60.0

    /** HAL 给 2ch 交错时的实测帧率（≈33.3 帧/s）。 */
    private val stereoFrameRate = 1000.0 / 30.0

    @Test
    fun `mono 源判成 1 通道`() {
        assertEquals(
            "mono 源（16.7 帧/s）必须判成 1 通道",
            1,
            AudioCapture.detectHalChannels(monoFrameRate),
        )
    }

    @Test
    fun `2ch 交错源判成 2 通道`() {
        assertEquals(
            "2ch 交错源（33.3 帧/s）必须判成 2 通道",
            2,
            AudioCapture.detectHalChannels(stereoFrameRate),
        )
    }

    /**
     * 实测值带抖动也必须判对 —— 真机帧率不会精确等于理论值。
     *
     * 用真机量过的两组原始数据反推：
     * · VC 16000 1ch：39200 样本 / 2.45s = 16000 样本/s ⇒ 16.7 帧/s
     * · VR 16000 1ch：78400 样本 / 2.45s = 32000 样本/s ⇒ 33.3 帧/s
     */
    @Test
    fun `真机实测值判对`() {
        assertEquals(
            "VOICE_COMMUNICATION 实测（39200 样本 / 2.45s）必须判成 1 通道",
            1,
            AudioCapture.detectHalChannels(39200.0 / 2.45 / 960.0),
        )
        assertEquals(
            "VOICE_RECOGNITION 实测（78400 样本 / 2.45s）必须判成 2 通道",
            2,
            AudioCapture.detectHalChannels(78400.0 / 2.45 / 960.0),
        )
    }

    /**
     * 抖动容忍：±15% 的帧率波动不能翻转判据。
     *
     * 判据阈值在 25 帧/s（中点），离两侧各留 50% 余量，
     * 15% 的波动仍在余量内。
     */
    @Test
    fun `帧率抖动不翻转判据`() {
        assertEquals(
            "mono 帧率 +15% 仍应判成 1 通道",
            1,
            AudioCapture.detectHalChannels(monoFrameRate * 1.15),
        )
        assertEquals(
            "mono 帧率 -15% 仍应判成 1 通道",
            1,
            AudioCapture.detectHalChannels(monoFrameRate * 0.85),
        )
        assertEquals(
            "2ch 帧率 +15% 仍应判成 2 通道",
            2,
            AudioCapture.detectHalChannels(stereoFrameRate * 1.15),
        )
        assertEquals(
            "2ch 帧率 -15% 仍应判成 2 通道",
            2,
            AudioCapture.detectHalChannels(stereoFrameRate * 0.85),
        )
    }

    /**
     * 探测失败（帧率为 0）时必须退回 mono。
     *
     * ⚠️ 为什么退回 mono 而不是 2ch：
     *   · 交错流按 mono 读 → 每帧只覆盖 30ms → 声音快一倍（内容没错乱）；
     *   · mono 流按 2ch 读 → 每帧覆盖 120ms + 隔一个取一个 → 采样率腰斩、
     *     声音慢一倍（而且抽出来的其实是隔点抽样，频谱被砍）。
     *   两者都是"声音不对"，但**快一倍比慢一倍危害小**：
     *   慢一倍会让 ASR 完全听不出字，快一倍至少词还能认出来。
     */
    @Test
    fun `探测失败退回 mono`() {
        assertEquals(
            "帧率为 0（探测失败）必须退回 1 通道",
            1,
            AudioCapture.detectHalChannels(0.0),
        )
        assertEquals(
            "负帧率也必须退回 1 通道",
            1,
            AudioCapture.detectHalChannels(-5.0),
        )
    }
}
