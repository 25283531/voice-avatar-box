/**
 * 时间显示：按**服务端判定的时区**（依 IP 国家/地区）显示，设备自报时区只作兜底。
 *
 * 为什么不是「统一北京时间」，也不是「直接用设备时区」
 * ------------------------------------------------
 * 一度想统一成 UTC+8，那是错的：家属在海外用手机看老人的服药记录，
 * 显示北京时间等于要他先做心算。
 * 但反过来直接 `new Date(s).getHours()`（设备本地时区）同样不行 ——
 * 老人机/家属手机的时区**可能是错的**（出厂默认、手动改过、英文系统），
 * 那时显示的"本地时间"其实也不是他眼前的钟点。
 *
 * 所以判定依据是 **IP 所在国家/地区**（由服务端做，见 `app/geo.py`）：
 * IP 反映真实位置，改不了、也不需要用户配置。前端只管拿到
 * `{timezone: 'Asia/Shanghai'}` 之后按它格式化。
 *
 * 数据流
 * ------
 * 1. App 启动 / 登录后调 `GET /api/geo/timezone` → `{timezone, offset_minutes, source}`；
 * 2. 调 :func:`setDisplayTimeZone` 记住它（本模块内）；
 * 3. 所有 `fmt*` 用它格式化。
 *
 * 兜底顺序：服务端判定 → 设备时区（Intl 默认）→ UTC+8。
 * 第三级兜底存在是因为**小程序 WebView 对 Intl 的 timeZone 支持参差**，
 * 万一前两级都不可用，宁可按项目主用户群（国内）的时区显示，也不要显示 UTC。
 *
 * 输入约定
 * --------
 * 后端返回的时间串**带偏移**：UTC 类是 `...Z`，北京墙上列（due_at /
 * fired_at / confirmed_at）是 `...+08:00`。带偏移的串 `new Date()` 解析是
 * 确定性的，所以换算到目标时区不会错。
 *
 * 万一收到**不带偏移**的裸串（历史接口 / 手写 dict 漏了），本项目里它的语义
 * 是「北京墙上时间」，因此补 `+08:00` 再解析 —— **不能补 Z**，那会让
 * due_at 类整体早 8 小时。
 */

import { ref } from 'vue'

import { elderRequestRaw } from '@/http/elder'

/**
 * 时区的"版本号"。
 *
 * 存在的唯一原因：让**计算属性**能感知到"时区变了"。
 * `startOfToday()` 会被写进 `computed()` 里（首页的"今天已确认次数"），
 * 而 computed 只跟着**响应式**依赖重算 —— 时区是模块级普通变量，改了它
 * computed 纹丝不动，表现是"海外用户首屏的‘今天’一直是设备时区那个"。
 * 在 `startOfToday()` 里读一下这个 ref 就自动建立了依赖，页面代码不必改。
 */
const tzVersion = ref(0)

/** 服务端按 IP 判定的时区（IANA 名）。空串 = 还没拿到，走兜底。 */
let displayTimeZone = ''
/** 判定来源，仅用于调试输出（'ip' / 'config' / 'fallback'）。 */
let displayTimeZoneSource = ''

/**
 * 本地缓存的键。
 *
 * 为什么要落 storage 而不是只放内存：模块级变量在**冷启动时是空的**，
 * 而首屏（首页的"今天"合计）在拿到网络应答之前就要渲染。缓存上一次的结论
 * 能让冷启动第一帧就用对时区，网络应答回来后再校正一次即可。
 */
const STORAGE_KEY = 'elder_display_tz'
const STORAGE_SRC = 'elder_display_tz_src'

function readCached(): void {
  try {
    const tz = (uni.getStorageSync(STORAGE_KEY) as string) || ''
    if (tz) {
      displayTimeZone = tz
      displayTimeZoneSource = (uni.getStorageSync(STORAGE_SRC) as string) || 'cache'
    }
  }
  catch {
    /* storage 不可用（隐私模式）→ 保持空串走兜底，不是致命问题 */
  }
}

