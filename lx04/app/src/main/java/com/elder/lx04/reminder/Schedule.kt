package com.elder.lx04.reminder

/**
 * 提醒排程判定 —— `firmware/components/elder/src/elder_rmd_sched.c` 的 1:1 移植。
 *
 * ## 为什么单独一个文件、且全是纯函数
 *
 * 排程是「今天该不该响」的唯一判据，错了就是老人漏服药。而它**完全可以脱离
 * Android 跑**：不吃 `System.currentTimeMillis()`、不查表、不看时区 ——
 * 所有输入都由调用方（[ReminderEngine.tick]）按本地时区算好再传进来。
 *
 * 这样单测能直接构造「2 月 29 日」「月末 + 隔 3 天」「跨年相位」这些
 * 真机上要么等一年、要么根本复现不了的边界。
 *
 * ## 与固件逐条对齐的三处坑
 *
 * 1. **[ordinalOf] 不能用 `ymd / 366`  之类的近似**：`% N` 隔天排程会跨年漂移，
 *    "隔三天"的提醒在元旦后整体错位。这里用 Howard Hinnant 的
 *    `days_from_civil`，纯整数、1970-01-01 = 0，与设备算出来逐位相同。
 * 2. **跨天检测用的日序号与 `% N` 用的日序号不是同一个**（固件明确分开）：
 *    前者只要"变了没有"，后者要真实日历日号。见 [dayOrdinalForRollover]。
 * 3. **`MONTHLY` 没有"月末回退"**：只比 `mday` 位图，所以 2 月 30 日这条规则
 *    在 2 月整月不响。这是固件的既有取舍（调查员已确认固件里未找到回退逻辑），
 *    移植时**照实保留**，不要"顺手修好" —— 一改就与服务端排出来的实例对不上。
 */
object Schedule {

    /** 一次性（对齐后端 `ScheduleKind.ONCE`）。 */
    const val KIND_ONCE = 0

    /** 每天。 */
    const val KIND_DAILY = 1

    /** 每周（按 [RmdWhen.repeatMask]）。 */
    const val KIND_WEEKLY = 2

    /** 每月（按 [RmdWhen.monthMask]）。 */
    const val KIND_MONTHLY = 3

    /** 隔 N 天（按 [RmdWhen.intervalDays] + [RmdWhen.anchorOrdinal]）。 */
    const val KIND_INTERVAL_DAYS = 4

    /**
     * 日历日期 → `YYYYMMDD` 整数。
     *
     * 越界一律返回 0（= "没有这个日期"），与固件 `elder_rmd_ymd_of` 一致。
     * 调用方把这个 0 当"未设置"用，**不要**当成 1970-01-01。
     */
    fun ymdOf(year: Int, mon: Int, mday: Int): Int {
        if (year < 1900 || year > 2999) return 0
        if (mon < 1 || mon > 12) return 0
        if (mday < 1 || mday > 31) return 0
        return year * 10_000 + mon * 100 + mday
    }

    /**
     * 日历日期 → 真实日序号（1970-01-01 = 0，之前为负）。
     *
     * Howard Hinnant《chrono-Compatible Low-Level Date Algorithms》的
     * `days_from_civil`。**必须用这个**而不是 `ymd/366` 之类的近似：
     * 后者把每年当 366 天，拿它做 `% N` 会跨年漂移。
     */
    fun ordinalOf(year: Int, mon: Int, mday: Int): Int {
        var y = year
        val m = mon
        val d = mday
        if (m <= 2) y -= 1
        val era = (if (y >= 0) y else y - 399) / 400
        val yoe = y - era * 400                                    // [0, 399]
        val doy = (153 * (m + (if (m > 2) -3 else 9)) + 2) / 5 + d - 1   // [0, 365]
        val doe = yoe * 365 + yoe / 4 - yoe / 100 + doy             // [0, 146096]
        return era * 146_097 + doe - 719_468
    }

    /**
     * **只用于跨天检测**的日序号：`tm_year * 366 + tm_yday`。
     *
     * 它不需要正确，只需要"过了一天就一定变、同一天一定不变"。
     * ⚠️ **绝不能拿它做 `% N`** —— 每年按 366 天算，隔天排程会跨年漂移。
     * 真实日号请用 [ordinalOf]。
     *
     * @param year 四位年份（如 2026）
     * @param dayOfYear 0 = 1 月 1 日（对应 C 的 `tm_yday`）
     */
    fun dayOrdinalForRollover(year: Int, dayOfYear: Int): Int = year * 366 + dayOfYear

