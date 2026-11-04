package com.elder.lx04

import android.content.Context
import android.provider.Settings
import android.util.Log

/**
 * 调试桥 —— 用广播在真机上直接驱动灯效与按键语义。
 *
 * ## 为什么需要它
 *
 * 灯效和按键的验收全在**屏幕和手感**上，而这些状态平时根本到不了：
 *
 *   · 服药未确认（左带长亮红）要等服务端下发提醒
 *   · 确认成功（右带天蓝闪 3 次）要真的按住中间键 3 秒 ——
 *     `input keyevent --longpress` 在同一毫秒内就发 UP，按住时长是 0ms，
 *     只会走"抖动"分支，什么都看不到
 *   · 粉红闪 5 次、提醒 nag 节奏同理
 *
 * 没有这个入口，每次验证都得等真实业务事件，或者改代码重打 APK。
 *
 * ## 为什么用广播 + 系统设置开关
 *
 * 广播很容易被任何 App 触发，所以**默认关闭**：只有
 * `settings put global elder_debug 1`（需要 root/shell）之后才响应。
 * 出厂的机器上这个值是 0，调试入口等于不存在。
 *
 * 用法：
 * ```
 * adb shell settings put global elder_debug 1
 * adb shell am broadcast -a com.elder.lx04.DEBUG --es cmd med_pending
 * adb shell am broadcast -a com.elder.lx04.DEBUG --es cmd confirmed
 * adb shell settings put global elder_debug 0     # 验完关掉
 * ```
 *
 * 支持的命令见 [MainActivity.handleDebugCommand]。
 */
object DebugBridge {

    /** 调试广播的 action。 */
    const val ACTION = "com.elder.lx04.DEBUG"

    /** 开关所在的系统设置项（Global）。 */
    const val SETTING = "elder_debug"

    private const val TAG = "ElderDebug"

    /**
     * 调试入口是否开启。
     *
     * 读不到设置项（权限/异常）一律当**关闭**处理 —— 失败要往安全的方向倒。
     */
    fun enabled(ctx: Context): Boolean = runCatching {
        Settings.Global.getInt(ctx.contentResolver, SETTING, 0) == 1
    }.getOrElse {
        Log.w(TAG, "读不到 $SETTING，按关闭处理: ${it.message}")
        false
    }
}
