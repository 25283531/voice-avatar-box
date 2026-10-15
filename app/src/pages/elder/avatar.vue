<route lang="json">
{
  "style": { "navigationBarTitleText": "选择形象" }
}
</route>

<script setup lang="ts">
import { onLoad, onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import { applyAvatar, listAvatars, updateDevice } from '@/api/elder'
import type { Avatar } from '@/api/elder'

const devicePk = ref(0)
const deviceName = ref('')
const avatars = ref<Avatar[]>([])
const currentId = ref<number | null>(null)

async function refresh() {
  try {
    avatars.value = await listAvatars()
  }
  catch { /* 已提示 */ }
}

const browseOnly = ref(false)

async function onPick(a: Avatar) {
  // 从「我的 → 系统形象库」进来时没有具体设备，只做浏览
  if (browseOnly.value) {
    uni.showToast({ title: '请到「设备」页选择设备后再绑定形象', icon: 'none' })
    return
  }
  try {
    await applyAvatar(a.id, devicePk.value)
    currentId.value = a.id
    uni.showToast({ title: `已切换为「${a.name}」`, icon: 'success' })
  }
  catch { /* 已提示 */ }
}

async function onClear() {
  if (browseOnly.value) return
  try {
    await updateDevice(devicePk.value, { avatar_id: 0 })
    currentId.value = null
    uni.showToast({ title: '已清除形象', icon: 'success' })
  }
  catch { /* 已提示 */ }
}

function clipCount(a: Avatar) {
  return Object.keys(a.clips || {}).length
}

onLoad((options: any) => {
  devicePk.value = Number(options?.devicePk || 0)
  browseOnly.value = devicePk.value <= 0
  deviceName.value = decodeURIComponent(options?.name || '')
  const cur = Number(options?.current || 0)
  currentId.value = cur || null
})
onShow(() => { refresh() })
</script>

<template>
  <view class="page">
    <view class="head">
      <text class="dname">{{ browseOnly ? '系统形象库' : deviceName }}</text>
      <text class="sub">{{ browseOnly ? '在「设备」页选择设备后即可绑定形象' : '切换后设备下次心跳生效（默认 5 分钟内）' }}</text>
    </view>

    <view v-for="a in avatars" :key="a.id" class="card" @click="onPick(a)">
      <view class="row between">
        <text class="title">{{ a.name }}</text>
        <text v-if="currentId === a.id" class="tag ok">使用中</text>
      </view>
      <text class="sub">{{ a.description || '暂无描述' }}</text>
      <text class="sub">资源包 {{ a.eaf_pack_name || '—' }} · 片段 {{ clipCount(a) }} 个 · {{ a.screen_width }}×{{ a.screen_height }}</text>
      <text v-if="a.tts_voice" class="sub">音色 {{ a.tts_voice }}</text>
    </view>

    <view v-if="avatars.length === 0" class="tip">还没有形象，请先到 WEB 管理后台添加</view>
    <button v-if="!browseOnly" class="btn ghost" @click="onClear">清除形象</button>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; }
.head { margin-bottom: 16rpx; }
.dname { font-size: 32rpx; font-weight: 600; }
.sub { font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; display: block; }
.card { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; }
.title { font-size: 30rpx; font-weight: 600; }
.row { display: flex; flex-direction: row; align-items: center; }
.between { justify-content: space-between; }
.tag { font-size: 22rpx; border-radius: 20rpx; padding: 4rpx 16rpx; }
.tag.ok { color: #16a34a; background: #e8f7ee; }
.btn { border-radius: 12rpx; font-size: 30rpx; padding: 22rpx 0; margin-top: 12rpx; }
.btn.ghost { background: #fff; color: #dc2626; border: 1rpx solid #f3c7c7; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; padding: 40rpx 0; }
</style>
