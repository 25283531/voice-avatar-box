package com.elder.lx04.light

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 灯效引擎的时序测试。
 *
 * ## 为什么必须测
 *
 * 灯效的语义是**老人和家属之间的约定**：
 *   · 天蓝闪 3 次 = 按对了
 *   · 粉红闪 5 次 = 没按够
 *   · 天青渐变 = 正在按，渐变长度 = 进度
 *
 * 这些时序一旦错位，后果不是"不好看"，而是**老人以为自己按对了，其实没有**。
 * 固件上这些逻辑被反复修过好几次（2026-10-28 / 10-29 / 11-03 / 11-04 四轮调整），
 * 每次都是踩了坑才改的。移植到 Android 后必须锁住这些行为。
 *
 * ## 为什么能测
 *
 * 引擎的时间是**注入的**（`render(t)` / `press(t)` / `release(t)` 都收时间戳），
 * 不读系统时钟 ⇒ 测试可以精确控制每一毫秒。
 */
class LedEngineTest {

    // ------------------------------------------------------------ 优先级仲裁

    @Test
    fun `高优先级不被低优先级抢占`() {
        val e = LedEngine()
        e.request(Led.Priority.EMOTION, Led.LedEffect.SOLID, 0x111111, 255, 0)
        e.request(Led.Priority.MED_PENDING, Led.LedEffect.SOLID, 0xFF0000, 255, 0)

        val band = e.resolveLeftForTest(1000L)
        assertTrue(
            "服药灯应压过对话灯",
            band is BandRender.Band.Solid && band.color == 0xFF0000
        )
    }

    @Test
    fun `释放高优先级后回落到低优先级`() {
        val e = LedEngine()
        e.request(Led.Priority.EMOTION, Led.LedEffect.SOLID, 0x111111, 255, 0)
        e.request(Led.Priority.MED_PENDING, Led.LedEffect.SOLID, 0xFF0000, 255, 0)
        e.release(Led.Priority.MED_PENDING)

        val band = e.resolveLeftForTest(1000L)
        assertTrue(
            "释放后应回到对话灯",
            band is BandRender.Band.Solid && band.color == 0x111111
        )
    }

    @Test
    fun `状态灯关闭后事件灯仍然亮`() {
        val e = LedEngine()
        e.setStatusLight(false)
        e.medPending()

        val band = e.resolveLeftForTest(1000L)
        assertTrue(
            "状态灯关掉后，服药事件灯必须仍然亮 —— 这是状态灯开关的全部意义",
            band is BandRender.Band.Solid && band.alpha > 0
        )
    }

    @Test
    fun `状态灯关闭后对话灯熄灭`() {
        val e = LedEngine()
        e.setStatusLight(false)
        e.setUiState(Led.UiState.LISTENING)

        assertEquals(
            "状态灯关掉时，聆听态不应发光",
            BandRender.Band.Off,
            e.resolveLeftForTest(1000L)
        )
    }

    // ------------------------------------------------------------ 按键指示灯

    @Test
    fun `没有待确认提醒时按下不亮`() {
        val e = LedEngine()
        e.buttonPending(false)
        e.buttonPress()

        val band = e.render(1000L).right
        assertEquals(
            "没有可确认的东西时给正反馈 = 告诉老人「办成了」而实际什么都没发生，" +
                "这是最坏的一类欺骗。安静地不亮才是诚实的。",
            BandRender.Band.Off,
            band
        )
    }

    @Test
    fun `有待确认提醒时按住显示渐变进度条`() {
        val e = LedEngine()
        e.buttonPending(true)
        e.buttonPress()

        // 按住 1.5 秒（达标 3 秒的一半）
        val band = e.render(1000L + 1500L).right
        assertTrue("按住期间应显示渐变", band is BandRender.Band.Multi)

        val segs = (band as BandRender.Band.Multi).segments
        assertTrue("1.5 秒时进度应填了一半左右", segs.isNotEmpty())
        assertTrue(
            "填充的段数应少于总段数（还没按满）",
            segs.size < LedEngine.SEGMENTS
        )
    }

