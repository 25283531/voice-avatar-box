<route lang="json">
{
  "style": { "navigationBarTitleText": "我的" }
}
</route>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import {
  cancelMembershipOrder,
  changeEmail,
  changePassword,
  createAgentRole,
  deriveAgentRole,
  listPresetRoles,
  createMembershipOrder,
  genOrderCode,
  getMe,
  getMembership,
  getPaymentInfo,
  listMembershipPlans,
  myMembershipOrders,
  notifyMembershipOrder,
} from '@/api/elder'
import type { MembershipInfo, MembershipOrder, MembershipPlan, PaymentInfo } from '@/api/elder'
import { clearElderToken, getElderToken } from '@/http/elder'

const account = ref<any>({})
const membership = ref<MembershipInfo | null>(null)
const plans = ref<MembershipPlan[]>([])
const logged = ref(true)

/* ---------------- 会员（人工核销下单） ---------------- */
const showPlans = ref(false)
const showOrder = ref(false)
const order = ref<MembershipOrder | null>(null)
const payment = ref<PaymentInfo | null>(null)
const orders = ref<MembershipOrder[]>([])
const submitting = ref(false)

async function loadMembership() {
  try {
    membership.value = await getMembership()
  }
  catch { /* 会员模块未部署 */ }
  try {
    plans.value = await listMembershipPlans()
  }
  catch { /* 忽略 */ }
  try {
    payment.value = await getPaymentInfo()
  }
  catch { /* 管理员还没配置收款方式 */ }
  try {
    orders.value = await myMembershipOrders()
  }
  catch { /* 忽略 */ }
}

/**
 * 下单：订单号由 APP 本地生成（4 位），若 48 小时内已被占用（409）则重新生成再试。
 */
async function onActivate(p: MembershipPlan) {
  if (submitting.value) return
  submitting.value = true
  try {
    let lastErr: any = null
    for (let i = 0; i < 4; i++) {
      try {
        const o = await createMembershipOrder(genOrderCode(), p.plan)
        order.value = o
        showPlans.value = false
        showOrder.value = true
        await loadMembership()
        return
      }
      catch (e: any) {
        // 订单号撞号 → 换一个再来
        if (String(e?.message || '').includes('409') || String(e?.message || '').includes('已存在')) {
          lastErr = e
          continue
        }
        lastErr = e
        break
      }
    }
    uni.showToast({ title: lastErr?.message ? String(lastErr.message).slice(0, 30) : '下单失败，请重试', icon: 'none' })
  }
  finally {
    submitting.value = false
  }
}

/** 我已付款 → 通知管理员核对 */
async function onNotifyPaid() {
  if (!order.value) return
  uni.showModal({
    title: '通知管理员',
    content: `确认已按金额 ￥${order.value.amount_yuan} 完成付款？管理员核对到账后会自动开通会员。`,
    success: async (res) => {
      if (!res.confirm) return
      try {
        order.value = await notifyMembershipOrder(order.value!.code)
        uni.showToast({ title: '已通知管理员，请耐心等待', icon: 'success' })
        await loadMembership()
      }
      catch { /* 已提示 */ }
    },
  })
}

async function onCancelOrder() {
  if (!order.value) return
  uni.showModal({
    title: '放弃订单',
    content: '放弃后需要重新下单，确定吗？',
    success: async (res) => {
      if (!res.confirm) return
      try {
        await cancelMembershipOrder(order.value!.code)
        showOrder.value = false
        order.value = null
        uni.showToast({ title: '已放弃订单', icon: 'none' })
        await loadMembership()
      }
      catch { /* 已提示 */ }
    },
  })
}

function statusText(s: string) {
  return ({
    pending: '等待付款',
    notified: '待管理员核对',
    confirmed: '已开通',
    rejected: '已被驳回',
    cancelled: '已放弃',
    expired: '已过期',
  } as Record<string, string>)[s] || s
}

