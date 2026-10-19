<route lang="json">
{
  "style": { "navigationBarTitleText": "我的" }
}
</route>

<script setup lang="ts">
import { onBackPress, onShow } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'
import {
  changeEmail,
  changePassword,
  createAgentRole,
  deriveAgentRole,
  getMe,
  listPresetRoles,
  listVoices,
  voicePreviewPath,
} from '@/api/elder'
import type { VoiceItem } from '@/api/elder'
import { clearElderToken, getElderBaseUrl, getElderToken } from '@/http/elder'

const account = ref<any>({})
const logged = ref(true)

/* ---------------- 修改密码 ---------------- */
const showPwd = ref(false)
const pwdForm = ref({ old: '', new: '', again: '' })

async function onSubmitPwd() {
  const { old: oldPwd, new: newPwd, again } = pwdForm.value
  if (!oldPwd || !newPwd) {
    uni.showToast({ title: '请填写原密码和新密码', icon: 'none' })
    return
  }
  if (newPwd !== again) {
    uni.showToast({ title: '两次输入的新密码不一致', icon: 'none' })
    return
  }
  if (newPwd.length < 6) {
    uni.showToast({ title: '新密码至少 6 位', icon: 'none' })
    return
  }
  try {
    await changePassword({ old_password: oldPwd, new_password: newPwd })
    uni.showToast({ title: '密码已修改，请重新登录', icon: 'success' })
    showPwd.value = false
    setTimeout(() => {
      clearElderToken()
      uni.reLaunch({ url: '/pages/login/index' })
    }, 800)
  }
  catch { /* 已提示 */ }
}

/* ---------------- 更换邮箱 ---------------- */
const showEmail = ref(false)
const email = ref('')

async function onSubmitEmail() {
  const v = email.value.trim()
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) {
    uni.showToast({ title: '邮箱格式不正确', icon: 'none' })
    return
  }
  try {
    account.value = await changeEmail(v)
    uni.showToast({ title: '邮箱已更新', icon: 'success' })
    showEmail.value = false
  }
  catch { /* 已提示 */ }
}

/* ---------------- 创建 / 派生角色 ---------------- */
// ⚠️ 角色接口已从 /api/providers 拆到 /api/roles（后端 A1+A2）。
// 继续走老接口会被 422 拒（/api/providers 只接受带 kind 的平台 AI 配置）。
const showRole = ref(false)
const roleForm = ref({ name: '', description: '', system_prompt: '', tts_voice: '' })
/** 基于哪个平台预设派生；为空 = 原创 */
const roleParent = ref<number | null>(null)
const presetRoles = ref<any[]>([])

/**
 * 「创建方式」下拉的选项。
 *
 * ⚠️ 需求（2026-10-19 用户报「三种方式堆放在上方不容易看出来」）：
 * 原来是把「原创 + 各平台预设」全部平铺成一列卡片堆在表单顶部，
 * 视觉上完全看不出它们是**互斥的三选一**，老人会以为要全填一遍。
 * 改成一个 picker 下拉：默认显示当前选项 + 描述，点开才展开全部。
 */
const sourceIndex = ref(0)
const sourceOptions = computed(() => [
  { id: 0, label: '完全原创', desc: '从零设定一个角色' },
  ...presetRoles.value.map((p: any) => ({
    id: p.id,
    label: p.name,
    desc: p.description || '平台预设',
  })),
])

/** picker 选中项 → roleParent（0 表示原创） */
function onPickSource(e: any) {
  sourceIndex.value = Number(e.detail.value) || 0
  roleParent.value = sourceOptions.value[sourceIndex.value]?.id ?? null
}

/** 当前选中的创建方式（用于弹窗顶部回显） */
const currentSource = computed(() => sourceOptions.value[sourceIndex.value])

/* ---------------- 音色 ---------------- */
/** 音色清单由服务端给（见 api/elder/index.ts 的 listVoices 注释：厂商 id 填错会静默失败） */
const voices = ref<VoiceItem[]>([])
const voiceIndex = ref(0)
const voiceBusy = ref('')

