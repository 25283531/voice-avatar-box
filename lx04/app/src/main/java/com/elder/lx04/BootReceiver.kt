package com.elder.lx04

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * 开机自启。
 *
 * ## 为什么需要它
 *
 * 注册了 `MAIN + HOME + DEFAULT` 之后，系统启动时会自动拉起 HOME 应用 ——
 * 这已经覆盖了"开机直接进入我们的系统"的大多数情况。
 *
 * 但有两个例外需要兜底：
 *   1. 某些 ROM 在首次开机 / 应用更新后会清掉 HOME 状态
 *   2. 精简 system 后如果没有唯一的 HOME provider，系统可能不拉起任何东西
 *
 * 所以再加一道 `BOOT_COMPLETED` 广播兜底。
 *
 * ## 注意事项
 *
 * Android 8.1 起，后台启动 Activity 受限（"后台启动 Activity" 限制），
 * 但 `BOOT_COMPLETED` 广播是**明确豁免**的 —— 系统允许接收器在开机时
 * 启动 Activity。所以这里直接 `startActivity` 是合法的。
 *
 * 用 `FLAG_ACTIVITY_NEW_TASK` 是因为从 BroadcastReceiver 启动 Activity
 * 必须带这个 flag（否则没有 task 可加）。
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        if (action != Intent.ACTION_BOOT_COMPLETED &&
            action != "android.intent.action.QUICKBOOT_POWERON" &&
            action != "android.intent.action.LOCKED_BOOT_COMPLETED"
        ) {
            return
        }

        Log.i(TAG, "开机广播 $action —— 拉起主界面")

        val launch = Intent(context, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
            // 从 Launcher 启动时带上这个，避免系统认为这是"从后台启动"
            addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
        }
        context.startActivity(launch)
    }

    companion object {
        private const val TAG = "ElderLX04"
    }
}
