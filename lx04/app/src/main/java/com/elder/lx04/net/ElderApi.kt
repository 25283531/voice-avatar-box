package com.elder.lx04.net

import android.util.Log
import com.elder.lx04.reminder.EventState
import com.elder.lx04.reminder.ReminderItem
import com.elder.lx04.reminder.ReminderType
import com.elder.lx04.reminder.RmdConst
import com.elder.lx04.reminder.RmdType
import com.elder.lx04.reminder.Schedule
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

/**
 * 设备端 HTTP 接口 —— `firmware/components/elder/src/sync.c` + `net.c` 的移植。
 *
 * ## 覆盖的三个接口
 *
 * | 接口 | 作用 | 固件出处 |
 * |---|---|---|
 * | `POST /api/device/sync` | 心跳 + 拉全量配置（提醒表/校时/时区/ws 地址/换 WiFi 指令） | `sync.c:1545-1790` |
 * | `POST /api/device/events` | 回执（触发/确认/漏服/跳过） | `sync.c:860-922` |
 * | `POST /api/device/interactions` | 交互上报（确认/漏服次数） | `sync.c:1073` |
 *
 * ## 两条不能违反的契约
 *
 * 1. **`reminders` 的"空数组"与"字段缺失"是相反的意思**
 *    - 空数组 ⇒ 服务端说"一条都没有" ⇒ **清空本地表**
 *    - 字段缺失/不是数组 ⇒ 响应残缺 ⇒ **一个字都不许动本地表**
 *
 *    用 `optJSONArray("reminders") ?: JSONArray()` 兜底会把后者当后者，
 *    一次残缺响应就能清空老人整张提醒表。固件为这个 bug 写过复盘
 *    （家属删光提醒后设备照旧天天响，因为旧代码把"0 条"当"没数据"）。
 *    所以这里必须显式 `has()` + `is JSONArray` 双重判定，见 [parseSync]。
 *
 * 2. **`due_at` 是"设备本地钟点"，不带时区偏移**
 *
 *    服务端按 `(reminder_id, due_at)` 定位"哪一天的这一次提醒"。发成 UTC
 *    或带偏移，会定位不到 → 服务端补建一条新实例 → 家属看到"确认了但
 *    状态还是待确认"。格式就是 `yyyy-MM-dd'T'HH:mm:ss`（本地时间）。
 *
 * ## 阻塞调用
 *
 * 所有方法都是阻塞的，调用方放工作线程。**不要在 UI 线程调**。
 */
