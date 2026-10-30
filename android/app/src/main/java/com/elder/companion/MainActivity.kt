package com.elder.companion

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.location.LocationManager
import android.net.Uri
import android.net.wifi.ScanResult
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.webkit.WebViewAssetLoader
import java.util.Locale

/**
 * 老人陪伴 & 提醒助手（家属端）Android 壳。
 *
 * 做法：CI 先把 uni-app 编译出的 H5 塞进 assets/web，App 启动后用
 * WebViewAssetLoader 把它映射成一个虚拟 https 域（https://elder.local/assets/web/index.html）
 * 交给 WebView 加载。
 *
 * 为什么不用 DCloud 云打包：uni-app 的 App 端原生运行时是 DCloud 闭源 AAR，
 * 只有云打包能产出 APK（还要账号、实名、每月配额）。本项目 H5 端功能完整，
 * 唯一用到原生能力的地方（plus.runtime.restart()）已经有 H5 降级分支，
 * 因此用一个 WebView 壳即可完整运行，且构建完全在 GitHub Actions 内完成。
 */
class MainActivity : Activity() {

    private lateinit var web: WebView
    private var filePathCallback: ValueCallback<Array<Uri>>? = null

    /**
     * WiFi 扫描结果的**缓存**，配合 [WifiBridge.scan] 的「同步返回 + 异步取结果」用。
     *
     * `wifiResults` 由主线程的广播接收器写、由 JavaBridge 线程的 `scan()` 读，
     * 所以标 `@Volatile`：不加的话读线程可能永远看到初始的空列表
     * （症状同样是「扫描一直返回 empty」）。
     */
    @Volatile
    private var wifiResults: List<ScanResult> = emptyList()

    /** 缓存写入时刻，用 `elapsedRealtime()`（不受用户改系统时间影响）；0 = 从未扫过 */
    @Volatile
    private var wifiResultsAt: Long = 0L

    private var wifiReceiver: BroadcastReceiver? = null

    // AddJavascriptInterface：桥只挂在 assetLoader 映射的 https://elder.local 上，
    // 外链一律交给系统浏览器（见 shouldOverrideUrlLoading），没有把桥暴露给外站的路径。
    @SuppressLint("SetJavaScriptEnabled", "AddJavascriptInterface")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val assetLoader = WebViewAssetLoader.Builder()
            .setDomain(APP_DOMAIN)
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()

        web = WebView(this).apply {
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                databaseEnabled = true
                javaScriptCanOpenWindowsAutomatically = true
                mediaPlaybackRequiresUserGesture = false
                cacheMode = WebSettings.LOAD_DEFAULT
                mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
                // 关闭 file 访问：页面一律走 assetLoader 的虚拟域
                allowFileAccess = false
                allowContentAccess = true
                useWideViewPort = true
                loadWithOverviewMode = false
                setSupportZoom(false)
                builtInZoomControls = false
                displayZoomControls = false
            }

            webViewClient = object : WebViewClient() {
                override fun shouldInterceptRequest(
                    view: WebView,
                    request: WebResourceRequest
                ): WebResourceResponse? = assetLoader.shouldInterceptRequest(request.url)

                override fun shouldOverrideUrlLoading(
                    view: WebView,
                    request: WebResourceRequest
                ): Boolean {
                    val url = request.url
                    // 虚拟域内的导航交给 WebView（assetLoader 会拦截）
                    if (url.host == APP_DOMAIN) return false
                    // 外链交给系统浏览器
                    return try {
                        startActivity(Intent(Intent.ACTION_VIEW, url))
                        true
                    } catch (e: Exception) {
                        Log.w(TAG, "无法打开外链: $url", e)
                        true
                    }
                }
            }