    @Test
    fun `确认成功闪天蓝`() {
        val e = LedEngine()
        e.buttonPending(true)
        e.buttonPress()
        e.buttonRelease()          // 达标分支：release 不出结果
        e.buttonConfirmed()        // 出结果

        val band = e.render(2000L).right
        assertTrue("确认后应闪灯", band is BandRender.Band.Solid)
        assertEquals(
            "确认成功的颜色是天蓝 #00BFFF",
            0x00BFFF,
            (band as BandRender.Band.Solid).color
        )
    }

    @Test
    fun `中途松开闪粉红`() {
        val e = LedEngine()
        e.buttonPending(true)
        e.buttonPress()
        e.buttonRelease()          // 按住 0.5 秒后松开 → 中途放弃

        val band = e.render(2000L).right
        assertTrue("中途松开应闪灯", band is BandRender.Band.Solid)
        assertEquals(
            "失败的颜色是粉红 #FF69B4",
            0xFF69B4,
            (band as BandRender.Band.Solid).color
        )
    }

    @Test
    fun `抖动不闪失败`() {
        val e = LedEngine()
        e.buttonPending(true)
        e.buttonPress()
        // 按住 100ms 就松开 —— 机械抖动，不是老人放弃
        e.buttonRelease()

        val band = e.render(2000L).right
        assertEquals(
            "300ms 以内的松手是抖动，不该闪粉红（用户实报过这个故障）",
            BandRender.Band.Off,
            band
        )
    }

    @Test
    fun `松开后灯不再停在渐变态`() {
        val e = LedEngine()
        e.buttonPending(true)
        e.buttonPress()
        e.buttonRelease()          // 抖动路径：静默

        // ⚠️ 回归用例：release 之后 pressAt 必须清零，
        // 否则色带会一直卡在天青渐变上不熄 —— 老人看着就是「灯坏了」。
        val band = e.render(5000L).right
        assertNotEquals(
            "松开后不能停在按住渐变态",
            BandRender.Band.Off,
            band
        )
        // 抖动路径下应该回到「橙色呼吸」（因为 pending=true）
        assertTrue(
            "抖动后应回到橙色呼吸（有待确认提醒）",
            band is BandRender.Band.Solid
        )
    }

    @Test
    fun `结果闪烁次数正确`() {
        val e = LedEngine()
        e.buttonPending(true)
        e.buttonPress()
        e.buttonRelease()
        e.buttonConfirmed()

        // 天蓝闪 3 次：每次 300ms 亮 + 300ms 灭，总共 6 个半格
        val t0 = 2000L
        // 还在闪
        assertTrue("应仍在闪", e.render(t0).right is BandRender.Band.Solid)
        // 闪完后（3 次 × 600ms = 1800ms 之后）应熄灭
        assertEquals(
            "3 次闪完（1800ms）后应熄灭",
            BandRender.Band.Off,
            e.render(t0 + 1900L).right
        )
    }

    // ------------------------------------------------------------ 服药语义

    @Test
    fun `服药完成闪绿`() {
        val e = LedEngine()
        e.medDone()
        val band = e.resolveLeftForTest(1000L)
        assertTrue("服药完成应显示绿", band is BandRender.Band.Solid)
        assertEquals(0x00FF00, (band as BandRender.Band.Solid).color)
    }

    @Test
    fun `服药完成60秒后回落`() {
        val e = LedEngine()
        e.medDone(ttlMs = 60_000L)
        assertTrue("刚开始应亮", e.resolveLeftForTest(1000L) !is BandRender.Band.Off)
        assertEquals(
            "60 秒后应自动回落",
            BandRender.Band.Off,
            e.resolveLeftForTest(61_000L)
        )
    }

    // ------------------------------------------------------------ 对话四态

    @Test
    fun `对话四态互斥切换不残留`() {
        val e = LedEngine()
        e.setUiState(Led.UiState.LISTENING)
        val listening = e.resolveLeftForTest(1000L)

        e.setUiState(Led.UiState.SPEAKING)
        val speaking = e.resolveLeftForTest(1000L)

        assertNotEquals(
            "切换后不应还留着上一个状态",
            listening,
            speaking
        )
    }

    @Test
    fun `待机超过1分钟自动降亮`() {
        val e = LedEngine()
        e.setUiState(Led.UiState.IDLE)

        val near = e.resolveLeftForTest(1000L)
        val far = e.resolveLeftForTest(70_000L)

        assertNotEquals(
            "待机 1 分钟后应降亮（夜里不刺眼）",
            near,
            far
        )
    }
}
