package com.elder.lx04.net

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 语音 WebSocket 客户端 —— 协议 v3。
 *
 * ## 协议事实（逐条对着服务端代码核过，不是照文档猜的）
 *
 * **握手**（`server/app/voice/session.py:1441`）
 * ```
 * Authorization: Bearer <OTA 拿到的 JWT>
 * Protocol-Version: 3        ← 决定二进制帧头格式，缺省按 v1（无头）
 * Device-Id: <MAC>           ← 带了就必须与 token 里的一致
 * Client-Id: <uuid>
 * ```
 *
 * **服务端先发 hello**（session.py:1355），设备不需要发：
 * ```json
 * {"type":"hello","transport":"websocket","session_id":"...",
 *  "audio_params":{"sample_rate":24000,"frame_duration":60}}
 * ```
 * ⚠️ 采样率**必须读这里**，不能写死 24k —— 服务端可以用环境变量改。
 *
 * **设备 → 服务端**（只有三种）
 * ```
 * {"type":"listen","state":"start","mode":"auto"}   ← 开始一轮聆听
 * {"type":"listen","state":"stop"}                  ← 说完了（触发 ASR+LLM）
 * {"type":"listen","state":"detect","text":"小智"}   ← 唤醒词通知（只记日志，不进 LLM）
 * {"type":"abort"}                                   ← 打断播报
 * ```
 * ⚠️ **没有"发文本当用户说话"的通道** —— `detect` 里的 text 只被 log 打印。
 * 所以上行音频是必需的，不是可选项。
 *
 * **服务端 → 设备**
 * ```
 * {"type":"stt","text":"..."}                              ← ASR 结果
 * {"type":"tts","state":"start"}                           ← 开始播报
 * {"type":"tts","state":"sentence_start","text":"..."}     ← 字幕（一句话一条）
 * {"type":"tts","state":"stop"}                            ← 播报结束 → 回落 idle
 * {"type":"llm","emotion":"happy"}                         ← 表情（21 个词之一）
 * {"type":"alert","status":"info","message":"..."}
 * {"type":"mcp","payload":{...}}                           ← 设备工具调用
 * ```
 * **字幕就是 `tts` + `state=="sentence_start"` 的 `text`。**
 *
 * **二进制帧**（Protocol-Version 3，4 字节头）
 * ```
 * [0] type     0 = Opus 音频, 1 = JSON
 * [1] reserved
 * [2..3] payload_size（大端）
 * [4..]  payload
 * ```
 *
 * ## 关于 mcp（设备工具）
 *
 * 服务端会在会话建立时调 `self.elder.sync_now`（tools.py:1013），等设备回包。
 * 我们**不实现设备本地提醒**，所以选择**不回包**：服务端 2 秒后超时，
 * 走它自己的降级分支。
 *
 * 为什么不回一个"成功"：那等于告诉服务端"设备已经同步好提醒了"，
 * 而实际上我们什么都没做 —— 老人到点不会响。宁可让它超时，
 * 也不能回一个假成功。这是这个项目里反复出现的一类坑（灯在骗人、
 * 回执在骗人），不能再犯。
 */
