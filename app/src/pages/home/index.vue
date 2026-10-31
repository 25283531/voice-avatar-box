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
  confirmReminder,
  homeSummary,
  listDevices,
} from '@/api/elder'
import type { Device, HomeSummary, MedicineTodayItem } from '@/api/elder'
import { getElderToken } from '@/http/elder'
import { fmtDateTime } from '@/utils/time'

/**
 * 这一页**不再自己算「今天」**（2026-10-31）。
 *
 * 历史包袱，别改回去：
 *   1. 最初用 `d.setHours(0,0,0,0)` —— 那是**设备**时区的零点，设备时区错了
 *      （出厂默认 / 手动改过 / 人在国外），「今天」的边界跟着错；
 *   2. 后来改成硬编码 UTC+8 —— 也是错的：家属在海外时，他看到的「今天」
 *      与他日历上的今天不是同一天（用户 2026-10-21 明确纠偏：不能统一时区）；
 *   3. 现在日界完全交给服务端 `_medicine_today`，按**每条规则自己的时区**
 *      算自然日，逐条明细（含药品名、状态、确认方式、所属设备）一起下发。
 *
 * 端侧只负责展示与筛选，所以本文件里没有一处 `new Date(0点)`。
 */

/** 保留本地同名函数：模板里已经在用 `fmtTime(...)`，改名要连带改模板。 */
function fmtTime(v?: string | null) {
  return fmtDateTime(v)
}

const devices = ref<Device[]>([])
const summary = ref<HomeSummary | null>(null)
const loading = ref(false)
const logged = ref(true)

/** 设备筛选：0 = 全部。见 medItems 的注释。 */
const devFilter = ref(0)
/** 正在提交的那一行（keyOf 的结果），用来禁用按钮 + 显示「提交中…」 */
const confirmingKey = ref('')

// ---------------------------------------------------------------- 明细
/**
 * 当天服药明细（已按设备筛选）。
 *
 * 为什么服务端合并返回、端侧再筛，而不是端侧逐台去拉：一台设备一次往返，
 * 家里两台就是两次，弱网下首页要等两轮；而且「今天」的日界在服务端，
 * 端侧自己拼必然与服务端的口径漂移。
 */
const medItems = computed<MedicineTodayItem[]>(() => {
  const all = summary.value?.medicine_today?.items || []
  if (!devFilter.value) return all
  return all.filter(it => it.device_id === devFilter.value)
})

/** 只在「有多台设备」且「没筛选」时显示设备列，否则那一列全是同一个值。 */
const showDeviceCol = computed(() => !devFilter.value && devices.value.length > 1)

/**
 * 「还没确认」= 0 未响铃 / 1 待确认 / 3 超时未确认。
 *
 * 不能写成 `state !== 2`：那样「已跳过」(4) 会被算进待确认里 —— 家属会去催
 * 一件老人本来就不用做的事。口径必须与 `_medicine_today` 的 pending_count 一致。
 */
function isPendingState(st: number) {
  return st === 0 || st === 1 || st === 3
}

const pendingItems = computed(() => medItems.value.filter(it => isPendingState(it.state)))
const confirmedItems = computed(() => medItems.value.filter(it => it.state === 2))

/**
 * 顶部三个数字：优先用逐条明细的条数（与下面的列表**必然一致**），
 * 老版本后端没有 items 时退回它给的计数。
 *
 * 为什么优先 items：只读计数的话，列表和数字来自两个不同口径，
 * 一旦有一边算错，用户看到的是「写着 2 条待确认、列表里只有 1 条」。
 */
const hasItems = computed(() => (summary.value?.medicine_today?.items || []).length > 0)
const medTotalCount = computed(() =>
  hasItems.value ? medItems.value.length : (summary.value?.medicine_today?.total_count || 0))
const medConfirmedCount = computed(() =>
  hasItems.value ? confirmedItems.value.length : (summary.value?.medicine_today?.confirmed_count || 0))
const medPendingCount = computed(() =>
  hasItems.value ? pendingItems.value.length : (summary.value?.medicine_today?.pending_count || 0))

const onlineCount = computed(() => devices.value.filter(d => d.online).length)

// ---------------------------------------------------------------- 文案 / 上色
/** 与服务端 `_STATE_TEXT` 同序；只在老后端没给 state_text 时兜底。 */
const STATE_TEXT = ['未响铃', '待确认', '已确认', '待确认', '已跳过']
/** 与服务端 `_CONFIRM_TEXT` 同序；只在老后端没给 confirm_method_text 时兜底。 */
const METHOD_TEXT = ['—', '语音确认', '按键确认', '子女代确认', '自动确认', '语音+按键确认']

function stateText(it: MedicineTodayItem) {
  return it.state_text || STATE_TEXT[it.state] || '—'
}
function stateClass(st: number) {
  return st === 2 ? 'ok' : (st === 3 ? 'bad' : (st === 1 ? 'warn' : ''))
}
function methodText(it: MedicineTodayItem) {
  return it.confirm_method_text || METHOD_TEXT[it.confirm_method] || '—'
}
function methodClass(m: number) {
  // 子女代确认用暖色，和「老人自己按的」区分开 —— 这是家属最需要一眼看出的差别
  return m === 3 ? 'app' : (m === 1 || m === 5 ? 'voice' : '')
}

