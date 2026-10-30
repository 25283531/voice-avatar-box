package com.elder.companion

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.webkit.WebViewAssetLoader

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

    @SuppressLint("SetJavaScriptEnabled")
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
        askMediaPermissionIfNeeded()

        if (savedInstanceState == null) {
            web.loadUrl("https://$APP_DOMAIN/assets/web/index.html")
        } else {
            web.restoreState(savedInstanceState)
        }
    }

    private fun askMediaPermissionIfNeeded() {
        val perm = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            Manifest.permission.READ_MEDIA_IMAGES
        } else {
            Manifest.permission.READ_EXTERNAL_STORAGE
        }
        if (checkSelfPermission(perm) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(perm), REQ_PERM)
        }
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
