<route lang="json">
{
  "style": { "navigationBarTitleText": "设备" }
}
</route>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import {
  applyAvatar,
  bindAgentRole,
  bindDevice,
  deleteDevice,
  listAgentRoles,
  listAvatars,
  listDevices,
  updateDevice,
} from '@/api/elder'
import type { Avatar, Device } from '@/api/elder'
import { getElderToken } from '@/http/elder'

const devices = ref<Device[]>([])
const avatars = ref<Avatar[]>([])
const roles = ref<any[]>([])
const loading = ref(false)

const showBind = ref(false)
const bindForm = ref({ code: '', alias: '' })

/** 正在配置哪台设备：null=关闭 / 'net'=配网 / 'agent'=绑角色 */
const panel = ref<null | { devicePk: number, mode: 'net' | 'agent' }>(null)

async function refresh() {
  if (!getElderToken()) return
  loading.value = true
  try {
    devices.value = await listDevices()
    avatars.value = await listAvatars().catch(() => [])
    roles.value = await listAgentRoles().catch(() => [])
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
    uni.showToast({ title: `已添加 ${r.name}`, icon: 'success' })
    showBind.value = false
    bindForm.value = { code: '', alias: '' }
    await refresh()
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || '添加失败', icon: 'none' })
  }
}

function onDelete(d: Device) {
  uni.showModal({
    title: '删除设备',
    content: `确定删除「${d.name}」？删除后该设备的提醒也会一并移除。`,
    success: async (res) => {
      if (!res.confirm) return
      try {
        await deleteDevice(d.id)
        uni.showToast({ title: '已删除', icon: 'success' })
        await refresh()
      }
      catch { /* 已提示 */ }
    },
  })
}

function openPanel(d: Device, mode: 'net' | 'agent') {
  panel.value = { devicePk: d.id, mode }
}

function closePanel() {
  panel.value = null
}

/** 选择形象 / agent 角色并绑定到设备 */
async function onPickAvatar(a: Avatar) {
  if (!panel.value) return
  try {
    await applyAvatar(a.id, panel.value.devicePk)
    uni.showToast({ title: `已绑定角色「${a.name}」`, icon: 'success' })
    closePanel()
    await refresh()
  }
  catch { /* 已提示 */ }
}

/** 绑定自建 agent 角色（后端支持时可用） */
async function onPickRole(r: any) {
  if (!panel.value) return
  try {
    await bindAgentRole(panel.value.devicePk, r.id)
    uni.showToast({ title: `已绑定角色「${r.name}」`, icon: 'success' })
    closePanel()
    await refresh()
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || '绑定失败', icon: 'none' })
  }
}

async function onRename(d: Device) {
  uni.showModal({
    title: '重命名设备',
    editable: true,
    placeholderText: d.name,
    success: async (res) => {
      const name = (res.content || '').trim()
      if (!res.confirm || !name) return
      try {
        await updateDevice(d.id, { name })
        await refresh()
      }
      catch { /* 已提示 */ }
    },
  })
}

function currentDevice() {
  return devices.value.find(d => d.id === panel.value?.devicePk) || null
}

onShow(() => { refresh() })
</script>

