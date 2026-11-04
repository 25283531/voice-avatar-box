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
 * ## 为什么能测（以及这里踩过的坑）
 *
 * 引擎的时间是**注入的**（`render(t)` / `buttonPress(t)` / `release(t)` 都收时间戳），
 * 不读系统时钟 ⇒ 测试可以精确控制每一毫秒。
 *
 * 第一版没做到这一点：`buttonPress()` 内部读 `SystemClock.elapsedRealtime()`，
 * 而它在 JVM 单测里是 AGP mockable android.jar 的 **stub，恒返回 0**。
 * 于是"按下时刻"永远是 0，而 0 又正好是旧实现里"没在按"的哨兵值 ——
 * 所有按键用例都静默退化成"没按下过"，断言全线失败
 * （CI run 41845796959：27 个用例挂 14 个）。
 *
 * 现在两条路都堵上了：
 *   1. 引擎的 [LedEngine.clock] 可替换成假时钟；
 *   2. 按键的按下时刻用 `null` 作哨兵，`t = 0` 也是合法的按下时刻。
 */
class LedEngineTest {

    /**
     * 测试夹具：一个可以手动推进的时钟 + 引擎。
     *
     * `at(ms)` 既推进时钟又作为渲染时间戳 —— 两边必须是同一个值，
     * 否则"结果闪烁"这类用例会出现"刚点下就已经闪完了"的假失败。
     */
    private class Fixture {
        private var t: Long = 0L
        val e = LedEngine().also { engine -> engine.clock = { t } }

        fun at(ms: Long): Fixture {
            t = ms
            return this
        }

        /** 左带：对话 / 提醒灯效。 */
        fun left(): BandRender.Band = e.resolveLeftForTest(t)

        /** 右带：按键指示灯。 */
        fun right(): BandRender.Band = e.render(t).right
    }

    // ------------------------------------------------------------ 优先级仲裁

    @Test
    fun `高优先级不被低优先级抢占`() {
        val f = Fixture()
        f.e.request(Led.Priority.EMOTION, Led.LedEffect.SOLID, 0x111111, 255, 0)
        f.e.request(Led.Priority.MED_PENDING, Led.LedEffect.SOLID, 0xFF0000, 255, 0)

        val band = f.at(1000L).left()
        assertTrue(
            "服药灯应压过对话灯",
            band is BandRender.Band.Solid && band.color == 0xFF0000
        )
    }

    @Test
    fun `释放高优先级后回落到低优先级`() {
        val f = Fixture()
        f.e.request(Led.Priority.EMOTION, Led.LedEffect.SOLID, 0x111111, 255, 0)
        f.e.request(Led.Priority.MED_PENDING, Led.LedEffect.SOLID, 0xFF0000, 255, 0)
        f.e.release(Led.Priority.MED_PENDING)

        val band = f.at(1000L).left()
        assertTrue(
            "释放后应回到对话灯",
            band is BandRender.Band.Solid && band.color == 0x111111
        )
    }

    @Test
    fun `状态灯关闭后事件灯仍然亮`() {
        val f = Fixture()
        f.e.setStatusLight(false)
        f.e.medPending()

        val band = f.at(1000L).left()
        assertTrue(
            "状态灯关掉后，服药事件灯必须仍然亮 —— 这是状态灯开关的全部意义",
            band is BandRender.Band.Solid && band.alpha > 0
        )
    }

    @Test
    fun `状态灯关闭后对话灯熄灭`() {
        val f = Fixture()
        f.e.setStatusLight(false)
        f.e.setUiState(Led.UiState.LISTENING)

        assertEquals(
            "状态灯关掉时，聆听态不应发光",
            BandRender.Band.Off,
            f.at(1000L).left()
        )
    }

    // ------------------------------------------------------------ 按键指示灯

    @Test
    fun `没有待确认提醒时按下不亮`() {
        val f = Fixture()
        f.e.buttonPending(false)
        f.e.buttonPress(0L)

        assertEquals(
            "没有可确认的东西时给正反馈 = 告诉老人「办成了」而实际什么都没发生，" +
                "这是最坏的一类欺骗。安静地不亮才是诚实的。",
            BandRender.Band.Off,
            f.at(1000L).right()
        )
    }

    @Test
    fun `有待确认提醒时按住显示渐变进度条`() {
        val f = Fixture()
        f.e.buttonPending(true)
        f.e.buttonPress(0L)

        // 按住 1.5 秒（达标 3 秒的一半）
        val band = f.at(1500L).right()
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
        val f = Fixture()
        f.e.buttonPending(true)
        f.e.buttonPress(0L)
        f.e.buttonRelease(LedEngine.CONFIRM_HOLD_MS)   // 按满 3 秒：release 本身不出结果
        f.e.buttonConfirmed(LedEngine.CONFIRM_HOLD_MS) // 出结果

        val band = f.at(LedEngine.CONFIRM_HOLD_MS + 100L).right()
        assertTrue("确认后应闪灯", band is BandRender.Band.Solid)
        assertEquals(
            "确认成功的颜色是天蓝 #00BFFF",
            0x00BFFF,
            (band as BandRender.Band.Solid).color
        )
        assertEquals("闪的那一下必须全亮，不能是半透明", 255, band.alpha)
    }

