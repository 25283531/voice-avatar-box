<route lang="json">
{
  "style": { "navigationBarTitleText": "交互统计" }
}
</route>

<script setup lang="ts">
import { onLoad, onShow } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'
import { statsOverview } from '@/api/elder'
import type { StatsOverview } from '@/api/elder'
import { fmtDateTime } from '@/utils/time'

const devicePk = ref(0)
const deviceName = ref('')
const stat = ref<StatsOverview | null>(null)
const days = ref(7)

const maxTotal = computed(() => {
  const list = stat.value?.series || []
  return Math.max(1, ...list.map(p => p.total))
})

function barHeight(n: number) {
  return 10 + Math.round((n / maxTotal.value) * 100)
}

function md(d: string) {
  return d.slice(5)
}

async function refresh() {
  if (!devicePk.value) return
  try {
    stat.value = await statsOverview(devicePk.value, days.value)
  }
  catch { /* 已提示 */ }
}

function changeDays(d: number) {
  days.value = d
  refresh()
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
      <text class="sub">{{ stat?.online ? '在线' : '离线' }} · 最近在线 {{ fmtDateTime(stat?.last_seen_at) }}</text>
    </view>

    <view class="tabs">
      <text :class="['tab', days === 7 ? 'on' : '']" @click="changeDays(7)">近 7 天</text>
      <text :class="['tab', days === 14 ? 'on' : '']" @click="changeDays(14)">近 14 天</text>
      <text :class="['tab', days === 30 ? 'on' : '']" @click="changeDays(30)">近 30 天</text>
    </view>

    <template v-if="stat">
      <view class="kpis">
        <view class="kpi"><text class="n">{{ stat.today_total }}</text><text class="l">今日交互</text></view>
        <view class="kpi"><text class="n">{{ stat.today_dialog }}</text><text class="l">对话轮次</text></view>
        <view class="kpi"><text class="n ok">{{ stat.today_confirm }}</text><text class="l">按时确认</text></view>
        <view class="kpi"><text class="n" :class="stat.missed_today ? 'bad' : 'ok'">{{ stat.missed_today }}</text><text class="l">超时未确认</text></view>
      </view>

      <view class="card">
        <text class="label">每日交互次数</text>
        <view class="bars">
          <view v-for="p in stat.series" :key="p.date" class="col">
            <view class="bar" :style="{ height: barHeight(p.total) + 'rpx' }" />
            <text class="x">{{ md(p.date) }}</text>
          </view>
        </view>
      </view>

      <view class="card">
        <text class="label">提醒对账</text>
        <view class="line"><text class="l">待处理</text><text class="v">{{ stat.pending_reminders }}</text></view>
        <view class="line"><text class="l">今日超时未确认</text><text class="v" :class="stat.missed_today ? 'bad' : ''">{{ stat.missed_today }}</text></view>
        <view class="line"><text class="l">区间累计交互</text><text class="v">{{ stat.total_7d }}</text></view>
      </view>

      <view class="card">
        <text class="label">今日构成</text>
        <view class="line"><text class="l">语音唤醒</text><text class="v">{{ stat.series[stat.series.length - 1]?.wake || 0 }}</text></view>
        <view class="line"><text class="l">对话</text><text class="v">{{ stat.today_dialog }}</text></view>
        <view class="line"><text class="l">提醒确认</text><text class="v ok">{{ stat.today_confirm }}</text></view>
        <view class="line"><text class="l">超时未确认</text><text class="v bad">{{ stat.today_miss }}</text></view>
      </view>
    </template>

    <view v-else class="tip">加载中…</view>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; }
.head { margin-bottom: 16rpx; }
.dname { font-size: 32rpx; font-weight: 600; }
.sub { font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; display: block; }
.tabs { display: flex; margin-bottom: 16rpx; }
.tab { flex: 1; text-align: center; padding: 14rpx 0; font-size: 26rpx; color: #6b7280; }
.tab.on { color: #2563eb; border-bottom: 4rpx solid #2563eb; font-weight: 600; }
.kpis { display: flex; flex-wrap: wrap; justify-content: space-between; }
.kpi { width: 48%; background: #eff6ff; border-radius: 16rpx; padding: 20rpx; margin-bottom: 16rpx; }
.n { font-size: 44rpx; font-weight: 700; color: #2563eb; display: block; }
.n.ok { color: #16a34a; }
.n.bad { color: #dc2626; }
.l { font-size: 22rpx; color: #8a8f99; }
.card { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; }
.label { font-size: 24rpx; color: #6b7280; display: block; margin-bottom: 16rpx; }
.bars { display: flex; align-items: flex-end; height: 240rpx; }
.col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; }
.bar { width: 60%; background: #2563eb; border-radius: 6rpx 6rpx 0 0; min-height: 4rpx; }
.x { font-size: 18rpx; color: #8a8f99; margin-top: 8rpx; }
.line { display: flex; justify-content: space-between; padding: 12rpx 0; border-bottom: 1rpx solid #f1f3f6; }
.l2 { font-size: 26rpx; color: #374151; }
.v { font-size: 28rpx; font-weight: 600; }
.v.ok { color: #16a34a; }
.v.bad { color: #dc2626; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; padding: 40rpx 0; }
</style>
