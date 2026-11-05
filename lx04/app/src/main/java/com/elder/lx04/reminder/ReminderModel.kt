package com.elder.lx04.reminder

/**
 * 提醒引擎的数据模型 —— 对齐固件 `elder_reminder.h` / `elder_sync.h`。
 *
 * ## ⚠️ 这些编号是**协议**，不是内部约定
 *
 * 枚举取值会经 `elder_sync_on_reminder_state()` 映射后发到服务端，服务端按
 * 数字理解「这次是语音确认还是按键确认」「这次是漏服还是跳过」。所以每个
 * 常量都**逐位照抄**固件，注释里标了出处。改一个数字，家属端看到的历史
 * 记录就整片错位，而且两边日志都正常 —— 这类错误没有症状，只有后果。
 */
object RmdConst {
    /** 固件 `ELDER_REMINDER_MAX`：本机最多 16 条。 */
    const val MAX_ITEMS = 16

    /** 固件 `ELDER_REMINDER_LABEL`：标签长度上限（不含 NUL）。 */
    const val LABEL_MAX = 31

    /** 固件 `ELDER_RMD_STALE_MIN`：晚于计划时刻超过 60 分钟 ⇒ 不补响，报 SKIPPED。 */
    const val STALE_MIN = 60L

    /** 固件 `reminder.c` 升级阈值：PENDING 超过 15 分钟未确认 ⇒ ESCALATED。 */
    const val ESCALATE_MIN = 15L

    /** 固件 `ELDER_HALF_REVIEW_MS`：语音确认后 10 分钟内没按按钮复核 ⇒ HALF_EXPIRED。 */
    const val HALF_REVIEW_MS = 10L * 60L * 1000L

    /** 固件 `CONFIRM_WINDOW_MS`：确认窗 30 秒。 */
    const val CONFIRM_WINDOW_MS = 30_000L

    /** 固件 `ELDER_MIN_TRUSTED_EPOCH`：2020-01-01T00:00:00Z。早于它视为"未校时"。 */
    const val MIN_TRUSTED_EPOCH_SEC = 1_577_836_800L

    /** 固件 `kMedArmedTtlUs`："按键发起"标记的有效期 10 分钟。 */
    const val MED_ARMED_TTL_MS = 10L * 60L * 1000L

    /** 固件 `sync.c` 的事件队列容量。 */
    const val QUEUE_MAX = 32
}

/**
 * 单条提醒的状态机（`elder_reminder.h:70-115`，`elder_rmd_state_t`）。
 *
 * 顺序即编号，不可插入 —— 固件注释专门交代过「为什么追加在末尾而不是插在
 * PENDING 后面」，理由同样是编号经上报映射。
 */
enum class RmdState {
    /** 未到时间。 */
    IDLE,

    /** 已触发，等待确认。 */
    PENDING,

    /** 已长按，等待确认窗内短按。（一步式后主流程不再用它，但状态仍在） */
    CONFIRM_WIN,

    /** 已确认完成。 */
    DONE,

    /** 超时升级（持续告警）。 */
    ESCALATED,

    /** 过期作废：到点时设备离线/断电太久（> 60 分钟），不补响。语义对齐服务端 SKIPPED。 */
    SKIPPED,

    /**
     * 半置信：老人用**语音**说了「我吃过了」，但还没按按钮复核。
     *
     * ⚠️ 语义上仍是"待确认"：[ReminderEngine.hasPendingConfirm] /
     * [ReminderEngine.hasPendingMedicine] / [ReminderEngine.firstRinging]
     * 三个判据**都必须包含它**。漏掉任何一个，老人被提示"请按按钮"、
     * 按下去却提示"没有待确认的提醒" —— 最难解释的一类 bug。
     */
    HALF_CONFIRMED,

    /**
     * 半置信复核窗口过期 —— 本次跳过，**且不上报**。
     *
     * ⚠️ 为什么不能复用 [SKIPPED]：SKIPPED 会上报 `EV_SKIPPED` 且 note 写死
     * "stale"，把服务端刚收到的「语音确认」覆盖成「跳过」，理由还与事实不符，
     * 子女端从此看不出老人说过什么。这个状态存在的唯一理由就是
     * "本地改状态、但什么都不发"。
     *
     * ⚠️ 跳过 ≠ 完成：[ReminderTable.doneToday] 绝不置位。语音是半置信，
     * 万一误识别成"吃过了"还置完成，等于替老人谎报服药。
     */
    HALF_EXPIRED;