async function copyCode() {
  if (!order.value) return
  uni.setClipboardData({ data: order.value.code, success: () => uni.showToast({ title: '订单号已复制', icon: 'none' }) })
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

        <!-- 进行中的订单 -->
        <view v-for="o in orders.filter(x => x.status === 'pending' || x.status === 'notified')" :key="o.code" class="order-item" @click="order = o; showOrder = true">
          <view class="row between">
            <text class="order-item-code">{{ o.code }}</text>
            <text class="order-item-status">{{ statusText(o.status) }}</text>
          </view>
          <view class="order-item-sub">
            ￥{{ o.amount_yuan }} · {{ o.plan_name || o.plan_code }}
          </view>
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
        <view class="tip left">
          下单后会生成一个 4 位订单号和专属金额，请按该金额付款，付款后点「我已付款」通知管理员。
        </view>
        <button class="btn ghost" @click="showPlans = false">
          关闭
        </button>
      </view>
    </view>

    <!-- 订单详情 / 付款指引 -->
    <view v-if="showOrder && order" class="mask" @click="showOrder = false">
      <view class="modal" @click.stop>
        <view class="title">
          请按下方金额付款
        </view>

        <view class="order-code" @click="copyCode">
          <text class="order-code-label">订单号</text>
          <text class="order-code-value">{{ order.code }}</text>
          <text class="order-code-tip">点击复制</text>
        </view>

        <view class="order-amount">
          <text class="order-amount-num">￥{{ order.amount_yuan }}</text>
          <text class="order-amount-tip">请务必按此金额付款，便于管理员核对</text>
        </view>

        <view v-if="payment" class="pay-info">
          <view class="pay-row">
            <text class="pay-label">收款方式</text>
            <text class="pay-value">{{ payment.channel }}</text>
          </view>
          <view class="pay-row">
            <text class="pay-label">收款账号</text>
            <text class="pay-value" @click="uni.setClipboardData({ data: payment.account })">{{ payment.account }}</text>
          </view>
          <view v-if="payment.account_name" class="pay-row">
            <text class="pay-label">收款人</text>
            <text class="pay-value">{{ payment.account_name }}</text>
          </view>
          <view v-if="payment.qrcode_url" class="pay-qr">
            <image :src="payment.qrcode_url" mode="widthFix" />
          </view>
          <view v-if="payment.note" class="pay-note">
            {{ payment.note }}
          </view>
        </view>
        <view v-else class="tip">
          管理员尚未配置收款方式，请联系客服
        </view>

        <view class="order-status">
          状态：{{ statusText(order.status) }} · 有效期至 {{ order.expires_at.replace('T', ' ').slice(0, 16) }}
        </view>

        <button v-if="order.status === 'pending'" class="btn" :disabled="submitting" @click="onNotifyPaid">
          我已付款，通知管理员
        </button>
        <view v-else-if="order.status === 'notified'" class="waiting">
          已通知管理员，核对到账后会自动开通，请稍候…
        </view>
        <button v-if="order.status === 'pending' || order.status === 'notified'" class="btn ghost" @click="onCancelOrder">
          放弃订单
        </button>
        <button class="btn ghost" @click="showOrder = false">
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
.tip.left { text-align: left; padding: 16rpx 0 0; }

/* 订单 */
.order-code { background: #fff7ed; border: 2rpx dashed #e8590c; border-radius: 12rpx; padding: 24rpx; text-align: center; margin-bottom: 20rpx; }
.order-code-label { display: block; font-size: 24rpx; color: #a16207; }
.order-code-value { display: block; font-size: 60rpx; font-weight: 800; letter-spacing: 8rpx; color: #9a3412; margin: 8rpx 0; }
.order-code-tip { display: block; font-size: 22rpx; color: #a16207; }
.order-amount { text-align: center; margin-bottom: 24rpx; }
.order-amount-num { display: block; font-size: 56rpx; font-weight: 800; color: #e8590c; }
.order-amount-tip { display: block; font-size: 24rpx; color: #8a8f99; margin-top: 8rpx; }
.pay-info { border: 1rpx solid #e3e8ef; border-radius: 12rpx; padding: 20rpx; margin-bottom: 20rpx; }
.pay-row { display: flex; flex-direction: row; justify-content: space-between; padding: 10rpx 0; }
.pay-label { font-size: 26rpx; color: #8a8f99; }
.pay-value { font-size: 26rpx; color: #1f2937; font-weight: 600; }
.pay-qr { margin: 16rpx 0; }
.pay-note { font-size: 24rpx; color: #8a8f99; line-height: 1.6; }
.order-status { font-size: 24rpx; color: #8a8f99; text-align: center; margin-bottom: 16rpx; }
.waiting { font-size: 26rpx; color: #16a34a; text-align: center; padding: 20rpx 0; }
.order-item { border-top: 1rpx solid #f1f3f6; padding: 20rpx 0 0; margin-top: 16rpx; }
.order-item-code { font-size: 34rpx; font-weight: 800; letter-spacing: 4rpx; color: #9a3412; }
.order-item-status { font-size: 24rpx; color: #e8590c; }
.order-item-sub { font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; }
</style>
