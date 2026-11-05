package com.elder.lx04.net

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject

/**
 * 设备端 MCP 工具分派 —— `firmware/xiaozhi/main/mcp_server.cc` 的移植。
 *
 * ## 这条通道为什么必须有
 *
 * 服务端把「设备能做的事」建模成 MCP 工具，跑的是标准 JSON-RPC 2.0：
 *
 * ```
 * 收：{"type":"mcp","payload":{"jsonrpc":"2.0","id":7,"method":"tools/call",
 *                              "params":{"name":"self.elder.confirm_reminder",
 *                                        "arguments":{"server_id":11}}}}
 * 回：{"jsonrpc":"2.0","id":7,"result":true}
 * ```
 *
 * 之前 Android 侧收到 mcp 是**直接丢弃**（`VoiceClient.kt` 写着"本客户端不实现，
 * 不回包"）。后果：
 *
 * - 老人说「我吃过了」→ 服务端调 `confirm_reminder` → 3 秒超时 → 服务端只能
 *   回一句「请让老人按一下设备上的按键确认」——而老人**刚刚才说过**
 * - 家属在 APP 改了提醒 → 服务端调 `sync_now` → 设备不动 → 要等 5 分钟心跳
 * - 服务端让设备待机 → 设备不理 → 麦克风一直开着
 *
 * ## ⚠️ 3 秒硬约束（不是"尽量快"）
 *
 * 服务端读超时分三档（见 `server/app/voice/session.py`）：
 * `confirm_reminder` 走 LLM 路径 8 秒、走**兜底路径只有 3.0 秒**、
 * `standby` 3 秒、其余 2 秒。而老人「只吃一种药、到点说吃了」走的正是
 * 兜底路径 —— 它是最常见的一条。
 *
 * 所以本类的方法**只做内存操作**（改状态、置标志），**绝不在里面发网络请求**。
 * 回执上报由调用方异步入队（见 `ReminderBridge`）。一旦在这里同步发 HTTP，
 * 一次网络抖动就会让老人听到「请按一下设备上的按键」。
 *
 * ## id 必须是整数并原样带回
 *
 * 服务端按 `int` 建索引等回包（`session.py` 里写明：固件对 id 做
 * `cJSON_IsNumber` 校验，字符串 id 会打一条 "Invalid id" 直接 return，
 * 服务端只看到超时 —— 这类失败在日志里长得像"设备没响应"，极难查）。
 */
class McpHandler(private val host: Host) {

    /** 设备侧能力（由 MainActivity 实现；保持本类不依赖 Android）。 */
    interface Host {
        /**
         * 确认一条提醒。
         *
         * @param serverId 后端 `reminders.id`；0 = "确认当前正在响的那条"
         * @return 设备是否真的确认了（`false` 不是错误：老人可能只是顺口说了一句）
         */
        fun onConfirmReminder(serverId: Long): Boolean

        /** 待机（停麦、回到等唤醒词的状态）。 */
        fun onStandby()

        /** 立刻同步一次（叫醒心跳，秒级生效）。 */
        fun onSyncNow()

        /**
         * 设置/查询**状态灯**。
         *
         * ⚠️ 语义必须保住：状态灯关了，**事件灯仍然会亮**（提醒/确认/失败照旧）。
         * 固件的工具描述对老人承诺了这句话，实现时把两个概念混成一个，
         * 模型就会对老人说谎。
         *
         * @return 改完之后**真实**的值（不是请求值 —— 设备可能拒绝）
         */
        fun onSetStatusLight(on: Boolean): Boolean

        fun getStatusLight(): Boolean

        /** 设置音量（0-100）。 */
        fun onSetVolume(volume: Int): Boolean

        /** 设置屏幕亮度（0-100）。 */
        fun onSetBrightness(brightness: Int): Boolean

        /** 设备状态 JSON（电量/音量/网络等），给 `self.get_device_status` 用。 */
        fun deviceStatusJson(): String
    }

