package com.elder.lx04.reminder

/**
 * 时间来源 —— 抽成接口是为了让引擎能在 JVM 单测里被「摆布时间」。
 *
 * 提醒引擎几乎所有 bug 都藏在时间边界上（跨天、late 59 vs 61 分钟、
 * 15 分钟升级、10 分钟复核窗口）。如果直接调 `System.currentTimeMillis()`，
 * 这些边界要么等一年才复现，要么只能靠 sleep —— 而 sleep 出来的测试
 * 又慢又不稳，最后一定会被注释掉。
 */
interface Clock {
    /** 当前 unix 秒。 */
    fun nowSec(): Long

    /** 单调毫秒（算间隔用，不受用户改时间影响）。 */
    fun nowMs(): Long

    /** 本地时区的当前时刻拆解。 */
    fun parts(): LocalParts

    /** 本地时区偏移（分钟，东八区 = 480）。 */
    fun tzOffsetMin(): Int

    companion object {
        /** 生产实现：系统时钟 + 系统时区。 */
        val SYSTEM: Clock = SystemClock()
    }
}

/** 本地时刻拆解（对应 C 的 `struct tm` 里我们用到的字段）。 */
data class LocalParts(
    /** 四位年份。 */
    val year: Int,
    /** 1-12。 */
    val mon: Int,
    /** 1-31。 */
    val mday: Int,
    /** 0-23。 */
    val hour: Int,
    /** 0-59。 */
    val minute: Int,
    /** 0 = 周日 … 6 = 周六（`Calendar.DAY_OF_WEEK` 减 1）。 */
    val wday: Int,
    /** 0 = 1 月 1 日（`Calendar.DAY_OF_YEAR` 减 1）。 */
    val yday: Int
)

/** 生产时钟。 */
private class SystemClock : Clock {
    override fun nowSec(): Long = System.currentTimeMillis() / 1000L
    override fun nowMs(): Long = System.currentTimeMillis()

    override fun parts(): LocalParts {
        val c = java.util.Calendar.getInstance()
        return LocalParts(
            year = c.get(java.util.Calendar.YEAR),
            mon = c.get(java.util.Calendar.MONTH) + 1,
            mday = c.get(java.util.Calendar.DAY_OF_MONTH),
            hour = c.get(java.util.Calendar.HOUR_OF_DAY),
            minute = c.get(java.util.Calendar.MINUTE),
            wday = c.get(java.util.Calendar.DAY_OF_WEEK) - 1,
            yday = c.get(java.util.Calendar.DAY_OF_YEAR) - 1
        )
    }

    override fun tzOffsetMin(): Int =
        java.util.TimeZone.getDefault().getOffset(System.currentTimeMillis()) / 60_000
}

/**
 * 提醒表的持久化 —— 固件用 NVS blob + CRC32，Android 用 JSON 文件。
 *
 * ⚠️ 实现必须做到**原子写**（写临时文件 → `rename`，或 `SharedPreferences`
 * 的 `commit()` 同步落盘）。提醒表写坏了等于老人当天所有提醒消失，
 * 而这类故障现场几乎无法复现。
 */
interface RmdStore {
    /** 读表；没有数据 / 解析失败都返回 null（不要抛，坏了要能自愈成空表）。 */
    fun load(): ReminderTable?

    /** 写表。失败返回 false，引擎会保留 dirty 并在下一轮重试。 */
    fun save(table: ReminderTable): Boolean
}

/**
 * 服药提醒引擎 —— `firmware/components/elder/src/reminder.c`（1893 行）的移植。
 *
 * ## 为什么状态机要 1:1 搬过来，而不是"用 Android 的方式重写"
 *
 * 这套状态机的每一个状态都对应服务端的一种事件结论（PENDING/FIRED/
 * CONFIRMED/MISSED/SKIPPED）。少一个状态、或者把两个状态合并，服务端就会
 * 收到错的结论 —— 而错的结论会直接变成家属手机上「老人漏服药」的告警。
 * 举个具体的：语音确认必须是 HALF_CONFIRMED 而不是 DONE，因为语音会识别错；
 * 一置 DONE 就等于替老人谎报服药。
 *
 * ## 触发判定是**三道闸门**，不是"到点就响"
 *
 * ```
 * !firedToday[i]  &&  state == IDLE  &&  nowMin >= due
 * ```
 *
 * 少任何一道，在"当天重启"后都会二次响铃。固件为这个 bug 付过代价
 * （`fired_today` 原本不落盘），Android 的进程重建比 ESP32 断电频繁得多，
 * 所以 [ReminderTable.firedToday] 从第一版就落盘。
 *
 * ## 线程模型
 *
 * **非线程安全**。所有方法都要在同一个线程上调用（Android 侧放在
 * `HandlerThread` 或单线程 dispatcher）。固件也是单任务驱动的（`tick` 由
 * 同步任务循环调用）。加锁反而会掩盖"谁在什么时候改了状态"的问题。
 */
