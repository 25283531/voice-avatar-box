/**
 * 文案表（仅简体中文）。
 *
 * 本项目面向国内老年用户，只提供中文界面，因此不再集成多语言框架，
 * 仅保留 t('key') 的调用形式，便于配网等复用页面无需改动调用处。
 * 切换语言 / 语言商店等能力已随之移除。
 */
import { ref } from 'vue'
import zh_CN from './zh_CN'

const messages: Record<string, Record<string, string>> = {
  zh_CN: zh_CN as unknown as Record<string, string>,
}

const currentLang = ref('zh_CN')

/** 初始化（兼容旧调用点，无实际动作） */
export function initI18n() {}

/** 切换语言（本项目仅中文，空实现并提示） */
export function changeLanguage() {
  uni.showToast({ title: '当前仅支持简体中文', icon: 'none' })
}

/** 取文案；找不到时返回 key 本身 */
export function t(key: string, params?: Record<string, string | number>): string {
  const value = messages[currentLang.value]?.[key]
  if (typeof value !== 'string') return key
  if (!params) return value
  let result = value
  Object.entries(params).forEach(([k, v]) => {
    result = result.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v))
  })
  return result
}

export function getCurrentLanguage(): string {
  return currentLang.value
}

export function getSupportedLanguages(): { code: string, name: string }[] {
  return [{ code: 'zh_CN', name: '简体中文' }]
}