readCached()

/** 记住服务端判定的时区。传入空串等价于"未知，走兜底"。 */
export function setDisplayTimeZone(tz?: string | null, source = ''): void {
  const next = (tz || '').trim()
  const changed = next !== displayTimeZone
  displayTimeZone = next
  displayTimeZoneSource = displayTimeZone ? source : ''
  if (changed) tzVersion.value += 1
  try {
    if (displayTimeZone) {
      uni.setStorageSync(STORAGE_KEY, displayTimeZone)
      uni.setStorageSync(STORAGE_SRC, displayTimeZoneSource)
    }
    else {
      uni.removeStorageSync(STORAGE_KEY)
      uni.removeStorageSync(STORAGE_SRC)
    }
  }
  catch {
    /* 同上：写不进去只是少了缓存，不影响本次会话的显示 */
  }
}

export function getDisplayTimeZone(): { timezone: string; source: string } {
  return { timezone: displayTimeZone, source: displayTimeZoneSource }
}

/** 设备自己的 IANA 时区（小程序/安卓 WebView 上可能拿不到 → 空串）。 */
export function detectDeviceTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  }
  catch {
    return ''
  }
}

/**
 * 向服务端要一次「我该按哪个时区显示」，并记住它。
 *
 * 调用时机：App 启动（`App.vue` 的 `onLaunch`）。**不要 await 在首屏渲染前** ——
 * 这个请求最坏要等 2~3 秒（服务端可能要去查 IP 归属地），而首屏完全可以先按
 * 上次缓存/设备时区画出来，值回来后再校正。
 *
 * 失败时**什么都不做**：保留缓存值或设备时区。绝不写一个"猜的"时区进来 ——
 * 猜错比不知道更难排查（界面上看起来一切正常，只是时间不对）。
 */
export async function bootstrapTimezone(): Promise<void> {
  const deviceTz = detectDeviceTimeZone()
  try {
    const raw = await elderRequestRaw<{ timezone?: string; source?: string }>(
      '/api/geo/timezone',
      {
        auth: false,
        toast: false,
        timeout: 8000,
        header: deviceTz ? { 'X-Client-Timezone': deviceTz } : {},
      },
    )
    if (raw.statusCode === 200 && raw.data?.timezone) {
      setDisplayTimeZone(raw.data.timezone, raw.data.source || 'server')
    }
  }
  catch {
    /* 网络不通 / 服务端不支持该端点（老版本）：沿用缓存或设备时区 */
  }
}

const OFFSET_RE = /([zZ]|[+-]\d{2}:?\d{2})$/

/** 解析后端时间串；失败返回 null（调用方显示占位符而不是 NaN）。 */
function parseServerTime(value: string | Date | null | undefined): Date | null {
  if (!value) return null
  if (value instanceof Date) {
    return isNaN(value.getTime()) ? null : value
  }
  let text = String(value).trim()
  if (!text) return null
  // "2026-10-21 14:00:00" → "2026-10-21T14:00:00"（Safari 不认空格分隔）
  if (text.includes(' ') && !text.includes('T')) {
    text = text.replace(' ', 'T')
  }
  if (!OFFSET_RE.test(text)) {
    text += '+08:00'
  }
  const d = new Date(text)
  return isNaN(d.getTime()) ? null : d
}

/**
 * 把 Date 拆成目标时区的字段。
 *
 * 优先用 `Intl.DateTimeFormat` 的 `timeZone`（现代浏览器 / 大多安卓 WebView
 * 都支持）；不支持或抛错时用**设备时区**（`getFullYear()` 那一套）——
 * 这是"兜底"，不是"正确答案"，两种情况在 `console` 里可区分。
 */
