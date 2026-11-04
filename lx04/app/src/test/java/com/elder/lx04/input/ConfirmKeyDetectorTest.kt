package com.elder.lx04.input

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 长按确认判定的测试。
 *
 * ## 为什么这些判定必须锁死
 *
 * 固件上这几条被反复修过：
 *   · 2026-10-20：把 SOS 长按求助删了（长按路径把 2560 字节的输入任务栈撑爆，设备重启）
 *   · 2026-11-03：多药提醒的长按不再直接确认，改成先问「您吃的是哪种药？」
 *   · 2026-11-04：加了 300ms 抖动兜底（用户实报「按键超过 1 秒就开始进行粉红色闪烁」）
 *
 * 每一条都是踩坑改出来的。移植到 Android 后如果判定漂移，
 * 后果是老人按对了却被告知失败 —— 这是最伤使用意愿的一类故障。
 */
class ConfirmKeyDetectorTest {

    @Test
    fun `按下开始新的一轮`() {
        val d = ConfirmKeyDetector()
        assertTrue("首次按下应是新按压", d.onDown(1000L))
        assertFalse("重复的 ACTION_DOWN 不算新按压", d.onDown(1200L))
    }

    @Test
    fun `按住3秒达标`() {
        val d = ConfirmKeyDetector()
        d.onDown(1000L)

        var reached = false
        // 每 100ms 检查一次（模拟系统重复回调）
        for (t in 1100L..5000L step 100L) {
            if (d.onRepeat(t)) {
                reached = true
                break
            }
        }
        assertTrue("按住 3 秒应达标", reached)
    }

    @Test
    fun `不到3秒松手算中途放弃`() {
        val d = ConfirmKeyDetector()
        d.onDown(1000L)
        d.onRepeat(1500L)     // 只按了 0.5 秒
        assertEquals(
            ConfirmKeyDetector.ReleaseResult.ReleasedEarly,
            d.onUp(1500L)
        )
    }

    @Test
    fun `满3秒松手算确认`() {
        val d = ConfirmKeyDetector()
        d.onDown(1000L)
        for (t in 1100L..4100L step 100L) {
            if (d.onRepeat(t)) break
        }
        assertEquals(
            ConfirmKeyDetector.ReleaseResult.Confirmed,
            d.onUp(4100L)
        )
    }

    @Test
    fun `300ms以内的松手算抖动`() {
        val d = ConfirmKeyDetector()
        d.onDown(1000L)
        assertEquals(
            "300ms 以内的松手是机械抖动，不能报「失败」",
            ConfirmKeyDetector.ReleaseResult.Debounced,
            d.onUp(1100L)
        )
    }

    @Test
    fun `抖动后可以重新按`() {
        val d = ConfirmKeyDetector()
        d.onDown(1000L)
        d.onUp(1050L)          // 抖动
        assertTrue(
            "抖动之后必须还能再按 —— 否则一次手抖就废了整轮",
            d.onDown(2000L)
        )
    }

    @Test
    fun `没有按press时松开无事发生`() {
        val d = ConfirmKeyDetector()
        assertEquals(
            ConfirmKeyDetector.ReleaseResult.None,
            d.onUp(1000L)
        )
    }

    @Test
    fun `按住时长被正确记录`() {
        val d = ConfirmKeyDetector()
        d.onDown(1000L)
        d.onRepeat(2500L)
        assertEquals(1500L, d.heldMs)
    }
}

/**
 * 顶部按键映射的测试。
 *
 * 重点：**音量键必须不被当成确认键**。
 * 用户明确要求「保留音量加减按键的功能」，而中间键才改作服药确认。
 * 一旦候选集合误收了音量键，音量就调不动了 —— 而且是那种
 * 「按了没反应，用户不知道原因」的最难排查故障。
 */
class TopKeysTest {

    @Test
    fun `音量键不是确认键`() {
        assertFalse(
            "音量+ 必须继续交给系统，否则音量功能就没了",
            TopKeys.isConfirmKey(TopKeys.VOLUME_UP)
        )
        assertFalse(
            "音量- 必须继续交给系统，否则音量功能就没了",
            TopKeys.isConfirmKey(TopKeys.VOLUME_DOWN)
        )
    }

    @Test
    fun `音量键被正确识别为音量键`() {
        assertTrue(TopKeys.isVolumeKey(TopKeys.VOLUME_UP))
        assertTrue(TopKeys.isVolumeKey(TopKeys.VOLUME_DOWN))
    }

    @Test
    fun `候选集合不含有系统语义的键`() {
        // 这些键有既定行为，误吞会让设备难用
        assertFalse("HOME 不能被当成确认键", TopKeys.isConfirmKey(android.view.KeyEvent.KEYCODE_HOME))
        assertFalse(
            "APP_SWITCH 不能被当成确认键",
            TopKeys.isConfirmKey(android.view.KeyEvent.KEYCODE_APP_SWITCH)
        )
        assertFalse(
            "BACK 不能被当成确认键",
            TopKeys.isConfirmKey(android.view.KeyEvent.KEYCODE_BACK)
        )
        assertFalse(
            "UNKNOWN 不是真实按键",
            TopKeys.isConfirmKey(android.view.KeyEvent.KEYCODE_UNKNOWN)
        )
    }

    @Test
    fun `长按常量与灯效引擎一致`() {
        // ⚠️ 这两个常量必须同源（3 秒）。
        // 各写一份的话，就会出现「灯已经亮满天青、按键却还没算确认」——
        // 灯在骗人，比没有灯更糟。
        assertEquals(
            "长按时长必须与灯效进度条同源",
            com.elder.lx04.light.LedEngine.CONFIRM_HOLD_MS,
            ConfirmKeyDetector.HOLD_MS
        )
    }
}