/**
 * 只保留 HH:MM。
 *
 * 不能用 fmtTime：那会输出「2026-10-31 08:00」，同一张表里已经按天分组，
 * 日期是冗余信息，一行挤两遍日期在小屏上会换行。
 */
function fmtHM(v?: string | null) {
  if (!v) return '—'
  const d = new Date(v)
  if (isNaN(d.getTime())) return String(v)
  const p = (n: number) => String(n).padStart(2, '0')
  return p(d.getHours()) + ':' + p(d.getMinutes())
}

// ---------------------------------------------------------------- 设备筛选
const devOptions = computed(() =>
  ['全部设备（' + devices.value.length + ' 台）']
    .concat(devices.value.map(d => (d.name || ('设备 ' + d.id)) + (d.online ? '' : '（离线）'))))

const devIndex = computed(() => {
  if (!devFilter.value) return 0
  const i = devices.value.findIndex(d => d.id === devFilter.value)
  return i < 0 ? 0 : i + 1
})

function onDevChange(e: any) {
  const i = Number(e?.detail?.value) || 0
  devFilter.value = i === 0 ? 0 : (devices.value[i - 1]?.id || 0)
}

// ---------------------------------------------------------------- 代确认
/**
 * 一行待确认的唯一标识。
 *
 * **不能只用 reminder_id**：同一条规则一天可能有 08:00 和 20:00 两个服药点
 * （reminder_id 相同、due_at 不同），只按 reminder_id 做 key 会让两行同时
 * 变成「提交中…」，确认请求也分不清该确认哪一次。
 */
function keyOf(it: MedicineTodayItem) {
  return it.reminder_id + '@' + it.due_at
}

function askConfirm(title: string): Promise<boolean> {
  return new Promise((resolve) => {
    uni.showModal({
      title: '代确认',
      content: '确认老人已经完成「' + title + '」？',
      success: r => resolve(!!r.confirm),
      fail: () => resolve(false),
    })
  })
}

async function confirmForElder(it: MedicineTodayItem) {
  const title = (it.title || '这条提醒') + '（' + fmtHM(it.due_at) + '）'
  if (!(await askConfirm(title)))
    return
  confirmingKey.value = keyOf(it)
  try {
    await confirmReminder(it.reminder_id, it.due_at)
    uni.showToast({ title: '已代确认', icon: 'success' })
    // 必须重拉：这条要从「待确认」挪到「已确认」，顶部计数也要跟着变
    await refresh()
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || '代确认失败', icon: 'none' })
  }
  finally {
    confirmingKey.value = ''
  }
}

