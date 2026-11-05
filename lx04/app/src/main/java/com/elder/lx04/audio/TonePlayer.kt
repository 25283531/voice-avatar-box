package com.elder.lx04.audio

import android.media.AudioManager
import android.media.ToneGenerator
import android.util.Log

/**
 * 本地提示音 —— 对齐固件 `elder_ui_play_tone()` 的六种语义。
 *
 * ## 为什么用 `ToneGenerator` 而不是打包音频资源
 *
 * 固件是内置 OGG 片段（`elder_clip.c`），LX04 上我们没有对应资源。这里先用
 * 系统内置提示音**保证"有声音"**，而不是等定制音频到位 —— 因为：
 *
 * **提醒如果没有声音，等于没有提醒。** 老人不会盯着屏幕等 8:00 那一行字；
 * 屏幕只是"他走过来之后"才看的东西。所以 `tone(REMINDER)` 必须有声，
 * 这是功能而不是体验。
 *
 * ⚠️ **这是过渡方案**，与固件的差别要记住：
 * - 固件的 `REMINDER` 是一段约 **4.9 秒的音乐**，目的是把老人从另一个房间
 *   "叫过来"，然后再念事项；这里只能给一声长提示音，**叫不过来**。
 * - 后续应把 `res/raw/` 里的六段音频补齐（可用 `tools/make_reminder_chime.py`
 *   的同源素材），改用 `SoundPool` 播放，音色与固件一致。
 *
 * ## 线程
 *
 * `ToneGenerator` 的 `startTone` 是异步的（立刻返回），可以在主线程调用。
 * 但构造函数会**同步**打开音频输出，个别 ROM 上会阻塞几十毫秒 ——
 * 所以这里延迟到第一次真正要响时才创建（见 [ensure]），避免拖慢启动。
 */
class TonePlayer {

    private var gen: ToneGenerator? = null
    private var failed = false

    /** 播放一个提示音（编号见 [com.elder.lx04.reminder.ReminderEngine.Tone]）。 */
    fun play(tone: Int) {
        val g = ensure() ?: return
        val (type, durationMs) = map(tone)
        if (type == 0) return
        runCatching { g.startTone(type, durationMs) }
            .onFailure { Log.w(TAG, "startTone 失败: ${it.message}") }
    }

    /** 停掉正在响的提示音（比如老人已经按键确认了）。 */
    fun stop() {
        runCatching { gen?.stopTone() }
    }

    fun release() {
        runCatching { gen?.release() }
        gen = null
    }

    private fun ensure(): ToneGenerator? {
        if (failed) return null
        gen?.let { return it }
        return runCatching {
            // 音量给 90：这是给老人用的设备，提示音太小就失去意义
            ToneGenerator(AudioManager.STREAM_MUSIC, 90).also { gen = it }
        }.getOrElse {
            // 某些 ROM 在没有音频输出设备时构造会抛（比如蓝牙独占）。
            // 静默降级但**留日志** —— 否则"提醒没声音"会变成无法追查的现象。
            failed = true
            Log.e(TAG, "ToneGenerator 创建失败，本次运行将没有提示音: ${it.message}")
            null
        }
    }

    /** 语义 → (ToneGenerator 音型, 时长ms)。 */
    private fun map(tone: Int): Pair<Int, Int> = when (tone) {
        // 通用弹窗提示（提醒触发）
        com.elder.lx04.reminder.ReminderEngine.Tone.POPUP ->
            ToneGenerator.TONE_PROP_ACK to 200

        // 感叹 —— 升级告警。用"否定音"而不是"确认音"：
        // 老人不需要理解音色，但"和刚才不一样"这件事他能听出来。
        com.elder.lx04.reminder.ReminderEngine.Tone.EXCLAMATION ->
            ToneGenerator.TONE_PROP_NACK to 400

        // 成功 —— 确认完成
        com.elder.lx04.reminder.ReminderEngine.Tone.SUCCESS ->
            ToneGenerator.TONE_PROP_ACK to 150

        // 震动音替代（LX04 无马达，只能用低频短音）
        com.elder.lx04.reminder.ReminderEngine.Tone.VIBRATION ->
            ToneGenerator.TONE_SUP_ERROR to 300

        com.elder.lx04.reminder.ReminderEngine.Tone.LOW_BATTERY ->
            ToneGenerator.TONE_PROP_BEEP2 to 200

        // 提醒专用：尽量长（3 秒），先把人叫过来。
        // ⚠️ 固件这里是 4.9 秒音乐，我们只能给一个长提示音 —— 见类注释。
        com.elder.lx04.reminder.ReminderEngine.Tone.REMINDER ->
            ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD to 3000

        else -> 0 to 0
    }

    private companion object {
        const val TAG = "ElderTone"
    }
}
