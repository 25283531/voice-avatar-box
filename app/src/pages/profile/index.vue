<route lang="json">
{
  "style": { "navigationBarTitleText": "我的" }
}
</route>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import {
  changeEmail,
  changePassword,
  createAgentRole,
  deriveAgentRole,
  getMe,
  listPresetRoles,
} from '@/api/elder'
import { clearElderToken, getElderToken } from '@/http/elder'

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

async function onOpenRole() {
  // 派生需要母版列表，所以打开弹窗时才去拉（首页不必为它多打一次请求）
  if (!presetRoles.value.length) {
    presetRoles.value = await listPresetRoles().catch(() => [])
  }
  roleParent.value = null
  roleForm.value = { name: '', description: '', system_prompt: '', tts_voice: '' }
  showRole.value = true
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
async function clearCache() {
  uni.showModal({
    title: '清理缓存',
    content: '将清理本地缓存（不影响账号与设备数据），确定继续？',
    success: async (res) => {
      if (!res.confirm) return
      try {
        const info = uni.getStorageInfoSync()
        const keep = ['elder_token', 'elder_user']
        for (const k of info.keys) {
          if (!keep.includes(k)) uni.removeStorageSync(k)
        }
        uni.showToast({ title: '缓存已清理', icon: 'success' })
      }
      catch {
        uni.showToast({ title: '清理失败', icon: 'none' })
      }
    },
  })
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
        <view class="menu-item" @click="uni.navigateTo({ url: '/pages/elder/avatar?devicePk=0' })">
          <text class="menu-label">系统形象库</text>
          <text class="menu-arrow">›</text>
        </view>
        <view class="menu-item" @click="clearCache">
          <text class="menu-label">缓存管理</text>
          <text class="menu-arrow">›</text>
        </view>
      </view>

      <!-- 关于 -->
      <view class="card menu">
        <view class="menu-item" @click="uni.navigateTo({ url: '/pages/login/privacy-policy-zh' })">
          <text class="menu-label">隐私政策</text>
          <text class="menu-arrow">›</text>
        </view>
        <view class="menu-item" @click="uni.navigateTo({ url: '/pages/login/user-agreement-zh' })">
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
    <view v-if="showRole" class="mask" @click="showRole = false">
      <view class="modal" @click.stop>
        <view class="title">
          创建我的角色
        </view>
        <!-- 基于平台角色派生（PRD Q3：默认继承调整已勾选）。
             派生是 copy-on-write 物理复制：母版之后被改也不影响已派生的角色。 -->
        <view class="group-label">从平台角色开始（可选）</view>
        <view class="pick" @click="roleParent = null">
          <text class="pick-name">完全原创</text>
          <text class="pick-sub">从零设定一个角色</text>
        </view>
        <view v-for="p in presetRoles" :key="`p${p.id}`" class="pick" @click="roleParent = p.id">
          <text class="pick-name">{{ p.name }}</text>
          <text class="pick-sub">{{ p.description || '平台预设' }}</text>
        </view>
        <input v-model="roleForm.name" class="input" placeholder="角色名称，如：贴心小棉袄" />
        <!-- 派生时这两项由母版复制过来，不需要家属再填一遍 -->
        <template v-if="roleParent === null">
          <input v-model="roleForm.description" class="input" placeholder="一句话描述（可选）" />
          <textarea v-model="roleForm.system_prompt" class="input textarea" placeholder="角色设定（提示词），决定它怎么说话、怎么关心老人" />
          <input v-model="roleForm.tts_voice" class="input" placeholder="音色（可选）" />
        </template>
        <view class="tip left">
          安全底线（不给医疗建议、不碰钱与隐私等）由平台统一设定，任何角色都不可关闭
        </view>
        <button class="btn" @click="onSubmitRole">
          {{ roleParent === null ? '创建角色' : '基于平台角色创建' }}
        </button>
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
</style>
