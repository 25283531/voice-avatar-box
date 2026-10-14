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

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        // uni-app H5 是 hash 路由，先交给页面自己回退
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
    }
}
