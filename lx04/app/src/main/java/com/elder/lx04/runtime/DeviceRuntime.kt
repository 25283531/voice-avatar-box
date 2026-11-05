package com.elder.lx04.runtime

import android.content.Context
import android.media.AudioManager
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.elder.lx04.net.ElderApi
import com.elder.lx04.net.McpHandler
import com.elder.lx04.reminder.Clock
import com.elder.lx04.reminder.ConfirmMethod
import com.elder.lx04.reminder.EventState
import com.elder.lx04.reminder.InteractionKind
import com.elder.lx04.reminder.ReminderEngine
import com.elder.lx04.reminder.ReminderItem
import com.elder.lx04.reminder.RmdState
import com.elder.lx04.reminder.RmdConst
import com.elder.lx04.store.DeviceStore
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * 设备运行时 —— 把「提醒引擎 + 心跳 + 回执 + MCP」串起来。
 *
 * ## 线程模型（必须看懂再改）
 *
 * | 谁 | 跑在哪 | 为什么 |
 * |---|---|---|
 * | [ReminderEngine] 的所有调用 | **主线程** | 引擎非线程安全；固件也是单任务驱动 |
 * | 心跳 / 回执的 HTTP | IO 单线程池 | 阻塞调用，放主线程会卡住按键响应 |
 * | HTTP 结果落到引擎 | 主线程（`handler.post`） | 回到引擎的唯一线程 |
 *
 * 所以：**任何改引擎状态的动作都必须经 [handler] 回到主线程**。
 * 这是这个类唯一容易改坏的地方 —— 在 IO 线程直接 `engine.confirmEx()`
 * 会和 tick 抢状态，症状是「偶尔确认了但屏没动」。
 */