const voiceOptions = computed(() => [
  ...voices.value.map(v => ({ id: v.id, label: v.name })),
  // 服务端声明 allow_custom 才给「其他音色」入口，否则用户填了厂商不认的 id
  // 会静默用到别的音色（他以为换成功了）。
  ...(allowCustomVoice.value ? [{ id: '', label: '其他音色（自己填）' }] : []),
])

const allowCustomVoice = ref(false)
const currentVoice = computed(() => voices.value[voiceIndex.value] || null)

async function loadVoices() {
  try {
    const cat = await listVoices()
    voices.value = cat.voices || []
    allowCustomVoice.value = cat.allow_custom === true
    // 默认选中服务端出厂音色；它在目录里就选中它，否则退回第一项。
    const def = cat.default_voice
    const idx = voices.value.findIndex(v => v.id === def)
    voiceIndex.value = idx >= 0 ? idx : 0
    if (voices.value[voiceIndex.value]) {
      roleForm.value.tts_voice = voices.value[voiceIndex.value].id
    }
  }
  catch {
    // 拉不到就退回「自己填」：静默给空下拉框= 用户以为功能坏了
    voices.value = []
    allowCustomVoice.value = true
  }
}

function onPickVoice(e: any) {
  const i = Number(e.detail.value) || 0
  voiceIndex.value = i
  const picked = voices.value[i]
  roleForm.value.tts_voice = picked ? picked.id : ''
}

/**
 * 试听。
 *
 * ⚠️ 必须先 download 到临时文件再播：直接给 audio context 喂带鉴权的 URL
 * 在 APP 端不带 header（401），H5 端又会被 CORS 挡。
 * downloadFile 能显式带 Authorization，所以走这条路两端都通。
 */
let audioCtx: UniApp.InnerAudioContext | null = null

function playPreview(voiceId: string) {
  if (!voiceId) {
    uni.showToast({ title: '请先选择音色', icon: 'none' })
    return
  }
  if (voiceBusy.value) {
    voiceBusy.value = '' // 再点一次 = 停止
    audioCtx?.stop()
    return
  }
  voiceBusy.value = voiceId
  uni.showLoading({ title: '合成中…', mask: true })
  uni.downloadFile({
    // 用 getElderBaseUrl()而不是自己再写一遍地址：地址是这个项目的
    // 真源（备案后要整体切换），复制第二份就会漏改。
    url: getElderBaseUrl() + voicePreviewPath(voiceId),
    header: { Authorization: `Bearer ${getElderToken()}` },
    timeout: 30000,
    success: (res: any) => {
      uni.hideLoading()
      if (res.statusCode !== 200) {
        voiceBusy.value = ''
        // 503 = 服务端没配 TTS。分诊后给能行动的话，而不是「加载失败」。
        uni.showToast({
          title: res.statusCode === 503 ? '服务未启用语音合成' : '试听失败',
          icon: 'none',
        })
        return
      }
      audioCtx?.destroy()
      audioCtx = uni.createInnerAudioContext()
      audioCtx.src = res.tempFilePath
      audioCtx.onEnded(() => { voiceBusy.value = '' })
      audioCtx.onError(() => {
        voiceBusy.value = ''
        uni.showToast({ title: '无法播放试听', icon: 'none' })
      })
      audioCtx.play()
    },
    fail: () => {
      uni.hideLoading()
      voiceBusy.value = ''
      uni.showToast({ title: '试听请求失败', icon: 'none' })
    },
  })
}

async function onOpenRole() {
  // 派生需要母版列表，所以打开弹窗时才去拉（首页不必为它多打一次请求）
  if (!presetRoles.value.length) {
    presetRoles.value = await listPresetRoles().catch(() => [])
  }
  if (!voices.value.length) {
    await loadVoices()
  }
  roleParent.value = null
  sourceIndex.value = 0
  roleForm.value = { name: '', description: '', system_prompt: '', tts_voice: '' }
  if (voices.value.length) roleForm.value.tts_voice = voices.value[voiceIndex.value]?.id || ''
  showRole.value = true
}

