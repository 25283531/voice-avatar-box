package com.elder.lx04.net

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * OTA 握手 —— 拿 WebSocket 地址和 token。
 *
 * ## 协议事实（来自服务端代码，不是猜的）
 *
 * ```
 * GET|POST  {base}/api/voice/ota
 *   Header: Device-Id: <MAC>          ← 唯一被服务端读取的身份
 *           Client-Id: <uuid>         ← 服务端不读，只为日志
 *           Activation-Version: 1
 *
 * 已绑定 → {"server_time":{...}, "websocket":{"url":"wss://...","token":"<JWT>"}}
 * 未绑定 → {"server_time":{...}, "activation":{"code":"1234567","expires_in":600,...}}
 * ```
 *
 * 判据是**响应里有没有 `websocket` 段**，不是有没有 `activation` 段
 * （服务端已绑定响应里不带 activation）。固件也是这么判的。
 *
 * ## 未绑定时要干什么
 *
 * 不能干等 —— 要把 7 位激活码显示在屏幕上，让家属在手机 App 里输入。
 * 这是产品流程的一部分，对应固件的 `ELDER_UI_ACTIVATING` 状态（winking 表情）。
 * 所以 [Result.NeedActivation] 是一等公民，不是错误分支。
 */
class OtaClient(
    /** 服务端根地址，例如 `https://152.136.230.132`。 */
    private val baseUrl: String,
    private val deviceId: String,
    private val clientId: String
) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS)
        .build()

    /** OTA 结果。 */
    sealed class Result {
        /** 已绑定，可以连 WebSocket。 */
        data class Bound(val wsUrl: String, val token: String) : Result()

        /** 未绑定，需要家属输入激活码。 */
        data class NeedActivation(
            val code: String,
            /** 激活码失效的剩余秒数（服务端给的 expires_in）。 */
            val expiresInSec: Int
        ) : Result()

        /** 网络/服务端问题。 */
        data class Failed(val message: String) : Result()
    }

    /** 发起一次 OTA 检查。**阻塞调用**，请放在工作线程。 */
    fun check(): Result {
        val url = baseUrl.trimEnd('/') + "/api/voice/ota"
        val req = Request.Builder()
            .url(url)
            .header("Device-Id", deviceId)
            .header("Client-Id", clientId)
            .header("Activation-Version", "1")
            .header("User-Agent", "elder-lx04/0.1.0")
            .get()
            .build()

        return runCatching {
            http.newCall(req).execute().use { resp ->
                val body = resp.body?.string().orEmpty()
                if (!resp.isSuccessful) {
                    return@use Result.Failed("OTA HTTP ${resp.code}: ${body.take(200)}")
                }
                parse(body)
            }
        }.getOrElse {
            Log.w(TAG, "OTA 请求失败: ${it.message}")
            Result.Failed(it.message ?: "网络不可用")
        }
    }

    private fun parse(body: String): Result {
        val obj = runCatching { JSONObject(body) }.getOrElse {
            return Result.Failed("OTA 响应不是 JSON: ${body.take(200)}")
        }

        val ws = obj.optJSONObject("websocket")
        if (ws != null) {
            val url = ws.optString("url", "")
            val token = ws.optString("token", "")
            if (url.isNotEmpty() && token.isNotEmpty()) {
                Log.i(TAG, "已绑定，WS 地址: $url")
                return Result.Bound(url, token)
            }
        }

        val act = obj.optJSONObject("activation")
        if (act != null) {
            val code = act.optString("code", "")
            if (code.isNotEmpty()) {
                Log.i(TAG, "未绑定，激活码: $code")
                return Result.NeedActivation(code, act.optInt("expires_in", 600))
            }
        }

        return Result.Failed("OTA 响应里既没有 websocket 也没有 activation")
    }

    private companion object {
        const val TAG = "ElderOta"
    }
}
