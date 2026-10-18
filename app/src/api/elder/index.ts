/**
 * 老人陪伴助手业务后端 API。
 * 接口与 server/app/routers 一一对应。
 */
import { elderDelete, elderGet, elderPatch, elderPost, elderRequestRaw } from '@/http/elder'
import type { ElderRawResult } from '@/http/elder'

// ============================================================ 类型
export interface ElderUser {
  id: number
  username: string
  nickname: string
  phone: string
  email: string
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
// ⚠️ 登录/注册是**请求前就没有 token** 的接口，必须走 auth:false。
//    历史事故（2026-10-18，用户实测"点登录/点注册都提示未登录"）：
//    这两个函数曾用 elderPost（默认 auth=true），而 elderRequest 在**发请求之前**
//    就检查 token —— 没有 token 直接 reject('未登录')，一个 HTTP 包都发不出去。
//    同文件的找回密码接口（auth:false）本就是正确范式。
//
//    也不能借道 elderRequest 的错误通道：它把 401 一律解释成"登录态失效"，
//    而登录失败的 401 detail 是「用户名或密码错误」（服务端刻意让"用户不存在"
//    与"密码错"返回完全相同的响应以防用户名枚举，见 auth.py 的
//    LOGIN_FAILED_DETAIL），两种 401 的文案不能混。故用 elderRequestRaw 自取 detail。
//
//    网络层失败（域名不存在/DNS 失败/断网）时 uni.request 的 errMsg 形如
//    "request:fail"，不加工直接怼给用户看不懂，统一包成"网络异常"。

/** 从原始响应取服务端 detail（仅字符串可用；对象型 detail 对这两个接口不会出现） */
function authDetail(raw: ElderRawResult, fallback: string): string {
  const detail = (raw.data as any)?.detail
  return typeof detail === 'string' && detail ? detail : fallback
}

export async function elderLogin(username: string, password: string) {
  let raw: ElderRawResult<{ access_token: string, user_id: number, username: string, is_admin: boolean }>
  try {
    raw = await elderRequestRaw('/api/auth/login', {
      method: 'POST', data: { username, password }, auth: false, toast: false,
    })
  }
  catch {
    throw new Error('网络异常，请检查网络连接')
  }
  if (raw.statusCode >= 200 && raw.statusCode < 300) return raw.data
  throw new Error(authDetail(raw, '登录失败'))
}

export async function elderRegister(data: { username: string, password: string, nickname?: string, phone?: string, email?: string }) {
  let raw: ElderRawResult<ElderUser>
  try {
    raw = await elderRequestRaw('/api/auth/register', {
      method: 'POST', data, auth: false, toast: false,
    })
  }
  catch {
    throw new Error('网络异常，请检查网络连接')
  }
  if (raw.statusCode >= 200 && raw.statusCode < 300) return raw.data
  throw new Error(authDetail(raw, '注册失败'))
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


// ============================================================ 账户（我的）
export interface ElderAccount {
  id: number
  username: string
  nickname: string
  phone: string
  email: string
  is_admin: boolean
}

export function getMe() {
  return elderGet<ElderAccount>('/api/auth/me')
}

export function changePassword(data: { old_password: string, new_password: string }) {
  return elderPost('/api/auth/password', data)
}

export function changeEmail(email: string) {
  return elderPatch<ElderAccount>('/api/auth/email', { email })
}

// ============================================================ 角色（人格）
// ⚠️ 角色与平台 AI 配置已彻底拆分（后端 A1+A2）：
//   * 角色 -> /api/roles（分区返回 presets / mine，含人格与 traits）
//   * 平台配置 -> /api/providers（**仅管理员**，返回带 api_key）
// 改造前这里走的是 /api/providers/safe，那个接口会把平台配置的
// provider/endpoint/model 连同角色一起返回，且任何人都能删 —— 见 P0-1/P0-2。
export interface AgentRole {
  id: number
  name: string
  description: string
  system_prompt: string
  tts_voice: string
  avatar_id?: number | null
  /** 'preset' = 平台预设（小暖）；'role' = 用户原创或派生 */
  record_type: 'preset' | 'role'
  owner_id?: number | null
  /** 派生来源；null = 原创 */
  parent_id?: number | null
  /** 只存「与母版不同」的项（overlay） */
  traits: Record<string, any>
  /** 叠加母版后的生效值，页面显示"实际是什么"用这个 */
  traits_effective: Record<string, any>
  /** 硬红线版本；变了代表承诺卡需要重新确认 */
  guardrail_version: string
  guardrail_acked: boolean
}

/** GET /api/roles 的分区结构：平台推荐 + 我的角色 */
export interface AgentRoleList {
  presets: AgentRole[]
  mine: AgentRole[]
}

/** 列出角色（默认 preset + mine 都要，页面分区渲染） */
export function listAgentRoles(scope: 'all' | 'preset' | 'mine' = 'all') {
  return elderGet<AgentRoleList>(`/api/roles?scope=${scope}`)
}

/** 只取平台预设（公开只读，仍需登录） */
export function listPresetRoles() {
  return elderGet<AgentRole[]>('/api/roles/presets')
}

/** 新建**原创**角色。派生角色请用 deriveAgentRole */
export function createAgentRole(data: Partial<AgentRole>) {
  return elderPost<AgentRole>('/api/roles', data)
}

/**
 * 从预设或自己的角色派生一份新的（copy-on-write 物理复制）。
 * inherit 默认 true：复制家属对母版的手动交代，不复制统计值。
 */
export function deriveAgentRole(roleId: number, name: string, inherit = true) {
  return elderPost<AgentRole>(`/api/roles/${roleId}/derive`, { name, inherit })
}

/** 改角色（owner 或管理员）。traits 只传要改的项 */
export function updateAgentRole(roleId: number, data: Partial<AgentRole>) {
  return elderPatch<AgentRole>(`/api/roles/${roleId}`, data)
}

/** 软删角色（画像与审计保留，设备自动解绑）。平台预设不可删，走下架 */
export function deleteAgentRole(roleId: number) {
  return elderDelete(`/api/roles/${roleId}`)
}

/** 还原为预设：清空 traits overlay、恢复 system_prompt/tts_voice，保留画像与示例 */
export function restoreAgentRole(roleId: number) {
  return elderPost<AgentRole>(`/api/roles/${roleId}/restore`, {})
}

/** 编译产物预览：家属可见的"所以机器人现在按什么在说话" */
export function getAgentRolePrompt(roleId: number) {
  return elderGet<{ role_id: number; role_name: string; prompt: string }>(
    `/api/roles/${roleId}/prompt`)
}

/** 安全承诺卡：人话红线列表 + 是否已确认 */
export function getAgentRoleGuardrails(roleId: number) {
  return elderGet<any>(`/api/roles/${roleId}/guardrails`)
}

/** 确认安全承诺卡（红线版本变化后需要重新确认） */
export function ackAgentRoleGuardrails(roleId: number) {
  return elderPost<any>(`/api/roles/${roleId}/guardrails/ack`, {})
}

/** 10 个性格维度的档位定义（含后果说明）。APP 不硬编码档位文案，跟这个接口走 */
export function getAgentRoleTraits() {
  return elderGet<any>('/api/roles/traits')
}

/** 把某个 agent 角色绑定到设备（一台设备一个生效角色） */
export function bindAgentRole(devicePk: number, roleId: number) {
  return elderPost(`/api/devices/${devicePk}/agent`, { role_id: roleId })
}

// ============================================================ 找回密码（邮箱验证码，全流程在 APP 完成）
export interface PasswordResetRequestOut {
  ok: boolean
  /** 重发冷却秒数（默认 60） */
  cooldown_sec: number
  /** 验证码有效时长（秒，默认 15 分钟） */
  expires_in_sec: number
}

/** 验证码校验失败时 detail 的结构（成功时是 { ok, username }） */
export interface PasswordResetFail {
  message: string
  attempts_left: number
}

/**
 * 第一步：提交邮箱，请求发送验证码。
 *
 * ⚠️ 后端对「已注册邮箱」和「未注册邮箱」返回**完全相同**的响应（防账号枚举），
 * 所以这里拿不到任何「邮箱是否存在」的信息，UI 上绝不能出现"该邮箱未注册"。
 * 未登录也要能调用，故 auth: 'optional'。
 */
export function requestPasswordReset(email: string) {
  return elderRequestRaw<PasswordResetRequestOut>(
    '/api/auth/password/reset/request',
    { method: 'POST', data: { email }, auth: false, toast: false },
  )
}

/**
 * 第二步：验证码 + 新密码 -> 重置。
 * 返回原始响应，由页面按状态码说人话（400 要看 attempts_left，429/503 各不相同）。
 */
export function verifyPasswordReset(data: { email: string, code: string, new_password: string }) {
  return elderRequestRaw<PasswordResetFail | { ok: boolean, username: string }>(
    '/api/auth/password/reset/verify',
    { method: 'POST', data, auth: false, toast: false },
  )
}

/** 判断校验响应是不是失败体（失败体里才有 message 字段） */
export function isResetFail(res: ElderRawResult<any>): res is ElderRawResult<PasswordResetFail> {
  return res.statusCode >= 400 && !!res.data && typeof (res.data as PasswordResetFail).message === 'string'
}

// ============================================================ 帮助与反馈（工单）

/** 工单分类：与后端 TicketCategory 保持一致，未知值后端按"其它"处理。
 *  ⚠️ `payment` 的 value 是**落库的短码**，改它会让老工单集体变「未知分类」，
 *  所以只改 label。 */
export const TICKET_CATEGORIES: { value: string, label: string }[] = [
  { value: 'payment', label: '付款/费用' },
  { value: 'device', label: '设备故障' },
  { value: 'reminder', label: '提醒异常' },
  { value: 'account', label: '账号问题' },
  { value: 'other', label: '其它' },
]

export interface SupportTicket {
  id: number
  user_id: number | null
  email: string
  title: string
  content: string
  category: string
  category_text: string
  device_id: string
  status: string
  status_text: string
  admin_reply: string
  created_at: string
  updated_at: string
  replied_at: string | null
}

/**
 * 提交问题。**匿名也能提交**（auth: 'optional'）：忘记密码的人登不进 APP，
 * 工单是他最后的求助通道，所以不能强制登录。登录时带上 token，后端会自动
 * 把这条工单关联到账号，管理端就能看到是谁提的。
 */
export function createSupportTicket(data: {
  email: string
  title: string
  content: string
  category?: string
  device_id?: string
}) {
  return elderRequestRaw<SupportTicket>(
    '/api/support/tickets',
    { method: 'POST', data, auth: 'optional', toast: false },
  )
}

/** 我提交过的问题（需登录，按提交时间倒序） */
export function mySupportTickets(limit = 50) {
  return elderGet<SupportTicket[]>('/api/support/tickets/mine', { limit })
}

// ============================================================ 首页聚合
export interface HomeSummary {
  devices: {
    id: number
    name: string
    online: boolean
    last_seen_at: string | null
    ip: string
    fw_version: string
    device_time: string | null
    agent_name: string | null
    avatar_name: string | null
  }[]
  medicine_today: {
    pending_count: number
    pending_times: string[]
    confirmed_count: number
    total_count: number
  }
}

export function homeSummary() {
  return elderGet<HomeSummary>('/api/home/summary')
}