/* ---------------- 安卓返回键 ---------------- */
/**
 * 拦截返回键：弹窗开着时**只关弹窗**，不退出页面、更不退出应用。
 *
 * ⚠️ 需求（2026-10-19 用户报「点返回键直接退出软件」）。
 * 根因：这三个弹窗是自己用 `v-if` + `.mask` 实现的，不是原生页面/组件，
 * 所以 uni 框架**不知道有东西需要关闭**，返回键直接走默认行为 = 退出应用。
 *
 * 用 onBackPress 而不是模板内联 —— 同页 goAvatar 上方写过原因：
 * 模板表达式不会被 uni 插件改写。而 onBackPress 必须由 @dcloudio/uni-app
 * 的生命周期钩子接管，所以放在 script 里是唯一正确形态。
 */
onBackPress((opts) => {
  if (showCache.value) {
    showCache.value = false
    return true
  }
  if (showRole.value) {
    closeRole()
    return true
  }
  if (showPwd.value) {
    showPwd.value = false
    return true
  }
  if (showEmail.value) {
    showEmail.value = false
    return true
  }
  // 没有任何弹窗 ⇒ 交回默认行为（允许退出应用）
  return false
})

/** 关闭弹窗时必须停掉试听，否则弹窗没了声音还在放，很吵 */
function stopPreview() {
  voiceBusy.value = ''
  audioCtx?.stop()
}

/** 统一的关闭入口：关弹窗 + 停试听（点遮罩、点返回键都走它） */
function closeRole() {
  showRole.value = false
  stopPreview()
}

async function onSubmitRole() {
  if (!roleForm.value.name.trim()) {
    uni.showToast({ title: '请填写角色名称', icon: 'none' })
    return
  }
  try {
    if (roleParent.value) {
      // 派生：copy-on-write 物理复制，inherit 默认 true（继承家属对母版的交代）
      await deriveAgentRole(roleParent.value, roleForm.value.name.trim(), true)
      uni.showToast({ title: '已基于平台角色创建', icon: 'success' })
    }
    else {
      await createAgentRole(roleForm.value)
      uni.showToast({ title: '角色已创建', icon: 'success' })
    }
    showRole.value = false
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || '创建失败', icon: 'none' })
  }
}

/* ---------------- 缓存管理 ---------------- */
// ⚠️ 需求（2026-10-19 用户报「点击后直接提示清理缓存」）：
// 点击「缓存管理」**不应该**直接弹确认框，更不该直接清——
// 老人点错了就没法撤销，且他们需要先知道「到底有多少东西可清」。
// 正确形态：点开一个面板 → 看到当前占用 → 在面板上选「清理」或「关闭」。
const showCache = ref(false)
// 初值就是真实值（0 项）而不是「计算中…」——
// 列表行的「可清理 N 项」在 onShow 之前也要能正确显示，
// 写占位文案会出现「点进面板才发现数字变了」的错位感。
const cacheInfo = ref({ total: '0 项', count: 0, items: [] as string[] })

/** 计算可清理的缓存量（保留登录态相关的 key）。
 *
 *⚠️ uni.getStorageInfoSync 只给**键值条数**，不给字节数
 *（currentSize/limitSize 在 H5 恒为 0，APP 端也是 0）。
 * 所以别去读 currentSize 装样子——那会显示「0 B」，
 * 用户会以为「没什么可清的」，从而永远不清。
 * 真正有意义的是「有多少个键」以及**哪些键**，尤其要说明
 * 「登录态不参与清理」，否则用户会担心把账号清掉。
 */
const KEEP_KEYS = ['elder_token', 'elder_user']

