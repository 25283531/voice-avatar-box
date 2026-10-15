<route lang="json">
{
  "type": "home",
  "style": { "navigationBarTitleText": "老人陪伴助手" }
}
</route>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'
import {
  deviceEvents,
  getMembership,
  homeSummary,
  listDevices,
  listReminders,
} from '@/api/elder'
import type { Device, MembershipInfo, Reminder, ReminderEvent } from '@/api/elder'
import { getElderToken } from '@/http/elder'

/** 今天零点（本地时区） */
function todayStart(): Date {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d
}

function fmtTime(v?: string | null) {
  if (!v) return '—'
  return v.replace('T', ' ').slice(0, 16)
}

const devices = ref<Device[]>([])
const reminders = ref<Reminder[]>([])
const events = ref<ReminderEvent[]>([])
const membership = ref<MembershipInfo | null>(null)
const loading = ref(false)
const logged = ref(true)

/** 当天待服药的提醒（含时间点） */
const medicinePending = computed(() => {
  const times: string[] = []
  let count = 0
  for (const r of reminders.value) {
    if (r.rtype !== 0 || !r.enabled) continue // 0 = 服药
    for (const t of r.times_of_day || []) {
      times.push(t)
      count++
    }
  }
  return { times: times.sort(), count }
})

/** 当天已确认服药次数 */
const confirmedToday = computed(() => {
  const start = todayStart().getTime()
  return events.value.filter(e =>
    e.confirmed_at && new Date(e.confirmed_at).getTime() >= start,
  ).length
})

/** 当天服药总数（已确认 + 待服药） */
const medicineTotal = computed(() => confirmedToday.value + medicinePending.value.count)

const onlineCount = computed(() => devices.value.filter(d => d.online).length)

async function refresh() {
  if (!getElderToken()) {
    logged.value = false
    return
  }
  logged.value = true
  loading.value = true
  try {
    devices.value = await listDevices()

    // 优先用后端聚合接口；拿不到就退回本地拼装（兼容旧版本后端）
    let summaryOk = false
    try {
      const s = await homeSummary()
      if (s && Array.isArray(s.devices)) {
        membership.value = s.membership || null
        summaryOk = true
      }
    }
    catch { /* 旧后端没有该接口，走下面的兼容分支 */ }

    if (!summaryOk) {
      try {
        membership.value = await getMembership()
      }
      catch { /* 会员模块未部署时不阻塞首页 */ }
    }

    // 服药状态：取第一个设备的提醒与回执本地计算
    const first = devices.value[0]
    if (first) {
      try {
        reminders.value = await listReminders(first.id)
      }
      catch { /* 已提示 */ }
      try {
        events.value = await deviceEvents(first.id, 60)
      }
      catch { /* 已提示 */ }
    }
    else {
      reminders.value = []
      events.value = []
    }
  }
  catch { /* 请求层已提示 */ }
  finally {
    loading.value = false
  }
}

function goLogin() {
  uni.reLaunch({ url: '/pages/login/index' })
}

function goTab(path: string) {
  uni.switchTab({ url: path })
}

onShow(() => { refresh() })
</script>

