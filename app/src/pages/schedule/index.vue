<route lang="json">
{
  "style": { "navigationBarTitleText": "日程" }
}
</route>

<script setup lang="ts">
import { onLoad, onShow } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'
import {
  confirmReminder,
  createReminder,
  deleteReminder,
  deviceEvents,
  listDevices,
  listReminders,
  toggleReminder,
  updateReminder,
} from '@/api/elder'
import type { Device, Reminder } from '@/api/elder'
import { getElderToken } from '@/http/elder'
import { fmtDateTime } from '@/utils/time'

const devices = ref<Device[]>([])
const devicePk = ref(0)
const list = ref<Reminder[]>([])
const loading = ref(false)

/** 0=服药 1=日程 */
const segment = ref(0)

const typeNames = ['服药', '日程', '喝水', '运动', '自定义']
const weekCn = ['一', '二', '三', '四', '五', '六', '日']

const current = computed(() => list.value.filter(r => r.rtype === segment.value))
const medicineCount = computed(() => list.value.filter(r => r.rtype === 0).length)
const scheduleCount = computed(() => list.value.filter(r => r.rtype === 1).length)

function kindText(r: Reminder) {
  switch (r.schedule_kind) {
    case 0: return `仅 ${r.once_date || '—'}`
    case 1: return '每天'
    case 2: return '每周 ' + (r.weekdays || []).map(w => weekCn[w - 1]).join('')
    case 3: return '每月 ' + (r.month_days || []).join('、') + ' 号'
    case 4: return `每 ${r.interval_days} 天`
    default: return '—'
  }
}

/** 保留本地名 `fmt`：模板已在用，改名要连带改模板。统一走北京时间。 */
function fmt(v?: string | null) {
  return fmtDateTime(v)
}

async function refresh() {
  if (!getElderToken()) return
  loading.value = true
  try {
    if (devices.value.length === 0) {
      devices.value = await listDevices()
      if (devices.value.length && !devicePk.value) devicePk.value = devices.value[0].id
    }
    if (devicePk.value) list.value = await listReminders(devicePk.value)
  }
  catch { /* 请求层已提示 */ }
  finally {
    loading.value = false
  }
}

function onSwitchDevice(e: any) {
  devicePk.value = Number(e.detail.value)
  refresh()
}

async function onToggle(r: Reminder) {
  try {
    await toggleReminder(r.id)
    await refresh()
  }
  catch { /* 已提示 */ }
}

function onDelete(r: Reminder) {
  uni.showModal({
    title: '删除提醒',
    content: `确定删除「${r.title}」？`,
    success: async (res) => {
      if (!res.confirm) return
      await deleteReminder(r.id)
      await refresh()
    },
  })
}

async function onConfirm(r: Reminder) {
  uni.showModal({
    title: '代老人确认',
    content: `确认「${r.title}」已完成？设备上的红灯会熄灭。`,
    success: async (res) => {
      if (!res.confirm) return
      try {
        await confirmReminder(r.id)
        uni.showToast({ title: '已确认', icon: 'success' })
        await refresh()
      }
      catch { /* 已提示 */ }
    },
  })
}

/** 新增：默认按当前分页建对应类型的提醒 */
async function onAdd() {
  if (!devicePk.value) {
    uni.showToast({ title: '请先添加并选择设备', icon: 'none' })
    return
  }
  const rtype = segment.value
  try {
    // 先建一条草稿，再跳到编辑页完善（保证 device_pk 由后端校验归属）
    const draft = await createReminder(devicePk.value, {
      title: rtype === 0 ? '新的服药提醒' : '新的日程提醒',
      speak_text: rtype === 0 ? '该吃药了' : '到时间啦',
      rtype,
      schedule_kind: 1,
      times_of_day: ['08:00'],
      require_confirm: true,
      confirm_methods: ['voice', 'button'],
      enabled: true,
    })
    uni.navigateTo({
      url: `/pages/elder/reminder-edit?devicePk=${devicePk.value}&id=${draft.id}`,
    })
  }
  catch { /* 已提示 */ }
}

function onEdit(r: Reminder) {
  uni.navigateTo({ url: `/pages/elder/reminder-edit?devicePk=${devicePk.value}&id=${r.id}` })
}

onLoad(() => { refresh() })
onShow(() => { refresh() })
</script>

