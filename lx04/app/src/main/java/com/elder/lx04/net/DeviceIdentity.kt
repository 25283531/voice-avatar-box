package com.elder.lx04.net

import android.content.Context
import android.net.wifi.WifiManager
import android.provider.Settings
import android.util.Log
import java.net.NetworkInterface

/**
 * 设备身份 —— 服务端靠它认设备。
 *
 * ## 为什么是 MAC
 *
 * 服务端 `server/app/voice/__init__.py` 的 OTA 接口只认请求头 `Device-Id`，
 * 内部走 `norm_mac()`（去掉分隔符、转小写）。家属端 App 绑定设备时，
 * 服务端用同一个 MAC 建记录。所以这个值必须**稳定且与绑定流程一致**。
 *
 * ## 为什么不用 WifiManager.getConnectionInfo().macAddress
 *
 * Android 6 之后它固定返回 `02:00:00:00:00:00`（隐私保护），拿不到真 MAC。
 * 所以按可靠性依次尝试：
 *   1. `NetworkInterface.getByName("wlan0").hardwareAddress` —— 首选
 *   2. `/sys/class/net/wlan0/address` —— 部分 ROM 只给 sysfs
 *   3. `Settings.Secure.ANDROID_ID` —— 兜底，稳定但不是 MAC
 *
 * 第 3 种会让"家属端按 MAC 绑定"对不上，所以只在前两条都失败时用，
 * 并且日志里明确标出来 —— 这种情况需要人工介入，不能静默降级。
 */
object DeviceIdentity {

    private const val TAG = "ElderDeviceId"

    @Volatile
    private var cached: String? = null

    /** 设备标识（优先真实 MAC）。 */
    fun mac(context: Context): String {
        cached?.let { return it }

        val mac = fromNetworkInterface() ?: fromSysfs()
        val id = if (mac != null) {
            normalize(mac)
        } else {
            val fallback = Settings.Secure.getString(
                context.contentResolver,
                Settings.Secure.ANDROID_ID
            ).orEmpty()
            Log.w(TAG, "拿不到 wlan0 的 MAC，退回 ANDROID_ID=$fallback —— " +
                "这会导致家属端按 MAC 绑定对不上，需要人工确认")
            "android-$fallback"
        }
        cached = id
        Log.i(TAG, "设备标识: $id")
        return id
    }

    /** Client-Id：客户端实例标识。服务端不校验，只用于日志区分。 */
    fun clientId(context: Context): String = "lx04-${mac(context)}"

    /** 与服端 `norm_mac()` 同口径：只留字母数字，转小写。 */
    private fun normalize(raw: String): String =
        raw.filter { it.isLetterOrDigit() }.lowercase()

    private fun fromNetworkInterface(): String? = runCatching {
        val nif = NetworkInterface.getByName("wlan0") ?: return@runCatching null
        val bytes = nif.hardwareAddress ?: return@runCatching null
        if (bytes.isEmpty()) return@runCatching null
        bytes.joinToString(":") { "%02x".format(it) }
    }.getOrNull()

    private fun fromSysfs(): String? = runCatching {
        val f = java.io.File("/sys/class/net/wlan0/address")
        if (!f.canRead()) return@runCatching null
        f.readText().trim().takeIf { it.isNotEmpty() }
    }.getOrNull()

    /**
     * WiFi 是否连着（状态栏要显示）。
     *
     * 不用 `NetworkInfo` —— 它在 API 29 被废弃，而且 8.1 上
     * `getActiveNetworkInfo` 在无网络时会返回 null，判空即可。
     */
    @Suppress("DEPRECATION")
    fun wifiConnected(context: Context): Boolean = runCatching {
        val wm = context.applicationContext
            .getSystemService(Context.WIFI_SERVICE) as WifiManager
        wm.isWifiEnabled && wm.connectionInfo.networkId != -1
    }.getOrDefault(false)
}