function partsInZone(d: Date, timeZone: string) {
  if (timeZone) {
    try {
      const fmt = new Intl.DateTimeFormat('en-US', {
        timeZone,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
      const bag: Record<string, string> = {}
      for (const p of fmt.formatToParts(d)) {
        if (p.type !== 'literal') bag[p.type] = p.value
      }
      if (bag.year && bag.month && bag.day) {
        // 某些实现把 24 点写成 "24"（hour12:false + 午夜），归一到 0
        const hour = bag.hour === '24' ? '00' : bag.hour
        return {
          year: Number(bag.year),
          month: Number(bag.month),
          day: Number(bag.day),
          hour: Number(hour || 0),
          minute: Number(bag.minute || 0),
        }
      }
    } catch {
      /* 落到下面的设备时区兜底 */
    }
  }
  return {
    year: d.getFullYear(),
    month: d.getMonth() + 1,
    day: d.getDate(),
    hour: d.getHours(),
    minute: d.getMinutes(),
  }
}

const pad = (n: number) => String(n).padStart(2, '0')

/**
 * `YYYY-MM-DD HH:mm`（目标时区）。
 * @param placeholder 解析失败或空值时的占位符。
 */
export function fmtDateTime(
  value: string | Date | null | undefined,
  placeholder = '—',
): string {
  const d = parseServerTime(value)
  if (!d) return placeholder
  const p = partsInZone(d, displayTimeZone)
  return `${p.year}-${pad(p.month)}-${pad(p.day)} ${pad(p.hour)}:${pad(p.minute)}`
}

/** `YYYY-MM-DD`（目标时区）。 */
export function fmtDate(
  value: string | Date | null | undefined,
  placeholder = '—',
): string {
  const d = parseServerTime(value)
  if (!d) return placeholder
  const p = partsInZone(d, displayTimeZone)
  return `${p.year}-${pad(p.month)}-${pad(p.day)}`
}

/** `HH:mm`（目标时区）。 */
export function fmtTime(
  value: string | Date | null | undefined,
  placeholder = '—',
): string {
  const d = parseServerTime(value)
  if (!d) return placeholder
  const p = partsInZone(d, displayTimeZone)
  return `${pad(p.hour)}:${pad(p.minute)}`
}

/**
 * epoch 秒/毫秒 → `YYYY-MM-DD HH:mm`（目标时区）。
 *
 * 后端有些地方给的是 **epoch 数字**（如记忆时间戳 `ts`）而不是 ISO 串。
 * 自动判断秒/毫秒：10 位数按秒、13 位数按毫秒。
 */
export function fmtEpoch(
  value: number | null | undefined,
  placeholder = '—',
): string {
  if (!value || !Number.isFinite(value)) return placeholder
  const ms = value < 1e12 ? value * 1000 : value
  return fmtDateTime(new Date(ms), placeholder)
}

/**
 * 某时刻在目标时区相对 UTC 的偏移（毫秒，东八区为 +28800000）。
 *
 * 算法：把该时刻在目标时区的**墙上字段**当作 UTC 再减去真实时刻 ——
 * 差值就是偏移。归整到秒是为了避开 `formatToParts` 不返回毫秒造成的抖动。
 */
function zoneOffsetMs(at: Date, timeZone: string): number {
  const floored = new Date(Math.floor(at.getTime() / 1000) * 1000)
  const p = partsInZone(floored, timeZone)
  const asIfUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute)
  return asIfUtc - floored.getTime()
}

/**
 * **目标时区的**今天零点（绝对时刻）。
 *
 * 用于"今天的服药记录"这类客户端筛选。不能用 `d.setHours(0,0,0,0)`
 * —— 那是**设备时区**的零点，而设备时区可能错（出厂默认/手动改过）；
 * 也不能用服务端时区 —— 用户看到的是哪个时区的日期，筛选就该用同一个。
 */
export function startOfToday(): Date {
  // 读一下版本号建立响应式依赖（理由见 tzVersion 的注释）
  void tzVersion.value
  const now = new Date()
  const p = partsInZone(now, displayTimeZone)
  const midnightAsUtc = Date.UTC(p.year, p.month - 1, p.day)
  return new Date(midnightAsUtc - zoneOffsetMs(now, displayTimeZone))
}
