<route lang="json">
{
  "style": { "navigationBarTitleText": "老人陪伴助手" }
}
</route>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import { bindDevice, elderLogin, listDevices } from '@/api/elder'
import type { Device } from '@/api/elder'
import { getElderBaseUrl, getElderToken, setElderBaseUrl, setElderToken, setElderUser } from '@/http/elder'

const devices = ref<Device[]>([])
const loading = ref(false)
const showLogin = ref(false)
const showBind = ref(false)

const loginForm = ref({ username: 'admin', password: 'admin123' })
const bindForm = ref({ code: '', alias: '' })
const baseUrl = ref(getElderBaseUrl())

async function doLogin() {
  try {
    const r = await elderLogin(loginForm.value.username, loginForm.value.password)
    setElderToken(r.access_token)
    setElderUser({ id: r.user_id, username: r.username, is_admin: r.is_admin })
    showLogin.value = false
    uni.showToast({ title: '登录成功', icon: 'success' })
    await refresh()
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || '登录失败', icon: 'none' })
  }
}

function saveBaseUrl() {
  setElderBaseUrl(baseUrl.value)
  uni.showToast({ title: '服务地址已保存', icon: 'success' })
}

async function refresh() {
  if (!getElderToken()) {
    showLogin.value = true
    return
  }
  loading.value = true
  try {
    devices.value = await listDevices()
  }
  catch { /* 请求层已提示 */ }
  finally {
    loading.value = false
  }
}

async function doBind() {
  const code = bindForm.value.code.replace(/[^0-9a-zA-Z]/g, '').toUpperCase()
  if (code.length !== 13) {
    uni.showToast({ title: '激活码应为 13 位（1 字母 + 12 数字）', icon: 'none' })
    return
  }
  try {
    const r = await bindDevice({ code, alias: bindForm.value.alias })
    uni.showToast({ title: `已绑定 ${r.name}`, icon: 'success' })
    showBind.value = false
    bindForm.value = { code: '', alias: '' }
    await refresh()
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || '绑定失败', icon: 'none' })
  }
}

function goto(path: string, devicePk: number, name: string) {
  uni.navigateTo({ url: `${path}?devicePk=${devicePk}&name=${encodeURIComponent(name)}` })
}

onShow(() => { refresh() })
</script>

<template>
  <view class="page">
    <!-- 登录 -->
    <view v-if="showLogin" class="card">
      <view class="title">登录业务后端</view>
      <input v-model="loginForm.username" class="input" placeholder="用户名" />
      <input v-model="loginForm.password" class="input" password placeholder="密码" />
      <input v-model="baseUrl" class="input" placeholder="服务地址 http://ip:8080" />
      <button class="btn ghost" @click="saveBaseUrl">保存服务地址</button>
      <button class="btn" @click="doLogin">登 录</button>
      <view class="tip">首次部署默认 admin / admin123，登录后请立即修改</view>
    </view>

    <!-- 设备列表 -->
    <template v-else>
      <view class="row between">
        <view class="title">我的设备</view>
        <view class="row">
          <text class="link" @click="showBind = true">+ 绑定</text>
          <text class="link" @click="refresh">刷新</text>
        </view>
      </view>

      <view v-if="loading" class="tip">加载中…</view>
      <view v-if="!loading && devices.length === 0" class="tip">还没有设备，点右上角「绑定」输入设备屏幕上显示的激活码</view>

      <view v-for="d in devices" :key="d.id" class="card">
        <view class="row between">
          <text class="name">{{ d.name }}</text>
          <text :class="['tag', d.online ? 'ok' : '']">{{ d.online ? '在线' : '离线' }}</text>
        </view>
        <view class="sub">标识 {{ d.device_id }}</view>
        <view class="sub">最近在线 {{ d.last_seen_at ? d.last_seen_at.replace('T', ' ').slice(0, 16) : '—' }}</view>
        <view class="row ops">
          <button class="btn small" @click="goto('/pages/elder/reminder', d.id, d.name)">提醒</button>
          <button class="btn small" @click="goto('/pages/elder/avatar', d.id, d.name)">形象</button>
          <button class="btn small" @click="goto('/pages/elder/stats', d.id, d.name)">统计</button>
        </view>
      </view>
    </template>

    <!-- 绑定弹层 -->
    <view v-if="showBind" class="mask" @click="showBind = false">
      <view class="modal" @click.stop>
        <view class="title">输入设备激活码</view>
        <view class="tip">在设备屏幕上可以看到形如 A-3829-4471-0056 的激活码，10 分钟内有效</view>
        <input v-model="bindForm.code" class="input big" placeholder="A-3829-4471-0056" />
        <input v-model="bindForm.alias" class="input" placeholder="给设备起个名字（可选）" />
        <button class="btn" @click="doBind">绑定设备</button>
      </view>
    </view>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; }
.card { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; }
.title { font-size: 32rpx; font-weight: 600; margin-bottom: 16rpx; }
.name { font-size: 30rpx; font-weight: 600; }
.sub { font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; }
.tip { font-size: 24rpx; color: #8a8f99; margin: 12rpx 0; }
.row { display: flex; flex-direction: row; align-items: center; }
.between { justify-content: space-between; }
.ops { margin-top: 16rpx; flex-wrap: wrap; }
.input { border: 1rpx solid #e3e8ef; border-radius: 10rpx; padding: 18rpx; margin-bottom: 16rpx; font-size: 28rpx; background: #fff; }
.input.big { font-size: 36rpx; letter-spacing: 2rpx; text-align: center; }
.btn { background: #2563eb; color: #fff; border-radius: 10rpx; font-size: 28rpx; padding: 18rpx 0; margin-top: 8rpx; }
.btn.ghost { background: #fff; color: #2563eb; border: 1rpx solid #2563eb; }
.btn.small { flex: 1; margin-right: 12rpx; padding: 12rpx 0; font-size: 26rpx; }
.tag { font-size: 22rpx; color: #8a8f99; background: #f1f3f6; border-radius: 20rpx; padding: 4rpx 16rpx; }
.tag.ok { color: #16a34a; background: #e8f7ee; }
.link { color: #2563eb; font-size: 26rpx; margin-left: 24rpx; }
.mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; }
.modal { width: 86%; background: #fff; border-radius: 20rpx; padding: 32rpx; }
</style>
