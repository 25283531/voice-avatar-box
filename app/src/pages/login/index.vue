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

      <view class="tip">
        忘记密码可在「我的 → 修改密码」处理，或联系管理员重置
      </view>
    </view>
  </view>
</template>

<style scoped>
.page { padding: 48rpx 40rpx; background: #f5f7fa; min-height: 100vh; }
.brand { text-align: center; margin-bottom: 48rpx; }
.logo { width: 150rpx; height: 150rpx; border-radius: 32rpx; }
.brand-name { font-size: 44rpx; font-weight: 700; margin-top: 20rpx; }
.brand-sub { font-size: 26rpx; color: #8a8f99; margin-top: 10rpx; }
.card { background: #fff; border-radius: 24rpx; padding: 36rpx 32rpx; }
.tabs { display: flex; margin-bottom: 24rpx; }
.tab { flex: 1; text-align: center; padding: 16rpx 0; font-size: 30rpx; color: #6b7280; }
.tab.on { color: #e8590c; font-weight: 700; border-bottom: 4rpx solid #e8590c; }
.input { border: 1rpx solid #e3e8ef; border-radius: 12rpx; padding: 22rpx; margin-bottom: 20rpx; font-size: 30rpx; background: #fff; width: auto; }
.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 32rpx; padding: 22rpx 0; font-weight: 600; }
.btn[disabled] { background: #f0b48a; }
.agree { display: flex; flex-direction: row; align-items: center; flex-wrap: wrap; margin-top: 24rpx; font-size: 24rpx; color: #6b7280; }
.checkbox { width: 32rpx; height: 32rpx; border: 2rpx solid #c8cdd4; border-radius: 6rpx; margin-right: 10rpx; }
.checkbox.on { background: #e8590c; border-color: #e8590c; }
.agree-text { color: #6b7280; }
.agree-link { color: #e8590c; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; margin-top: 28rpx; }
</style>