function calcCache() {
  const info = uni.getStorageInfoSync()
  const keys = info.keys || []
  const items = keys.filter(k => !KEEP_KEYS.includes(k))
  cacheInfo.value = {
    total: `${items.length} 项`,
    count: items.length,
    items,
  }
}

function openCache() {
  calcCache()
  showCache.value = true
}

async function doClearCache() {
  try {
    for (const k of cacheInfo.value.items) uni.removeStorageSync(k)
    calcCache()
    uni.showToast({ title: '缓存已清理', icon: 'success' })
  }
  catch {
    uni.showToast({ title: '清理失败', icon: 'none' })
  }
}

/* ---------------- 页面跳转 ---------------- */
// ⚠️⚠️ 跳转**必须**写在 script 里的具名函数里，不能写成模板内联的
// `@click="uni.navigateTo(...)"`（2026-10-19 用户报「系统形象库/隐私政策/
// 用户协议点不动」三个症状，同一根因）。
//
// 原因：`uni.*` 是@dcloudio/uni-app 插件在**构建期**注入的，
// 它认的是 JS 源码里的调用点。写在模板表达式里时，插件不参与改写，
// 产物里原样变成 `o.uni.navigateTo(...)`——`o` 是 setup 上下文的解构对象，
// 它身上的 `uni` 根本不是那个 API 对象 ⇒ 调用即抛 TypeError。
//
// 为什么「点不动」而不是「报错」：异常发生在 Vue 的事件处理器里，
// 没有 try/catch 也没有全局 errorHandler ⇒ 被静默吞掉。
// 加上**构建不报错**（模板表达式语法完全合法），
// 于是症状是「构建成功 + 点击毫无反应」，极难归因。
//
// 判据：产物里必须出现 `l.navigateTo(` 这种**短变量名**（插件改写后的形态），
// 出现 `o.uni.navigateTo(` 就是没被改写，即坏掉了。
function goAvatar() {
  // devicePk=0：形象库是平台级资源，与具体设备无关。
  // 传 0 而不是不带参数——onLoad 里读 query.devicePk 时 undefined 会走进错分支。
  uni.navigateTo({ url: '/pages/elder/avatar?devicePk=0' })
}

function goPrivacy() {
  uni.navigateTo({ url: '/pages/login/privacy-policy-zh' })
}

function goAgreement() {
  uni.navigateTo({ url: '/pages/login/user-agreement-zh' })
}

function logout() {
  uni.showModal({
    title: '退出登录',
    content: '确定退出当前账号？',
    success: (res) => {
      if (!res.confirm) return
      clearElderToken()
      uni.reLaunch({ url: '/pages/login/index' })
    },
  })
}

function goLogin() {
  uni.reLaunch({ url: '/pages/login/index' })
}

async function refresh() {
  if (!getElderToken()) {
    logged.value = false
    return
  }
  logged.value = true
  try {
    account.value = await getMe()
  }
  catch { /* 已提示 */ }
}

onShow(() => { refresh() })
</script>