    /**
     * 今天这条排期该不该响。
     *
     * @param w 排期规则；`null` 表示没有排期信息 ⇒ **按每天**（宁可多响，不能静默不响）
     * @param todayYmd 今天的 `YYYYMMDD`（本地时区）
     * @param todayOrdinal 今天的真实日序号（本地时区，用 [ordinalOf]）
     * @param todayWday 星期几：0 = 周日 … 6 = 周六（对应 C 的 `tm_wday`）
     * @param warn 一次性告警回调（`ONCE` 没日期、未知 kind）。固件会打日志；
     *             Android 侧传 `null` 即可，但**不要**把它当异常抛 ——
     *             弹窗会挡住提醒本身。
     */
    fun dueOn(
        w: RmdWhen?,
        todayYmd: Int,
        todayOrdinal: Int,
        todayWday: Int,
        warn: ((String) -> Unit)? = null
    ): Boolean {
        // 没有排期信息 ⇒ 每天。固件注释：「宁可多响也不能静默不响」。
        if (w == null) return true

        // 区间优先：无论哪种 kind，出了 [start, end] 都不响。
        if (w.startYmd != 0 && todayYmd < w.startYmd) return false
        if (w.endYmd != 0 && todayYmd > w.endYmd) return false

        return when (w.kind) {
            KIND_ONCE -> {
                if (w.onceYmd == 0) {
                    warn?.invoke("一次性提醒没有日期（onceYmd=0），按每天处理")
                    true
                } else {
                    todayYmd == w.onceYmd
                }
            }

            KIND_DAILY -> true

            // bit0 = 周日，与 C 的 tm_wday 同序。
            KIND_WEEKLY -> (w.repeatMask and (1 shl todayWday)) != 0

            // bit0 = 1 号。只比 mday，没有月末回退（见类注释第 3 条）。
            KIND_MONTHLY -> {
                val mday = todayYmd % 100
                mday in 1..31 && (w.monthMask and (1 shl (mday - 1))) != 0
            }

            KIND_INTERVAL_DAYS -> {
                val n = w.intervalDays
                if (n <= 1) {
                    true
                } else {
                    // 负数取模修正：Kotlin 的 % 保留被除数符号，
                    // 直接拿去比较会在 anchor 之前的日子上判错。
                    ((todayOrdinal - w.anchorOrdinal) % n + n) % n == 0
                }
            }

            else -> {
                warn?.invoke("未知排程 kind=${w.kind}，按每天处理")
                true
            }
        }
    }
}

/**
 * 一条提醒的排期规则（对齐固件 `elder_rmd_when_t`）。
 *
 * 字段名与后端 `ReminderIn` 的对应关系写在每个字段上 —— 服务端 `/sync`
 * 下发的 `reminders[]` 就是这些字段，别在映射层改名。
 */
data class RmdWhen(
    /** 见 `Schedule.KIND_*`；对应后端 `schedule_kind`。 */
    val kind: Int = Schedule.KIND_DAILY,

    /** 每周哪几天：bit0 = 周日 … bit6 = 周六。对应后端 `weekdays`。 */
    val repeatMask: Int = 0,

    /** 一次性提醒的日期 `YYYYMMDD`；0 = 未设置。对应后端 `once_date`。 */
    val onceYmd: Int = 0,

    /** 每月哪几天：bit0 = 1 号 … bit30 = 31 号。对应后端 `month_days`。 */
    val monthMask: Int = 0,

    /** 隔几天；<= 1 视为每天。对应后端 `interval_days`。 */
    val intervalDays: Int = 1,

    /**
     * 隔天排程的相位基准（真实日序号）。
     *
     * 服务端 `start_date` 优先 → `created_at` → 首次同步当天。
     * ⚠️ 合并新表时要**尽量继承旧值**：一变，老人"每三天"的提醒就会
     * 整体挪一天，而他并不知道为什么今天的药提前/推迟了。
     */
    val anchorOrdinal: Int = 0,

    /** 生效起始 `YYYYMMDD`；0 = 不限。对应后端 `start_date`。 */
    val startYmd: Int = 0,

    /** 生效截止 `YYYYMMDD`；0 = 不限。对应后端 `end_date`。 */
    val endYmd: Int = 0
)
