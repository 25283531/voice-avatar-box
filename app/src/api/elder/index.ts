/**
 * 老人陪伴助手业务后端 API。
 * 接口与 server/app/routers 一一对应。
 */
import { elderDelete, elderGet, elderPatch, elderPost } from '@/http/elder'

// ============================================================ 类型
export interface ElderUser {
  id: number
  username: string
  nickname: string
  phone: string
  is_admin: boolean
}

export interface Device {
  id: number
  device_id: string
  name: string
  owner_id: number | null
  avatar_id: number | null
  online: boolean
  last_seen_at: string | null
  fw_version: string
  ip: string
  settings: Record<string, any>
}

export interface ActivationCode {
  code: string
  display: string
  expires_at: string
  device_id: string | null
}

export interface Reminder {
  id: number
  device_id: number
  title: string
  speak_text: string
  rtype: number
  schedule_kind: number
  times_of_day: string[]
  weekdays: number[]
  month_days: number[]
  interval_days: number
  once_date: string | null
  start_date: string | null
  end_date: string | null
  dose: string
  require_confirm: boolean
  repeat_interval_min: number
  repeat_times: number
  escalate_after_min: number
  confirm_methods: string[]
  enabled: boolean
  next_fire_at: string | null
}

export interface ReminderEvent {
  id: number
  reminder_id: number
  device_id: number
  due_at: string
  fired_at: string | null
  confirmed_at: string | null
  state: number
  confirm_method: number
  fired_count: number
  escalated: boolean
  note: string
}

export interface Avatar {
  id: number
  name: string
  description: string
  eaf_pack_name: string
  screen_width: number
  screen_height: number
  clips: Record<string, string>
  system_prompt: string
  tts_voice: string
  emotion_lights: Record<string, any>
}

export interface StatsOverview {
  device_id: string
  device_name: string
  online: boolean
  last_seen_at: string | null
  today_total: number
  today_dialog: number
  today_confirm: number
  today_miss: number
  total_7d: number
  pending_reminders: number
  missed_today: number
  series: { date: string, total: number, wake: number, dialog: number, confirm: number, miss: number, sos: number }[]
}

// ============================================================ 认证
export function elderLogin(username: string, password: string) {
  return elderPost<{ access_token: string, user_id: number, username: string, is_admin: boolean }>(
    '/api/auth/login', { username, password })
}

export function elderRegister(data: { username: string, password: string, nickname?: string, phone?: string }) {
  return elderPost<ElderUser>('/api/auth/register', data)
}

// ============================================================ 设备
export function listDevices() {
  return elderGet<Device[]>('/api/devices')
}

export function createDevice(data: { device_id: string, name?: string }) {
  return elderPost<Device>('/api/devices', data)
}

export function issueActivationCode(devicePk: number) {
  return elderPost<ActivationCode>(`/api/devices/${devicePk}/code`, {})
}

export function bindDevice(data: { code: string, device_id?: string, alias?: string }) {
  return elderPost<{ device_id: string, device_pk: number, device_token: string, name: string }>(
    '/api/devices/bind', data)
}

export function updateDevice(devicePk: number, data: Partial<Pick<Device, 'name' | 'avatar_id' | 'settings'>>) {
  return elderPatch<Device>(`/api/devices/${devicePk}`, data)
}

export function deleteDevice(devicePk: number) {
  return elderDelete(`/api/devices/${devicePk}`)
}

export function deviceEvents(devicePk: number, limit = 50) {
  return elderGet<ReminderEvent[]>(`/api/devices/${devicePk}/events`, { limit })
}

// ============================================================ 提醒
export function listReminders(devicePk: number) {
  return elderGet<Reminder[]>('/api/reminders', { device_pk: devicePk })
}

export function createReminder(devicePk: number, data: Partial<Reminder>) {
  return elderPost<Reminder>(`/api/reminders?device_pk=${devicePk}`, data)
}

export function updateReminder(reminderPk: number, data: Partial<Reminder>) {
  return elderPatch<Reminder>(`/api/reminders/${reminderPk}`, data)
}

export function toggleReminder(reminderPk: number) {
  return elderPost<Reminder>(`/api/reminders/${reminderPk}/toggle`, {})
}

export function deleteReminder(reminderPk: number) {
  return elderDelete(`/api/reminders/${reminderPk}`)
}

export function confirmReminder(reminderPk: number) {
  return elderPost<ReminderEvent>(`/api/reminders/${reminderPk}/confirm`, {})
}

export function upcomingEvents(devicePk: number, hours = 24) {
  return elderGet<ReminderEvent[]>('/api/reminders/upcoming', { device_pk: devicePk, hours })
}

// ============================================================ 形象
export function listAvatars() {
  return elderGet<Avatar[]>('/api/avatars')
}

export function applyAvatar(avatarPk: number, devicePk: number) {
  return elderPost(`/api/avatars/${avatarPk}/apply/${devicePk}`, {})
}

// ============================================================ 统计
export function statsOverview(devicePk: number, days = 7) {
  return elderGet<StatsOverview>('/api/stats/overview', { device_pk: devicePk, days })
}
