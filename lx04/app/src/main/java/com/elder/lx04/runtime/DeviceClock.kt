package com.elder.lx04.runtime

import com.elder.lx04.reminder.Clock
import com.elder.lx04.reminder.LocalParts
import java.util.Calendar
import java.util.TimeZone

/**
 * 设备时钟 —— 用**服务端下发的时区偏移**解释"现在几点"。
 *
 * ## 为什么不能直接用系统时区
 *
 * 固件每次心跳会拿 `tz_offset_minutes` 去 `setenv("TZ", ...)`，因为
 * 「早上八点吃药」这句规则属于**规则时区**，而规则时区是服务端按设备 IP
 * 判定的（`device_api.py::_device_tz_for`）。设备自己算不出自己的 IP 归属地。
 *
 * 如果 Android 侧直接用 `Calendar.getInstance()`（系统时区），设备被带到
 * 另一个时区时就会出现「创建按 A 时区、响铃按 B 时区」——两边的日志都正常，
 * 只有老人该吃的药没在八点响。
 *
 * ## 实现方式：不改进程时区
 *
 * `TimeZone.setDefault()` 虽然不需要权限，但它会改掉整个 JVM 的默认时区，
 * 影响日志、OkHttp、其它库的时间格式化 —— 用一个全局副作用换一个局部需求
 * 不划算。这里改成：把 `now + offsetMs` 喂给一个 **UTC** Calendar，读出来的
 * 字段就是"目标时区的墙上时间"，而 `nowSec()` 仍是纯 unix 秒。
 *
 * ## 与固件的一致性
 *
 * - [nowSec] / [nowMs]：绝对时刻，不随时区变
 * - [parts]：目标时区的墙上时间（hour/minute/wday/yday）
 * - [tzOffsetMin]：服务端下发值，用于回执 `due_at` 的本地钟点
 *
 * 三者分工与固件 `time(NULL)` / `localtime_r()` / `g.tz_offset_min` 一一对应。
 */
class DeviceClock(
    /** 每次现取：心跳会更新它。缓存住就会一直用开机时的旧值。 */
    private val offsetProvider: () -> Int,
    /** 注入时间源，供 JVM 单测摆布跨天边界。 */
    private val timeSource: () -> Long = { System.currentTimeMillis() }
) : Clock {

    override fun nowSec(): Long = timeSource() / 1000L

    override fun nowMs(): Long = timeSource()

    override fun tzOffsetMin(): Int = offsetProvider()

    override fun parts(): LocalParts {
        val c = Calendar.getInstance(TimeZone.getTimeZone("UTC"))
        c.timeInMillis = timeSource() + tzOffsetMin() * 60_000L
        return LocalParts(
            year = c.get(Calendar.YEAR),
            mon = c.get(Calendar.MONTH) + 1,
            mday = c.get(Calendar.DAY_OF_MONTH),
            hour = c.get(Calendar.HOUR_OF_DAY),
            minute = c.get(Calendar.MINUTE),
            // C 的 tm_wday：0 = 周日；Calendar：1 = 周日
            wday = c.get(Calendar.DAY_OF_WEEK) - 1,
            // C 的 tm_yday：0 = 1 月 1 日
            yday = c.get(Calendar.DAY_OF_YEAR) - 1
        )
    }
}