    /** 是否属于"待处理"集合 —— 按键/语音选目标（firstRinging）的口径。 */
    val isRinging: Boolean
        get() = this == PENDING || this == CONFIRM_WIN || this == ESCALATED ||
            this == HALF_CONFIRMED
}

/** 提醒类型（`elder_rmd_type_t`）：0 = 日程，1 = 服药。 */
object RmdType {
    const val SCHEDULE = 0
    const val MEDICINE = 1
}

/** 后端 `ReminderType`：0=服药 1=日程 2=喝水 3=运动 4=自定义。 */
object ReminderType {
    const val MEDICINE = 0
    const val SCHEDULE = 1
    const val WATER = 2
    const val SPORT = 3
    const val CUSTOM = 4
}

/**
 * 确认方式（`elder_sync.h:51-57`，`elder_confirm_method_t`）。
 *
 * 服务端合并规则：库里已是 `VOICE` 且本次回执是 `BUTTON` + CONFIRMED
 * ⇒ 合并成 `VOICE_AND_BUTTON(5)` = 全置信。
 */
object ConfirmMethod {
    const val NONE = 0
    const val VOICE = 1
    const val BUTTON = 2
    const val APP = 3
    const val AUTO = 4
}

/** 上报的事件状态（`elder_sync.h:43-49`，`elder_event_state_t`）。 */
object EventState {
    const val PENDING = 0
    const val FIRED = 1
    const val CONFIRMED = 2
    const val MISSED = 3
    const val SKIPPED = 4
}

/** 交互类型（`elder_sync.h:59-66`）。 */
object InteractionKind {
    const val WAKE = 0
    const val DIALOG = 1
    const val REMINDER_CONFIRM = 2
    const val REMINDER_MISS = 3
    const val SOS = 4
    const val TOUCH = 5
}

/**
 * 一条提醒（对齐 `elder_rmd_item_t`，并把排期字段一并摊平）。
 *
 * 固件把排期放在独立的 `when[]` 扩展数组里（V3 才加），那是为了兼容旧 blob；
 * Android 上没有这个包袱，直接摊进条目更不容易错位。
 */
data class ReminderItem(
    /** 后端 `Reminder.id`；0 = 设备本地自建（会被 `/reminders/local` 补报后回写）。 */
    var serverId: Long = 0L,

    /** 计划时刻。 */
    var hour: Int = 0,
    var minute: Int = 0,

    /** [RmdType]：0 日程 / 1 服药。**服药类禁止短按确认**（防误触）。 */
    var type: Int = RmdType.SCHEDULE,

    /** 后端 `ReminderType`（0=服药 1=日程 2=喝水 3=运动 4=自定义），只用于展示分组。 */
    var rtype: Int = ReminderType.SCHEDULE,

    var enabled: Boolean = true,

    /** 药名 / 事项名。长度按 [RmdConst.LABEL_MAX] 截断。 */
    var label: String = "",

    /**
     * 本次触发的到期时刻（unix 秒）。
     *
     * ⚠️ **回执定位实例靠它**：服务端按 `(reminder_id, due_at)` 找"哪一天的
     * 这一次提醒"。为 0 时用 [dueOf] 兜底算，**绝不能退回 `now()`** ——
     * 固件吃过这个亏：服务端按 `(rid, now)` 找不到实例，于是新建一条，
     * 实测出现过 `due_at` 与 `confirmed_at` 逐秒相同的假实例。
     */
    var firedDueAt: Long = 0L,

    // ---- 排期（对应后端 reminders[] 的字段）----
    var scheduleKind: Int = Schedule.KIND_DAILY,
    var repeatMask: Int = 0,
    var onceYmd: Int = 0,
    var monthMask: Int = 0,
    var intervalDays: Int = 1,
    var anchorOrdinal: Int = 0,
    var startYmd: Int = 0,
    var endYmd: Int = 0,

    // ---- 多药信息（服务端 `medicines[]`，最多 4 味）----
    /** 药名列表；空 = 没有多药信息（老数据）。 */
    var medicines: List<String> = emptyList(),

    /** 服务端拼好的「药名 + 剂量」一行（全角顿号分隔），用于全屏明细。 */
    var medLine: String = ""
) {
    /** 派生：排期规则。 */
    fun toWhen(): RmdWhen = RmdWhen(
        kind = scheduleKind,
        repeatMask = repeatMask,
        onceYmd = onceYmd,
        monthMask = monthMask,
        intervalDays = intervalDays,
        anchorOrdinal = anchorOrdinal,
        startYmd = startYmd,
        endYmd = endYmd
    )

    /** 是否服药类（决定短按能不能直接确认）。 */
    val isMedicine: Boolean get() = type == RmdType.MEDICINE

    /** 计划时刻当天的分钟数。 */
    val dueMinuteOfDay: Int get() = hour * 60 + minute

    /**
     * 回执用的 due_at（unix 秒）—— 对齐固件 `due_of_item()`。
     *
     * 1. [firedDueAt] > 0 ⇒ 直接用它
     * 2. 否则用自身的 `hour:minute` 拼今天；若落在将来则**退一天**
     *    （回执说的是"已经发生过的那一次"，未来的时刻不可能是它）
     *
     * @param nowSec 当前 unix 秒
     * @param tzOffsetMin 本地时区偏移（分钟，东八区 = 480）
     */
    fun dueOf(nowSec: Long, tzOffsetMin: Int): Long {
        if (firedDueAt > 0) return firedDueAt
        val dayStart = dayStartSec(nowSec, tzOffsetMin)
        var due = dayStart + dueMinuteOfDay * 60L
        if (due > nowSec) due -= 86_400L
        return due
    }

    private fun dayStartSec(nowSec: Long, tzOffsetMin: Int): Long {
        val local = nowSec + tzOffsetMin * 60L
        return local - Math.floorMod(local, 86_400L) - tzOffsetMin * 60L
    }
}