class ReminderEngine(
    private val store: RmdStore,
    private val listener: Listener,
    private val clock: Clock = Clock.SYSTEM
) {

    /** 引擎对外的事件（对应固件 `set_state()` 的同步回调 + `elder_bridge.cc` 的行为序列）。 */
    interface Listener {
        /**
         * 某条提醒的状态变了。
         *
         * @param old 迁移前
         * @param now 迁移后
         * @param method 本次迁移关联的确认方式（[ConfirmMethod]；只对确认类迁移有意义）
         */
        fun onStateChanged(idx: Int, old: RmdState, now: RmdState, method: Int)

        /** 全屏提醒文案；`null` = 收掉。文案由 [screenTextOf] 产出，是唯一真源。 */
        fun onReminderText(text: String?)

        /** 播放提示音（`elder_tone_t` 编号）。 */
        fun onTone(tone: Int)

        /** TTS 播报（在线时用）。 */
        fun onAnnounce(text: String)

        /** 播放预录片段（`elder_clip_id_t` 编号）；实现返回 false 表示不可用，调用方回落提示音。 */
        fun onPlayClip(clipId: Int): Boolean

        /** 时间不可信（未校时）。屏显提醒老人"时间未校准，请连网"。 */
        fun onTimeUntrusted()
    }

    /** 提示音编号（对齐 `elder_ui.h:49-60` 的 `elder_tone_t`）。 */
    object Tone {
        const val NONE = 0
        const val POPUP = 1
        const val EXCLAMATION = 2
        const val SUCCESS = 3
        const val VIBRATION = 4
        const val LOW_BATTERY = 5
        const val REMINDER = 6
    }

    /** 预录片段编号（对齐 `elder_clip.h:38-60` 的 `elder_clip_id_t`）。 */
    object Clip {
        const val NONE = 0
        const val MEDICINE = 1
        const val TODO = 2
        const val MED_CONFIRM = 3
        const val CONFIRM_HINT = 4
        const val ESCALATED = 5
        const val SCHED_DONE = 6
        const val DONE = 7
    }

    /** 当前表（只读给外部看；改请走方法）。 */
    var table: ReminderTable = ReminderTable()
        private set

    /** 每条提醒的运行态。**不落盘**：重启后由 [restoreDailyState] 从位图恢复。 */
    private val states = Array(RmdConst.MAX_ITEMS) { RmdState.IDLE }

    /** 确认窗截止（`nowMs` 基准）。跨重启无意义，一律不落盘。 */
    private val confirmDeadlineMs = LongArray(RmdConst.MAX_ITEMS)

    /** 进入 PENDING 的时刻（`nowMs`）。重启后为 0，那时退回 `nowMin - due` 判断。 */
    private val pendingSinceMs = LongArray(RmdConst.MAX_ITEMS)

    /** 进入 HALF_CONFIRMED 的时刻（`nowMs`）。 */
    private val halfSinceMs = LongArray(RmdConst.MAX_ITEMS)

    private var lastConfirmMethod = ConfirmMethod.NONE
    private var dirty = false
    private var lastFlushMs = 0L
    private var lastTimeWarnMs = 0L
    private var lastNoticeMs = -1L

    /** 同一分钟去重（键 = `firedDueAt / 60`），对应固件 `s_last_announced_min`。 */
    private var lastAnnouncedMin = -1L

    /** 升级去重，对应固件 `s_last_escalated_min`。 */
    private var lastEscalatedMin = -1L

    /** 当前是否正在显示的提醒文案；`null` = 屏上没有。引擎靠它对账。 */
    private var showingText: String? = null

    /** 最近一次确认的"按键发起"标记（serverId + 时刻），TTL 10 分钟。 */
    private var medArmedServerId = 0L
    private var medArmedMs = 0L

    // ------------------------------------------------------------------ 生命周期

    /**
     * 从持久化恢复并重建运行态。
     *
     * 必须在任何 [tick] 之前调用一次。表坏了就当空表（老人会收到服务端
     * 下一轮心跳下发的完整表），**不要**让引擎起不来 —— 起不来等于连
     * 语音都没有。
     */
    fun start() {
        val loaded = store.load()
        table = loaded ?: ReminderTable()
        table.clampMasks()
        restoreDailyState()
    }

    /**
     * 从位图恢复运行态（对应固件 `restore_daily_state()`）。
     *
     * ⚠️ **不调 [setState]**：重启时重放全部副作用会再播一遍提示音、
     * 再闪一遍灯，老人会以为提醒重新响了一次。这里只静默改状态。
     */
    private fun restoreDailyState() {
        for (i in table.items.indices) {
            states[i] = when {
                table.isDone(i) -> RmdState.DONE
                table.isPending(i) -> RmdState.PENDING
                else -> RmdState.IDLE
            }
            // fired 位补齐：已完成/待确认的一定响过，漏补会让它当天再响一次。
            if (table.isDone(i) || table.isPending(i)) table.setFired(i, true)
            confirmDeadlineMs[i] = 0L
            pendingSinceMs[i] = 0L
            halfSinceMs[i] = 0L
        }
    }

    // ------------------------------------------------------------------ 主循环

    /**
     * 主循环 —— 固件 `tick()`（`reminder.c:494-685`）的移植。建议每秒一次。
     *
     * 顺序即契约，别重排：时间闸门 → 跨天 → 逐条触发/过期 → 确认窗 →
     * 升级 → 半置信超时 → 反向对账。
     */
    fun tick() {
        val nowSec = clock.nowSec()

        // ---- 1. 时间闸门：未校时一律不触发 ----
        //
        // 依据用户明确要求：「时间不准时，离线提醒就是错的，比不提醒更危险」。
        // 光打日志等于没说 —— 老人看到的是一切正常的待机屏，而当天所有提醒
        // 一条都不会响。所以必须屏显告知（30 分钟限频，不发声：此刻多半离线，
        // 突兀的提示音只会吓人）。
        if (nowSec < RmdConst.MIN_TRUSTED_EPOCH_SEC) {
            val nowMs = clock.nowMs()
            if (lastTimeWarnMs == 0L || nowMs - lastTimeWarnMs > 60_000L) {
                lastTimeWarnMs = nowMs
            }
            if (lastNoticeMs < 0 || nowMs - lastNoticeMs > 30L * 60L * 1000L) {
                lastNoticeMs = nowMs
                listener.onTimeUntrusted()
            }
            return
        }

        val p = clock.parts()

        // ---- 2. 跨天重置 ----
        //
        // 日序号取自本地时区。**绝不能**用 nowSec/86400（那是 UTC 日）：
        // CST-8 下会把每天 08:00 判成跨天，清掉已确认记录并让提醒二次响铃。
        val ord = Schedule.dayOrdinalForRollover(p.year, p.yday)
        if (table.lastDayOrdinal != 0 && ord != table.lastDayOrdinal) {
            resetForNewDay()
        }
        table.lastDayOrdinal = ord

        val todayYmd = Schedule.ymdOf(p.year, p.mon, p.mday)
        val todayOrd = Schedule.ordinalOf(p.year, p.mon, p.mday)
        val nowMin = p.hour * 60 + p.minute

        for (i in 0 until minOf(table.items.size, RmdConst.MAX_ITEMS)) {
            val it = table.items[i]
            if (!Schedule.dueOn(it.toWhen(), todayYmd, todayOrd, p.wday)) continue

            val due = it.dueMinuteOfDay

            // ---- 3. 触发：三道闸门 ----
            if (!table.isFired(i) && states[i] == RmdState.IDLE && nowMin >= due) {
                table.setFired(i, true)
                dirty = true
                // 回执定位实例靠 firedDueAt：写成"今天 hour:minute:00"
                it.firedDueAt = dueSecOf(nowSec, due, clock.tzOffsetMin())

                val lateMin = nowMin - due
                if (lateMin > RmdConst.STALE_MIN) {
                    // 过期太久：到点前后设备离线/断电。不补响、不升级，
                    // 只报 SKIPPED 让子女端知道"这次没提醒"，而不是"漏服"。
                    setState(i, RmdState.SKIPPED, ConfirmMethod.NONE)
                } else {
                    pendingSinceMs[i] = clock.nowMs()
                    setState(i, RmdState.PENDING, ConfirmMethod.NONE)
                }
            }

            // ---- 4. 确认窗超时 → 回到未确认 ----
            if (states[i] == RmdState.CONFIRM_WIN && confirmDeadlineMs[i] != 0L &&
                clock.nowMs() > confirmDeadlineMs[i]
            ) {
                confirmDeadlineMs[i] = 0L
                setState(i, RmdState.PENDING, ConfirmMethod.NONE)
            }

            // ---- 5. PENDING 超 15 分钟 → 升级 ----
            //
            // 基准是**响铃时刻**，不是计划时刻：用 due 的话，设备晚开机 20 分钟
            // 就会在响铃的同一个 tick 里被判漏服，子女端收到假告警。
            // pendingSinceMs 是内存值，重启后为 0；那时退回 due 基准 ——
            // 能走到这里说明这条当天确实响过（fired 位已落盘），判漏服是合理的。
            if (states[i] == RmdState.PENDING) {
                val overdue = if (pendingSinceMs[i] != 0L) {
                    clock.nowMs() - pendingSinceMs[i] >= RmdConst.ESCALATE_MIN * 60_000L
                } else {
                    nowMin - due >= RmdConst.ESCALATE_MIN
                }
                if (overdue) setState(i, RmdState.ESCALATED, ConfirmMethod.NONE)
            }

            // ---- 6. 半置信复核窗口超时 → 跳过本次（且不上报）----
            if (states[i] == RmdState.HALF_CONFIRMED) {
                val base = if (halfSinceMs[i] != 0L) halfSinceMs[i] else pendingSinceMs[i]
                if (base != 0L && clock.nowMs() - base >= RmdConst.HALF_REVIEW_MS) {
                    halfSinceMs[i] = 0L
                    setState(i, RmdState.HALF_EXPIRED, ConfirmMethod.NONE)
                }
            }
        }

        // ---- 7. 反向对账：引擎有待确认项，屏上却没提醒 ----
        //
        // 固件为这个专门加了 >=5 秒的重推：屏和引擎脱节时，老人看不到该按什么，
        // 而设备一直在闪灯催他按 —— 他根本不知道该按哪个。
        if (hasPendingConfirm() && showingText == null) {
            val idx = firstRinging()
            if (idx >= 0) showReminderText(screenTextOf(idx))
        }

        // ---- 8. 落盘（30 秒去抖）----
        flushIfDue()
    }

    /** 跨天重置（对应固件 `reminder.c:523-544`）。 */
    private fun resetForNewDay() {
        table.doneToday = 0
        table.pendingToday = 0
        // 「今天响过」也是**当日**事实，跨天必须一起清。
        // 漏清的后果：新一天的第一次 tick 里每条都因 fired 位还在而当天再也不响。
        table.firedToday = 0
        for (i in states.indices) {
            states[i] = RmdState.IDLE
            confirmDeadlineMs[i] = 0L
            pendingSinceMs[i] = 0L
            halfSinceMs[i] = 0L
        }
        for (it in table.items) it.firedDueAt = 0L
        lastAnnouncedMin = -1L
        lastEscalatedMin = -1L
        dirty = true
        // 跨天清空 ⇒ 灯不该再亮、屏不该再挂。
        if (showingText != null) {
            showingText = null
            listener.onReminderText(null)
        }
    }

    /**
     * `nowSec` 所在**本地日**的 `dueMin` 分钟对应的 unix 秒。
     *
     * 先把 unix 秒挪到"本地墙上时间"，取整到当天 0 点，再挪回 unix 秒。
     * 不能直接用 `nowSec - nowSec % 86400`：那是 UTC 日，东八区下算出来的
     * 起点会差 8 小时，回执的 due_at 就整体错位 —— 服务端按
     * `(reminder_id, due_at)` 定位实例，错位等于确认到了别人身上。
     */
    private fun dueSecOf(nowSec: Long, dueMin: Int, tzOffsetMin: Int): Long {
        val off = tzOffsetMin * 60L
        val local = nowSec + off
        val dayStartLocal = local - Math.floorMod(local, 86_400L)
        return dayStartLocal - off + dueMin * 60L
    }

    // ------------------------------------------------------------------ 状态迁移

    /**
     * 统一的状态迁移入口 —— 固件 `set_state()`。
     *
     * **所有副作用都从这里出去**，不要在别处直接改 [states]。固件的教训：
     * 有一处绕过 set_state 直接赋状态，结果屏收了、灯没灭。
     */
    private fun setState(idx: Int, now: RmdState, method: Int) {
        if (idx !in states.indices) return
        val old = states[idx]
        if (old == now) return
        states[idx] = now
        lastConfirmMethod = method
        dirty = true

        // 位图同步（DONE/PENDING 与 doneToday/pendingToday 必须一致，
        // 否则重启后 restoreDailyState 会还原成错的状态）
        when (now) {
            RmdState.DONE -> {
                table.setDone(idx, true)
                table.setPending(idx, false)
            }
            RmdState.PENDING, RmdState.ESCALATED, RmdState.CONFIRM_WIN -> {
                table.setPending(idx, true)
                table.setDone(idx, false)
            }
            RmdState.SKIPPED, RmdState.HALF_EXPIRED, RmdState.IDLE -> {
                // 跳过 ≠ 完成：done 位绝不置。
                table.setPending(idx, false)
            }
            RmdState.HALF_CONFIRMED -> {
                // 半置信**仍然是待确认**：pending 位保持，
                // done 位绝不置（否则等于替老人谎报服药）。
                table.setPending(idx, true)
            }
        }

        listener.onStateChanged(idx, old, now, method)
        applySideEffects(idx, old, now)
    }

    /**
     * 状态迁移的副作用 —— 固件 `elder_bridge.cc:513-741` 的行为序列。
     *
     * 这一段是"产品行为"而不是"技术细节"：什么时候念什么、什么时候收屏、
     * 什么时候只闪灯不出声，都是用户在真机上一条条定过的。
     */
    private fun applySideEffects(idx: Int, old: RmdState, now: RmdState) {
        val item = table.items.getOrNull(idx) ?: return

        when (now) {
            RmdState.PENDING -> {
                // 每条都推全屏（即使本分钟已播过音）
                showReminderText(screenTextOf(idx))
                // 日程类：直接判完成（不需要人确认），但**仍在上面推了屏**，
                // 顺序不能反 —— 先 auto_done 再推屏的话，屏上会出现一条
                // 已完成的事项，老人按下去会得到"没有待确认的提醒"。
                if (!item.isMedicine) {
                    autoDoneInner(idx)
                    return
                }
                // 同一分钟去重：键 = firedDueAt/60
                val minKey = item.firedDueAt / 60
                if (minKey != lastAnnouncedMin) {
                    lastAnnouncedMin = minKey
                    val clipOk = listener.onPlayClip(Clip.MEDICINE)
                    listener.onTone(Tone.REMINDER)
                    if (!clipOk) {
                        listener.onAnnounce(announceTextOf(item))
                    }
                }
            }

            RmdState.DONE -> {
                hideReminder()
                listener.onTone(Tone.SUCCESS)
            }

            RmdState.ESCALATED -> {
                // ⚠️ **不收全屏**：升级时要让老人一直看见"该吃什么"，
                // 收了他就只剩闪灯，不知道在催什么。
                val minKey = item.firedDueAt / 60
                if (minKey != lastEscalatedMin) {
                    lastEscalatedMin = minKey
                    val clipId = if (item.isMedicine) Clip.CONFIRM_HINT else Clip.ESCALATED
                    if (!listener.onPlayClip(clipId)) {
                        listener.onTone(Tone.EXCLAMATION)
                    }
                }
            }

            RmdState.SKIPPED, RmdState.HALF_EXPIRED -> {
                // 收屏，防止屏上留一个"按不动的操作"。
                hideReminder()
            }

            else -> Unit
        }
    }

    private fun showReminderText(text: String) {
        showingText = text
        listener.onReminderText(text)
    }

    private fun hideReminder() {
        if (showingText == null) return
        showingText = null
        listener.onReminderText(null)
    }

    /** 在线时的播报话术：「您今天{上午|中午|下午|晚上}有{服药提醒|待办事项}，请您知悉」。 */
    private fun announceTextOf(item: ReminderItem): String {
        val p = clock.parts()
        val period = when {
            p.hour < 12 -> "上午"
            p.hour < 14 -> "中午"
            p.hour < 18 -> "下午"
            else -> "晚上"
        }
        val what = if (item.isMedicine) "服药提醒" else "待办事项"
        // ⚠️ 必须写成 `${period}` 而不是 `$period`：Kotlin 的标识符允许 Unicode
        //    字母，而汉字属于 Letter 类 —— `$period有` 会被解析成标识符
        //    `period有`（未定义），编译报 "unresolved reference: period有"。
        //    这个坑只在"变量后面紧跟中文"时出现，普通英文串里永远遇不到。
        return "您今天${period}有${what}，请您知悉"
    }

    // ------------------------------------------------------------------ 确认入口

    /**
     * 按下标确认 —— 固件 `elder_reminder_confirm_ex()`。
     *
     * @param method [ConfirmMethod]
     * @param forceDone `true` 表示"置信度已经够了"，与 method 无关，
     *        直落 DONE。目前唯一使用者是服务端判定（[confirmServer]）。
     *
     * ⚠️ `VOICE` 且非 forceDone ⇒ **HALF_CONFIRMED**，不是 DONE。
     * 语音会识别错，只能记下来不能当完成。置 DONE 等于替老人谎报服药。
     */
    fun confirmEx(idx: Int, method: Int, forceDone: Boolean = false): Boolean {
        if (idx !in states.indices) return false
        if (idx >= table.items.size) return false
        val st = states[idx]
        if (!st.isRinging) return false

        // 先记方式再改状态：setState 会同步回调，那时上层要能读到正确的方式。
        lastConfirmMethod = method
        confirmDeadlineMs[idx] = 0L

        if (method == ConfirmMethod.VOICE && !forceDone) {
            // 半置信：置复核窗口起点。必须在 setState 之前 ——
            // 晚一行就可能被 tick 读到 0 而用 pendingSinceMs 兜底。
            halfSinceMs[idx] = clock.nowMs()
            setState(idx, RmdState.HALF_CONFIRMED, method)
        } else {
            // 无论哪条路径，都要清掉复核窗口起点：留着它 tick 会在 10 分钟后
            // 把这条已 DONE 的提醒改判成 HALF_EXPIRED。
            halfSinceMs[idx] = 0L
            setState(idx, RmdState.DONE, method)
        }
        return true
    }

    /**
     * 服务端代确认（APP 上点的"我已服用"）—— 固件 `elder_reminder_confirm_remote()`。
     *
     * 定位方式：`serverId` 相同且 `|firedDueAt - dueAt| <= 60s`；
     * 落 DONE，method **强制 APP**（因为确实是家属在手机上点的）。
     */
    fun confirmRemote(serverId: Long, dueAt: Long, method: Int = ConfirmMethod.APP): Boolean {
        val idx = pickItemForConfirm(serverId, dueAt) ?: return false
        return confirmEx(idx, method, forceDone = true)
    }

    /**
     * 服务端判定确认 —— 固件 `elder_reminder_confirm_server()`。
     *
     * 用于「老人说了话、服务端在完整对话里问清了哪味药」的场景：
     * 置信度够，所以**直落 DONE**（不走半置信）。
     *
     * 只接受 VOICE / BUTTON：APP/AUTO 走 [confirmRemote] / [autoDone]。
     *
     * @param method 由"这次确认是不是按键发起"决定（命中 10 分钟内的按键标记
     *        ⇒ [ConfirmMethod.BUTTON]，否则 [ConfirmMethod.VOICE]）。服务端
     *        靠它把 VOICE 升级成 VOICE_AND_BUTTON(5) = 全置信。
     */
    fun confirmServer(serverId: Long, method: Int): Boolean {
        if (serverId <= 0L) return false
        if (method != ConfirmMethod.VOICE && method != ConfirmMethod.BUTTON) return false
        val idx = pickItemForConfirm(serverId, 0L) ?: return false
        return confirmEx(idx, method, forceDone = true)
    }

    /**
     * 语音确认（服务端没给 serverId 时的旧路径）—— 固件 `elder_confirm_pending_medicine()`。
     *
     * 命中"按键发起"标记 ⇒ 走定向路径报 BUTTON（老人在 10 分钟内按过键，
     * 那就是全置信）；否则报 VOICE，落 HALF_CONFIRMED。
     *
     * @return `false` 表示当前没有待确认项（**不是错误**：老人可能只是
     *         顺口说了句"吃过了"）。调用方据此回 MCP `false`，服务端据此
     *         知道设备状态没同步上。
     */
    fun confirmPendingMedicine(): Boolean {
        val idx = firstRinging()
        if (idx < 0) {
            // 引擎里没有待确认项但屏还挂着（日程类早已 auto_done）—— 收掉。
            // 不收的话老人会说"我说了、它也答应了，屏幕一直赶不走"。
            hideReminderIfResolved()
            return false
        }
        val item = table.items[idx]
        val armed = isMedArmed(item.serverId)
        if (armed) {
            consumeArmed()
            return confirmEx(idx, ConfirmMethod.BUTTON, forceDone = true)
        }
        return confirmEx(idx, ConfirmMethod.VOICE)
    }

    /**
     * 按键确认 —— 一步式（生产固件 `elder_bridge.cc:1246-1260`）。
     *
     * 这是**长按满 3 秒即刻调用**的入口，不等松手。
     *
     * @return `false` 表示当前没有可确认项（灯应闪失败色，屏应提示）
     */
    fun confirmByButton(): Boolean {
        val idx = firstRinging()
        if (idx < 0) {
            hideReminderIfResolved()
            return false
        }
        val item = table.items[idx]

        if (!item.isMedicine) {
            // 日程/待办：直接确认
            val ok = confirmEx(idx, ConfirmMethod.BUTTON, forceDone = true)
            if (ok) listener.onPlayClip(Clip.SCHED_DONE)
            return ok
        }

        // 服药类：置"按键发起"标记，交给上层播追问话术并开麦
        // （"您吃的是哪种药？"）。服务端在完整对话里判定后回调 confirmServer。
        //
        // 为什么不在这一步直接确认：多药提醒要区分"吃了哪几味"，一刀切确认
        // 会把没吃的药也记成吃了 —— 医疗场景下这是不能接受的失真。
        armMed(item.serverId)
        return true
    }

    /**
     * 自动完成（只对非服药类开放）—— 固件 `elder_reminder_auto_done()`。
     *
     * 不回调、不闪完成灯、不额外上报：它不是"人确认了"，只是"这条不需要确认"。
     */
    fun autoDone(idx: Int): Boolean {
        if (idx !in states.indices || idx >= table.items.size) return false
        if (table.items[idx].isMedicine) return false   // 服药类禁止自动完成
        return autoDoneInner(idx)
    }

    private fun autoDoneInner(idx: Int): Boolean {
        if (states[idx] == RmdState.DONE) return false
        states[idx] = RmdState.DONE
        table.setDone(idx, true)
        table.setPending(idx, false)
        dirty = true
        return true
    }

    // ------------------------------------------------------------------ 判据

    /** 是否有待确认的提醒（含半置信）—— 灯/屏同源判据。 */
    fun hasPendingConfirm(): Boolean {
        for (i in 0 until minOf(table.items.size, RmdConst.MAX_ITEMS)) {
            if (states[i].isRinging) return true
        }
        return false
    }

    /** 是否有待确认的**服药**提醒。 */
    fun hasPendingMedicine(): Boolean {
        for (i in 0 until minOf(table.items.size, RmdConst.MAX_ITEMS)) {
            if (states[i].isRinging && table.items[i].isMedicine) return true
        }
        return false
    }

    /** 按键此刻是否"有可做的事"（决定灯亮不亮）。对应 `elder_reminder_button_actionable()`。 */
    fun buttonActionable(): Boolean = hasPendingConfirm() || showingText != null

    /**
     * 正在等确认的那条（多条取 `firedDueAt` 最大）—— **按键/语音选目标的唯一口径**。
     *
     * ⚠️ 按键和语音必须共用这一个判据。各写一套的话会出现
     * "按键确认第 1 条、语音确认第 3 条"的错乱。
     */
    fun firstRinging(): Int {
        var best = -1
        var bestDue = Long.MIN_VALUE
        for (i in 0 until minOf(table.items.size, RmdConst.MAX_ITEMS)) {
            if (!table.items[i].enabled) continue
            if (!states[i].isRinging) continue
            val due = table.items[i].firedDueAt
            if (best < 0 || due > bestDue) {
                best = i
                bestDue = due
            }
        }
        return best
    }

    /** 取某条的状态（只读）。 */
    fun stateOf(idx: Int): RmdState =
        if (idx in states.indices) states[idx] else RmdState.IDLE

    /** 最近一次的确认方式。 */
    fun lastConfirmMethod(): Int = lastConfirmMethod

    /** 屏上当前文案；`null` = 没有。 */
    fun currentReminderText(): String? = showingText

    // ------------------------------------------------------------------ 定位与标记

    /**
     * 按 `(serverId, dueAt)` 定位条目 —— 固件 `pick_item_for_confirm()`。
     *
     * `dueAt != 0` 按 ±60 秒匹配（`firedDueAt == 0` 的不参与）；
     * `dueAt == 0` 取 `enabled && 可确认态` 中 `firedDueAt` 最大者。
     */
    private fun pickItemForConfirm(serverId: Long, dueAt: Long): Int? {
        var best = -1
        var bestDue = Long.MIN_VALUE
        for (i in 0 until minOf(table.items.size, RmdConst.MAX_ITEMS)) {
            val it = table.items[i]
            if (it.serverId != serverId) continue
            if (!it.enabled) continue
            if (dueAt != 0L) {
                if (it.firedDueAt == 0L) continue
                if (Math.abs(it.firedDueAt - dueAt) > 60L) continue
            } else if (!states[i].isRinging) {
                continue
            }
            if (best < 0 || it.firedDueAt > bestDue) {
                best = i
                bestDue = it.firedDueAt
            }
        }
        return if (best >= 0) best else null
    }

    private fun armMed(serverId: Long) {
        medArmedServerId = serverId
        medArmedMs = clock.nowMs()
    }

    /** 按键发起标记是否命中（顺手清过期的）。 */
    private fun isMedArmed(serverId: Long): Boolean {
        if (medArmedMs <= 0L || serverId <= 0L) return false
        if (clock.nowMs() - medArmedMs > RmdConst.MED_ARMED_TTL_MS) {
            consumeArmed()
            return false
        }
        return medArmedServerId == serverId
    }

    private fun consumeArmed() {
        medArmedServerId = 0L
        medArmedMs = 0L
    }

    // ------------------------------------------------------------------ 换表

    /**
     * 用服务端下发的表整表替换 —— 固件 `elder_reminder_replace_all()`。
     *
     * ## 运行态继承（这一段错了就是"确认过的提醒又响了"）
     *
     * 新表和旧表按 `serverId` 对齐；对不上的再按内容（type + hour + minute + label）
     * 兜底。命中就把 `state / firedToday / firedDueAt / pendingSince` 等
     * **继承过去**。一条旧条目只能被认领一次（`claimed` 位图）——
     * 否则两条同内容的新条目会抢同一份旧状态。
     *
     * @param incoming 服务端 `reminders[]`（已按后端顺序）
     */
    fun replaceAll(incoming: List<ReminderItem>) {
        val old = table.items
        val oldStates = IntArray(old.size) { states[it].ordinal }
        val oldDone = table.doneToday
        val oldPending = table.pendingToday
        val oldFired = table.firedToday

        val claimed = BooleanArray(old.size)
        val newItems = ArrayList<ReminderItem>(minOf(incoming.size, RmdConst.MAX_ITEMS))
        val newStates = IntArray(RmdConst.MAX_ITEMS) { RmdState.IDLE.ordinal }
        val newFiredDue = LongArray(RmdConst.MAX_ITEMS)

        for (inc in incoming) {
            if (newItems.size >= RmdConst.MAX_ITEMS) break
            val dst = inc.copy()

            // 1) 按 serverId 找
            var src = -1
            if (dst.serverId != 0L) {
                for (j in old.indices) {
                    if (!claimed[j] && old[j].serverId != 0L && old[j].serverId == dst.serverId) {
                        src = j; break
                    }
                }
            }
            // 2) 按内容兜底
            if (src < 0) {
                for (j in old.indices) {
                    if (claimed[j]) continue
                    val o = old[j]
                    if (o.type == dst.type && o.hour == dst.hour &&
                        o.minute == dst.minute && o.label == dst.label
                    ) {
                        src = j; break
                    }
                }
            }

            val idx = newItems.size
            if (src >= 0) {
                claimed[src] = true
                newStates[idx] = oldStates[src]
                newFiredDue[idx] = old[src].firedDueAt
            }
            newItems.add(dst)
        }

        table.items = newItems
        table.clampMasks()

        // 位图按新条目重算：旧位图的下标语义已经变了，必须先清再按继承的状态重建。
        table.doneToday = 0
        table.pendingToday = 0
        table.firedToday = 0

        for (i in newItems.indices) {
            states[i] = RmdState.values()[newStates[i]]
            table.items[i].firedDueAt = newFiredDue[i]
            when (states[i]) {
                RmdState.DONE -> table.setDone(i, true)
                RmdState.PENDING, RmdState.ESCALATED, RmdState.CONFIRM_WIN,
                RmdState.HALF_CONFIRMED -> table.setPending(i, true)
                else -> Unit
            }
            if (states[i] != RmdState.IDLE) table.setFired(i, true)
            // 跨重启的 30 秒窗无意义
            confirmDeadlineMs[i] = 0L
        }

        // 未继承到的条目一律从 IDLE 起（不能留着上一张表的状态）
        for (i in newItems.size until RmdConst.MAX_ITEMS) {
            states[i] = RmdState.IDLE
            confirmDeadlineMs[i] = 0L
            pendingSinceMs[i] = 0L
            halfSinceMs[i] = 0L
        }

        dirty = true
        flush()

        // 同步后对账：引擎已无待确认项而屏还挂着 ⇒ 收掉
        if (!hasPendingConfirm() && showingText != null) hideReminder()
        else if (hasPendingConfirm() && showingText == null) firstRinging().let {
            if (it >= 0) showReminderText(screenTextOf(it))
        }

        // 保留变量只为可读性：旧位图不再使用
        @Suppress("UNUSED_EXPRESSION")
        oldDone; oldPending; oldFired
    }

    // ------------------------------------------------------------------ 文案

    /**
     * 全屏提醒文案 —— 固件 `elder_reminder_screen_text()`（唯一真源）。
     *
     * 格式契约（显示层按此拆行）：
     * - 第 1 个非空行 = 标题（「该吃<label>了」/「<label>时间到了」）
     * - 以「请」开头的行 = 提示语
     * - 其余行 = 明细（一味药一行 / 多条清单）
     *
     * ⚠️ **行动指令（长按确认）不在文案里**，由显示层画在按钮上。
     * 把"请长按确认"写进文案，换交互（比如以后改成语音确认）时文案就撒谎了。
     */
    fun screenTextOf(idx: Int): String {
        val item = table.items.getOrNull(idx)
            ?: return "时间到了"

        val title = if (item.isMedicine) "该吃${item.label}了" else "${item.label}时间到了"
        val hint = if (item.isMedicine) "请及时服用" else "请及时处理"
        val detail = item.medLine.trim()

        return buildString {
            append(title)
            append('\n')
            append(hint)
            if (detail.isNotEmpty()) {
                // 多条明细按全角顿号拆行：一味药一行，老人一眼看清吃什么。
                for (line in detail.split('、', '；', ';')) {
                    val t = line.trim()
                    if (t.isNotEmpty()) {
                        append('\n')
                        append(t)
                    }
                }
            }
        }
    }

    private fun hideReminderIfResolved() {
        if (!hasPendingConfirm() && showingText != null) hideReminder()
    }

    // ------------------------------------------------------------------ 落盘

    /** 30 秒去抖落盘；失败保留 dirty 下轮重试。 */
    private fun flushIfDue() {
        if (!dirty) return
        val nowMs = clock.nowMs()
        if (lastFlushMs != 0L && nowMs - lastFlushMs < 30_000L) return
        flush()
    }

    /**
     * 立即落盘。
     *
     * ⚠️ 失败**不能清 dirty**：清掉就再也不会重试，而"响过"这个事实丢了
     * 就会导致二次响铃（固件为此专门写了注释）。
     */
    fun flush() {
        if (!dirty) return
        val ok = store.save(table)
        lastFlushMs = clock.nowMs()
        if (ok) dirty = false
    }
}