<template>
  <view class="page">
    <!-- 未登录 -->
    <view v-if="!logged" class="card empty">
      <view class="empty-title">
        未登录
      </view>
      <view class="empty-sub">
        登录后可管理设备与提醒
      </view>
      <button class="btn" @click="goLogin">
        去登录
      </button>
    </view>

    <template v-else>
      <!-- 账户 -->
      <view class="card account">
        <view class="avatar">
          {{ (account.nickname || account.username || '?').slice(0, 1) }}
        </view>
        <view class="account-info">
          <text class="account-name">{{ account.nickname || account.username || '—' }}</text>
          <text class="account-sub">账号 {{ account.username || '—' }}{{ account.email ? ` · ${account.email}` : '' }}</text>
        </view>
      </view>

      <!-- 功能列表 -->
      <view class="card menu">
        <view class="menu-item" @click="showPwd = true">
          <text class="menu-label">修改密码</text>
          <text class="menu-arrow">›</text>
        </view>
        <view class="menu-item" @click="showEmail = true; email = account.email || ''">
          <text class="menu-label">更换绑定邮箱</text>
          <text class="menu-value">{{ account.email || '未绑定' }}</text>
          <text class="menu-arrow">›</text>
        </view>
        <view class="menu-item" @click="onOpenRole">
          <text class="menu-label">创建我的角色</text>
          <text class="menu-arrow">›</text>
        </view>
        <view class="menu-item" @click="goAvatar">
          <text class="menu-label">系统形象库</text>
          <text class="menu-arrow">›</text>
        </view>
        <view class="menu-item" @click="openCache">
          <text class="menu-label">缓存管理</text>
          <text class="menu-value">可清理 {{ cacheInfo.count }} 项</text>
          <text class="menu-arrow">›</text>
        </view>
      </view>

      <!-- 关于 -->
      <view class="card menu">
        <view class="menu-item" @click="goPrivacy">
          <text class="menu-label">隐私政策</text>
          <text class="menu-arrow">›</text>
        </view>
        <view class="menu-item" @click="goAgreement">
          <text class="menu-label">用户协议</text>
          <text class="menu-arrow">›</text>
        </view>
        <view class="about">
          <text class="about-name">老人陪伴助手</text>
          <text class="about-sub">版本 1.0.0</text>
        </view>
      </view>

      <button class="btn logout" @click="logout">
        退出登录
      </button>
    </template>

    <!-- 修改密码 -->
    <view v-if="showPwd" class="mask" @click="showPwd = false">
      <view class="modal" @click.stop>
        <view class="title">
          修改密码
        </view>
        <input v-model="pwdForm.old" class="input" password placeholder="原密码" />
        <input v-model="pwdForm.new" class="input" password placeholder="新密码（至少 6 位）" />
        <input v-model="pwdForm.again" class="input" password placeholder="确认新密码" />
        <button class="btn" @click="onSubmitPwd">
          确认修改
        </button>
      </view>
    </view>

    <!-- 更换邮箱 -->
    <view v-if="showEmail" class="mask" @click="showEmail = false">
      <view class="modal" @click.stop>
        <view class="title">
          更换绑定邮箱
        </view>
        <input v-model="email" class="input" placeholder="请输入新邮箱" />
        <button class="btn" @click="onSubmitEmail">
          确认更换
        </button>
      </view>
    </view>

    <!-- 创建 / 派生角色 -->
    <view v-if="showRole" class="mask" @click="closeRole">
      <view class="modal" @click.stop>
        <view class="title">
          创建我的角色
        </view>

        <!-- 创建方式：改成一个下拉（picker）。
             ⚠️ 原来是「原创 + 各平台预设」平铺成一列卡片堆在表单顶部，
             老人看不出它们是**互斥三选一**，会以为要一项项点过去。
             现在默认只显示一行「当前选项 + 说明」，点开才展开全部。 -->
        <view class="group-label">创建方式</view>
        <picker mode="selector" :range="sourceOptions" range-key="label"
                :value="sourceIndex" @change="onPickSource">
          <view class="select">
            <view class="select-main">
              <text class="select-label">{{ currentSource?.label || '完全原创' }}</text>
              <text class="select-desc">{{ currentSource?.desc }}</text>
            </view>
            <text class="menu-arrow">⌄</text>
          </view>
        </picker>

        <input v-model="roleForm.name" class="input" placeholder="角色名称，如：贴心小棉袄" />

        <!-- 派生时这两项由母版复制过来，不需要家属再填一遍 -->
        <template v-if="roleParent === null">
          <input v-model="roleForm.description" class="input" placeholder="一句话描述（可选）" />
          <textarea v-model="roleForm.system_prompt" class="input textarea" placeholder="角色设定（提示词），决定它怎么说话、怎么关心老人" />

          <!-- 音色：清单由服务端给 + 试听（2026-10-19 用户报「没有任何音色供选择」）。
               原来这是个**自由输入框**，用户不知道该填什么厂商 id，
               填错更糟——厂商会静默换成别的音色，他以为换成功了。 -->
          <view class="group-label">音色（可选）</view>
          <picker mode="selector" :range="voiceOptions" range-key="label"
                  :value="voiceIndex" @change="onPickVoice">
            <view class="select">
              <view class="select-main">
                <text class="select-label">{{ currentVoice?.name || '默认音色' }}</text>
                <text class="select-desc">{{ currentVoice?.scene }}</text>
              </view>
              <text class="menu-arrow">⌄</text>
            </view>
          </picker>
          <!-- 选中项的详细说明 + 试听按钮。
               说明文字必须给：光有「晓晓（女声· 温柔亲和）」用户还是不知道
               该选哪个，得知道「日常陪伴、闲聊、提醒播报——首选」。 -->
          <view v-if="currentVoice" class="voice-note">
            <text class="voice-note-text">{{ currentVoice.note }}</text>
          </view>
          <button class="btn ghost play" @click="playPreview(currentVoice?.id || '')">
            {{ voiceBusy ? '■ 停止试听' : '▶ 试听这个音色' }}
          </button>

          <!-- allow_custom 时才给自由输入：厂商 id 填错会静默换音色，
               不让用户自己填就别开这个口子。 -->
          <template v-if="allowCustomVoice">
            <input v-model="roleForm.tts_voice" class="input" placeholder="音色标识（可选，不填用默认）" />
          </template>
        </template>
        <template v-else>
          <view class="tip left">
            角色设定、形象与音色将从「{{ currentSource?.label }}」复制一份，可稍后在角色详情里修改
          </view>
        </template>

        <view class="tip left">
          安全底线（不给医疗建议、不碰钱与隐私等）由平台统一设定，任何角色都不可关闭
        </view>
        <button class="btn" @click="onSubmitRole">
          {{ roleParent === null ? '创建角色' : '基于平台角色创建' }}
        </button>
      </view>
    </view>

    <!-- 缓存管理 -->
    <view v-if="showCache" class="mask" @click="showCache = false">
      <view class="modal" @click.stop>
        <view class="title">
          缓存管理
        </view>
        <view class="cache-num">
          可清理 <text class="cache-count">{{ cacheInfo.count }}</text> 项
        </view>
        <view class="tip left">
          这些是留在手机上的临时数据（列表缓存、图片缓存等），清掉不影响账号、角色和设备。
        </view>
        <view v-if="cacheInfo.items.length" class="cache-list">
          <text class="cache-item" v-for="k in cacheInfo.items" :key="k">{{ k }}</text>
        </view>
        <view v-else class="tip left">
          目前没有可清理的内容。
        </view>
        <view class="cache-keep">
          登录状态（elder_token / elder_user）不参与清理，避免把用户踢下线。
        </view>
        <button class="btn" @click="doClearCache">清理缓存</button>
        <button class="btn ghost" @click="showCache = false">关闭</button>
      </view>
    </view>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; padding-bottom: 40rpx; }
