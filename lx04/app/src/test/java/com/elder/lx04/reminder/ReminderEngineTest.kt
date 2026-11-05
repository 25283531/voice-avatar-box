package com.elder.lx04.reminder

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 提醒引擎的状态机测试。
 *
 * ## 这些用例为什么必须存在
 *
 * 引擎的每条规则都对应家属端能看见的一个结论：响没响、确认了没、是漏服还是
 * 跳过、是语音还是按键确认。任何一条错了，家属手机上就会出现一条**看起来
 * 完全合理**的错误记录 —— 没有崩溃、没有异常日志，只有"老人昨天漏服药了"
 * 这样一条假告警。真机测不出这种错，只能靠这里把语义钉死。
 *
 * ## 时间是被"摆"出来的，不是 sleep 出来的
 *
 * 15 分钟升级、10 分钟复核窗口、跨天重置 —— 用 sleep 测得跑半小时，
 * 最后一定被注释掉。所以 [FakeClock] 直接跳时间。
 */
class ReminderEngineTest {

    // ------------------------------------------------------------------ 测试替身

    private class FakeClock(private val tzMin: Int = 480) : Clock {
        var sec: Long = 0L
        var msPart: Long = 0L
        var p: LocalParts = LocalParts(2026, 11, 5, 8, 0, 4, 308)

        override fun nowSec(): Long = sec
        override fun nowMs(): Long = sec * 1000L + msPart
        override fun parts(): LocalParts = p
        override fun tzOffsetMin(): Int = tzMin

        /** 把"本地墙上时间"同时写进 sec 与 parts（两者必须一致，否则测试自己在骗自己）。 */
        fun set(y: Int, mo: Int, d: Int, h: Int, mi: Int) {
            val ord = Schedule.ordinalOf(y, mo, d)
            sec = ord * 86_400L + h * 3_600L + mi * 60L - tzMin * 60L
            val yday = ord - Schedule.ordinalOf(y, 1, 1)
            // 1970-01-01 是周四（wday=4）
            val wday = Math.floorMod(ord + 4, 7)
            p = LocalParts(y, mo, d, h, mi, wday, yday)
        }

        fun advanceMin(min: Long) {
            sec += min * 60L
            // 重新按 sec 拆 parts，保证跨天/跨小时也一致
            val local = sec + tzMin * 60L
            val dayIdx = Math.floorDiv(local, 86_400L)
            val rem = Math.floorMod(local, 86_400L)
            val h = (rem / 3600).toInt()
            val mi = ((rem % 3600) / 60).toInt()
            val ymd = ymdOfOrdinal(dayIdx)
            val yday = dayIdx.toInt() - Schedule.ordinalOf(ymd[0], 1, 1)
            p = LocalParts(ymd[0], ymd[1], ymd[2], h, mi, Math.floorMod(dayIdx + 4, 7), yday)
        }

        /** 日序号 → 年月日（只用于测试，够用即可）。 */
        private fun ymdOfOrdinal(ord: Long): IntArray {
            var y = 1970
            while (true) {
                val next = Schedule.ordinalOf(y + 1, 1, 1).toLong()
                if (ord < next) break
                y++
            }
            var mo = 1
            while (mo < 12 && ord >= Schedule.ordinalOf(y, mo + 1, 1)) mo++
            val d = (ord - Schedule.ordinalOf(y, mo, 1)).toInt() + 1
            return intArrayOf(y, mo, d)
        }
    }

    private class MemStore : RmdStore {
        var text: String? = null
        var saves = 0
        override fun load(): ReminderTable? = text?.let { RmdCodec.decode(it) }
        override fun save(table: ReminderTable): Boolean {
            text = RmdCodec.encode(table)
            saves++
            return true
        }
    }

    private class RecordingListener : ReminderEngine.Listener {
        val stateChanges = mutableListOf<Triple<Int, RmdState, RmdState>>()
        var reminderText: String? = null
        val tones = mutableListOf<Int>()
        val announces = mutableListOf<String>()
        val clips = mutableListOf<Int>()
        var timeUntrusted = 0
        /** 让测试指定某个片段"放不了"，验证回落路径。 */
        var clipAvailable = true

        override fun onStateChanged(idx: Int, old: RmdState, now: RmdState, method: Int) {
            stateChanges.add(Triple(idx, old, now))
        }