class DeviceRuntime(
    private val context: Context,
    private val store: DeviceStore,
    private val ui: Ui,
    baseUrl: String,
    deviceId: String,
    clientId: String
) {

    /** 运行时与界面的交界（MainActivity 实现）。 */
    interface Ui {
        /** 全屏提醒；`null` = 收掉。 */
        fun showReminder(text: String?)

        /** 提示音（[ReminderEngine.Tone]）。 */
        fun tone(tone: Int)

        /** 播报一句话（在线 TTS）。 */
        fun announce(text: String)

        /** 播预录片段；返回 false 表示本设备没有片段资源，调用方回落提示音。 */
        fun playClip(clipId: Int): Boolean

        /** 切界面状态（与固件 `elder_ui_state_t` 对齐的字符串）。 */
        fun setUiState(state: String)

        /** 设置屏幕亮度（0-100）；返回是否真的生效。 */
        fun setBrightness(percent: Int): Boolean

        /** 是否已联网（决定能不能用 TTS 而不是本地提示音）。 */
        fun isOnline(): Boolean
    }

    // ------------------------------------------------------------------ 依赖

    private val handler = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor { r ->
        Thread(r, "elder-io").apply { isDaemon = true }
    }

    private val engine = ReminderEngine(store.reminderStore, EngineListener(), Clock.SYSTEM)

    private val api = ElderApi(baseUrl, deviceId, clientId) { store.token }

    /**
     * 回执队列 —— 固件 `elder_sync` 的 `evtq`（落盘、32 条、FIFO、满丢最旧）。
     *
     * ⚠️ **必须落盘**。只放内存的后果不只是"这一次没记上"，而是一条精确的
     * 假告警链：断网期间确认 2 次 → 进程被杀 → 2 条 CONFIRMED 丢失 →
     * 服务端 30 分钟后把 PENDING 判成 MISSED → 家属看到 2 条「老人漏服药」。
     * 这**正是**回执机制本身要消灭的东西，不落盘等于白做。
     */
    private val eventQueue = ArrayDeque<PendingEvent>()

    /** 队列 → 文本（tab 分隔 + 字符串 Base64，避免分隔符冲突）。 */
    private fun encodeQueue(): String = eventQueue.joinToString("\n") { e ->
        listOf(
            e.reminderId.toString(), e.dueAt.toString(),
            e.state.toString(), e.method.toString(),
            b64(e.medicine), b64(e.note)
        ).joinToString("\t")
    }

    /** 启动时恢复队列（进程重启后继续补发）。 */
    private fun loadQueue() {
        val text = store.eventQueueText
        if (text.isEmpty()) return
        for (line in text.split('\n')) {
            val f = line.split('\t')
            if (f.size < 6) continue
            eventQueue.addLast(
                PendingEvent(
                    reminderId = f[0].toLongOrNull() ?: 0L,
                    dueAt = f[1].toLongOrNull() ?: 0L,
                    state = f[2].toIntOrNull() ?: 0,
                    method = f[3].toIntOrNull() ?: 0,
                    medicine = unb64(f[4]),
                    note = unb64(f[5])
                )
            )
        }
        if (eventQueue.isNotEmpty()) Log.i(TAG, "恢复待发回执 ${eventQueue.size} 条")
    }

    private fun saveQueue() {
        store.eventQueueText = encodeQueue()
    }

    private fun b64(s: String): String =
        java.util.Base64.getEncoder().encodeToString(s.toByteArray(Charsets.UTF_8))

    private fun unb64(s: String): String = runCatching {
        String(java.util.Base64.getDecoder().decode(s), Charsets.UTF_8)
    }.getOrDefault("")

    private var heartbeatRunning = false
    private var syncIntervalSec = ElderApi.DEFAULT_SYNC_INTERVAL_SEC

    /** 服务端让"立刻同步"时置位，把等待中的心跳提前叫醒。 */
    private var syncNowRequested = false

    // ------------------------------------------------------------------ 生命周期

    /** 启动：读本地表 → 起 tick 循环 → 立刻排一次心跳。 */
    fun start() {
        handler.post {
            engine.start()
            loadQueue()
            // 有本地表就先把屏点亮 —— 断网时这就是唯一的信息来源
            refreshUiFromEngine()
        }
        scheduleTick()
        scheduleHeartbeat(0L)
    }

    fun stop() {
        heartbeatRunning = false
        handler.removeCallbacksAndMessages(null)
        engine.flush()
    }

    /** 每秒 tick 一次（与固件 `elder_reminder_tick_once` 的节奏一致）。 */
    private fun scheduleTick() {
        handler.postDelayed({
            runCatching { engine.tick() }
                .onFailure { Log.e(TAG, "tick 失败: ${it.message}", it) }
            scheduleTick()
        }, 1_000L)
    }

    // ------------------------------------------------------------------ 心跳

    /**
     * 心跳循环。
     *
     * 节奏：成功 ⇒ `sync_interval_sec`；失败 ⇒ 指数退避（上限 60 秒）；
     * 未绑定 ⇒ 最多 15 秒再看一眼（固件 `sync.c:1844`）。
     */
    private fun scheduleHeartbeat(delayMs: Long) {
        handler.postDelayed({ doHeartbeat() }, delayMs)
    }

    private fun doHeartbeat() {
        if (heartbeatRunning) return
        heartbeatRunning = true
        syncNowRequested = false

        io.execute {
            val res = runCatching {
                api.sync(FW_VERSION, HW_MODEL, null)
            }.getOrElse {
                Log.w(TAG, "心跳异常: ${it.message}")
                null
            }

            handler.post {
                heartbeatRunning = false

                if (res == null || !res.ok) {
                    backoffMs = if (backoffMs == 0L) 5_000L else minOf(backoffMs * 2, 60_000L)
                    Log.w(TAG, "心跳失败，${backoffMs / 1000}s 后重试：${res?.message ?: "网络不可用"}")
                    scheduleHeartbeat(backoffMs)
                    return@post
                }
                backoffMs = 0L

                applySyncResult(res)

                // 立刻同步的请求若在本轮开始后才到，再来一次
                val wait = when {
                    syncNowRequested -> 1_000L
                    !store.hasToken -> ElderApi.UNBOUND_RETRY_SEC * 1000L
                    else -> res.intervalSec * 1000L
                }
                scheduleHeartbeat(wait)
            }
        }
    }

    private var backoffMs = 0L

    /** 落地一次心跳结果。**顺序即契约**（时区 → 校时 → 提醒表 → ws → 换 WiFi）。 */
    private fun applySyncResult(res: ElderApi.SyncResult) {
        // 1. 时区
        if (res.tzOffsetMin != null) store.tzOffsetMin = res.tzOffsetMin

        // 2. 校时（偏移层，不改系统时钟）
        if (res.serverTimeSec != null) {
            val offset = res.serverTimeSec * 1000L - System.currentTimeMillis()
            // 只在偏差超过 2 秒时才写，避免每次心跳都动 prefs
            if (Math.abs(offset - store.clockOffsetMs) > 2_000L) {
                Log.i(TAG, "校时：本地偏差 ${offset / 1000}s")
                store.clockOffsetMs = offset
            }
        }

        // 3. 提醒表（null = 服务端没给/残缺 ⇒ 一个字都不动）
        val rmd = res.reminders
        if (rmd != null) {
            engine.replaceAll(rmd)
            Log.i(TAG, "提醒表已更新：${rmd.size} 条")
        }

        // 4. 心跳周期
        syncIntervalSec = res.intervalSec

        // 5. ws 地址
        if (res.wsUrl != null && res.wsUrl != store.wsUrl) {
            store.wsUrl = res.wsUrl
            Log.i(TAG, "ws 地址更新：${res.wsUrl}")
        }

        refreshUiFromEngine()
        flushEventQueue()
    }

    // ------------------------------------------------------------------ 回执

    private data class PendingEvent(
        val reminderId: Long,
        val dueAt: Long,
        val state: Int,
        val method: Int,
        val medicine: String,
        val note: String
    )

    /** 入队（对应固件 `elder_sync_enqueue_event`）。在线时立刻尝试发送。 */
    private fun enqueueEvent(
        reminderId: Long,
        dueAt: Long,
        state: Int,
        method: Int,
        medicine: String = "",
        note: String = ""
    ) {
        // 本地自建条目（serverId=0）**不上报**：服务端没有对应规则，
        // 补建出来的实例会挂在一个不存在的提醒下，家属看到幽灵记录。
        if (reminderId <= 0L) return

        if (eventQueue.size >= RmdConst.QUEUE_MAX) {
            // 满了丢**最旧**的一条（固件同样策略）：最新的回执更能反映现状
            eventQueue.removeFirst()
            Log.w(TAG, "回执队列满，丢弃最旧一条")
        }
        eventQueue.addLast(PendingEvent(reminderId, dueAt, state, method, medicine, note))
        saveQueue()
        flushEventQueue()
    }

    private var flushing = false

    /** 严格 FIFO 发送；失败即停（幂等：服务端按 (reminder_id, due_at) 定位）。 */
    private fun flushEventQueue() {
        if (flushing || eventQueue.isEmpty()) return
        flushing = true
        val batch = ArrayList<PendingEvent>(eventQueue)
        val tz = store.tzOffsetMin

        io.execute {
            var sent = 0
            for (e in batch) {
                val ok = runCatching {
                    api.reportEvent(e.reminderId, e.dueAt, e.state, e.method, tz, e.medicine, e.note)
                }.getOrDefault(false)
                if (!ok) break
                sent++
            }
            val done = sent
            handler.post {
                repeat(done) { if (eventQueue.isNotEmpty()) eventQueue.removeFirst() }
                if (done > 0) saveQueue()
                flushing = false
                if (done < batch.size) {
                    Log.w(TAG, "回执发送中断：成功 $done/${batch.size}，稍后重试")
                }
            }
        }
    }

    // ------------------------------------------------------------------ 引擎回调

    /** 引擎事件 → 界面。 */
    private inner class EngineListener : ReminderEngine.Listener {
        override fun onStateChanged(idx: Int, old: RmdState, now: RmdState, method: Int) {
            val item = engine.table.items.getOrNull(idx) ?: return
            val due = item.dueOf(System.currentTimeMillis() / 1000L, store.tzOffsetMin)

            // 状态 → 上报事件（对齐固件 `elder_sync_on_reminder_state`）
            when (now) {
                RmdState.PENDING -> {
                    enqueueEvent(item.serverId, due, EventState.FIRED, ConfirmMethod.NONE)
                    ui.setUiState("reminder")
                    // 交互上报：提醒确实触发了
                    reportInteractionAsync(InteractionKind.REMINDER_CONFIRM)
                }
                RmdState.DONE -> {
                    enqueueEvent(item.serverId, due, EventState.CONFIRMED, method)
                    ui.setUiState("confirmed")
                }
                RmdState.ESCALATED -> {
                    ui.setUiState("missed")
                }
                RmdState.SKIPPED -> {
                    // 注意：SKIPPED 报 note="stale"，让它区别于"漏服"
                    enqueueEvent(item.serverId, due, EventState.SKIPPED, ConfirmMethod.NONE, note = "stale")
                }
                RmdState.HALF_EXPIRED -> {
                    // ⚠️ **什么都不发**：发了会把服务端刚收到的「语音确认」
                    //    覆盖成「跳过」，且理由写成"设备离线错过"，与事实不符。
                }
                else -> Unit
            }
        }

        override fun onReminderText(text: String?) {
            ui.showReminder(text)
        }

        override fun onTone(tone: Int) {
            ui.tone(tone)
        }

        override fun onAnnounce(text: String) {
            if (ui.isOnline()) ui.announce(text) else ui.tone(ReminderEngine.Tone.POPUP)
        }

        override fun onPlayClip(clipId: Int): Boolean = ui.playClip(clipId)

        override fun onTimeUntrusted() {
            ui.showReminder("时间未校准，请连网")
            ui.setUiState("confused")
        }
    }

    private fun reportInteractionAsync(kind: Int) {
        io.execute { runCatching { api.reportInteraction(kind, 0L) } }
    }

    // ------------------------------------------------------------------ MCP

    private val mcp = McpHandler(object : McpHandler.Host {
        override fun onConfirmReminder(serverId: Long): Boolean {
            // ⚠️ 这里只改内存，**不发网络** —— 服务端兜底路径只等 3.0 秒。
            //    回执由 onStateChanged 异步入队。
            val ok = if (serverId > 0L) {
                // 命中"按键发起"标记 ⇒ 报 BUTTON（全置信），否则 VOICE。
                // ⚠️ 标记必须按 TTL 过期：老人上午按了一次键、下午随口说"吃了"，
                //    若标记永不过期，下午那次会被误升格成全置信，而日志里一切正常。
                val armed = recentButtonConfirm &&
                    System.currentTimeMillis() - recentButtonAt < RmdConst.MED_ARMED_TTL_MS
                engine.confirmServer(
                    serverId,
                    if (armed) ConfirmMethod.BUTTON else ConfirmMethod.VOICE
                )
            } else {
                engine.confirmPendingMedicine()
            }
            if (ok) refreshUiFromEngine()
            return ok
        }

        override fun onStandby() {
            ui.setUiState("idle")
            // 服务端已念完告别语，这里只需停麦、回 idle。
            // 冷却由服务端 `_dismissed_until` 负责（180 秒），设备侧不重复实现。
            onStandbyRequested?.invoke()
        }

        override fun onSyncNow() {
            syncNowRequested = true
            // 把等待中的心跳提前叫醒
            handler.removeCallbacksAndMessages(null)
            scheduleTick()
            scheduleHeartbeat(200L)
        }

        override fun onSetStatusLight(on: Boolean): Boolean {
            store.statusLightOn = on
            refreshUiFromEngine()
            return store.statusLightOn
        }

        override fun getStatusLight(): Boolean = store.statusLightOn

        override fun onSetVolume(volume: Int): Boolean = runCatching {
            val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
            am.setStreamVolume(
                AudioManager.STREAM_MUSIC,
                (volume * max / 100).coerceIn(0, max),
                0
            )
            true
        }.getOrDefault(false)

        override fun onSetBrightness(brightness: Int): Boolean {
            val ok = ui.setBrightness(brightness)
            if (ok) lastBrightness = brightness
            return ok
        }

        /**
         * 设备状态 —— 结构**必须嵌套**，与固件 `GetDeviceStatusJson()`
         * （`boards/common/board.cc`）的形状一致：
         *
         * ```
         * {"audio_speaker":{"volume":50},"screen":{"brightness":80},
         *  "battery":{"level":0,"charging":false}}
         * ```
         *
         * ⚠️ 扁平的 `{"volume":50,"battery":0}` 不会报错，但服务端按
         * `d["audio_speaker"]["volume"]` 取值会**四个字段全取不到** ——
         * 模型于是认为"问不到设备状态"，只会含糊其辞地绕过去。
         */
        override fun deviceStatusJson(): String = JSONObject().apply {
            put("audio_speaker", JSONObject().put("volume", currentVolumePercent()))
            put("screen", JSONObject().put("brightness", lastBrightness))
            // ⚠️ 只有"真的有电池"才输出这一段。
            //    LX04 是插电音箱，没有电池；BatteryManager 在无电池设备上会给
            //    0 或 100，两种都会被模型念成事实（"电量是零，快充电"）。
            //    报一个错的电量比不报更糟 —— 不报时模型只会说"我查不到"。
            if (batteryLevel in 1..100 && hasBattery) {
                put(
                    "battery",
                    JSONObject().put("level", batteryLevel).put("charging", batteryCharging)
                )
            }
        }.toString()
    })

    /**
     * 处理一条 `type=mcp` 的 payload，返回要回给服务端的 JSON（`null` = 不回）。
     *
     * **必须在主线程调用**（改引擎状态）。
     */
    fun handleMcp(payload: JSONObject?): String? = mcp.handle(payload)

    // ------------------------------------------------------------------ 按键

    /** 最近一次设置的亮度（0-100）与电量，供 get_device_status 上报。 */
    private var lastBrightness = 80
    private var batteryLevel = 100
    private var batteryCharging = false

    /** 设备是否真的有电池（由 Activity 从 `ACTION_BATTERY_CHANGED` 的 present 标志判定）。 */
    private var hasBattery = false

    /** 外部（Activity 的电池广播）把电量喂进来。 */
    fun updateBattery(level: Int, charging: Boolean, present: Boolean) {
        batteryLevel = level
        batteryCharging = charging
        hasBattery = present
    }

    /** 最近一次按键确认是否落到了服药类（用于把 VOICE 升级成全置信 BUTTON）。 */
    private var recentButtonConfirm = false
    private var recentButtonAt = 0L

    /** 有待确认项吗（按键裁决要用：确认机 vs PTT 机）。 */
    fun hasPendingConfirm(): Boolean = engine.hasPendingConfirm()

    /** 正在响的那条（没有则 `null`）。 */
    fun firstRingingItem(): ReminderItem? {
        val idx = engine.firstRinging()
        return if (idx >= 0) engine.table.items.getOrNull(idx) else null
    }

    /**
     * 长按满 3 秒 —— 一步式确认（**不等松手**）。
     *
     * @return `true` = 已受理（灯应给天蓝回执）；`false` = 没有可确认项
     */
    fun onHoldConfirm(): Boolean {
        val idx = engine.firstRinging()
        if (idx < 0) return false
        val item = engine.table.items[idx]

        val ok = engine.confirmByButton()
        if (!ok) return false

        if (item.isMedicine) {
            // 服药类：置了"按键发起"标记，接下来要追问哪一味药。
            recentButtonConfirm = true
            recentButtonAt = System.currentTimeMillis()
        }
        refreshUiFromEngine()
        return true
    }

    /** 短按（<3 秒松手）。日程类直接确认；服药类什么都不做（防误触）。 */
    fun onShortPress(): Boolean {
        val item = firstRingingItem() ?: return false
        if (item.isMedicine) return false   // 服药类禁止短按确认
        val ok = engine.confirmByButton()
        if (ok) refreshUiFromEngine()
        return ok
    }

    // ------------------------------------------------------------------ 其它

    private fun currentVolumePercent(): Int = runCatching {
        val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        if (max <= 0) 0 else am.getStreamVolume(AudioManager.STREAM_MUSIC) * 100 / max
    }.getOrDefault(0)

    /** 把引擎的当前状态刷到界面（灯/屏）。 */
    fun refreshUiFromEngine() {
        val text = engine.currentReminderText()
        ui.showReminder(text)
        val pending = engine.hasPendingConfirm()
        onPendingChanged?.invoke(pending)
    }

    /** 有待确认项变化时通知界面（灯效的 buttonPending）。 */
    var onPendingChanged: ((Boolean) -> Unit)? = null

    /** 待机请求（由服务端 `self.elder.standby` 触发）。 */
    var onStandbyRequested: (() -> Unit)? = null

    /** 主动触发一次同步（供 UI/调试用）。 */
    fun requestSyncNow() {
        syncNowRequested = true
        handler.post { handler.removeCallbacksAndMessages(null); scheduleTick(); scheduleHeartbeat(200L) }
    }

    private companion object {
        const val TAG = "ElderRuntime"

        /** 固件版本号：心跳上报用，管理后台设备表的「固件版本」列读它。 */
        const val FW_VERSION = "lx04-1.0.0"

        /** 硬件型号：管理后台「硬件型号」列读它。 */
        const val HW_MODEL = "LX04"
    }
}