<template>
  <view class="page">
    <view class="head">
      <view>
        <view class="head-title">
          我的设备
        </view>
        <view class="head-sub">
          共 {{ devices.length }} 台 · 在线 {{ devices.filter(d => d.online).length }} 台
        </view>
      </view>
      <text class="add" @click="showBind = true">+ 添加设备</text>
    </view>

    <view v-if="loading" class="tip">
      加载中…
    </view>
    <view v-else-if="devices.length === 0" class="card empty">
      <view class="empty-title">
        还没有设备
      </view>
      <view class="empty-sub">
        打开设备电源，屏幕上会显示 13 位激活码，点右上角「添加设备」输入即可绑定
      </view>
    </view>

    <view v-for="d in devices" :key="d.id" class="card">
      <view class="row between">
        <text class="dname">{{ d.name }}</text>
        <text :class="['tag', d.online ? 'ok' : '']">{{ d.online ? '在线' : '离线' }}</text>
      </view>
      <view class="sub">标识 {{ d.device_id }}</view>
      <view class="sub">最近在线 {{ d.last_seen_at ? d.last_seen_at.replace('T', ' ').slice(0, 16) : '—' }}</view>
      <view class="sub">固件 {{ d.fw_version || '—' }}{{ d.ip ? ` · ${d.ip}` : '' }}</view>

      <view class="row ops">
        <button class="btn small" @click="openPanel(d, 'net')">
          配网
        </button>
        <button class="btn small" @click="openPanel(d, 'agent')">
          绑定角色
        </button>
        <button class="btn small ghost" @click="onRename(d)">
          重命名
        </button>
        <button class="btn small danger" @click="onDelete(d)">
          删除
        </button>
      </view>
    </view>

    <!-- 添加设备 -->
    <view v-if="showBind" class="mask" @click="showBind = false">
      <view class="modal" @click.stop>
        <view class="title">
          添加设备
        </view>
        <view class="tip left">
          在设备屏幕上可以看到形如 A-3829-4471-0056 的激活码，10 分钟内有效
        </view>
        <input v-model="bindForm.code" class="input big" placeholder="A-3829-4471-0056" />
        <input v-model="bindForm.alias" class="input" placeholder="给设备起个名字（可选）" />
        <button class="btn" @click="doBind">
          绑定设备
        </button>
      </view>
    </view>

    <!-- 配网 / 绑定角色 面板 -->
    <view v-if="panel" class="mask" @click="closePanel">
      <view class="modal" @click.stop>
        <template v-if="panel.mode === 'net'">
          <view class="title">
            给「{{ currentDevice()?.name }}」配网
          </view>
          <view class="tip left">
            配网需要让设备进入配对模式：长按设备按键 5 秒，听到提示音后点下面按钮。
          </view>
          <button class="btn" @click="uni.navigateTo({ url: '/pages/device-config/index' })">
            开始配网
          </button>
        </template>

        <template v-else>
          <view class="title">
            为「{{ currentDevice()?.name }}」绑定角色
          </view>
          <view class="tip left">
            每台设备可以绑定不同的角色，角色决定形象、音色和说话风格
          </view>

          <view v-if="roles.length" class="group-label">我的角色</view>
          <view v-for="r in roles" :key="`r${r.id}`" class="pick" @click="onPickRole(r)">
            <text class="pick-name">{{ r.name }}</text>
            <text class="pick-sub">{{ r.description || '自定义角色' }}</text>
          </view>

          <view class="group-label">系统形象</view>
          <view v-for="a in avatars" :key="`a${a.id}`" class="pick" @click="onPickAvatar(a)">
            <text class="pick-name">{{ a.name }}</text>
            <text class="pick-sub">{{ a.description || a.tts_voice || '' }}</text>
          </view>

          <view v-if="!roles.length && !avatars.length" class="tip">
            暂无可选角色，可到「我的 → 创建角色」新建
          </view>
        </template>

        <button class="btn ghost" @click="closePanel">
          关闭
        </button>
      </view>
    </view>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; padding-bottom: 40rpx; }
.head { display: flex; flex-direction: row; align-items: center; justify-content: space-between; margin-bottom: 20rpx; }
.head-title { font-size: 34rpx; font-weight: 700; }
.head-sub { font-size: 24rpx; color: #8a8f99; margin-top: 4rpx; }
.add { font-size: 28rpx; color: #e8590c; font-weight: 600; }
.card { background: #fff; border-radius: 20rpx; padding: 28rpx; margin-bottom: 24rpx; }
.dname { font-size: 32rpx; font-weight: 600; }
.sub { font-size: 24rpx; color: #8a8f99; margin-top: 8rpx; display: block; }
.row { display: flex; flex-direction: row; align-items: center; }
.between { justify-content: space-between; }
.ops { margin-top: 20rpx; flex-wrap: wrap; }
.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 28rpx; padding: 18rpx 0; margin-top: 8rpx; }
.btn.small { flex: 1; min-width: 40%; margin-right: 12rpx; margin-top: 12rpx; padding: 14rpx 0; font-size: 26rpx; }
.btn.ghost { background: #fff; color: #e8590c; border: 1rpx solid #e8590c; }
.btn.danger { background: #fff; color: #dc2626; border: 1rpx solid #f3c7c7; margin-right: 0; }
.tag { font-size: 22rpx; color: #8a8f99; background: #f1f3f6; border-radius: 20rpx; padding: 4rpx 16rpx; }
.tag.ok { color: #16a34a; background: #e8f7ee; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; padding: 40rpx 0; }
.tip.left { text-align: left; padding: 12rpx 0 20rpx; }
.empty { text-align: center; padding: 60rpx 28rpx; }
.empty-title { font-size: 32rpx; font-weight: 600; }
.empty-sub { font-size: 26rpx; color: #8a8f99; margin-top: 16rpx; line-height: 1.6; }
.mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 99; }
.modal { width: 86%; max-height: 80vh; overflow-y: auto; background: #fff; border-radius: 20rpx; padding: 32rpx; }
.title { font-size: 32rpx; font-weight: 600; margin-bottom: 8rpx; }
.input { border: 1rpx solid #e3e8ef; border-radius: 10rpx; padding: 18rpx; margin-bottom: 16rpx; font-size: 28rpx; background: #fff; }
.input.big { font-size: 36rpx; letter-spacing: 2rpx; text-align: center; }
.group-label { font-size: 26rpx; color: #8a8f99; margin: 20rpx 0 8rpx; }
.pick { border: 1rpx solid #e3e8ef; border-radius: 12rpx; padding: 20rpx; margin-bottom: 12rpx; }
.pick-name { font-size: 28rpx; font-weight: 600; display: block; }
.pick-sub { font-size: 24rpx; color: #8a8f99; display: block; margin-top: 6rpx; }
</style>