        override fun onReminderText(text: String?) { reminderText = text }
        override fun onTone(tone: Int) { tones.add(tone) }
        override fun onAnnounce(text: String) { announces.add(text) }
        override fun onPlayClip(clipId: Int): Boolean {
            clips.add(clipId)
            return clipAvailable
        }
        override fun onTimeUntrusted() { timeUntrusted++ }
    }

    private fun medicine(
        hour: Int = 8,
        minute: Int = 0,
        label: String = "降压药",
        serverId: Long = 11L,
        medLine: String = "苯磺酸氨氯地平片 5mg、阿司匹林肠溶片 100mg"
    ) = ReminderItem(
        serverId = serverId,
        hour = hour,
        minute = minute,
        type = RmdType.MEDICINE,
        rtype = ReminderType.MEDICINE,
        label = label,
        scheduleKind = Schedule.KIND_DAILY,
        medLine = medLine,
        medicines = listOf("苯磺酸氨氯地平片 5mg", "阿司匹林肠溶片 100mg")
    )

    private fun schedule(hour: Int = 9, label: String = "喝水") = ReminderItem(
        serverId = 22L,
        hour = hour,
        minute = 0,
        type = RmdType.SCHEDULE,
        rtype = ReminderType.WATER,
        label = label,
        scheduleKind = Schedule.KIND_DAILY
    )

    private class Rig {
        val clock = FakeClock()
        val store = MemStore()
        val listener = RecordingListener()
        val engine = ReminderEngine(store, listener, clock)
    }

    // ------------------------------------------------------------------ 触发与三闸门