<template>
  <view class="page">
    <!-- 未登录 -->
    <view v-if="!logged" class="card empty">
      <view class="empty-title">
        请先登录
      </view>
      <view class="empty-sub">
        登录后可查看设备状态与服药提醒
      </view>
      <button class="btn" @click="goLogin">
        去登录
      </button>
    </view>

    <template v-else>
      <!-- 会员状态 -->
      <view class="card vip" :class="{ on: membership?.is_vip }">
        <view class="row between">
          <text class="vip-name">{{ membership?.is_vip ? '会员已开通' : '未开通会员' }}</text>
          <text v-if="membership?.is_vip && membership.expires_at" class="vip-exp">
            {{ membership.expires_at.slice(0, 10) }} 到期
          </text>
        </view>
        <view class="vip-sub">
          <template v-if="membership?.is_vip">
            剩余 {{ membership.days_left }} 天 · {{ (membership.benefits || []).join(' · ') || '尊享全部权益' }}
          </template>
          <template v-else>
            开通会员可解锁更多设备与自定义角色
          </template>
        </view>
        <button class="btn small" @click="goTab('/pages/profile/index')">
          {{ membership?.is_vip ? '续费会员' : '开通会员' }}
        </button>
      </view>

      <!-- 设备状态 -->
      <view class="card">
        <view class="row between">
          <text class="card-title">我的设备</text>
          <text class="card-link" @click="goTab('/pages/device/index')">管理</text>
        </view>

        <view v-if="loading" class="tip">
          加载中…
        </view>
        <view v-else-if="devices.length === 0" class="tip">
          还没有绑定设备，去「设备」页添加
        </view>

        <view v-for="d in devices" :key="d.id" class="device">
          <view class="row between">
            <text class="device-name">{{ d.name }}</text>
            <text :class="['tag', d.online ? 'ok' : '']">{{ d.online ? '在线' : '离线' }}</text>
          </view>
          <view class="device-sub">
            网络{{ d.online ? '已连接' : '未连接' }} · 设备时间 {{ fmtTime(d.last_seen_at) }}
          </view>
          <view class="device-sub">
            标识 {{ d.device_id }}{{ d.ip ? ` · ${d.ip}` : '' }}
          </view>
        </view>

        <view v-if="devices.length > 0" class="device-summary">
          共 {{ devices.length }} 台，在线 {{ onlineCount }} 台
        </view>
      </view>

      <!-- 服药提醒状态 -->
      <view class="card">
        <view class="row between">
          <text class="card-title">今日服药</text>
          <text class="card-link" @click="goTab('/pages/schedule/index')">去管理</text>
        </view>

        <view class="stat-row">
          <view class="stat">
            <text class="stat-num warn">
              {{ medicinePending.count }}
            </text>
            <text class="stat-label">待服药</text>
          </view>
          <view class="stat">
            <text class="stat-num ok">
              {{ confirmedToday }}
            </text>
            <text class="stat-label">已确认</text>
          </view>
          <view class="stat">
            <text class="stat-num">
              {{ medicineTotal }}
            </text>
            <text class="stat-label">今日合计</text>
          </view>
        </view>

        <view v-if="medicinePending.times.length" class="times">
          <text v-for="(t, i) in medicinePending.times" :key="i" class="time-chip">{{ t }}</text>
        </view>
        <view v-else class="tip">
          今天的药都吃完啦
        </view>
      </view>
    </template>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; padding-bottom: 40rpx; }
.card { background: #fff; border-radius: 20rpx; padding: 28rpx; margin-bottom: 24rpx; }
.card-title { font-size: 32rpx; font-weight: 600; }
.card-link { font-size: 26rpx; color: #e8590c; }
.row { display: flex; flex-direction: row; align-items: center; }
.between { justify-content: space-between; }
.tip { font-size: 26rpx; color: #8a8f99; text-align: center; padding: 32rpx 0; }

/* 会员卡 */
.vip { background: linear-gradient(135deg, #fff7ed, #ffedd5); border: 1rpx solid #fed7aa; }
.vip.on { background: linear-gradient(135deg, #fff7ed, #ffe8cc); }
.vip-name { font-size: 32rpx; font-weight: 700; color: #9a3412; }
.vip-exp { font-size: 24rpx; color: #c2410c; }
.vip-sub { font-size: 24rpx; color: #a16207; margin: 12rpx 0 16rpx; }
.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 28rpx; padding: 18rpx 0; }
.btn.small { padding: 12rpx 32rpx; font-size: 26rpx; margin-top: 0; }

/* 设备 */
.device { border-top: 1rpx solid #f1f3f6; padding: 20rpx 0 4rpx; }
.device-name { font-size: 30rpx; font-weight: 600; }
.device-sub { font-size: 24rpx; color: #8a8f99; margin-top: 8rpx; }
.device-summary { font-size: 24rpx; color: #8a8f99; margin-top: 16rpx; }
.tag { font-size: 22rpx; color: #8a8f99; background: #f1f3f6; border-radius: 20rpx; padding: 4rpx 16rpx; }
.tag.ok { color: #16a34a; background: #e8f7ee; }

/* 服药统计 */
.stat-row { display: flex; margin: 20rpx 0 8rpx; }
.stat { flex: 1; text-align: center; }
.stat-num { display: block; font-size: 52rpx; font-weight: 700; color: #1f2937; }
.stat-num.warn { color: #e8590c; }
.stat-num.ok { color: #16a34a; }
.stat-label { display: block; font-size: 24rpx; color: #8a8f99; margin-top: 4rpx; }
.times { display: flex; flex-wrap: wrap; margin-top: 12rpx; }
.time-chip { font-size: 26rpx; color: #9a3412; background: #fff7ed; border: 1rpx solid #fed7aa; border-radius: 12rpx; padding: 8rpx 20rpx; margin: 0 12rpx 12rpx 0; }

/* 空态 */
.empty { text-align: center; padding: 60rpx 28rpx; }
.empty-title { font-size: 34rpx; font-weight: 600; }
.empty-sub { font-size: 26rpx; color: #8a8f99; margin: 16rpx 0 28rpx; }
</style>
