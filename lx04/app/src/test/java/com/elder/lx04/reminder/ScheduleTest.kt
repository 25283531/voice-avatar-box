package com.elder.lx04.reminder

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 排程纯函数的边界测试。
 *
 * 这些用例的价值在于**只能在这里跑**：2 月 29 日、跨年相位、月末 31 号，
 * 在真机上要么等一年、要么根本造不出来。而它们错了就是"老人某天收不到
 * 服药提醒"—— 那天刚好是闰年 2 月 29 日的概率很低，但一旦发生，没人会
 * 想到是排程算法。
 */
class ScheduleTest {

    @Test
    fun `ymdOf 组装与越界`() {
        assertEquals(20261105, Schedule.ymdOf(2026, 11, 5))
        // 越界一律 0 = "没有这个日期"，调用方当"未设置"用
        assertEquals(0, Schedule.ymdOf(1899, 1, 1))
        assertEquals(0, Schedule.ymdOf(3000, 1, 1))
        assertEquals(0, Schedule.ymdOf(2026, 0, 1))
        assertEquals(0, Schedule.ymdOf(2026, 13, 1))
        assertEquals(0, Schedule.ymdOf(2026, 1, 0))
        assertEquals(0, Schedule.ymdOf(2026, 1, 32))
    }

    @Test
    fun `ordinalOf 以 1970-01-01 为零点`() {
        assertEquals(0, Schedule.ordinalOf(1970, 1, 1))
        assertEquals(1, Schedule.ordinalOf(1970, 1, 2))
        // 2026-11-05 是 1970 以来的第 20762 天（与 Linux date +%s / 86400 同源）
        assertEquals(20762, Schedule.ordinalOf(2026, 11, 5))
    }

    @Test
    fun `ordinalOf 闰年二月多一天`() {
        // 2024 是闰年：2/28 → 2/29 相差 1 天，2/29 → 3/1 也相差 1 天
        val d28 = Schedule.ordinalOf(2024, 2, 28)
        val d29 = Schedule.ordinalOf(2024, 2, 29)
        val d01 = Schedule.ordinalOf(2024, 3, 1)
        assertEquals(1, d29 - d28)
        assertEquals(1, d01 - d29)
        // 2023 非闰年：2/28 → 3/1 直接 +1
        assertEquals(1, Schedule.ordinalOf(2023, 3, 1) - Schedule.ordinalOf(2023, 2, 28))
    }

    @Test
    fun `null 排期按每天处理`() {
        // 宁可多响，也不能静默不响 —— 漏一条服药提醒的后果远大于多响一次
        assertTrue(Schedule.dueOn(null, 20261105, 20762, 3))
    }

    @Test
    fun `WEEKLY 按 repeatMask 命中星期`() {
        // bit0=周日 … bit6=周六。2026-11-05 是周四（wday=4）
        val onlyThursday = RmdWhen(kind = Schedule.KIND_WEEKLY, repeatMask = 1 shl 4)
        assertTrue(Schedule.dueOn(onlyThursday, 20261105, 20762, 4))
        assertFalse(Schedule.dueOn(onlyThursday, 20261105, 20762, 3))
        assertFalse(Schedule.dueOn(onlyThursday, 20261105, 20762, 0))
    }

    @Test
    fun `MONTHLY 按 mday 位图命中，且不做月末回退`() {
        val day5 = RmdWhen(kind = Schedule.KIND_MONTHLY, monthMask = 1 shl 4)  // bit4 = 5 号
        assertTrue(Schedule.dueOn(day5, 20261105, 20762, 4))
        assertFalse(Schedule.dueOn(day5, 20261106, 20763, 5))

        // 31 号的规则在 2 月整月不响 —— 这是固件既有取舍，必须照实保留。
        // 顺手"修好"（改成月末回落）会让本地排程与服务端排出的实例对不上。
        val day31 = RmdWhen(kind = Schedule.KIND_MONTHLY, monthMask = 1 shl 30)
        assertFalse(Schedule.dueOn(day31, 20260228, 20112, 6))
        assertTrue(Schedule.dueOn(day31, 20260131, 20088, 6))
    }

    @Test
    fun `INTERVAL_DAYS 相位正确且不跨年漂移`() {
        val anchor = Schedule.ordinalOf(2026, 11, 5)
        val every3 = RmdWhen(kind = Schedule.KIND_INTERVAL_DAYS, intervalDays = 3, anchorOrdinal = anchor)

        assertTrue(Schedule.dueOn(every3, 20261105, anchor, 4))          // 当天
        assertFalse(Schedule.dueOn(every3, 20261106, anchor + 1, 5))
        assertFalse(Schedule.dueOn(every3, 20261107, anchor + 2, 6))
        assertTrue(Schedule.dueOn(every3, 20261108, anchor + 3, 0))      // 第 3 天
        // anchor 之前的日子用负数取模修正，不能崩也不能全 true
        assertFalse(Schedule.dueOn(every3, 20261104, anchor - 1, 3))
        assertTrue(Schedule.dueOn(every3, 20261102, anchor - 3, 1))

        // 跨年相位：2027-01-01 与 anchor 差 57 天，57 % 3 == 0 ⇒ 响。
        // 如果误用 `ymd/366` 之类的近似日号，这里会错。
        val newYear = Schedule.ordinalOf(2027, 1, 1)
        assertEquals(0, (newYear - anchor) % 3)
        assertTrue(Schedule.dueOn(every3, 20270101, newYear, 5))
    }

    @Test
    fun `INTERVAL_DAYS 的 n 小于等于 1 视为每天`() {
        val daily = RmdWhen(kind = Schedule.KIND_INTERVAL_DAYS, intervalDays = 1, anchorOrdinal = 0)
        assertTrue(Schedule.dueOn(daily, 20261105, 20762, 4))
        assertTrue(Schedule.dueOn(daily, 20261106, 20763, 5))
    }

    @Test
    fun `区间限制优先于任何 kind`() {
        val w = RmdWhen(
            kind = Schedule.KIND_DAILY,
            startYmd = 20261110,
            endYmd = 20261120
        )
        assertFalse(Schedule.dueOn(w, 20261109, 20766, 1))   // 未开始
        assertTrue(Schedule.dueOn(w, 20261110, 20767, 2))    // 第一天
        assertTrue(Schedule.dueOn(w, 20261120, 20777, 5))    // 最后一天
        assertFalse(Schedule.dueOn(w, 20261121, 20778, 6))   // 已结束
    }

    @Test
    fun `ONCE 没有日期时按每天并告警`() {
        var warned = ""
        val once = RmdWhen(kind = Schedule.KIND_ONCE, onceYmd = 0)
        assertTrue(Schedule.dueOn(once, 20261105, 20762, 4) { warned = it })
        assertTrue(warned.isNotEmpty())

        val onDay = RmdWhen(kind = Schedule.KIND_ONCE, onceYmd = 20261105)
        assertTrue(Schedule.dueOn(onDay, 20261105, 20762, 4))
        assertFalse(Schedule.dueOn(onDay, 20261106, 20763, 5))
    }

    @Test
    fun `未知 kind 按每天并告警`() {
        var warned = ""
        assertTrue(Schedule.dueOn(RmdWhen(kind = 99), 20261105, 20762, 4) { warned = it })
        assertTrue(warned.isNotEmpty())
    }
}
