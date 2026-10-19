<route lang="json">
{
  "style": { "navigationBarTitleText": "登录" }
}
</route>

<script setup lang="ts">
import { ref } from 'vue'
import { elderLogin, elderRegister } from '@/api/elder'
import { setElderToken, setElderUser } from '@/http/elder'

const mode = ref<'login' | 'register'>('login')
const form = ref({ username: '', password: '', again: '', nickname: '', email: '' })
const agreed = ref(false)
const busy = ref(false)

function switchMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
}

async function onSubmit() {
  if (busy.value) return
  const { username, password, again, nickname, email } = form.value

  if (!username.trim() || !password) {
    uni.showToast({ title: '请输入账号和密码', icon: 'none' })
    return
  }
  if (mode.value === 'register') {
    if (password !== again) {
      uni.showToast({ title: '两次输入的密码不一致', icon: 'none' })
      return
    }
    if (password.length < 6) {
      uni.showToast({ title: '密码至少 6 位', icon: 'none' })
      return
    }
    if (!agreed.value) {
      uni.showToast({ title: '请先同意用户协议和隐私政策', icon: 'none' })
      return
    }
  }

  busy.value = true
  try {
    if (mode.value === 'register') {
      await elderRegister({
        username: username.trim(),
        password,
        nickname: nickname.trim(),
        email: email.trim(),
      })
      uni.showToast({ title: '注册成功，请登录', icon: 'success' })
      mode.value = 'login'
      form.value.again = ''
      return
    }

    const r = await elderLogin(username.trim(), password)
    setElderToken(r.access_token)
    setElderUser({ id: r.user_id, username: r.username, is_admin: r.is_admin })
    uni.showToast({ title: '登录成功', icon: 'success' })
    setTimeout(() => uni.reLaunch({ url: '/pages/home/index' }), 300)
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || (mode.value === 'login' ? '登录失败' : '注册失败'), icon: 'none' })
  }
  finally {
    busy.value = false
  }
}

function openDoc(url: string) {
  uni.navigateTo({ url })
}
</script>

<template>
  <view class="page">
    <view class="brand">
      <image class="logo" src="/static/icons/192x192.png" mode="aspectFit" />
      <view class="brand-name">
        老人陪伴助手
      </view>
      <view class="brand-sub">
        让关心随时在线
      </view>
    </view>

    <view class="card">
      <view class="tabs">
        <text :class="['tab', mode === 'login' ? 'on' : '']" @click="mode = 'login'">登录</text>
        <text :class="['tab', mode === 'register' ? 'on' : '']" @click="switchMode">注册</text>
      </view>

      <input v-model="form.username" class="input" placeholder="账号" maxlength="32" />
      <input v-model="form.password" class="input" password placeholder="密码" maxlength="32" />
      <template v-if="mode === 'register'">
        <input v-model="form.again" class="input" password placeholder="确认密码" maxlength="32" />
        <input v-model="form.nickname" class="input" placeholder="称呼（可选）" maxlength="32" />
        <input v-model="form.email" class="input" placeholder="邮箱（可选，用于找回密码）" maxlength="64" />
      </template>

      <button class="btn" :disabled="busy" @click="onSubmit">
        {{ busy ? '请稍候…' : (mode === 'login' ? '登 录' : '注 册') }}
      </button>

      <view v-if="mode === 'register'" class="agree">
        <text :class="['checkbox', agreed ? 'on' : '']" @click="agreed = !agreed" />
        <text class="agree-text">我已阅读并同意</text>
        <text class="agree-link" @click="openDoc('/pages/login/user-agreement-zh')">《用户协议》</text>
        <text class="agree-text">和</text>
        <text class="agree-link" @click="openDoc('/pages/login/privacy-policy-zh')">《隐私政策》</text>
      </view>

      <view v-if="mode === 'login'" class="tip">
        忘记密码可在「我的 → 修改密码」处理，或联系管理员重置
      </view>
    </view>
  </view>
</template>

<!--
  一屏显示（2026-10-19 用户实测修复）

  现象：安卓 APP 注册页装不下，底部「注册」按钮和协议勾选要滚动才看得到。

  原因（按 rpx 算的，750rpx = 屏幕宽）：
    原布局注册态总高 ≈ 1326rpx，而典型安卓视口可用高只有 ~1200rpx
    （16:9 屏扣掉状态栏/导航栏），必然溢出。逐项构成：
      page padding 96 + brand 326（logo 150 + 名称 + 副标题 + 48 下边距）
      + card padding 72 + tabs 138 + 5 个输入框 535 + 按钮 89
      + 协议行 56 + 底部提示 62。

  做法：压「非信息」的垂直开销，信息本身不动。
    · brand 是装饰，占了 326rpx 却零信息量 → logo 与间距各砍 ~40%
    · 输入框上下内边距 22→16、间距 20→16（5 个框省 60rpx）
    · card / page 内边距同步收窄
    · 底部「忘记密码」提示**注册态隐藏**：它讲的是登录态的事，
      注册的人此刻根本用不上（登录态仍保留）
    · 协议行仍允许折行（宽屏一行、小屏两行都放得下）

  结果：注册态 ≈ 1047rpx、登录态 ≈ 1140rpx，均在一屏内，且仍保留
  页面滚动作为极小屏（< 5 吋）的兜底——不写 overflow:hidden，
  免得万一装不下时用户连滚动都没得滚。
-->
<style scoped>
.page { padding: 24rpx 32rpx; background: #f5f7fa; min-height: 100vh; box-sizing: border-box; }
.brand { text-align: center; margin-bottom: 24rpx; }
.logo { width: 112rpx; height: 112rpx; border-radius: 26rpx; }
.brand-name { font-size: 40rpx; font-weight: 700; margin-top: 14rpx; }
.brand-sub { font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; }
.card { background: #fff; border-radius: 24rpx; padding: 28rpx; box-sizing: border-box; }
.tabs { display: flex; margin-bottom: 18rpx; }
.tab { flex: 1; text-align: center; padding: 14rpx 0; font-size: 30rpx; color: #6b7280; }
.tab.on { color: #e8590c; font-weight: 700; border-bottom: 4rpx solid #e8590c; }
.input { border: 1rpx solid #e3e8ef; border-radius: 12rpx; padding: 16rpx 22rpx; margin-bottom: 16rpx; font-size: 30rpx; background: #fff; width: auto; box-sizing: border-box; }
.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 32rpx; padding: 18rpx 0; font-weight: 600; margin-top: 4rpx; }
.btn[disabled] { background: #f0b48a; }
.agree { display: flex; flex-direction: row; align-items: center; flex-wrap: wrap; margin-top: 20rpx; font-size: 24rpx; color: #6b7280; line-height: 1.6; }
.checkbox { width: 32rpx; height: 32rpx; border: 2rpx solid #c8cdd4; border-radius: 6rpx; margin-right: 10rpx; }
.checkbox.on { background: #e8590c; border-color: #e8590c; }
.agree-text { color: #6b7280; }
.agree-link { color: #e8590c; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; margin-top: 20rpx; line-height: 1.6; }
</style>