    /**
     * 处理一条 `type=mcp` 的 payload。
     *
     * @return 要回给服务端的 JSON 字符串；`null` = 这条不需要回
     *         （通知类消息）。
     */
    fun handle(payload: JSONObject?): String? {
        if (payload == null) return null

        val method = payload.optString("method", "")

        // 只有 tools/call 需要处理。initialize / tools/list 是 MCP client
        // 的握手方法，服务端（作为 client）**不会**发给我们 —— 我们是对端
        // 的"被调方"，只需响应 tools/call。真收到了也给个明确的 error，
        // 比静默不回更容易排查。
        if (method != "tools/call") {
            Log.i(TAG, "收到非 tools/call 的 MCP 方法：$method（忽略）")
            return null
        }

        // ⚠️ id 必须是整数。`optInt` 对 "7" 这类字符串也会给出 7，
        //    但回包时必须写成数字 7 —— 用 JSONObject.put(String, int)。
        if (!payload.has("id")) {
            Log.w(TAG, "MCP 请求没有 id，无法回包")
            return null
        }
        val id = payload.optInt("id", -1)
        if (id < 0) {
            Log.w(TAG, "MCP 请求 id 非法：${payload.opt("id")}")
            return null
        }

        val params = payload.optJSONObject("params")
        val name = params?.optString("name", "").orEmpty()
        val args = params?.optJSONObject("arguments") ?: JSONObject()

        if (name.isEmpty()) return error(id, "缺少工具名")

        return try {
            when (name) {
                // ---- elder 专属 5 个 ----
                "self.elder.confirm_reminder" -> {
                    // 负数/类型不对一律当 0（= 确认当前正在响的那条）：
                    // 固件同样是"取不到或取到怪值就当 0"，走旧路径最安全。
                    var sid = args.optLong("server_id", 0L)
                    if (sid < 0L) sid = 0L
                    ok(id, host.onConfirmReminder(sid).toString())
                }

                "self.elder.standby" -> {
                    host.onStandby()
                    ok(id, "true")
                }

                "self.elder.sync_now" -> {
                    host.onSyncNow()
                    ok(id, "true")
                }

                "self.elder.set_status_light" -> {
                    // `on` 是必需参数。缺了不能猜 true/false ——
                    // 猜错会出现"家属说关灯，灯反而亮着"。
                    if (!args.has("on")) return error(id, "缺少参数 on")
                    val want = args.optBoolean("on", false)
                    val actual = host.onSetStatusLight(want)
                    ok(id, JSONObject().put("on", actual).toString())
                }

                "self.elder.get_status_light" ->
                    ok(id, JSONObject().put("on", host.getStatusLight()).toString())

                // ---- 上游通用工具（服务端实际会调的那几个）----
                "self.get_device_status" -> ok(id, host.deviceStatusJson())

                "self.audio_speaker.set_volume" -> {
                    if (!args.has("volume")) return error(id, "缺少参数 volume")
                    ok(id, host.onSetVolume(args.optInt("volume", 50).coerceIn(0, 100)).toString())
                }

                "self.screen.set_brightness" -> {
                    if (!args.has("brightness")) return error(id, "缺少参数 brightness")
                    ok(id, host.onSetBrightness(args.optInt("brightness", 50).coerceIn(0, 100)).toString())
                }

                // ---- 认识但不实现：明确回 error，别让服务端干等 ----
                "self.screen.set_theme",
                "self.screen.get_info",
                "self.screen.preview_image",
                "self.assets.set_download_url" ->
                    error(id, "本设备不支持 $name")

                else -> {
                    Log.w(TAG, "未知 MCP 工具：$name")
                    error(id, "未知工具 $name")
                }
            }
        } catch (e: Exception) {
            // 任何异常都要回 error：不回 = 服务端等到超时，
            // 而超时和"设备拒绝"在服务端日志里长得一样。
            Log.e(TAG, "处理 $name 失败: ${e.message}", e)
            error(id, e.message ?: "内部错误")
        }
    }

    /**
     * 成功回包 —— **必须带 MCP 标准信封**。
     *
     * ```
     * {"jsonrpc":"2.0","id":7,
     *  "result":{"content":[{"type":"text","text":"true"}],"isError":false}}
     * ```
     *
     * ⚠️ 这里踩过一次：一开始把返回值裸着塞进 `result`（`"result":true`），
     * 看起来"和固件一样"，实际固件每个工具的回调都自己包了
     * `content/isError` 信封（`mcp_server.cc` 的 `ReplyResult` 调用点）。
     * 裸值的后果不是报错，而是**服务端把 8 个工具全判失败** ——
     * 因为它在 `result["content"][0]["text"]` 上取值，取不到就是"失败"。
     * 而失败的表现是"老人说吃过了，设备回没确认"，极难往格式上想。
     *
     * 值的编码约定：布尔/数字一律转字符串（`"true"` / `"50"`）；
     * 结构化结果转 JSON 字符串（`"{\"on\":true}"`）。与固件一致。
     */
    private fun ok(id: Int, text: String): String =
        JSONObject().put("jsonrpc", "2.0").put("id", id)
            .put(
                "result",
                JSONObject()
                    .put(
                        "content",
                        JSONArray().put(
                            JSONObject().put("type", "text").put("text", text)
                        )
                    )
                    .put("isError", false)
            )
            .toString()

    /** 失败回包。 */
    private fun error(id: Int, message: String): String =
        JSONObject()
            .put("jsonrpc", "2.0")
            .put("id", id)
            .put("error", JSONObject().put("code", -32603).put("message", message))
            .toString()

    companion object {
        private const val TAG = "ElderMcp"
    }
}
