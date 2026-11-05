package com.elder.lx04.store

import android.content.Context
import android.util.Log
import com.elder.lx04.reminder.ReminderTable
import com.elder.lx04.reminder.RmdCodec
import com.elder.lx04.reminder.RmdStore

/**
 * 设备端本地持久化 —— token、服务端地址、提醒表。
 *
 * ## 为什么必须有这一层（不是"锦上添花"）
 *
 * 调查发现：Android 侧**完全没有持久化**，token 是每次启动现取的一次性变量。
 * 后果是把整台设备的功能绑在"每一次 OTA 握手都成功"上：
 *
 *   · 服务端重启 / 网络抖动 / 证书过期 ⇒ 这一次 OTA 失败 ⇒ **连语音都没有**
 *   · 固件不是这么干的：token 落 NVS，心跳失败照样能用旧 token 重连
 *
 * 提醒表更明显：不落盘就意味着进程被杀（系统回收、崩溃、OTA）之后
 * 当天所有提醒连同「响过」的事实一起消失 —— 而「响过」丢了的直接后果是
 * 二次响铃，随即被升级分支判成漏服。固件为这个 bug 付过代价。
 *
 * ## 为什么用 `SharedPreferences` 而不是自己写文件
 *
 * 需要的是**原子 + 已 fsync** 的写入：`commit()` 是同步落盘（`apply()` 不是，
 * 它只保证"最终会写"，进程被杀时可能丢）。自己写文件要处理 tmp+rename、
 * fsync 目录、损坏回滚 —— 这些 SharedPreferences 已经做对了。
 */
class DeviceStore(context: Context) {

    private val prefs = context.applicationContext
        .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    // ------------------------------------------------------------------ 凭据

    /**
     * 设备 token（设备 JWT）。
     *
     * 空串 = 还没拿到过。UI 上表现为"离线"，但**不代表要重新激活** ——
     * 只要 [hasToken] 为真，OTA 失败时就应该继续用旧 token 尝试连接。
     */
    var token: String
        get() = prefs.getString(KEY_TOKEN, "").orEmpty()
        set(v) { prefs.edit().putString(KEY_TOKEN, v).commit() }

    /** WebSocket 地址（服务端下发，可能随部署变化）。 */
    var wsUrl: String
        get() = prefs.getString(KEY_WS_URL, "").orEmpty()
        set(v) { prefs.edit().putString(KEY_WS_URL, v).commit() }

    /** token 最后一次写入的时刻（unix 秒），用于日志与"多久没成功过"判断。 */
    var tokenAt: Long
        get() = prefs.getLong(KEY_TOKEN_AT, 0L)
        set(v) { prefs.edit().putLong(KEY_TOKEN_AT, v).commit() }

    /** 是否有可用 token。 */
    val hasToken: Boolean get() = token.isNotEmpty()

    /** 一次性记住 OTA 结果（token + ws 地址同时写，避免"半个新、半个旧"）。 */
    fun saveCredentials(ws: String, tok: String, nowSec: Long) {
        prefs.edit()
            .putString(KEY_WS_URL, ws)
            .putString(KEY_TOKEN, tok)
            .putLong(KEY_TOKEN_AT, nowSec)
            .commit()
    }

    /** 服务端地址（默认走编译期常量，允许运行时改）。 */
    var baseUrl: String
        get() = prefs.getString(KEY_BASE_URL, "").orEmpty()
        set(v) { prefs.edit().putString(KEY_BASE_URL, v).commit() }

    // ------------------------------------------------------------------ 时钟

    /**
     * 本地时钟相对服务端的偏移（毫秒）。
     *
     * ## 为什么要偏移，而不是"直接把系统时间设对"
     *
     * 普通应用**无权设置系统时钟**（`SystemClock.setCurrentTimeMillis` 需要
     * `SET_TIME` 签名权限，系统应用才有）。固件可以直接写 RTC，我们不行 ——
     * 照搬固件的「写 RTC」在 Android 上会静默无效，而且日志里一切正常。
     *
     * 所以：所有"现在几点"的判断都用 `System.currentTimeMillis() + offset`。
     * 偏移随每次心跳更新（服务端 `server_time`）。
     *
     * ⚠️ 偏移落盘：断电重启后、联网之前，用上次的偏移比用裸系统时间准
     * （LX04 无电池，断电后系统时钟会漂）。
     */
    var clockOffsetMs: Long
        get() = prefs.getLong(KEY_CLOCK_OFFSET, 0L)
        set(v) { prefs.edit().putLong(KEY_CLOCK_OFFSET, v).commit() }

    /** 时区偏移（分钟，东八区 = 480）。服务端 `tz_offset_minutes` 下发。 */
    var tzOffsetMin: Int
        get() = prefs.getInt(KEY_TZ_OFFSET, DEFAULT_TZ_OFFSET)
        set(v) { prefs.edit().putInt(KEY_TZ_OFFSET, v).commit() }

    // ------------------------------------------------------------------ 状态灯

    /** 状态灯开关（`self.elder.set_status_light`）。事件灯不受它影响。 */
    var statusLightOn: Boolean
        get() = prefs.getBoolean(KEY_STATUS_LIGHT, true)
        set(v) { prefs.edit().putBoolean(KEY_STATUS_LIGHT, v).commit() }

    // ------------------------------------------------------------------ 提醒表

    /** 提醒表持久化实现（供 [com.elder.lx04.reminder.ReminderEngine] 使用）。 */
    val reminderStore: RmdStore = object : RmdStore {
        override fun load(): ReminderTable? {
            val text = prefs.getString(KEY_RMD_TABLE, "").orEmpty()
            if (text.isEmpty()) return null
            val t = RmdCodec.decode(text)
            if (t == null) {
                // 坏数据不自愈成"静默空表"：先清掉，让服务端下一轮心跳重发。
                // 但**必须留日志**——静默清空等于老人的提醒凭空消失，没有痕迹。
                Log.e(TAG, "提醒表解析失败，已清空等待服务端重发（原长 ${text.length}）")
                prefs.edit().remove(KEY_RMD_TABLE).commit()
            }
            return t
        }

        override fun save(table: ReminderTable): Boolean =
            prefs.edit().putString(KEY_RMD_TABLE, RmdCodec.encode(table)).commit()
    }

    private companion object {
        const val TAG = "ElderStore"
        const val PREFS_NAME = "elder_device"
        const val KEY_TOKEN = "token"
        const val KEY_WS_URL = "ws_url"
        const val KEY_TOKEN_AT = "token_at"
        const val KEY_BASE_URL = "base_url"
        const val KEY_CLOCK_OFFSET = "clock_offset_ms"
        const val KEY_TZ_OFFSET = "tz_offset_min"
        const val KEY_STATUS_LIGHT = "status_light_on"
        const val KEY_RMD_TABLE = "rmd_table"

        /** 东八区。与固件 NVS 默认值一致（服务端没说时沿用）。 */
        const val DEFAULT_TZ_OFFSET = 480
    }
}