// ---------------------------------------------------------------- 加载
async function refresh() {
  if (!getElderToken()) {
    logged.value = false
    return
  }
  logged.value = true
  loading.value = true
  try {
    // 设备列表与首页聚合互不依赖，但 uni 的 request 是回调式，
    // 这里保持顺序 await 更易读；两者失败互不连累。
    try {
      devices.value = await listDevices()
    }
    catch { /* 请求层已提示 */ }
    try {
      summary.value = await homeSummary()
    }
    catch { summary.value = null /* 首页聚合挂了不该让设备卡片一起白掉 */ }
  }
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

        <!-- 多设备筛选：服务端把名下所有设备的服药统计合并返回，
             家里既有客厅那台又有卧室那台时，光看「08:00 降压药待确认」
             根本不知道该去哪台设备前提醒老人。只有一台时不显示。 -->
        <picker
          v-if="devices.length > 1"
          class="dev-picker"
          mode="selector"
          :range="devOptions"
          :value="devIndex"
          @change="onDevChange"
        >
          <view class="picker-inner">
            <text class="picker-label">设备</text>
            <text class="picker-value">{{ devOptions[devIndex] }}</text>
            <text class="picker-arrow">▾</text>
          </view>
        </picker>

        <view class="stat-row">
          <view class="stat">
            <text class="stat-num warn">
              {{ medPendingCount }}
            </text>
            <text class="stat-label">待服药</text>
          </view>
          <view class="stat">
            <text class="stat-num ok">
              {{ medConfirmedCount }}
            </text>
            <text class="stat-label">已确认</text>
          </view>
          <view class="stat">
            <text class="stat-num">
              {{ medTotalCount }}
            </text>
            <text class="stat-label">今日合计</text>
          </view>
        </view>

        <!-- A. 今日待确认：逐条列出「哪一次、什么药、哪台设备」，并给代确认入口。
             只给上面那个计数，家属看到「还有 1 次没吃」仍然不知道该催哪一次。 -->
        <view class="sec-title">
          待确认
        </view>
        <view v-if="pendingItems.length" class="list">
          <view v-for="it in pendingItems" :key="'p' + keyOf(it)" class="med-item">
            <view class="row between">
              <view class="med-head">
                <text class="med-time">{{ fmtHM(it.due_at) }}</text>
                <text :class="['tag', stateClass(it.state)]">{{ stateText(it) }}</text>
              </view>
              <button
                v-if="it.state !== 0"
                class="btn small confirm"
                :disabled="confirmingKey === keyOf(it)"
                @click="confirmForElder(it)"
              >
                {{ confirmingKey === keyOf(it) ? '提交中…' : '代确认' }}
              </button>
              <!-- state === 0 是「还没到点」。给药不该给代确认按钮：
                   08:00 的药在 06:00 就被家属点掉，等于替老人谎报服药，
                   而且这条会从待确认里消失，真到点没人再提醒。 -->
              <text v-else class="muted">
                未到时间
              </text>
            </view>
            <view class="med-title">
              {{ it.title || '—' }}
            </view>
            <view v-if="showDeviceCol" class="med-meta">
              {{ it.device_name || ('设备 ' + it.device_id) }}
            </view>
          </view>
        </view>
        <view v-else-if="!hasItems && medPendingCount > 0" class="tip">
          服务端版本较旧，只能给出条数、给不出明细，请升级服务端
        </view>
        <view v-else class="tip">
          今天没有待确认的提醒
        </view>

        <!-- B. 今日已确认：老人自己说的、按的，还是子女代确认的，一眼分得清 -->
        <view class="sec-title">
          已确认
        </view>
        <view v-if="confirmedItems.length" class="list">
          <view v-for="it in confirmedItems" :key="'d' + keyOf(it)" class="med-item">
            <view class="row between">
              <view class="med-head">
                <text class="med-time">{{ fmtHM(it.due_at) }}</text>
                <text :class="['tag', methodClass(it.confirm_method)]">{{ methodText(it) }}</text>
              </view>
              <text class="muted">
                {{ fmtTime(it.confirmed_at) }}
              </text>
            </view>
            <view class="med-title">
              {{ it.title || '—' }}
            </view>
            <view v-if="showDeviceCol" class="med-meta">
              {{ it.device_name || ('设备 ' + it.device_id) }}
            </view>
          </view>
        </view>
        <view v-else class="tip">
          今天还没有已确认的提醒
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
.muted { font-size: 24rpx; color: #8a8f99; }

.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 28rpx; padding: 18rpx 0; }
.btn.small { padding: 10rpx 28rpx; font-size: 26rpx; margin-top: 0; line-height: 1.6; }
.btn.confirm { background: #e8590c; }
.btn.confirm[disabled] { background: #f0b48a; }

/* 设备 */
.device { border-top: 1rpx solid #f1f3f6; padding: 20rpx 0 4rpx; }
.device-name { font-size: 30rpx; font-weight: 600; }
.device-sub { font-size: 24rpx; color: #8a8f99; margin-top: 8rpx; }
.device-summary { font-size: 24rpx; color: #8a8f99; margin-top: 16rpx; }
.tag { font-size: 22rpx; color: #8a8f99; background: #f1f3f6; border-radius: 20rpx; padding: 4rpx 16rpx; }
.tag.ok { color: #16a34a; background: #e8f7ee; }
.tag.warn { color: #e8590c; background: #fff7ed; }
.tag.bad { color: #dc2626; background: #fef2f2; }
.tag.app { color: #7c3aed; background: #f5f3ff; }
.tag.voice { color: #0369a1; background: #e0f2fe; }

/* 设备筛选 */
.dev-picker { margin: 16rpx 0 4rpx; }
.picker-inner { display: flex; flex-direction: row; align-items: center; background: #f5f7fa; border-radius: 12rpx; padding: 16rpx 20rpx; }
.picker-label { font-size: 24rpx; color: #8a8f99; margin-right: 12rpx; }
.picker-value { flex: 1; font-size: 26rpx; color: #1f2937; }
.picker-arrow { font-size: 22rpx; color: #8a8f99; }

/* 服药统计 */
.stat-row { display: flex; margin: 20rpx 0 8rpx; }
.stat { flex: 1; text-align: center; }
.stat-num { display: block; font-size: 52rpx; font-weight: 700; color: #1f2937; }
.stat-num.warn { color: #e8590c; }
.stat-num.ok { color: #16a34a; }
.stat-label { display: block; font-size: 24rpx; color: #8a8f99; margin-top: 4rpx; }

/* 明细 */
.sec-title { font-size: 26rpx; font-weight: 600; color: #374151; margin: 24rpx 0 4rpx; }
.list { display: flex; flex-direction: column; }
.med-item { border-top: 1rpx solid #f1f3f6; padding: 20rpx 0 16rpx; }
.med-head { display: flex; flex-direction: row; align-items: center; }
.med-time { font-size: 30rpx; font-weight: 700; color: #1f2937; margin-right: 12rpx; }
.med-title { font-size: 28rpx; color: #374151; margin-top: 8rpx; }
.med-meta { font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; }

/* 空态 */
.empty { text-align: center; padding: 60rpx 28rpx; }
.empty-title { font-size: 34rpx; font-weight: 600; }
.empty-sub { font-size: 26rpx; color: #8a8f99; margin: 16rpx 0 28rpx; }
</style>