/**
 * 整张提醒表（对齐 `elder_rmd_table_t`）。
 *
 * ## 三个位图的分工（抄错任何一个都会出事）
 *
 * - [doneToday]：bit i = 第 i 条今天**已完成**
 * - [pendingToday]：bit i = 第 i 条今天**处于待确认**
 * - [firedToday]：bit i = 第 i 条今天**响过**
 *
 * [firedToday] 必须落盘。固件注释记着一次事故：它原本只在 RAM 里，于是
 * 「当天任意时刻重启、只要已过服药时刻就会补响一遍」，随后被升级分支立刻
 * 推成 ESCALATED ⇒ 子女端收到假漏服告警。而 Android 的进程重建比 ESP32
 * 断电频繁得多（系统回收、OTA、崩溃），这个坑**必踩**，所以这里从第一版
 * 就落盘。
 */
data class ReminderTable(
    var items: MutableList<ReminderItem> = mutableListOf(),
    var doneToday: Int = 0,
    var pendingToday: Int = 0,
    var firedToday: Int = 0,

    /**
     * 跨天检测用的日序号（`tm_year*366 + tm_yday`，见
     * [Schedule.dayOrdinalForRollover]）。0 = 还没算过。
     */
    var lastDayOrdinal: Int = 0
) {
    fun bitOf(idx: Int): Int = 1 shl idx

    fun isDone(idx: Int): Boolean = (doneToday and bitOf(idx)) != 0
    fun isPending(idx: Int): Boolean = (pendingToday and bitOf(idx)) != 0
    fun isFired(idx: Int): Boolean = (firedToday and bitOf(idx)) != 0

    fun setDone(idx: Int, on: Boolean) {
        doneToday = if (on) doneToday or bitOf(idx) else doneToday and bitOf(idx).inv()
    }

    fun setPending(idx: Int, on: Boolean) {
        pendingToday = if (on) pendingToday or bitOf(idx) else pendingToday and bitOf(idx).inv()
    }

    fun setFired(idx: Int, on: Boolean) {
        firedToday = if (on) firedToday or bitOf(idx) else firedToday and bitOf(idx).inv()
    }

    /** 按当前条目数重算位图掩码（条目减少时高位要清掉）。 */
    fun clampMasks() {
        val valid = if (items.size >= 32) -1 else (1 shl items.size) - 1
        doneToday = doneToday and valid
        pendingToday = pendingToday and valid
        firedToday = firedToday and valid
    }
}