.card { background: #fff; border-radius: 20rpx; padding: 28rpx; margin-bottom: 24rpx; }
.row { display: flex; flex-direction: row; align-items: center; }
.between { justify-content: space-between; }
.sub { font-size: 24rpx; color: #8a8f99; margin-top: 8rpx; display: block; }
.group-title { font-size: 30rpx; font-weight: 600; }

/* 账户 */
.account { display: flex; flex-direction: row; align-items: center; }
.avatar { width: 110rpx; height: 110rpx; border-radius: 50%; background: #e8590c; color: #fff; font-size: 48rpx; font-weight: 700; display: flex; align-items: center; justify-content: center; }
.account-info { margin-left: 24rpx; }
.account-name { font-size: 34rpx; font-weight: 700; display: block; }
.account-sub { font-size: 24rpx; color: #8a8f99; display: block; margin-top: 8rpx; }

/* 菜单 */
.menu { padding: 0 28rpx; }
.menu-item { display: flex; flex-direction: row; align-items: center; padding: 28rpx 0; border-bottom: 1rpx solid #f1f3f6; }
.menu-item:last-child { border-bottom: none; }
.menu-label { flex: 1; font-size: 30rpx; color: #1f2937; }
.menu-value { font-size: 26rpx; color: #8a8f99; margin-right: 12rpx; }
.menu-arrow { font-size: 32rpx; color: #c8cdd4; }
.about { text-align: center; padding: 32rpx 0 8rpx; }
.about-name { display: block; font-size: 30rpx; font-weight: 600; }
.about-sub { display: block; font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; }

.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 28rpx; padding: 18rpx 0; margin-top: 8rpx; }
.btn.small { padding: 14rpx 40rpx; font-size: 26rpx; margin-top: 16rpx; }
.btn.ghost { background: #fff; color: #e8590c; border: 1rpx solid #e8590c; margin-top: 16rpx; }
.btn.logout { background: #fff; color: #dc2626; border: 1rpx solid #f3c7c7; margin-top: 8rpx; }
.ops { margin-top: 0; }

.tip { font-size: 24rpx; color: #8a8f99; text-align: center; padding: 32rpx 0; }
.empty { text-align: center; padding: 60rpx 28rpx; }
.empty-title { font-size: 32rpx; font-weight: 600; }
.empty-sub { font-size: 26rpx; color: #8a8f99; margin: 16rpx 0 28rpx; }

.mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 99; }
.modal { width: 86%; max-height: 80vh; overflow-y: auto; background: #fff; border-radius: 20rpx; padding: 32rpx; }
.title { font-size: 32rpx; font-weight: 600; margin-bottom: 16rpx; }
.input { border: 1rpx solid #e3e8ef; border-radius: 10rpx; padding: 18rpx; margin-bottom: 16rpx; font-size: 28rpx; background: #fff; width: auto; }
.textarea { width: auto; height: 160rpx; line-height: 1.5; }
.tip.left { text-align: left; padding: 16rpx 0 0; }

/* 表单分组标题（创建方式 / 音色） */
.group-label { font-size: 26rpx; color: #6b7280; margin-bottom: 10rpx; }

/* picker 选择框。
   ⚠️ 高度不能靠 padding 撑：APP 端 <picker> 里包的是原生控件，
   内容盒比文字行高矮时文字会被裁掉上半截（登录页踩过一次）。
   这里给显式 height + line-height。 */
.select {
  display: flex; flex-direction: row; align-items: center;
  height: 88rpx; line-height: 88rpx;
  border: 1rpx solid #e3e8ef; border-radius: 10rpx;
  padding: 0 18rpx; margin-bottom: 16rpx; background: #fff;
}
.select-main { flex: 1; overflow: hidden; }
.select-label { font-size: 28rpx; color: #1f2937; display: block; }
.select-desc {
  font-size: 22rpx; color: #8a8f99; display: block;
  line-height: 1.3; margin-top: 2rpx;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 音色说明 + 试听 */
.voice-note { margin: -6rpx 0 12rpx; }
.voice-note-text { font-size: 24rpx; color: #6b7280; line-height: 1.5; }
.btn.play { margin-top: 0; margin-bottom: 16rpx; }

/* 缓存管理面板 */
.cache-num { font-size: 30rpx; color: #1f2937; margin-bottom: 8rpx; }
.cache-count { font-size: 44rpx; font-weight: 700; color: #e8590c; }
.cache-list {
  margin: 16rpx 0; padding: 16rpx; max-height: 220rpx; overflow-y: auto;
  background: #f7f8fa; border-radius: 10rpx;
}
.cache-item {
  display: block; font-size: 22rpx; color: #6b7280;
  line-height: 1.8; word-break: break-all;
}
.cache-keep {
  margin: 12rpx 0 4rpx; padding: 12rpx 16rpx;
  font-size: 22rpx; color: #8a8f99; line-height: 1.5;
  background: #fff8f0; border-radius: 8rpx;
}
</style>
