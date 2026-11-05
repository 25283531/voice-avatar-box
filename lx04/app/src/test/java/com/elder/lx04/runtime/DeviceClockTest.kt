package com.elder.lx04.runtime

import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant

/**
 * 设备时钟的时区边界测试。
 *
 * 这些用例存在的理由：真机上「校时偏差 28799s」（≈8 小时）暴露出服务端时间
 * 是**不带偏移的本地墙上时间**。时钟一旦错 8 小时，「早上八点吃药」就会在
 * 下午四点响 —— 而所有日志都正常，只有老人被耽误了。
 *
 * 跨天/跨时区在真机上要么等一年、要么得改设备设置，只有这里能稳定复现。
 */
class DeviceClockTest {

    private fun utc(iso: String): Long = Instant.parse(iso).toEpochMilli()

    @Test
    fun `东八区把 UTC 下午四点算成次日零点`() {
        // 2026-11-05T16:00:00Z == 北京时间 2026-11-06 00:00
        val t = utc("2026-11-05T16:00:00Z")
        val c = DeviceClock(offsetProvider = { 480 }, timeSource = { t })
        val p = c.parts()

        assertEquals(2026, p.year)
        assertEquals(11, p.mon)
        assertEquals(6, p.mday)     // 跨天了
        assertEquals(0, p.hour)
        assertEquals(0, p.minute)
    }

    @Test
    fun `零偏移就是 UTC`() {
        val t = utc("2026-11-05T16:00:00Z")
        val c = DeviceClock(offsetProvider = { 0 }, timeSource = { t })
        val p = c.parts()

        assertEquals(5, p.mday)
        assertEquals(16, p.hour)
    }

    @Test
    fun `西五区把 UTC 下午四点算成上午十一点`() {
        val t = utc("2026-11-05T16:00:00Z")
        val c = DeviceClock(offsetProvider = { -300 }, timeSource = { t })
        assertEquals(11, c.parts().hour)
        assertEquals(5, c.parts().mday)
    }

    @Test
    fun `绝对时刻不随时区改变`() {
        val t = utc("2026-11-05T16:00:00Z")
        val east = DeviceClock(offsetProvider = { 480 }, timeSource = { t })
        val west = DeviceClock(offsetProvider = { -300 }, timeSource = { t })

        assertEquals(east.nowSec(), west.nowSec())
        assertEquals(east.nowMs(), west.nowMs())
        // 只有"墙上时间"不同
        assertEquals(0, east.parts().hour)
        assertEquals(11, west.parts().hour)
    }

    @Test
    fun `wday 与 yday 按目标时区的日历日算`() {
        // 2026-11-05 是周四 ⇒ tm_wday = 4
        val t = utc("2026-11-05T04:00:00Z") // 北京 12:00，同一天
        val c = DeviceClock(offsetProvider = { 480 }, timeSource = { t })
        assertEquals(4, c.parts().wday)
        // 1 月 1 日的 yday 必须是 0
        val jan = utc("2026-01-01T02:00:00Z") // 北京 10:00，仍是 1 月 1 日
        val c2 = DeviceClock(offsetProvider = { 480 }, timeSource = { jan })
        assertEquals(0, c2.parts().yday)
    }

    @Test
    fun `校时偏差会进入时间源`() {
        val t = utc("2026-11-05T16:00:00Z")
        // 模拟设备慢了 8 小时：应用层把偏移加回时间源
        val c = DeviceClock(
            offsetProvider = { 480 },
            timeSource = { t + 8 * 3600_000L }
        )
        // 北京墙上时间变成次日 08:00
        assertEquals(8, c.parts().hour)
        assertEquals(6, c.parts().mday)
    }
}