            webChromeClient = object : WebChromeClient() {
                // uni-app H5 的 chooseImage 走 <input type=file>，必须接住否则选不了图
                override fun onShowFileChooser(
                    view: WebView,
                    callback: ValueCallback<Array<Uri>>,
                    params: FileChooserParams
                ): Boolean {
                    filePathCallback?.onReceiveValue(null)
                    filePathCallback = callback
                    return try {
                        startActivityForResult(params.createIntent(), REQ_FILE)
                        true
                    } catch (e: Exception) {
                        Log.w(TAG, "无法打开文件选择器", e)
                        filePathCallback = null
                        false
                    }
                }
            }
        }

        setContentView(web)

        // WiFi 扫描桥：挂在 window.__elderWifi 上（H5 侧封装见 app/src/utils/wifi-scan.ts）。
        //
        // ⚠️ 三个坑叠在一起，缺一个就是「点扫描毫无反应」且不报错：
        //   1. 必须 addJavascriptInterface 到 **WebView 实例** 上，而不是往
        //      window 上塞东西 —— 本壳没有任何注入 JS 的时机；
        //   2. 桥对象的方法必须带 @JavascriptInterface 注解，API 17+ 不加注解的
        //      方法在 JS 里**看不见**，WebView 也不会报错；
        //   3. 只对 assetLoader 映射出的 https://elder.local 生效 —— 外链被
        //      shouldOverrideUrlLoading 交给系统浏览器了，不存在把桥暴露给外站的路径。
        web.addJavascriptInterface(WifiBridge(), "__elderWifi")

        askPermissionsIfNeeded()

        if (savedInstanceState == null) {
            web.loadUrl("https://$APP_DOMAIN/assets/web/index.html")
        } else {
            web.restoreState(savedInstanceState)
        }
    }

    /**
     * 一次性申请「选图」和「WiFi 扫描」两组运行时权限。
     *
     * 为什么合并成**一次** `requestPermissions`：同一时刻连着调两次，第二个请求
     * 在部分 OEM 上会被直接丢弃 —— 症状是 WiFi 权限永远拿不到、`scan()` 永远回
     * `no_permission`，而且不报任何错。一次批量申请由系统自己排队弹窗，行为确定。
     *
     * 时机：`onCreate`（WebView 起来之前）。**不在 `scan()` 里申请** —— 那个方法
     * 跑在 WebView 的 JavaBridge 线程上，没有 Activity 的权限回调可用，只能同步
     * 返回一个错误码；用户拒权时由 `scan()` 回 `no_permission`，页面据此提示去
     * 设置里手动开。
     */
    private fun askPermissionsIfNeeded() {
        val wanted = ArrayList<String>(2)
        val media = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            Manifest.permission.READ_MEDIA_IMAGES
        } else {
            Manifest.permission.READ_EXTERNAL_STORAGE
        }
        if (checkSelfPermission(media) != PackageManager.PERMISSION_GRANTED) wanted.add(media)
        val wifi = wifiPermission()
        if (checkSelfPermission(wifi) != PackageManager.PERMISSION_GRANTED) wanted.add(wifi)
        if (wanted.isNotEmpty()) requestPermissions(wanted.toTypedArray(), REQ_PERM)
    }

    /** 扫描要用的运行时权限：13+ 是 NEARBY_WIFI_DEVICES，12 及以下只能是定位 */
    private fun wifiPermission(): String =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            Manifest.permission.NEARBY_WIFI_DEVICES
        } else {
            Manifest.permission.ACCESS_FINE_LOCATION
        }

    // ------------------------------------------------------------------ WiFi 扫描

    /**
     * 暴露给 H5 的桥对象（`window.__elderWifi`）。
     *
     * 契约见 `build-out/_wifi-spec.md` 第三节：`scan()` 同步返回一个 **JSON 字符串**
     * （`@JavascriptInterface` 只能回传基本类型，回对象会得到 `[object Object]`）。
     */
    private inner class WifiBridge {
        @JavascriptInterface
        fun scan(): String = scanWifiJson()
    }

    /**
     * 扫一次，**同步**返回 JSON。组合方式：缓存 + 触发扫描 + 回 `busy`。
     *
     * 为什么不能在这里等广播（这是本节最关键的一处取舍）：
     * `startScan()` 是异步的，结果只能从 `SCAN_RESULTS_AVAILABLE_ACTION` 广播拿到；
     * 而 `@JavascriptInterface` 的方法跑在 WebView 的 **JavaBridge 线程**上，用
     * `CountDownLatch.await()` 阻塞等广播，会把 H5 整个 JS 线程一起卡住（WebView
     * 的 JS 调用要回到这个线程上排队）—— 扫描本身要 1~4 秒，用户看到的是页面冻结。
     *
     * 所以改成「**异步写缓存、同步读缓存**」：
     *   - 广播到达时把结果和时间戳写进 [wifiResults] / [wifiResultsAt]（主线程）；
     *   - `scan()` 只读缓存：30 秒内算新鲜，直接返回；否则触发一次 `startScan()`
     *     并回 `busy`，H5 侧（`wifi-scan.ts`）每 700ms 重试一次，广播落地后
     *     下一次重试就能拿到真实结果 —— 用户不需要为此再点一次按钮。
     *
     * 为什么 30 秒：Android 9+ 对前台应用的 `startScan()` 有节流（2 分钟 4 次），
     * 缓存有效期定得太短会把用户点的那几次全撞在节流上，于是每次都回 `busy`。
     * 30 秒既覆盖了「用户看完列表再点一次」的场景，也远小于节流窗口。
     *
     * 错误码只有四个，页面靠它给可行动的话（`busy` 由 TS 侧内部重试消化，
     * 只有重试也超时才会上抛）。
     */
    private fun scanWifiJson(): String {
        if (checkSelfPermission(wifiPermission()) != PackageManager.PERMISSION_GRANTED) {
            return errorJson(ERR_NO_PERMISSION)
        }

        // 「位置信息」总开关：关掉时 getScanResults() 返回**空列表且不抛异常**，
        // 这是最常见的静默失败。必须在这里显式判，并回 location_off 而不是 empty ——
        // 页面要靠它显示「请打开手机的定位开关」这句用户能照做的话。
        if (!isLocationEnabled()) return errorJson(ERR_LOCATION_OFF)

        val wm = getSystemService(Context.WIFI_SERVICE) as? WifiManager
            ?: return errorJson(ERR_EMPTY)

        val now = SystemClock.elapsedRealtime()
        val cachedAt = wifiResultsAt
        if (cachedAt > 0L && now - cachedAt <= WIFI_CACHE_TTL_MS) {
            return successJson(wifiResults)
        }

        val started = try {
            wm.startScan()
        } catch (e: Exception) {
            // 权限在扫描途中被撤销等情况：按「没扫到」处理，别把异常抛回 JS
            Log.w(TAG, "startScan 失败", e)
            false
        }
        if (!started) {
            // Android 9+ 的节流（前台 2 分钟 4 次）会让 startScan 直接返回 false。
            // 仍然回 busy：H5 会重试，缓存落地后自然拿到结果，不会变成「点了没反应」。
            Log.w(TAG, "startScan 被拒（多为系统节流），本次回 busy")
        }
        return errorJson(ERR_BUSY)
    }

    /** 位置服务总开关。API 28+ 用 isLocationEnabled，更低版本只能分别问两个 provider */
    private fun isLocationEnabled(): Boolean {
        val lm = getSystemService(Context.LOCATION_SERVICE) as? LocationManager ?: return false
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) return lm.isLocationEnabled
        return isLocationEnabledLegacy(lm)
    }

    /**
     * API 24~27 没有 `LocationManager.isLocationEnabled`（API 28 才有），
     * 只能自己拼：GPS 或 NETWORK 任一 provider 开着就算「定位可用」。
     *
     * `@Suppress` 打在函数上：`isProviderEnabled` 在 API 28 被标记为 deprecated，
     * 但这里正是低版本分支，不能因为一条 warning 就把它删掉。
     */
    @Suppress("DEPRECATION")
    private fun isLocationEnabledLegacy(lm: LocationManager): Boolean =
        lm.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
            lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)

    /** 广播回调里刷新缓存；`getScanResults()` 可能因权限被撤销抛异常 */
    private fun refreshWifiCache() {
        val wm = getSystemService(Context.WIFI_SERVICE) as? WifiManager ?: return
        try {
            wifiResults = wm.scanResults ?: emptyList()
            wifiResultsAt = SystemClock.elapsedRealtime()
        } catch (e: Exception) {
            Log.w(TAG, "读取扫描结果失败", e)
        }
    }

    private fun registerWifiReceiver() {
        if (wifiReceiver != null) return
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context?, intent: Intent?) {
                if (intent?.action != WifiManager.SCAN_RESULTS_AVAILABLE_ACTION) return
                refreshWifiCache()
            }
        }
        val filter = IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                // targetSdk 34 起注册广播必须显式给 flag，否则直接抛异常。
                // 系统广播用 NOT_EXPORTED 即可（只拦第三方 App 伪造的广播）。
                registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                registerReceiver(receiver, filter)
            }
            wifiReceiver = receiver
        } catch (e: Exception) {
            Log.w(TAG, "注册 WiFi 扫描广播失败", e)
        }
    }

    private fun unregisterWifiReceiver() {
        val receiver = wifiReceiver ?: return
        wifiReceiver = null
        try {
            unregisterReceiver(receiver)
        } catch (e: Exception) {
            Log.w(TAG, "注销 WiFi 扫描广播失败", e)
        }
    }

    /**
     * 组装成功结果：**按信号强度降序**、**同名 SSID 只留最强的一个**、丢掉空 SSID。
     *
     * 空 SSID 是隐藏网络，列表里没法点，而且真机上常出现好几条；
     * 同名 SSID 是 Mesh / 双频同名，留着会让用户看到两三条一样的名字。
     */
    private fun successJson(results: List<ScanResult>): String {
        val best = HashMap<String, ScanResult>()
        for (r in results) {
            val ssid = r.SSID
            if (ssid.isNullOrEmpty()) continue
            val current = best[ssid]
            if (current == null || r.level > current.level) best[ssid] = r
        }
        if (best.isEmpty()) return errorJson(ERR_EMPTY)

        val sorted = best.values.sortedByDescending { it.level }
        val sb = StringBuilder(64 + sorted.size * 48)
        sb.append("{\"ok\":true,\"list\":[")
        for (i in sorted.indices) {
            if (i > 0) sb.append(',')
            val r = sorted[i]
            sb.append("{\"ssid\":\"").append(jsonEscape(r.SSID ?: ""))
                .append("\",\"level\":").append(r.level)
                .append(",\"secured\":").append(if (isSecured(r)) "true" else "false")
                .append('}')
        }
        sb.append("],\"error\":\"\"}")
        return sb.toString()
    }

    /** 开放网络（无 WPA/WEP/EAP）为 false；`capabilities` 形如 `[WPA2-PSK-CCMP][ESS]` */
    private fun isSecured(r: ScanResult): Boolean {
        val caps = r.capabilities ?: return false
        val upper = caps.uppercase(Locale.US)
        return upper.contains("WPA") || upper.contains("WEP") || upper.contains("EAP")
    }

    private fun errorJson(code: String): String =
        "{\"ok\":false,\"list\":[],\"error\":\"$code\"}"

    /**
     * SSID 是外部输入（别人家的路由器想叫什么就叫什么），直接拼进 JSON 会破坏格式
     * —— 一个叫 `a"b` 的热点就能让 `JSON.parse` 整段失败、页面显示「扫描失败」。
     */
    private fun jsonEscape(s: String): String {
        val sb = StringBuilder(s.length + 8)
        for (ch in s) {
            when {
                ch == '"' -> sb.append("\\\"")
                ch == '\\' -> sb.append("\\\\")
                ch == '\n' -> sb.append("\\n")
                ch == '\r' -> sb.append("\\r")
                ch == '\t' -> sb.append("\\t")
                ch < ' ' -> sb.append("\\u").append(ch.code.toString(16).padStart(4, '0'))
                else -> sb.append(ch)
            }
        }
        return sb.toString()
    }

    override fun onResume() {
        super.onResume()
        // 扫描结果广播只在页面可见时收：Activity 不可见时收到的结果没人会看，
        // 而且 targetSdk 34 下必须成对注册/注销，漏注销会泄漏 receiver。
        registerWifiReceiver()
    }

    override fun onPause() {
        unregisterWifiReceiver()
        super.onPause()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_FILE) return
        val cb = filePathCallback ?: return
        filePathCallback = null
        val picked: Array<Uri>? = if (resultCode == RESULT_OK) {
            data?.clipData?.let { clip ->
                Array(clip.itemCount) { i -> clip.getItemAt(i).uri }
            } ?: data?.data?.let { arrayOf(it) }
        } else {
            null
        }
        cb.onReceiveValue(picked)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        if (this::web.isInitialized) web.saveState(outState)
    }

    /**
     * 返回键：**先问页面有没有浮层要关**，再决定是回退还是退出。
     *
     * ⚠️ 为什么不能只写 `web.canGoBack()`（2026-10-30 用户报「创建我的角色页面
     * 按返回键直接退出 APP」）：本页弹层都是 H5 自己用 `v-if` + `.mask` 画的，
     * 不是原生窗口，WebView 完全不知道有东西需要关闭；而 tabBar 各页切换
     * 不产生 history 记录 ⇒ `canGoBack()` 恒为 false ⇒ 直接落到
     * `super.onBackPressed()`，Activity 被 finish，用户看到的就是「退出应用」。
     *
     * 框架的 `onBackPress` 在这条链路上也救不了：它靠 popstate 触发，
     * 而这里根本没有 popstate（history 是空的）。所以必须由壳主动询问，
     * 页面侧的实现见 `app/src/utils/back-guard.ts`。
     *
     * 协议：`window.__elderHandleBack()` 返回 1 = 页面已消化（别退），
     * 返回 0 / 函数不存在 / 抛异常 = 交回默认行为。三个兜底都写在这里，
     * 页面侧哪天被改坏了也只是退回旧行为，不会让返回键彻底失灵。
     */
    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (!this::web.isInitialized) {
            super.onBackPressed()
            return
        }
        web.evaluateJavascript(BACK_PROBE_JS) { raw ->
            // evaluateJavascript 把返回值序列化成 JSON：数字 1 → "1"
            if (raw != null && raw.trim().trim('"') == "1") {
                return@evaluateJavascript
            }
            fallbackBack()
        }
    }

    /**
     * 页面没消化返回键时的默认行为：能回退就回退，否则退出。
     *
     * `@Suppress` 打在函数上而不是调用点上：`super.onBackPressed()` 从
     * onBackPressed 的 override 里挪到普通成员函数后，Kotlin 不再把它当作
     * 「override 里调用 super」看待，会在编译期报 deprecation warning；
     * 而注解写在表达式语句前属于 EXPRESSION target，不同 Kotlin 版本解析
     * 行为不一致，别赌。
     */
    @Suppress("DEPRECATION")
    private fun fallbackBack() {
        if (this::web.isInitialized && web.canGoBack()) {
            web.goBack()
        } else {
            super.onBackPressed()
        }
    }

    companion object {
        private const val TAG = "ElderCompanion"
        private const val APP_DOMAIN = "elder.local"
        private const val REQ_FILE = 1001
        private const val REQ_PERM = 1002

        /** 扫描缓存有效期：见 [scanWifiJson] 里关于节流与缓存长度的取舍说明 */
        private const val WIFI_CACHE_TTL_MS = 30_000L

        /** 错误码与 `app/src/utils/wifi-scan.ts` 的 WifiScanError 逐字一致 */
        private const val ERR_NO_PERMISSION = "no_permission"
        private const val ERR_LOCATION_OFF = "location_off"
        private const val ERR_EMPTY = "empty"
        private const val ERR_BUSY = "busy"

        /**
         * 询问页面是否消化这次返回键。**字符串必须与 `back-guard.ts` 里的
         * `window.__elderHandleBack` 逐字一致** —— 两侧改名只改一边的话，
         * 页面永远返回 0，症状就退回「按返回键直接退出」，而且不报任何错。
         * `tools/smoke_app_back_bridge.py` 会校验这个一致性。
         */
        private const val BACK_PROBE_JS =
            "(function(){try{return (typeof window.__elderHandleBack==='function' && " +
                "window.__elderHandleBack())?1:0}catch(e){return 0}})()"
    }
}
