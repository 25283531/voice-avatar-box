/**
 * 老人陪伴助手业务后端的独立请求层。
 *
 * 设计约定（重要）：
 *   1. 服务端地址在**代码级写死**，不提供任何"网络设置 / 服务地址"入口，
 *      避免用户配错地址导致连不上（需求：手机 APP 不能进行网络设置）。
 *   2. 不复用项目里已有的 alova 实例：那套绑定的是小智 manager-api（Java），
 *      鉴权体系、响应结构都不同；本业务后端是独立的 FastAPI 服务，
 *      响应体就是裸数据（非 {code,msg,data}），因此单独用 uni.request 封装。
 *   3. **不接入小智任何服务**（无小智账号登录、无小智 OTA、无小智智能体市场）。
 */
const TOKEN_KEY = 'elder_token'
const USER_KEY = 'elder_user'

/**
 * 业务后端地址（服务端接口 + 业务接口统一走这个地址）。
 *
 * 2026-10-18 起为 **IP 直连**：国内云服务器域名未备案被厂商按 SNI 阻断
 * （非浏览器客户端全死于 TLS 握手、仅浏览器时通时断），连 IP 不带 SNI 可绕过。
 * 前提：服务端必须配 **IP 证书**（SAN 含 152.136.230.132，Let's Encrypt 自
 * 2026-01 起可发行，160 小时有效期需自动续期）——否则 OkHttp 主机名校验失败。
 * 备案完成后切回域名：同步改这里与固件 Kconfig 的 OTA 默认值即可。
 */
const BASE_URL = 'https://152.136.230.132'

/** 返回写死的业务后端地址（只读，不允许运行时修改） */
export function getElderBaseUrl(): string {
  return BASE_URL
}

export function getElderToken(): string {
  return (uni.getStorageSync(TOKEN_KEY) as string) || ''
}

export function setElderToken(token: string) {
  uni.setStorageSync(TOKEN_KEY, token)
}

export function clearElderToken() {
  uni.removeStorageSync(TOKEN_KEY)
  uni.removeStorageSync(USER_KEY)
}

export function getElderUser(): Record<string, any> {
  const raw = uni.getStorageSync(USER_KEY)
  if (!raw) return {}
  try {
    return typeof raw === 'string' ? JSON.parse(raw) : raw
  }
  catch {
    return {}
  }
}

export function setElderUser(user: Record<string, any>) {
  uni.setStorageSync(USER_KEY, JSON.stringify(user))
}

export interface ElderRequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  data?: Record<string, any>
  /**
   * 是否需要携带 token，默认 true。
   * 'optional' = 有 token 就带上（登录后提交会自动关联账号），没 token 也不报错，
   * 专给「匿名也能用」的接口（提交工单：忘记密码的人根本登不进来）。
   */
  auth?: boolean | 'optional'
  /** 是否弹出错误提示，默认 true */
  toast?: boolean
  timeout?: number
  /**
   * 额外请求头（与默认的 Content-Type/Authorization 合并，同名时以这里为准）。
   *
   * 目前只有 `/api/geo/timezone` 用：它需要 `X-Client-Timezone` 来细化
   * 「跨多时区国家」（IP 只能定到国家，纽约还是洛杉矶要靠设备自报）。
   */
  header?: Record<string, string>
}

/** 原始响应：连同 HTTP 状态码一起交出（成功失败都 resolve） */
export interface ElderRawResult<T = any> {
  statusCode: number
  data: T
}

/**
 * 原始请求：不做任何加工，**失败也 resolve**，把 { statusCode, data } 原样交出。
 *
 * 存在的理由：找回密码这组的失败分支是需要分诊的 —— 429 冷却、503 平台没配邮件、
 * 400 验证码错误（而且 detail 是 { message, attempts_left } 结构），要靠状态码和
 * attempts_left 才能说出「验证码不对，你还可以试 3 次」这种人话；elderRequest 会
 * 把对象型 detail 直接 String() 成 "[object Object]"。这类接口走这里自己写文案。
 */
