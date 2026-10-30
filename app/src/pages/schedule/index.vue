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
  listReminderAudits,
  listReminders,
  restoreReminder,
  toggleReminder,
  updateReminder,
} from '@/api/elder'
import type { Device, Reminder, ReminderAudit } from '@/api/elder'
import { getElderToken } from '@/http/elder'
import { fmtDateTime } from '@/utils/time'

const devices = ref<Device[]>([])
const devicePk = ref(0)
const list = ref<Reminder[]>([])
const loading = ref(false)

/** 0=服药 1=日程 */
const segment = ref(0)

/**
 * 一级视图：'list' = 当前提醒 / 'history' = 操作历史。
 *
 * 为什么要有历史：提醒是**软删除**（删掉的规则还在库里），不给入口，
 * 那些数据对家属就等于不存在 —— 用户要的正是「删了也能查到」。
 */
const view = ref<'list' | 'history'>('list')
const audits = ref<ReminderAudit[]>([])
const auditLoading = ref(false)
const auditHasMore = ref(false)

const typeNames = ['服药', '日程', '喝水', '运动', '自定义']
const weekCn = ['一', '二', '三', '四', '五', '六', '日']

const current = computed(() => {
  // 已删除的排到后面：它们还在列表里（家属要看得见自己删过什么），
  // 但不该插在正在生效的提醒中间抢注意力。
  return list.value
    .filter(r => r.rtype === segment.value)
    .sort((a, b) => Number(a.is_deleted) - Number(b.is_deleted))
})
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

/** 操作人：actor_name 为空时退回 via_label（如「手机 APP」），都空才显示破折号 */
function actorText(a: ReminderAudit) {
  return a.actor_name || a.via_label || a.via || '—'
}

async function refresh() {
  if (!getElderToken()) return
  loading.value = true
  try {
    if (devices.value.length === 0) {
      devices.value = await listDevices()
      if (devices.value.length && !devicePk.value) devicePk.value = devices.value[0].id
    }
    // includeDeleted：已删除的提醒也要列出来（带「已删除」灰标 + 恢复按钮）。
    // 不传的话它们会凭空消失，家属会以为"数据丢了"而不是"我删了、还能恢复"。
    if (devicePk.value) list.value = await listReminders(devicePk.value, { includeDeleted: true })
  }
  catch { /* 请求层已提示 */ }
  finally {
    loading.value = false
  }
}

/** 拉一页操作历史。reset=true 从头拉（进页面 / 换设备 / 刚恢复完），false 是加载更多。 */
async function loadAudits(reset: boolean) {
  if (!devicePk.value) return
  auditLoading.value = true
  try {
    const page = await listReminderAudits(devicePk.value, {
      limit: 100,
      offset: reset ? 0 : audits.value.length,
    })
    audits.value = reset ? page : audits.value.concat(page)
    // 后端没给 total：这一页装满 100 条就认为可能还有下一页。
    // 少给一次「加载更多」好过给一个点了没反应的按钮。
    auditHasMore.value = page.length >= 100
  }
  catch { auditHasMore.value = false /* 请求层已提示 */ }
  finally {
    auditLoading.value = false
  }
}

function switchView(v: 'list' | 'history') {
  view.value = v
  if (v === 'history') loadAudits(true)
}

function onLoadMore() {
  loadAudits(false)
}

/** 恢复一条已删除的提醒（列表里的灰标行、历史里的 can_restore 行共用） */
async function doRestore(id: number, title: string) {
  try {
    await restoreReminder(id)
    uni.showToast({ title: '已恢复', icon: 'success' })
    await refresh()
    // 历史若正开着，流水里要出现这条 restore —— 否则家属刚恢复、回头
    // 一看历史里没有"恢复"记录，会怀疑到底成没成。
    if (view.value === 'history') await loadAudits(true)
    else if (title) uni.showToast({ title: `已恢复「${title}」`, icon: 'none' })
  }
  catch { /* 请求层已提示 */ }
}

function onRestore(r: Reminder) {
  doRestore(r.id, r.title)
}

function onRestoreFromAudit(a: ReminderAudit) {
  doRestore(a.reminder_id, a.title)
}

