<route lang="json">
{
  "style": { "navigationBarTitleText": "我的" }
}
</route>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import {
  activateMembership,
  changeEmail,
  changePassword,
  createAgentRole,
  getMe,
  getMembership,
  listMembershipPlans,
} from '@/api/elder'
import type { MembershipInfo, MembershipPlan } from '@/api/elder'
import { clearElderToken, getElderToken } from '@/http/elder'

const account = ref<any>({})
const membership = ref<MembershipInfo | null>(null)
const plans = ref<MembershipPlan[]>([])
const logged = ref(true)

/* ---------------- 会员 ---------------- */
const showPlans = ref(false)

async function loadMembership() {
  try {
    membership.value = await getMembership()
  }
  catch { /* 会员模块未部署 */ }
  try {
    plans.value = await listMembershipPlans()
  }
  catch { /* 忽略 */ }
}

async function onActivate(p: MembershipPlan) {
  uni.showModal({
    title: '确认开通',
    content: `${p.name}（${p.months} 个月）￥${p.price}，确认开通？`,
    success: async (res) => {
      if (!res.confirm) return
      try {
        membership.value = await activateMembership({ plan: p.plan, months: p.months })
        uni.showToast({ title: '开通成功', icon: 'success' })
        showPlans.value = false
      }
      catch { /* 已提示 */ }
    },
  })
}

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

/* ---------------- 创建 agent 角色 ---------------- */
const showRole = ref(false)
const roleForm = ref({ name: '', description: '', system_prompt: '', tts_voice: '' })

async function onSubmitRole() {
  if (!roleForm.value.name.trim()) {
    uni.showToast({ title: '请填写角色名称', icon: 'none' })
    return
  }
  try {
    await createAgentRole(roleForm.value)
    uni.showToast({ title: '角色已创建', icon: 'success' })
    showRole.value = false
    roleForm.value = { name: '', description: '', system_prompt: '', tts_voice: '' }
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
  await loadMembership()
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
        登录后可管理设备、提醒与会员
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

      <!-- 会员 -->
      <view class="card">
        <view class="row between">
          <text class="group-title">会员</text>
          <text class="vip-tag" :class="{ on: membership?.is_vip }">
            {{ membership?.is_vip ? `剩余 ${membership.days_left} 天` : '未开通' }}
          </text>
        </view>
        <view v-if="membership?.is_vip" class="sub">
          {{ membership.expires_at ? `${membership.expires_at.slice(0, 10)} 到期` : '' }}{{ membership.benefits?.length ? ` · ${membership.benefits.join(' / ')}` : '' }}
        </view>
        <view class="row ops">
          <button class="btn small" @click="showPlans = true">
            {{ membership?.is_vip ? '续费会员' : '开通会员' }}
          </button>
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
        <view class="menu-item" @click="showRole = true">
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

    <!-- 会员套餐 -->
    <view v-if="showPlans" class="mask" @click="showPlans = false">
      <view class="modal" @click.stop>
        <view class="title">
          {{ membership?.is_vip ? '续费会员' : '开通会员' }}
        </view>
        <view v-for="p in plans" :key="p.plan" class="plan" @click="onActivate(p)">
          <view class="row between">
            <text class="plan-name">{{ p.name }}</text>
            <text class="plan-price">￥{{ p.price }}</text>
          </view>
          <view class="plan-sub">{{ p.months }} 个月 · {{ (p.benefits || []).join(' / ') }}</view>
        </view>
        <view v-if="!plans.length" class="tip">
          暂无可选套餐
        </view>
        <button class="btn ghost" @click="showPlans = false">
          关闭
        </button>
      </view>
    </view>

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

    <!-- 创建角色 -->
    <view v-if="showRole" class="mask" @click="showRole = false">
      <view class="modal" @click.stop>
        <view class="title">
          创建我的角色
        </view>
        <input v-model="roleForm.name" class="input" placeholder="角色名称，如：贴心小棉袄" />
        <input v-model="roleForm.description" class="input" placeholder="一句话描述（可选）" />
        <textarea v-model="roleForm.system_prompt" class="input textarea" placeholder="角色设定（提示词），决定它怎么说话、怎么关心老人" />
        <input v-model="roleForm.tts_voice" class="input" placeholder="音色（可选）" />
        <button class="btn" @click="onSubmitRole">
          创建角色
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

/* 会员 */
.vip-tag { font-size: 24rpx; color: #8a8f99; background: #f1f3f6; border-radius: 20rpx; padding: 6rpx 18rpx; }
.vip-tag.on { color: #9a3412; background: #ffedd5; font-weight: 600; }

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
.plan { border: 1rpx solid #e3e8ef; border-radius: 12rpx; padding: 20rpx; margin-bottom: 12rpx; }
.plan-name { font-size: 28rpx; font-weight: 600; }
.plan-price { font-size: 30rpx; font-weight: 700; color: #e8590c; }
.plan-sub { font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; }
</style>
