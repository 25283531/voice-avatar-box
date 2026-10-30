/**
 * 原生 WiFi 扫描桥的 H5 封装（给「改 WiFi」页面用）。
 *
 * ⚠️ 为什么必须走原生桥：本项目手机端不是 DCloud 的 app-vue 原生运行时，而是自写的
 * Android WebView 薄壳（`android/app/src/main/java/com/elder/companion/MainActivity.kt`）。
 * `uni.getWifiList` 只在 DCloud 原生运行时里存在，本项目里它**不存在**，H5 侧也没有
 * 任何标准 Web API 能列 WiFi。所以壳里挂了一个 `window.__elderWifi`（`@JavascriptInterface`），
 * 本文件负责把它包成 Promise 并做降级。
 *
 * 契约见 `build-out/_wifi-spec.md` 第三节，壳侧返回的是一段 **JSON 字符串**：
 *   成功 `{"ok":true,"list":[{"ssid":"MyHome-5G","level":-45,"secured":true}],"error":""}`
 *   失败 `{"ok":false,"list":[],"error":"no_permission"|"location_off"|"empty"|"busy"}`
 *
 * 三条设计要点：
 *  1. **桥不存在 ⇒ unsupported**。这是最关键的一条降级路径：浏览器 / 微信里调试、
 *     以及任何没有这个桥的产物形态，页面必须能据此退回「手输 SSID」。
 *  2. **busy 内部重试**。原生侧是「触发扫描 → 同步回 busy → 等广播落地」的两段式，
 *     用户不该为此再点一次按钮，所以这里自己轮询。
 *  3. **绝不抛异常**。返回值来自原生拼的字符串，解析失败、字段缺失、类型不对都必须
 *     安全降级 —— 让页面拿一个「失败」结果去显示提示，比让调用方 catch 一个
 *     `SyntaxError` 可靠得多。
 */

/** 一个可选的 WiFi，`level` 是 RSSI（dBm，越接近 0 越强） */
export interface WifiItem {
  ssid: string
  level: number
  secured: boolean
}

/** 原生侧约定的错误码；`unsupported` 只可能由本文件产生（桥不存在） */
export type WifiScanError = 'unsupported' | 'no_permission' | 'location_off' | 'empty' | 'busy'

export interface WifiScanResult {
  ok: boolean
  list: WifiItem[]
  error: WifiScanError | ''
}

/** 原生桥的形状：只有一个同步方法，返回 JSON 字符串 */
interface NativeWifiBridge {
  scan: () => string
}

/** 轮询间隔：700ms × 6 次 ≈ 4 秒，覆盖一次真实扫描（1~4 秒）的等待 */
const RETRY_INTERVAL_MS = 700
const MAX_RETRY = 6

const KNOWN_ERRORS: WifiScanError[] = [
  'unsupported',
  'no_permission',
  'location_off',
  'empty',
  'busy',
]

function fail(error: WifiScanError): WifiScanResult {
  return { ok: false, list: [], error }
}

/**
 * 取原生桥。取不到返回 null —— 所有调用方都靠这个判断降级，不要直接摸 `window`。
 *
 * 用 `globalThis` 而不是 `window`：同一份代码要能在 H5 / 小程序环境里被 import
 * 而不炸（小程序没有 window）。
 */
function getBridge(): NativeWifiBridge | null {
  try {
    const w = globalThis as any
    const bridge = w.__elderWifi
    if (bridge && typeof bridge.scan === 'function') return bridge as NativeWifiBridge
    return null
  }
  catch {
    // 极端情况下访问注入对象本身就可能抛（某些 WebView 实现的坑），按不支持处理
    return null
  }
}

/** 当前环境是否有原生扫描能力。页面据此决定要不要显示「扫描」按钮。 */
export function isWifiScanSupported(): boolean {
  return getBridge() !== null
}

function normalizeError(value: unknown): WifiScanError {
  return typeof value === 'string' && (KNOWN_ERRORS as string[]).includes(value)
    ? (value as WifiScanError)
    : 'unsupported'
}

/**
 * 解析原生返回的 JSON 字符串，**任何异常形态都降级成 `unsupported`**。
 *
 * 注意 `ok:true` 但 `list` 不是数组的情况：原生侧正常不会这么发（空列表会回 `empty`），
 * 但真出现了也只能给空列表 —— 让页面显示「没扫到」，而不是崩。
 */
function parseNative(raw: unknown): WifiScanResult {
  if (typeof raw !== 'string' || raw === '') return fail('unsupported')

  let data: any
  try {
    data = JSON.parse(raw)
  }
  catch {
    return fail('unsupported')
  }
  if (!data || typeof data !== 'object') return fail('unsupported')

  const list: WifiItem[] = []
  if (Array.isArray(data.list)) {
    for (const item of data.list) {
      if (!item || typeof item.ssid !== 'string' || item.ssid === '') continue
      list.push({
        ssid: item.ssid,
        // 原生侧给的是 Int；缺失/非数字时按最弱处理，不要留 NaN 进排序
        level: typeof item.level === 'number' && Number.isFinite(item.level) ? item.level : -100,
        secured: item.secured === true,
      })
    }
  }

  if (data.ok === true) return { ok: true, list, error: '' }
  return fail(normalizeError(data.error))
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

/**
 * 扫描一次，拿 WiFi 列表。
 *
 * 非 APP 环境（浏览器 / 微信 / 云打包产物）返回 `unsupported`，调用方据此回退到手输。
 *
 * 原生侧是「同步返回 + 异步结果」的组合：第一次调用只会触发 `startScan()` 并回
 * `busy`，广播落地后缓存才有效。所以这里在内部按 700ms 轮询，最多重试 6 次
 * （约 4 秒）；仍未拿到就返回 `busy`，由页面提示「再试一次」。
 * 用户视角只有一次点击。
 */
export async function scanWifi(): Promise<WifiScanResult> {
  const bridge = getBridge()
  if (!bridge) return fail('unsupported')

  let last: WifiScanResult = fail('busy')
  for (let attempt = 0; attempt <= MAX_RETRY; attempt++) {
    if (attempt > 0) await delay(RETRY_INTERVAL_MS)

    let raw: string
    try {
      raw = bridge.scan()
    }
    catch {
      // 桥在（对象存在）但调用失败：属于壳的实现问题，按「不支持」降级最安全，
      // 页面会退回手输，用户仍然能完成改 WiFi。
      return fail('unsupported')
    }

    const result = parseNative(raw)
    // 只有 busy 值得重试：no_permission / location_off 需要用户去改系统设置，
    // empty 是真的没有可用网络，重试都是白等。
    if (result.error !== 'busy') return result
    last = result
  }
  return last
}