export function elderRequestRaw<T = any>(
  path: string,
  options: ElderRequestOptions = {},
): Promise<ElderRawResult<T>> {
  const { method = 'GET', data, auth = true, timeout = 10000, header: extra } = options

  // X-Client 告诉服务端这次操作来自「手机 APP」还是「网页」。
  // 提醒的操作历史（/api/reminders/audits）会把它记成 via=app/web，
  // 家属看流水时才能分清"我在 APP 上删的"和"我爸在网页上删的"。
  // 放在这个唯一出口而不是每个调用点：漏一个调用点，那次操作就会记成
  // 来源未知，而那种缺口在流水里看起来跟"从别处操作"一模一样。
  const header: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Client': 'app',
  }
  if (auth === 'optional') {
    const token = getElderToken()
    if (token) header.Authorization = `Bearer ${token}`
  }
  else if (auth) {
    const token = getElderToken()
    header.Authorization = `Bearer ${token}`
  }
  if (extra) Object.assign(header, extra)

  return new Promise<ElderRawResult<T>>((resolve, reject) => {
    uni.request({
      url: BASE_URL + path,
      method: method as any,
      data: data as any,
      header,
      timeout,
      success: (res: any) => {
        resolve({ statusCode: res.statusCode as number, data: res.data as T })
      },
      fail: (err: any) => {
        reject(new Error((err && err.errMsg) || '网络请求失败'))
      },
    })
  })
}

/**
 * 发起请求。成功直接返回业务数据；失败抛出 Error（message 为服务端 detail）。
 */
export function elderRequest<T = any>(path: string, options: ElderRequestOptions = {}): Promise<T> {
  const { auth = true, toast = true } = options

  if (auth === true && !getElderToken()) {
    uni.showToast({ title: '请先登录', icon: 'none' })
    return Promise.reject(new Error('未登录'))
  }

  return new Promise<T>((resolve, reject) => {
    elderRequestRaw<T>(path, options)
      .then((raw) => {
        // 204 No Content：没有响应体
        if (raw.statusCode === 204) {
          resolve(undefined as any)
          return
        }
        if (raw.statusCode >= 200 && raw.statusCode < 300) {
          resolve(raw.data)
          return
        }
        const body: any = raw.data
        const detail = (body && (body.detail || body.message)) || `请求失败(${raw.statusCode})`
        if (raw.statusCode === 401) {
          clearElderToken()
          uni.showToast({ title: '登录已失效，请重新登录', icon: 'none' })
        }
        else if (toast) {
          uni.showToast({ title: String(detail).slice(0, 40), icon: 'none' })
        }
        reject(new Error(detail))
      })
      .catch((err: any) => {
        const msg = (err && err.message) || '网络请求失败'
        if (toast) uni.showToast({ title: '网络异常，请稍后重试', icon: 'none' })
        reject(new Error(msg))
      })
  })
}

/** 带 query 参数的 GET 便捷方法 */
export function elderGet<T = any>(path: string, params: Record<string, any> = {}): Promise<T> {
  const qs = Object.keys(params)
    .filter(k => params[k] !== undefined && params[k] !== null && params[k] !== '')
    .map(k => `${encodeURIComponent(k)}=${encodeURIComponent(String(params[k]))}`)
    .join('&')
  return elderRequest<T>(qs ? `${path}?${qs}` : path, { method: 'GET' })
}

export function elderPost<T = any>(path: string, data?: Record<string, any>): Promise<T> {
  return elderRequest<T>(path, { method: 'POST', data })
}

export function elderPatch<T = any>(path: string, data?: Record<string, any>): Promise<T> {
  return elderRequest<T>(path, { method: 'PATCH', data })
}

export function elderDelete<T = any>(path: string): Promise<T> {
  return elderRequest<T>(path, { method: 'DELETE' })
}