class VoiceClient(
    private val listener: Listener
) {

    /** 上层要实现的回调。**注意：都在 OkHttp 的线程上回调，不是主线程。** */
    interface Listener {
        fun onConnected()
        fun onDisconnected(reason: String)

        /** 收到 hello，`sampleRate` 是下行音频采样率。 */
        fun onHello(sampleRate: Int, frameMs: Int)

        /** ASR 识别出用户说了什么。 */
        fun onStt(text: String)

        /** TTS 状态变化；`text` 只在 `sentence_start` 时有值（字幕）。 */
        fun onTtsState(state: String, text: String?)

        /** 表情：happy / thinking / sad ...（21 个词之一）。 */
        fun onEmotion(emotion: String)

        /** 服务端主动提示。 */
        fun onAlert(message: String)

        /** 收到一帧 Opus 音频（已剥掉 4 字节帧头）。 */
        fun onAudio(opus: ByteArray)

        /**
         * 服务端调设备侧 MCP 工具（`{"type":"mcp","payload":{...}}`）。
         *
         * 返回**要回给服务端的 JSON**；`null` = 这条不需要回（通知类）。
         *
         * ## ⚠️ 必须在 3 秒内返回
         *
         * 服务端读超时分三档：`confirm_reminder` 走 LLM 路径 8s、走**兜底路径
         * 只有 3.0s**、`standby` 3s、其余 2s。而老人「只吃一种药、到点说吃了」
         * 走的正是兜底路径。所以实现里**只做内存操作，绝不能发网络** ——
         * 一次网络抖动就会让老人听到「请让老人按一下设备上的按键」，
         * 而他刚刚才说过。
         *
         * 回执上报由监听方异步入队，不在这条链路上。
         */
        fun onMcp(payload: org.json.JSONObject?): String?
    }

    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        // 读超时给 0（不超时）：服务端在一轮对话之间可能长时间不发东西，
        // 用默认 10 秒会频繁误判断线。真正的断线靠 ping 和 onFailure 发现。
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    @Volatile
    private var socket: WebSocket? = null

    @Volatile
    var isConnected: Boolean = false
        private set

    /** 下行采样率，hello 到达后更新。 */
    @Volatile
    var sampleRate: Int = 24_000
        private set

    @Volatile
    var sessionId: String = ""
        private set

    /** 建立连接。返回 false 表示连请求都没发出去。 */
    fun connect(wsUrl: String, token: String, deviceId: String, clientId: String): Boolean {
        val req = Request.Builder()
            .url(wsUrl)
            .header("Authorization", "Bearer $token")
            .header("Protocol-Version", "3")
            .header("Device-Id", deviceId)
            .header("Client-Id", clientId)
            .build()

        socket = http.newWebSocket(req, object : WebSocketListener() {

            override fun onOpen(webSocket: WebSocket, response: Response) {
                isConnected = true
                Log.i(TAG, "WS 已连接: $wsUrl")
                listener.onConnected()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleJson(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: okio.ByteString) {
                handleBinary(bytes.toByteArray())
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                isConnected = false
                val why = "WS 断开: ${t.message} (HTTP ${response?.code ?: "-"})"
                Log.w(TAG, why, t)
                listener.onDisconnected(why)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                isConnected = false
                val why = when (code) {
                    4401 -> "鉴权失败（token 无效或已过期，需要重新 OTA）"
                    4001 -> "设备与 token 不匹配"
                    else -> "连接关闭 code=$code reason=$reason"
                }
                Log.w(TAG, why)
                listener.onDisconnected(why)
            }
        })
        return true
    }

    // ---------------------------------------------------------------- 发送

    /** 发一帧 Opus 音频。 */
    fun sendAudio(opus: ByteArray) {
        val ws = socket ?: return
        if (!isConnected) return
        ws.send(okio.ByteString.of(*frame(opus, TYPE_OPUS)))
    }

    /** 开始一轮聆听（服务端进入 listening 并起静音看门狗）。 */
    fun listenStart() = sendJson("""{"type":"listen","state":"start","mode":"auto"}""")

    /** 说完了 —— 服务端据此触发 ASR → LLM → TTS。 */
    fun listenStop() = sendJson("""{"type":"listen","state":"stop"}""")

    /**
     * 唤醒词通知。
     *
     * @param word  唤醒词文本，只用于服务端日志。
     * @param greet 是否要服务端播唤醒应答。
     *
     * ⚠️ **按键唤醒必须传 `greet = false`**（与服务端固件同一套语义）。
     *
     * 服务端 `_on_listen` 的 detect 分支里：
     * ```
     * no_greet = obj.get("greet") is False
     * ```
     * 缺省必须**照旧应答**（缺省 ≠ 不要），所以要"不应答"就必须显式带
     * `"greet": false`。判据用 `is False` 而不是 `not obj.get("greet")`。
     *
     * 为什么按键唤醒不应答：老人按下键就是要说话，服务端若回一句
     * 「你好，要聊点什么？」，设备会转成 Speaking、上行音频被丢弃，
     * 他刚开口的字就没了 —— 正是现场反馈里的「前面两个字没识别到」。
     *
     * @see server/app/voice/session.py `_on_listen` 的 `state == "detect"` 分支
     * @see firmware/xiaozhi/main/application.cc:1007-1013（固件的同一处理）
     */
    fun listenDetect(word: String, greet: Boolean = true) {
        // ⚠️ 手拼 JSON 有引号注入风险（word 将来可能带引号），
        //    这里用 JSONObject 拼，字段顺序无所谓。
        val obj = JSONObject()
        obj.put("type", "listen")
        obj.put("state", "detect")
        obj.put("text", word)
        if (!greet) obj.put("greet", false)
        sendJson(obj.toString())
    }

    /** 打断当前播报。 */
    fun abort() = sendJson("""{"type":"abort"}""")

    /**
     * 设备主动播报一句话。
     *
     * 信封格式**必须**是 `{"type":"mcp","payload":{"type":"tts","text":...}}` ——
     * 服务端 `session.py` 的 `_on_device_announce` 只认这一种（2026-10-22 专门
     * 修过「屏变了但不出声」）。自己发明一个 `{"type":"tts_request"}` 之类的
     * 格式不会报错，只会**静默没有声音**。
     */
    fun sendAnnounce(text: String) {
        if (text.isEmpty()) return
        val obj = org.json.JSONObject().apply {
            put("type", "mcp")
            put("payload", org.json.JSONObject().apply {
                put("type", "tts")
                put("text", text)
            })
        }
        sendJson(obj.toString())
    }

    private fun sendJson(json: String) {
        val ws = socket ?: return
        if (!isConnected) return
        ws.send(json)
    }

    fun disconnect() {
        isConnected = false
        runCatching { socket?.close(1000, "bye") }
        socket = null
    }

    // ---------------------------------------------------------------- 接收

    /**
     * 诊断用：非空时把**每一个下行二进制帧的原始字节**追加落盘。
     *
     * 只在 `dump_start` 调试命令后打开，默认 null 不影响生产。
     * 用途：把「服务端发的字节」和「设备解出的 PCM」分开，定位
     * 播报变成高频擦擦声时究竟是哪一段把数据弄坏了。
     */
    @Volatile
    var dumpFile: java.io.File? = null

    private fun handleBinary(bytes: ByteArray) {
        dumpFile?.let { f ->
            runCatching {
                java.io.FileOutputStream(f, true).use { it.write(bytes) }
            }
        }
        if (bytes.size < 4) {
            Log.w(TAG, "二进制帧太短: ${bytes.size}")
            return
        }
        val type = bytes[0].toInt() and 0xFF
        val size = ((bytes[2].toInt() and 0xFF) shl 8) or (bytes[3].toInt() and 0xFF)
        val end = minOf(4 + size, bytes.size)
        val payload = bytes.copyOfRange(4, end)

        when (type) {
            TYPE_OPUS -> listener.onAudio(payload)
            TYPE_JSON -> handleJson(String(payload, Charsets.UTF_8))
            else -> Log.w(TAG, "未知二进制帧类型: $type")
        }
    }

    private fun handleJson(text: String) {
        val obj = runCatching { JSONObject(text) }.getOrElse {
            Log.w(TAG, "无法解析的 JSON: ${text.take(160)}")
            return
        }
        when (obj.optString("type")) {
            "hello" -> {
                sessionId = obj.optString("session_id", "")
                val ap = obj.optJSONObject("audio_params")
                sampleRate = ap?.optInt("sample_rate", 24_000) ?: 24_000
                val frameMs = ap?.optInt("frame_duration", 60) ?: 60
                Log.i(TAG, "hello: session=$sessionId 下行=${sampleRate}Hz/${frameMs}ms")
                listener.onHello(sampleRate, frameMs)
            }

            "stt" -> listener.onStt(obj.optString("text", ""))

            "tts" -> {
                val state = obj.optString("state", "")
                val sub = obj.optString("text", "").takeIf { it.isNotEmpty() }
                listener.onTtsState(state, sub)
            }

            "llm" -> listener.onEmotion(obj.optString("emotion", "neutral"))

            "alert" -> listener.onAlert(obj.optString("message", ""))

            "mcp" -> {
                // 设备是 MCP server（服务端是 client）：收到 tools/call 必须回包。
                //
                // ⚠️ 这里是**唯一**的分派入口：如果本类"顺手"处理一部分工具，
                //    就会和 McpHandler 形成两处分派，症状是某些工具偶发回两个包
                //    （服务端按 id 建索引，第二个会覆盖第一个）或干脆不回。
                val payload = obj.optJSONObject("payload")
                val reply = listener.onMcp(payload)
                if (reply != null) {
                    sendJson(reply)
                } else {
                    Log.w(TAG, "MCP 请求没有产生回包（可能是通知类消息）")
                }
            }

            else -> Log.d(TAG, "忽略未知消息: ${text.take(120)}")
        }
    }

    private fun frame(payload: ByteArray, type: Int): ByteArray {
        val out = ByteArray(4 + payload.size)
        out[0] = type.toByte()
        out[1] = 0
        out[2] = ((payload.size shr 8) and 0xFF).toByte()
        out[3] = (payload.size and 0xFF).toByte()
        System.arraycopy(payload, 0, out, 4, payload.size)
        return out
    }

    private companion object {
        const val TAG = "ElderVoice"
        const val TYPE_OPUS = 0
        const val TYPE_JSON = 1
    }
}