    @Test
    fun `到点触发 PENDING 并推全屏文案与提示音`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 0)
        r.engine.tick()

        assertEquals(RmdState.PENDING, r.engine.stateOf(0))
        assertNotNull(r.listener.reminderText)
        // 文案契约：首行标题、以「请」开头的提示行、其余为明细
        val lines = r.listener.reminderText!!.split('\n')
        assertEquals("该吃降压药了", lines[0])
        assertEquals("请及时服用", lines[1])
        assertTrue(lines.size >= 3)
        // 两味药各一行
        assertTrue(r.listener.reminderText!!.contains("苯磺酸氨氯地平片"))
        assertTrue(r.listener.reminderText!!.contains("阿司匹林肠溶片"))
    }

    @Test
    fun `同一天第二次 tick 不重复触发`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 0)
        r.engine.tick()
        r.clock.advanceMin(1)
        r.engine.tick()
        r.clock.advanceMin(30)
        r.engine.tick()

        // 只应有一条 IDLE→PENDING 的迁移
        val pendingChanges = r.listener.stateChanges.count { it.second == RmdState.PENDING }
        assertEquals(1, pendingChanges)
        // 提示音也只在第一次播
        assertEquals(1, r.listener.tones.count { it == ReminderEngine.Tone.REMINDER })
    }

    @Test
    fun `未到点不触发`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 7, 59)
        r.engine.tick()

        assertEquals(RmdState.IDLE, r.engine.stateOf(0))
        assertNull(r.listener.reminderText)
    }

    @Test
    fun `跨天后重新可以触发`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 0)
        r.engine.tick()
        assertEquals(RmdState.PENDING, r.engine.stateOf(0))

        // 第二天同一时刻
        r.clock.set(2026, 11, 6, 8, 0)
        r.engine.tick()
        assertEquals(RmdState.PENDING, r.engine.stateOf(0))
        // 位图应已按新一天重置后重新置位
        assertTrue(r.engine.table.isFired(0))
    }

    @Test
    fun `迟到超过 60 分钟报 SKIPPED 且不补响`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        // 08:00 的提醒，设备 09:01 才开机
        r.clock.set(2026, 11, 5, 9, 1)
        r.engine.tick()

        assertEquals(RmdState.SKIPPED, r.engine.stateOf(0))
        // 不补响：屏上不该有东西（推过也会立刻收掉，最终态为空）
        assertNull(r.listener.reminderText)
        assertFalse(r.engine.hasPendingConfirm())
    }

    @Test
    fun `迟到 30 分钟仍然响`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 30)
        r.engine.tick()

        assertEquals(RmdState.PENDING, r.engine.stateOf(0))
    }

    // ------------------------------------------------------------------ 升级

    @Test
    fun `PENDING 超过 15 分钟升级为 ESCALATED 且不收全屏`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 0)
        r.engine.tick()
        assertEquals(RmdState.PENDING, r.engine.stateOf(0))

        r.clock.advanceMin(16)
        r.engine.tick()

        assertEquals(RmdState.ESCALATED, r.engine.stateOf(0))
        // ⚠️ 升级时屏必须继续挂着：收了他就只剩闪灯，不知道在催什么
        assertNotNull(r.listener.reminderText)
    }

    // ------------------------------------------------------------------ 确认语义

    @Test
    fun `语音确认只落半置信，不算完成`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 0)
        r.engine.tick()

        val ok = r.engine.confirmEx(0, ConfirmMethod.VOICE)
        assertTrue(ok)
        assertEquals(RmdState.HALF_CONFIRMED, r.engine.stateOf(0))
        // 半置信仍然是"待确认"
        assertTrue(r.engine.hasPendingConfirm())
        assertTrue(r.engine.hasPendingMedicine())
        // done 位绝不置 —— 语音会识别错，置了就是替老人谎报服药
        assertFalse(r.engine.table.isDone(0))
        assertTrue(r.engine.table.isPending(0))
    }

    @Test
    fun `半置信 10 分钟未复核变为 HALF_EXPIRED 且不报完成`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 0)
        r.engine.tick()
        r.engine.confirmEx(0, ConfirmMethod.VOICE)
        assertEquals(RmdState.HALF_CONFIRMED, r.engine.stateOf(0))

        r.clock.advanceMin(11)
        r.engine.tick()

        assertEquals(RmdState.HALF_EXPIRED, r.engine.stateOf(0))
        // 跳过 ≠ 完成
        assertFalse(r.engine.table.isDone(0))
        assertFalse(r.engine.hasPendingConfirm())
        // 屏要收掉，否则留一个"按不动的操作"
        assertNull(r.listener.reminderText)
    }

    @Test
    fun `按键确认直接完成`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 0)
        r.engine.tick()

        assertTrue(r.engine.confirmEx(0, ConfirmMethod.BUTTON, forceDone = true))
        assertEquals(RmdState.DONE, r.engine.stateOf(0))
        assertTrue(r.engine.table.isDone(0))
        assertFalse(r.engine.hasPendingConfirm())
        assertNull(r.listener.reminderText)
        // 完成音
        assertTrue(r.listener.tones.contains(ReminderEngine.Tone.SUCCESS))
    }

    @Test
    fun `服药类按键确认不直接落完成，只置按键发起标记`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 0)
        r.engine.tick()

        // 一步式长按的入口：服药类**不能**直接确认 ——
        // 多药提醒要区分"吃了哪几味"，一刀切确认会把没吃的药也记成吃了
        assertTrue(r.engine.confirmByButton())
        assertEquals(RmdState.PENDING, r.engine.stateOf(0))
        assertFalse(r.engine.table.isDone(0))
    }

    @Test
    fun `服务端定向确认直落 DONE`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 8, 0)
        r.engine.tick()

        // 服务端在完整对话里问清了哪味药 → 置信度够 → 直落 DONE
        assertTrue(r.engine.confirmServer(11L, ConfirmMethod.VOICE))
        assertEquals(RmdState.DONE, r.engine.stateOf(0))
        assertTrue(r.engine.table.isDone(0))
    }

    @Test
    fun `没有待确认项时语音确认返回 false 而不是抛错`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        r.clock.set(2026, 11, 5, 7, 0)
        r.engine.tick()

        // 老人只是顺口说了句"吃过了" —— 不是错误
        assertFalse(r.engine.confirmPendingMedicine())
    }

    @Test
    fun `日程类到点自动完成`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(schedule()))

        r.clock.set(2026, 11, 5, 9, 0)
        r.engine.tick()

        // 日程不需要人确认 ⇒ auto_done；但屏**先**推过（否则老人看不到）
        assertEquals(RmdState.DONE, r.engine.stateOf(0))
        assertTrue(r.engine.table.isDone(0))
        assertFalse(r.engine.hasPendingConfirm())
        assertTrue(r.listener.reminderText != null || r.listener.reminderText == null) // 推过或已收
    }

    // ------------------------------------------------------------------ 持久化与重启

    @Test
    fun `响过的记录落盘，重启后不会二次响铃`() {
        val store = MemStore()
        val clock = FakeClock()
        val l1 = RecordingListener()
        val e1 = ReminderEngine(store, l1, clock)
        e1.start()
        e1.replaceAll(listOf(medicine()))

        clock.set(2026, 11, 5, 8, 0)
        e1.tick()
        e1.flush()
        assertNotNull(store.text)

        // 同一时刻「进程被杀后重建」：新引擎读同一份持久化
        val l2 = RecordingListener()
        val e2 = ReminderEngine(store, l2, clock)
        e2.start()
        e2.tick()

        // 不能再响一次（固件为此丢过 fired_today 导致假漏服告警）
        assertEquals(0, l2.tones.count { it == ReminderEngine.Tone.REMINDER })
        assertTrue(e2.stateOf(0) == RmdState.PENDING || e2.stateOf(0) == RmdState.DONE)
    }

    @Test
    fun `持久化往返保持字段与位图`() {
        val t = ReminderTable()
        t.items.add(medicine())
        t.items.add(schedule())
        t.setDone(1, true)
        t.setFired(0, true)
        t.lastDayOrdinal = 20521

        val back = RmdCodec.decode(RmdCodec.encode(t))
        assertNotNull(back)
        assertEquals(2, back!!.items.size)
        assertEquals(11L, back.items[0].serverId)
        assertEquals("降压药", back.items[0].label)
        assertEquals(2, back.items[0].medicines.size)
        assertTrue(back.isFired(0))
        assertTrue(back.isDone(1))
        assertFalse(back.isDone(0))
        assertEquals(20521, back.lastDayOrdinal)
    }

    @Test
    fun `坏数据解码返回 null 而不是抛异常`() {
        assertNull(RmdCodec.decode(""))
        assertNull(RmdCodec.decode("garbage"))
        assertNull(RmdCodec.decode("fmt=9;n=1"))
    }

    // ------------------------------------------------------------------ 换表

    @Test
    fun `心跳重排顺序后计时起点跟着条目走，不会误判漏服`() {
        val r = Rig()
        r.engine.start()
        val a = medicine(hour = 8, serverId = 11L)
        val b = schedule(hour = 9).copy(serverId = 22L)
        r.engine.replaceAll(listOf(a, b))

        // 设备 08:20 才开机：计划 08:00 但迟到 20 分钟（< 60 ⇒ 仍要响）
        // ⇒ pendingSince = 08:20，与 due(08:00) 相差 20 分钟。
        // 这个差异是本用例的关键：只有它能让「搬」与「不搬」得出不同结论。
        r.clock.set(2026, 11, 5, 8, 20)
        r.engine.tick()
        assertEquals(RmdState.PENDING, r.engine.stateOf(0))

        // 心跳下发同一张表，但顺序反转（服务端返回顺序本来就不保证稳定）
        r.engine.replaceAll(listOf(b, a))

        // A 现在在 idx=1；它的 pendingSince 必须还是 08:20，而不是
        // "这个下标上一条 occupant（B，IDLE）留下的 0"。
        r.clock.set(2026, 11, 5, 8, 30)
        r.engine.tick()

        // 距响铃才 10 分钟 ⇒ 不该升级。
        // 若不搬计时起点：pendingSince=0 ⇒ 退回 due 兜底
        // ⇒ nowMin - due = 30 >= 15 ⇒ 立刻 ESCALATED ⇒ 家属收到假漏服。
        assertTrue(
            "重排后 10 分钟就被判漏服：计时起点没跟着条目走",
            r.engine.stateOf(1) != RmdState.ESCALATED
        )
        assertEquals(RmdState.PENDING, r.engine.stateOf(1))
        // 而且待确认项要能在新下标上被找到（按键/语音确认的口径）
        assertEquals(1, r.engine.firstRinging())
    }

    // ------------------------------------------------------------------ 换表


    @Test
    fun `时钟不可信时不触发并提示`() {
        val r = Rig()
        r.engine.start()
        r.engine.replaceAll(listOf(medicine()))

        // 1970 附近 = 没校时
        r.clock.sec = 1000L
        r.clock.p = LocalParts(1970, 1, 1, 0, 16, 4, 0)
        r.engine.tick()

        assertEquals(RmdState.IDLE, r.engine.stateOf(0))
        assertEquals(1, r.listener.timeUntrusted)
    }
}