class ElderApi(
    private val baseUrl: String,
    private val deviceId: String,
    private val clientId: String,
    /** 每次请求现取 —— token 会被心跳/OTA 更新，缓存住就会一直用旧的。 */
    private val tokenProvider: () -> String
) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        // 读超时给 12 秒：sync 要下发整张提醒表 + 头像信息，局域网慢时更久
        .readTimeout(12, TimeUnit.SECONDS)
        // 心跳每 5 分钟一次，连接复用能省掉一次 TCP+TLS 握手
        .retryOnConnectionFailure(true)
        .build()

    private val jsonMedia = "application/json; charset=utf-8".toMediaType()

    // ------------------------------------------------------------------ 心跳

    /** 心跳结果。 */
    data class SyncResult(
        val ok: Boolean,
        /**
         * 服务端下发的提醒表。
         *
         * ⚠️ `null` 与空列表意思**相反**：
         * - `null` = 响应里没有 `reminders`（或类型不对）⇒ 保留本地表
         * - 空列表 = 服务端说一条都没有 ⇒ 清空本地表
         */
        val reminders: List<ReminderItem>?,
        /** 服务端时间（unix 秒）；`null` = 没给，别改本地偏移。 */
        val serverTimeSec: Long?,
        /** 时区偏移（分钟）；`null` = 服务端没说，沿用本地值。 */
        val tzOffsetMin: Int?,
        /** 服务端下发的 ws 地址（可能随部署变）。 */
        val wsUrl: String?,
        /** 心跳周期（秒），服务端没给时用 [DEFAULT_SYNC_INTERVAL_SEC]。 */
        val intervalSec: Int,
        /** 改 WiFi 指令；`null` = 没有。 */
        val wifiChange: WifiChange?,
        /** 失败原因（成功时为空串）。 */
        val message: String = ""
    )

    /** 服务端下发的换 WiFi 指令（`DeviceWifiChangeOut`）。 */
    data class WifiChange(val reqId: Long, val ssid: String, val password: String)

    /**
     * 心跳 + 拉全量配置 —— `POST /api/device/sync?fw_version=&hw_model=`。
     *
     * @param wifiResult 上一次改 WiFi 的终态（断网时攒下来的），随心跳补发。
     *        服务端**先收结果再下发新指令**，顺序反了会出现自相矛盾的中间态。
     */
    fun sync(
        fwVersion: String,
        hwModel: String,
        wifiResult: JSONObject? = null
    ): SyncResult {
        val url = buildString {
            append(baseUrl.trimEnd('/'))
            append("/api/device/sync?fw_version=")
            append(encode(fwVersion))
            // hw_model 为空时**不加这个参数**（退化成旧 URL 形态，老服务端照常解析）
            if (hwModel.isNotEmpty()) {
                append("&hw_model=")
                append(encode(hwModel))
            }
        }

        val body = JSONObject().apply {
            if (wifiResult != null) put("wifi_result", wifiResult)
        }.toString().toRequestBody(jsonMedia)

        val result = request(url, body) ?: return SyncResult(
            ok = false, reminders = null, serverTimeSec = null, tzOffsetMin = null,
            wsUrl = null, intervalSec = DEFAULT_SYNC_INTERVAL_SEC, wifiChange = null,
            message = "网络不可用"
        )

        return parseSync(result)
    }

    /**
     * 解析 sync 响应。
     *
     * **处理顺序即契约**（固件 `sync.c:1609-1790`）：
     * 时区 → 校时 → 提醒表 → 周期 → ws 地址 → 换 WiFi。
     *
     * 顺序错了的症状都很隐蔽：先校时后设时区，日志时区会自相矛盾；
     * 提醒表在换 WiFi 之后处理，换 WiFi 断网 45 秒期间到点的提醒会漏。
     */
    private fun parseSync(json: String): SyncResult {
        val obj = runCatching { JSONObject(json) }.getOrElse {
            return SyncResult(
                ok = false, reminders = null, serverTimeSec = null, tzOffsetMin = null,
                wsUrl = null, intervalSec = DEFAULT_SYNC_INTERVAL_SEC, wifiChange = null,
                message = "响应不是 JSON: ${json.take(120)}"
            )
        }

        // ---- 1. 时区（必须在校时之前）----
        val tz = if (obj.has("tz_offset_minutes") && !obj.isNull("tz_offset_minutes")) {
            obj.optInt("tz_offset_minutes", 0)
        } else null

        // ---- 2. 校时 ----
        val serverTime = parseServerTime(obj.optString("server_time", ""))

        // ---- 3. 提醒表（空数组 vs 缺失，语义相反）----
        val reminders: List<ReminderItem>? = readReminders(obj)

        // ---- 4. 心跳周期（只接受 60..3600，与固件一致）----
        val interval = obj.optInt("sync_interval_sec", DEFAULT_SYNC_INTERVAL_SEC)
            .let { if (it in 60..3600) it else DEFAULT_SYNC_INTERVAL_SEC }

        // ---- 5. ws 地址 ----
        val ws = obj.optString("ws_url", "").takeIf { it.isNotEmpty() }

        // ---- 6. 换 WiFi 指令（**只解析不执行**；执行必须由调用方最后做）----
        val wifi = obj.optJSONObject("wifi_change")?.let { w ->
            WifiChange(
                reqId = w.optLong("req_id", 0L),
                ssid = w.optString("ssid", ""),
                password = w.optString("password", "")
            )
        }

        return SyncResult(
            ok = true,
            reminders = reminders,
            serverTimeSec = serverTime,
            tzOffsetMin = tz,
            wsUrl = ws,
            intervalSec = interval,
            wifiChange = wifi
        )
    }

    /**
     * 从响应里读提醒表。
     *
     * @return `null` = 字段缺失或不是数组 ⇒ **调用方必须保留本地表**
     */
    private fun readReminders(obj: JSONObject): List<ReminderItem>? {
        // ⚠️ 这两行是整个文件最关键的判据。
        //    少写 `has()`，`optJSONArray` 在字段缺失时返回 null，
        //    再被 `?: JSONArray()` 兜成空数组 ⇒ "残缺响应"被当成"服务端说没有"
        //    ⇒ 老人整张提醒表被清空，而且服务端那边看起来一切正常。
        if (!obj.has("reminders")) return null
        val arr = obj.opt("reminders")
        if (arr !is JSONArray) return null

        val out = ArrayList<ReminderItem>(minOf(arr.length(), RmdConst.MAX_ITEMS))
        for (i in 0 until arr.length()) {
            if (out.size >= RmdConst.MAX_ITEMS) break
            val r = arr.optJSONObject(i) ?: continue
            out.add(parseReminder(r))
        }
        return out
    }

    /** 单条提醒：后端 `ReminderOut` → [ReminderItem]。 */
    private fun parseReminder(r: JSONObject): ReminderItem {
        val times = r.optJSONArray("times_of_day")
        // 服务端 `times_of_day` 是 ["HH:MM"] 列表；一条提醒只取第一个时刻
        // （固件同样只支持每条一个钟点，多时刻由服务端拆成多条）。
        val (h, m) = parseHhMm(times?.optString(0, "") ?: "")

        val kind = r.optInt("schedule_kind", Schedule.KIND_DAILY)

        // weekdays 是「1=周一 … 7=周日」还是「0=周日 … 6=周六」？
        // 服务端 `month_days`/`weekdays` 用的是 ISO 口径（1..7，7=周日），
        // 而固件 `repeat_mask` 是 bit0=周日。转换必须在这里做完，
        // 让引擎只看到 bit0=周日 这一种口径 —— 两套口径混用，症状是
        // "周日那条提醒在周一响"，而且只有周日那条错。
        val repeatMask = isoWeekdaysToMask(r.optJSONArray("weekdays"))

        val monthMask = intListToBitMask(r.optJSONArray("month_days"), max = 31)

        val medicines = ArrayList<String>()
        val medArr = r.optJSONArray("medicines")
        if (medArr != null) {
            for (i in 0 until minOf(medArr.length(), 4)) {
                val mo = medArr.optJSONObject(i) ?: continue
                val name = mo.optString("name", "").trim()
                val dose = mo.optString("dose", "").trim()
                if (name.isNotEmpty()) {
                    medicines.add(if (dose.isEmpty()) name else "$name $dose")
                }
            }
        }
        // 老字段（一味药）：`dose` 是剂量，`title` 是药名
        val title = r.optString("title", "")
        val medLine = if (medicines.isNotEmpty()) {
            medicines.joinToString("、")
        } else {
            val dose = r.optString("dose", "").trim()
            if (dose.isEmpty()) "" else dose
        }

        return ReminderItem(
            serverId = r.optLong("id", 0L),
            hour = h,
            minute = m,
            type = if (r.optInt("rtype", ReminderType.SCHEDULE) == ReminderType.MEDICINE) {
                RmdType.MEDICINE
            } else {
                RmdType.SCHEDULE
            },
            rtype = r.optInt("rtype", ReminderType.SCHEDULE),
            enabled = r.optBoolean("enabled", true),
            label = title.take(RmdConst.LABEL_MAX),
            scheduleKind = kind,
            repeatMask = repeatMask,
            onceYmd = ymdOfDate(r.optString("once_date", "")),
            monthMask = monthMask,
            intervalDays = r.optInt("interval_days", 1).coerceAtLeast(1),
            startYmd = ymdOfDate(r.optString("start_date", "")),
            endYmd = ymdOfDate(r.optString("end_date", "")),
            medicines = medicines,
            medLine = medLine
        )
    }

    // ------------------------------------------------------------------ 回执

    /**
     * 上报一次提醒事件。
     *
     * @param reminderId 后端 `Reminder.id`（本地自建条目是 0，此时**不要上报**：
     *        服务端没有对应规则，补建出来的实例会挂在一个不存在的提醒下）
     * @param dueAtSec 这次实例的到期时刻（**用条目的 `fired_due_at`**，
     *        不是 `now()` —— 见 [com.elder.lx04.reminder.ReminderItem.dueOf]）
     * @param state [EventState]
     * @param method [com.elder.lx04.reminder.ConfirmMethod]
     * @param medicine 本次确认的是哪一味药；空串 = 整条确认
     */
    fun reportEvent(
        reminderId: Long,
        dueAtSec: Long,
        state: Int,
        method: Int,
        tzOffsetMin: Int,
        medicine: String = "",
        note: String = ""
    ): Boolean {
        if (reminderId <= 0L) return false

        val dueIso = isoLocal(dueAtSec, tzOffsetMin)
        val body = JSONObject().apply {
            put("reminder_id", reminderId)
            put("due_at", dueIso)
            put("state", state)
            put("confirm_method", method)
            if (state == EventState.CONFIRMED) put("confirmed_at", isoLocal(nowSec(), tzOffsetMin))
            if (state == EventState.FIRED || state == EventState.MISSED) {
                put("fired_at", isoLocal(nowSec(), tzOffsetMin))
            }
            if (note.isNotEmpty()) put("note", note)
            if (medicine.isNotEmpty()) put("medicine", medicine)
        }

        val res = request(baseUrl.trimEnd('/') + "/api/device/events", body.toString().toRequestBody(jsonMedia))
        return res != null
    }

    /** 交互上报（确认/漏服次数）。`kind` 见 `InteractionKind`。 */
    fun reportInteraction(kind: Int, durationMs: Long): Boolean {
        val body = JSONObject().apply {
            put("kind", kind)
            put("duration_ms", durationMs)
        }
        val res = request(
            baseUrl.trimEnd('/') + "/api/device/interactions",
            body.toString().toRequestBody(jsonMedia)
        )
        return res != null
    }

    // ------------------------------------------------------------------ 底层

    /** 发一次请求，返回响应体；任何失败都返回 null（不抛）。 */
    private fun request(url: String, body: okhttp3.RequestBody?): String? {
        val tok = tokenProvider()
        val builder = Request.Builder()
            .url(url)
            .header("Device-Id", deviceId)
            .header("Client-Id", clientId)
            .header("User-Agent", "elder-lx04/1.0")
        if (tok.isNotEmpty()) builder.header("Authorization", "Bearer $tok")

        val req = if (body != null) {
            builder.post(body).build()
        } else {
            builder.get().build()
        }

        return runCatching {
            http.newCall(req).execute().use { resp ->
                val text = resp.body?.string().orEmpty()
                if (!resp.isSuccessful) {
                    // 401/403 单独打：token 过期时这是唯一线索，
                    // 而且它和"网络不通"长得一模一样（都是拿不到数据）。
                    Log.w(TAG, "HTTP ${resp.code} $url -> ${text.take(160)}")
                    null
                } else {
                    text
                }
            }
        }.getOrElse {
            Log.w(TAG, "请求失败 $url: ${it.message}")
            null
        }
    }

    private fun nowSec(): Long = System.currentTimeMillis() / 1000L

    // ------------------------------------------------------------------ 工具

    private fun parseServerTime(s: String): Long? {
        if (s.isEmpty()) return null
        // 服务端给的是带偏移的 ISO8601（如 2026-11-05T21:30:00+08:00）。
        // 逐种格式尝试，全失败就返回 null —— **绝不能猜**：
        // 拿错的时间去校时，会把设备推到几个小时之外，当天提醒全错。
        val patterns = listOf(
            "yyyy-MM-dd'T'HH:mm:ssXXX",
            "yyyy-MM-dd'T'HH:mm:ss.SSSXXX",
            "yyyy-MM-dd'T'HH:mm:ss",
            "yyyy-MM-dd'T'HH:mm:ss.SSS"
        )
        for (p in patterns) {
            runCatching {
                val f = SimpleDateFormat(p, Locale.US)
                if (!p.contains("XXX")) f.timeZone = TimeZone.getTimeZone("UTC")
                val t = f.parse(s) ?: return@runCatching
                return t.time / 1000L
            }
        }
        Log.w(TAG, "无法解析 server_time: $s")
        return null
    }

    /** 本地钟点 ISO（**不带偏移**）—— 服务端按它和 `reminder_events.due_at` 对账。 */
    private fun isoLocal(sec: Long, tzOffsetMin: Int): String {
        val f = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)
        // 固件的时间轴是"墙上时间当 UTC"，这里同样：把本地钟点格式化出来，
        // 不让 SimpleDateFormat 再叠加一次时区。
        f.timeZone = TimeZone.getTimeZone("UTC")
        return f.format(Date((sec + tzOffsetMin * 60L) * 1000L))
    }

    private fun parseHhMm(s: String): Pair<Int, Int> {
        val parts = s.split(':')
        if (parts.size < 2) return 0 to 0
        val h = parts[0].trim().toIntOrNull() ?: 0
        val m = parts[1].trim().toIntOrNull() ?: 0
        return h.coerceIn(0, 23) to m.coerceIn(0, 59)
    }

    private fun ymdOfDate(s: String): Int {
        // "2026-11-05" 或 "2026-11-05T..." 都接受
        val datePart = s.substringBefore('T')
        val p = datePart.split('-')
        if (p.size != 3) return 0
        val y = p[0].toIntOrNull() ?: return 0
        val mo = p[1].toIntOrNull() ?: return 0
        val d = p[2].toIntOrNull() ?: return 0
        return Schedule.ymdOf(y, mo, d)
    }

    /**
     * 服务端 `weekdays`（ISO：1=周一 … 7=周日）→ 固件 `repeat_mask`（bit0=周日）。
     *
     * ⚠️ 两套口径必须在这里一次转完。混用的症状是"周日那条在周一响"，
     * 而且**只有周日那条错** —— 周一到周六都对，所以谁都不会怀疑转换。
     */
    private fun isoWeekdaysToMask(arr: JSONArray?): Int {
        if (arr == null) return 0
        var mask = 0
        for (i in 0 until arr.length()) {
            val iso = arr.optInt(i, 0)
            if (iso !in 1..7) continue
            // ISO 7(周日) → bit0；ISO 1(周一) → bit1 … ISO 6(周六) → bit6
            val bit = iso % 7
            mask = mask or (1 shl bit)
        }
        return mask
    }

    /** `month_days`（1..31）→ bit0=1 号 的位图。 */
    private fun intListToBitMask(arr: JSONArray?, max: Int): Int {
        if (arr == null) return 0
        var mask = 0
        for (i in 0 until arr.length()) {
            val d = arr.optInt(i, 0)
            if (d in 1..max) mask = mask or (1 shl (d - 1))
        }
        return mask
    }

    private fun encode(s: String): String = java.net.URLEncoder.encode(s, "UTF-8")

    companion object {
        private const val TAG = "ElderApi"

        /** 心跳默认周期（固件 `CONFIG_ELDER_SYNC_INTERVAL_SEC` 缺省值）。 */
        const val DEFAULT_SYNC_INTERVAL_SEC = 300

        /** 未绑定时最多等 15 秒再看一眼（固件 `sync.c:1844`）。 */
        const val UNBOUND_RETRY_SEC = 15
    }
}