function onSwitchDevice(e: any) {
  devicePk.value = Number(e.detail.value)
  // 换设备必须清空历史：留着上一台的流水，家属会以为「这台设备也有过这些操作」。
  audits.value = []
  auditHasMore.value = false
  refresh()
  if (view.value === 'history') loadAudits(true)
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
    // 文案直接回答"删了还能不能找回来"：提醒是软删除，删掉只是不再响，
    // 记录仍在「历史」里。只写"确定删除？"会让人以为不可逆而不敢删。
    content: `确定删除「${r.title}」？\n删除后仍可在「操作历史」里查到，并可恢复。`,
    success: async (res) => {
      if (!res.confirm) return
      await deleteReminder(r.id)
      await refresh()
      if (view.value === 'history') await loadAudits(true)
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
      <!-- 一级切换：提醒 / 历史。
           历史是「删掉的提醒去哪了」的答案 —— 提醒软删除，规则还在库里，
           不给这个入口，家属就以为数据已经没了。 -->
      <view class="tabs">
        <view :class="['tab', view === 'list' ? 'on' : '']" @click="switchView('list')">
          提醒
        </view>
        <view :class="['tab', view === 'history' ? 'on' : '']" @click="switchView('history')">
          历史
        </view>
      </view>

      <template v-if="view === 'list'">
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
          <view v-for="r in current" :key="r.id" :class="['card', r.is_deleted ? 'deleted' : '']">
            <view class="row between">
              <text class="title">{{ r.title }}</text>
              <text v-if="r.is_deleted" class="tag del">已删除</text>
              <text v-else :class="['tag', r.enabled ? 'ok' : '']">{{ r.enabled ? '启用' : '暂停' }}</text>
            </view>
            <view class="sub">{{ typeNames[r.rtype] }} · {{ kindText(r) }}</view>
            <view class="sub times">时间 {{ (r.times_of_day || []).join('  ') }}</view>
            <view v-if="r.dose" class="sub">
              剂量 {{ r.dose }}
            </view>
            <view class="sub">下次 {{ fmt(r.next_fire_at) }}</view>
            <view class="row ops">
              <template v-if="r.is_deleted">
                <button class="btn small" @click="onRestore(r)">
                  恢复
                </button>
              </template>
              <template v-else>
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
              </template>
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

      <!-- 操作历史：动作名 / 来源 / 摘要都是**服务端算好的中文**，直接显示。
           前端不再拼一遍 —— 两处各写一套措辞，同一次操作就会出现两种说法。 -->
      <template v-else>
        <view v-if="auditLoading && audits.length === 0" class="tip">
          加载中…
        </view>

        <view v-for="a in audits" :key="a.id" :class="['card', 'audit', a.action === 'delete' ? 'audit-del' : '']">
          <view class="row between">
            <text class="title">{{ a.action_label || a.action }}</text>
            <text class="sub time">{{ fmt(a.created_at) }}</text>
          </view>
          <view class="sub">
            {{ a.title || '—' }}
          </view>
          <view class="sub">
            操作人：{{ actorText(a) }}
          </view>
          <view class="sub summary">
            {{ a.summary }}
          </view>
          <view v-if="a.can_restore" class="row ops">
            <button class="btn small" @click="onRestoreFromAudit(a)">
              恢复这条提醒
            </button>
          </view>
        </view>

        <view v-if="!auditLoading && audits.length === 0" class="tip">
          暂无操作记录
        </view>

        <button v-if="auditHasMore" class="btn add" :disabled="auditLoading" @click="onLoadMore">
          {{ auditLoading ? '加载中…' : '加载更多' }}
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
/* 已删除：灰到底，和「启用/暂停」区分开 —— 它不是一种状态，它是"已经不在生效" */
.tag.del { color: #9ca3af; background: #eef0f3; }
.card.deleted { opacity: 0.7; }
/* 历史里「删除」那一行：家属翻历史十有八九是找"我删过什么"，
   一条红左边框比任何文字标签都先被看见。 */
.audit-del { border-left: 8rpx solid #dc2626; }
.audit .time { margin-top: 0; }
.audit .summary { color: #4b5563; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; padding: 40rpx 0; }
.empty { text-align: center; padding: 60rpx 28rpx; }
.empty-title { font-size: 32rpx; font-weight: 600; }
.empty-sub { font-size: 26rpx; color: #8a8f99; margin-top: 16rpx; }
</style>
