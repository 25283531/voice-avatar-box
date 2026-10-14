/**
 * 老人陪伴助手业务后端的独立请求层。
 *
 * 为什么不复用项目里已有的 alova 实例：
 *   现有实例绑定的是小智 manager-api（Java），鉴权体系、响应结构都不同；
 *   本业务后端是独立的 FastAPI 服务，响应体就是裸数据（非 {code,msg,data}），
 *   因此这里单独用 uni.request 封装一套，互不干扰，也便于单独切换服务地址。
 */
const TOKEN_KEY = 'elder_token'
const BASE_URL_KEY = 'elder_base_url'
const USER_KEY = 'elder_user'

/** 默认业务后端地址；打包前请在 env/.env 里配置 VITE_ELDER_BASEURL */
const DEFAULT_BASE_URL: string = (import.meta.env.VITE_ELDER_BASEURL as string) || 'https://voice.250886.xyz'

export function getElderBaseUrl(): string {
  return (uni.getStorageSync(BASE_URL_KEY) as string) || DEFAULT_BASE_URL
}

export function setElderBaseUrl(url: string) {
  uni.setStorageSync(BASE_URL_KEY, url)
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
  } catch {
    return {}
  }
}

export function setElderUser(user: Record<string, any>) {
  uni.setStorageSync(USER_KEY, JSON.stringify(user))
}

export interface ElderRequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  data?: Record<string, any>
  /** 是否需要携带 token，默认 true */
  auth?: boolean
  /** 是否弹出错误提示，默认 true */
  toast?: boolean
  timeout?: number
}

/**
 * 发起请求。成功直接返回业务数据；失败抛出 Error（message 为服务端 detail）。
 */
export function elderRequest<T = any>(path: string, options: ElderRequestOptions = {}): Promise<T> {
  const { method = 'GET', data, auth = true, toast = true, timeout = 10000 } = options

  const header: Record<string, string> = { 'Content-Type': 'application/json' }
  if (auth) {
    const token = getElderToken()
    if (!token) {
      uni.showToast({ title: '请先登录', icon: 'none' })
      return Promise.reject(new Error('未登录'))
    }
    header.Authorization = `Bearer ${token}`
  }

  return new Promise<T>((resolve, reject) => {
    uni.request({
      url: getElderBaseUrl() + path,
      method: method as any,
      data: data as any,
      header,
      timeout,
      success: (res: any) => {
        // 204 No Content
        if (res.statusCode === 204) {
          resolve(undefined as any)
          return
        }
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data as T)
          return
        }
        const detail = (res.data && (res.data.detail || res.data.message)) || `请求失败(${res.statusCode})`
        if (res.statusCode === 401) {
          clearElderToken()
          uni.showToast({ title: '登录已失效，请重新登录', icon: 'none' })
        } else if (toast) {
          uni.showToast({ title: String(detail).slice(0, 40), icon: 'none' })
        }
        reject(new Error(detail))
      },
      fail: (err: any) => {
        const msg = (err && err.errMsg) || '网络请求失败'
        if (toast) uni.showToast({ title: '网络异常，请检查服务地址', icon: 'none' })
        reject(new Error(msg))
      },
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