    @Test
    fun `中途松开闪粉红`() {
        val f = Fixture()
        f.e.buttonPending(true)
        f.e.buttonPress(0L)
        f.e.buttonRelease(1500L)          // 按住 1.5 秒后松开 → 中途放弃

        val band = f.at(1600L).right()
        assertTrue("中途松开应闪灯", band is BandRender.Band.Solid)
        assertEquals(
            "失败的颜色是粉红 #FF69B4",
            0xFF69B4,
            (band as BandRender.Band.Solid).color
        )
    }

    @Test
    fun `抖动不闪失败`() {
        val f = Fixture()
        f.e.buttonPending(true)
        f.e.buttonPress(0L)
        // 按住 100ms 就松开 —— 机械抖动，不是老人放弃
        f.e.buttonRelease(100L)

        val band = f.at(2000L).right()
        assertTrue("抖动后应安静回落到橙色呼吸", band is BandRender.Band.Solid)
        assertNotEquals(
            "300ms 以内的松手是抖动，不该闪粉红（用户实报过这个故障）",
            Led.Color.BUTTON_FAIL,
            (band as BandRender.Band.Solid).color
        )
    }

    @Test
    fun `松开后灯不再停在渐变态`() {
        val f = Fixture()
        f.e.buttonPending(true)
        f.e.buttonPress(0L)
        f.e.buttonRelease(100L)          // 抖动路径：静默

        // ⚠️ 回归用例：release 之后 pressAt 必须清空，
        // 否则色带会一直卡在天青渐变上不熄 —— 老人看着就是「灯坏了」。
        val band = f.at(5000L).right()
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
        val f = Fixture()
        f.e.buttonPending(true)
        f.e.buttonPress(0L)
        f.e.buttonRelease(LedEngine.CONFIRM_HOLD_MS)
        f.e.buttonConfirmed(LedEngine.CONFIRM_HOLD_MS)
        // 确认完成后由调用方清掉待确认标记（App 侧的真实顺序）
        f.e.buttonPending(false)

        // 天蓝闪 3 次：每次 300ms 亮 + 300ms 灭，总共 1800ms
        val t0 = LedEngine.CONFIRM_HOLD_MS
        // 还在闪
        assertTrue("应仍在闪", f.at(t0 + 100L).right() is BandRender.Band.Solid)
        // 闪完后（3 次 × 600ms = 1800ms 之后）应熄灭
        assertEquals(
            "3 次闪完（1800ms）后应熄灭",
            BandRender.Band.Off,
            f.at(t0 + 1900L).right()
        )
    }

    // ------------------------------------------------------------ 服药语义

    @Test
    fun `服药完成闪绿`() {
        val f = Fixture()
        f.e.medDone()
        val band = f.at(1000L).left()
        assertTrue("服药完成应显示绿", band is BandRender.Band.Solid)
        assertEquals(0x00FF00, (band as BandRender.Band.Solid).color)
    }

    @Test
    fun `服药完成60秒后回落`() {
        val f = Fixture()
        f.e.medDone(ttlMs = 60_000L)
        assertTrue("刚开始应亮", f.at(1000L).left() !is BandRender.Band.Off)
        assertEquals(
            "60 秒后应自动回落",
            BandRender.Band.Off,
            f.at(61_000L).left()
        )
    }

    // ------------------------------------------------------------ 对话四态

    @Test
    fun `对话四态互斥切换不残留`() {
        val f = Fixture()
        f.e.setUiState(Led.UiState.LISTENING)
        val listening = f.at(1000L).left()

        f.e.setUiState(Led.UiState.SPEAKING)
        val speaking = f.at(1000L).left()

        assertNotEquals(
            "切换后不应还留着上一个状态",
            listening,
            speaking
        )
    }

    @Test
    fun `待机超过1分钟自动降亮`() {
        val f = Fixture()
        f.e.setUiState(Led.UiState.IDLE)

        // 两个时刻都取在"星光正在亮"的那 300ms 窗口里
        // （2000ms 一格，格内前 300ms 亮），否则取到的是熄灭帧，
        // 亮度差异就测不出来了。
        val near = f.at(100L).left()
        val far = f.at(60_100L).left()

        assertTrue("两帧都应是星光", near is BandRender.Band.Segment)
        assertTrue("两帧都应是星光", far is BandRender.Band.Segment)
        assertNotEquals(
            "待机 1 分钟后应降亮（夜里不刺眼）",
            near,
            far
        )
    }
}