<template>
  <view class="page">
    <!-- 设备选择 -->
    <view v-if="devices.length > 1" class="device-bar">
      <text class="device-bar-label">设备</text>
      <picker :range="devices" range-key="name" :value="devices.findIndex(d => d.id === devicePk)" @change="onSwitchDevice">
        <view class="device-picker">
          {{ devices.find(d => d.id === devicePk)?.name || '请选择设备' }}
          <text class="arrow">▾</text>
        </view>
      </picker>
    </view>

    <view v-if="devices.length === 0 && !loading" class="card empty">
      <view class="empty-title">
        还没有设备
      </view>
      <view class="empty-sub">
        请先到「设备」页添加设备，再为它设置提醒
      </view>
    </view>

    <template v-else>
      <!-- 分段切换 -->
      <view class="tabs">
        <view :class="['tab', segment === 0 ? 'on' : '']" @click="segment = 0">
          服药提醒
          <text class="badge">{{ medicineCount }}</text>
        </view>
        <view :class="['tab', segment === 1 ? 'on' : '']" @click="segment = 1">
          日程提醒
          <text class="badge">{{ scheduleCount }}</text>
        </view>
      </view>

      <view v-if="loading" class="tip">
        加载中…
      </view>

      <template v-else>
        <view v-for="r in current" :key="r.id" class="card">
          <view class="row between">
            <text class="title">{{ r.title }}</text>
            <text :class="['tag', r.enabled ? 'ok' : '']">{{ r.enabled ? '启用' : '暂停' }}</text>
          </view>
          <view class="sub">{{ typeNames[r.rtype] }} · {{ kindText(r) }}</view>
          <view class="sub times">时间 {{ (r.times_of_day || []).join('  ') }}</view>
          <view v-if="r.dose" class="sub">
            剂量 {{ r.dose }}
          </view>
          <view class="sub">下次 {{ fmt(r.next_fire_at) }}</view>
          <view class="row ops">
            <button class="btn small" @click="onEdit(r)">
              编辑
            </button>
            <button class="btn small ghost" @click="onToggle(r)">
              {{ r.enabled ? '暂停' : '启用' }}
            </button>
            <button v-if="r.require_confirm" class="btn small ok" @click="onConfirm(r)">
              代确认
            </button>
            <button class="btn small danger" @click="onDelete(r)">
              删除
            </button>
          </view>
        </view>

        <view v-if="current.length === 0" class="tip">
          {{ segment === 0 ? '还没有服药提醒' : '还没有日程提醒' }}
        </view>
        <button class="btn add" @click="onAdd">
          + 新增{{ segment === 0 ? '服药' : '日程' }}提醒
        </button>
      </template>
    </template>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; padding-bottom: 40rpx; }
.device-bar { display: flex; flex-direction: row; align-items: center; background: #fff; border-radius: 16rpx; padding: 20rpx 24rpx; margin-bottom: 20rpx; }
.device-bar-label { font-size: 26rpx; color: #8a8f99; margin-right: 20rpx; }
.device-picker { font-size: 28rpx; font-weight: 600; color: #1f2937; }
.arrow { color: #8a8f99; margin-left: 8rpx; }
.tabs { display: flex; background: #fff; border-radius: 16rpx; margin-bottom: 20rpx; overflow: hidden; }
.tab { flex: 1; text-align: center; padding: 22rpx 0; font-size: 28rpx; color: #6b7280; }
.tab.on { color: #e8590c; font-weight: 600; background: #fff7ed; }
.badge { display: inline-block; min-width: 36rpx; margin-left: 8rpx; font-size: 22rpx; color: #fff; background: #e8590c; border-radius: 18rpx; padding: 2rpx 10rpx; }
.card { background: #fff; border-radius: 20rpx; padding: 28rpx; margin-bottom: 24rpx; }
.title { font-size: 30rpx; font-weight: 600; }
.sub { font-size: 24rpx; color: #8a8f99; margin-top: 8rpx; display: block; }
.sub.times { color: #e8590c; font-weight: 600; }
.row { display: flex; flex-direction: row; align-items: center; }
.between { justify-content: space-between; }
.ops { margin-top: 20rpx; flex-wrap: wrap; }
.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 28rpx; padding: 18rpx 0; }
.btn.small { flex: 1; min-width: 40%; margin-right: 12rpx; margin-top: 12rpx; padding: 14rpx 0; font-size: 26rpx; }
.btn.ghost { background: #fff; color: #e8590c; border: 1rpx solid #e8590c; }
.btn.ok { background: #16a34a; }
.btn.danger { background: #fff; color: #dc2626; border: 1rpx solid #f3c7c7; margin-right: 0; }
.btn.add { margin-top: 8rpx; padding: 24rpx 0; font-size: 30rpx; }
.tag { font-size: 22rpx; color: #8a8f99; background: #f1f3f6; border-radius: 20rpx; padding: 4rpx 16rpx; }
.tag.ok { color: #16a34a; background: #e8f7ee; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; padding: 40rpx 0; }
.empty { text-align: center; padding: 60rpx 28rpx; }
.empty-title { font-size: 32rpx; font-weight: 600; }
.empty-sub { font-size: 26rpx; color: #8a8f99; margin-top: 16rpx; }
</style>
