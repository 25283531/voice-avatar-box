<route lang="json">
{
  "style": { "navigationBarTitleText": "提醒管理" }
}
</route>

<script setup lang="ts">
import { onLoad, onShow } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'
import { confirmReminder, deleteReminder, deviceEvents, listReminders, toggleReminder } from '@/api/elder'
import type { Reminder, ReminderEvent } from '@/api/elder'

const devicePk = ref(0)
const deviceName = ref('')
const list = ref<Reminder[]>([])
const events = ref<ReminderEvent[]>([])
const segment = ref(0) // 0=规则 1=回执

const typeNames = ['服药', '日程', '喝水', '运动', '自定义']
const stateNames = ['待触发', '已响铃', '已确认', '超时未确认', '已跳过']
const methodNames = ['—', '语音', '按键', '手机端', '自动']

const weekCn = ['一', '二', '三', '四', '五', '六', '日']

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

function fmt(v?: string | null) {
  if (!v) return '—'
  return v.replace('T', ' ').slice(0, 16)
}

const pendingCount = computed(() => list.value.filter(r => r.enabled).length)

async function refresh() {
  if (!devicePk.value) return
  try {
    list.value = await listReminders(devicePk.value)
    events.value = await deviceEvents(devicePk.value, 30)
  }
  catch { /* 已提示 */ }
}

async function onToggle(r: Reminder) {
  try {
    await toggleReminder(r.id)
    await refresh()
  }
  catch { /* 已提示 */ }
}

async function onDelete(r: Reminder) {
  const ok = await new Promise<boolean>((resolve) => {
    uni.showModal({
      title: '删除提醒',
      content: `确定删除「${r.title}」？`,
      success: res => resolve(!!res.confirm),
      fail: () => resolve(false),
    })
  })
  if (!ok) return
  await deleteReminder(r.id)
  await refresh()
}

async function onConfirm(r: Reminder) {
  const ok = await new Promise<boolean>((resolve) => {
    uni.showModal({
      title: '代老人确认',
      content: `确认「${r.title}」已完成？设备上的红灯会熄灭。`,
      success: res => resolve(!!res.confirm),
      fail: () => resolve(false),
    })
  })
  if (!ok) return
  try {
    await confirmReminder(r.id)
    uni.showToast({ title: '已确认', icon: 'success' })
    await refresh()
  }
  catch { /* 已提示 */ }
}

function onEdit(r?: Reminder) {
  const url = r
    ? `/pages/elder/reminder-edit?devicePk=${devicePk.value}&id=${r.id}`
    : `/pages/elder/reminder-edit?devicePk=${devicePk.value}`
  uni.navigateTo({ url })
}

onLoad((options: any) => {
  devicePk.value = Number(options?.devicePk || 0)
  deviceName.value = decodeURIComponent(options?.name || '')
})
onShow(() => { refresh() })
</script>

<template>
  <view class="page">
    <view class="head">
      <text class="dname">{{ deviceName }}</text>
      <text class="sub">共 {{ list.length }} 条规则，启用中 {{ pendingCount }} 条</text>
    </view>

    <view class="tabs">
      <text :class="['tab', segment === 0 ? 'on' : '']" @click="segment = 0">提醒规则</text>
      <text :class="['tab', segment === 1 ? 'on' : '']" @click="segment = 1">执行回执</text>
    </view>

    <!-- 规则 -->
    <template v-if="segment === 0">
      <view v-for="r in list" :key="r.id" class="card">
        <view class="row between">
          <text class="title">{{ r.title }}</text>
          <text :class="['tag', r.enabled ? 'ok' : '']">{{ r.enabled ? '启用' : '暂停' }}</text>
        </view>
        <view class="sub">{{ typeNames[r.rtype] }} · {{ kindText(r) }}</view>
        <view class="sub">时间 {{ (r.times_of_day || []).join('  ') }}</view>
        <view v-if="r.dose" class="sub">剂量 {{ r.dose }}</view>
        <view class="sub">下次 {{ fmt(r.next_fire_at) }}</view>
        <view class="sub">确认方式 {{ (r.confirm_methods || []).join(' / ') || '—' }}</view>
        <view class="row ops">
          <button class="btn small" @click="onEdit(r)">编辑</button>
          <button class="btn small ghost" @click="onToggle(r)">{{ r.enabled ? '暂停' : '启用' }}</button>
          <button v-if="r.require_confirm" class="btn small ok" @click="onConfirm(r)">代确认</button>
          <button class="btn small danger" @click="onDelete(r)">删除</button>
        </view>
      </view>

      <view v-if="list.length === 0" class="tip">还没有提醒规则</view>
      <button class="btn add" @click="onEdit()">+ 新增提醒</button>
    </template>

    <!-- 回执 -->
    <template v-else>
      <view v-for="e in events" :key="e.id" class="card">
        <view class="row between">
          <text class="title">{{ fmt(e.due_at) }}</text>
          <text :class="['tag', e.state === 2 ? 'ok' : (e.state === 3 ? 'bad' : '')]">{{ stateNames[e.state] }}</text>
        </view>
        <view class="sub">提醒 #{{ e.reminder_id }} · 确认方式 {{ methodNames[e.confirm_method] }}</view>
        <view v-if="e.confirmed_at" class="sub">确认于 {{ fmt(e.confirmed_at) }}</view>
        <view v-if="e.note" class="sub">{{ e.note }}</view>
      </view>
      <view v-if="events.length === 0" class="tip">暂无回执</view>
    </template>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; }
.head { margin-bottom: 16rpx; }
.dname { font-size: 32rpx; font-weight: 600; }
.sub { font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; display: block; }
.tabs { display: flex; margin-bottom: 16rpx; }
.tab { flex: 1; text-align: center; padding: 16rpx 0; font-size: 28rpx; color: #6b7280; }
.tab.on { color: #2563eb; border-bottom: 4rpx solid #2563eb; font-weight: 600; }
.card { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; }
.title { font-size: 30rpx; font-weight: 600; }
.row { display: flex; flex-direction: row; align-items: center; }
.between { justify-content: space-between; }
.ops { margin-top: 16rpx; flex-wrap: wrap; }
.btn { background: #2563eb; color: #fff; border-radius: 10rpx; font-size: 26rpx; padding: 12rpx 0; }
.btn.small { flex: 1; margin-right: 12rpx; }
.btn.ghost { background: #fff; color: #2563eb; border: 1rpx solid #2563eb; }
.btn.ok { background: #16a34a; }
.btn.danger { background: #fff; color: #dc2626; border: 1rpx solid #f3c7c7; margin-right: 0; }
.btn.add { margin-top: 24rpx; padding: 22rpx 0; font-size: 30rpx; }
.tag { font-size: 22rpx; color: #8a8f99; background: #f1f3f6; border-radius: 20rpx; padding: 4rpx 16rpx; }
.tag.ok { color: #16a34a; background: #e8f7ee; }
.tag.bad { color: #dc2626; background: #fdeaea; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; padding: 40rpx 0; }
</style>
