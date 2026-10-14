<route lang="json">
{
  "style": { "navigationBarTitleText": "编辑提醒" }
}
</route>

<script setup lang="ts">
import { onLoad } from '@dcloudio/uni-app'
import { ref } from 'vue'
import { createReminder, listReminders, updateReminder } from '@/api/elder'
import type { Reminder } from '@/api/elder'

const devicePk = ref(0)
const editId = ref(0)

const typeOptions = ['服药', '日程', '喝水', '运动', '自定义']
const kindOptions = ['每天', '每周指定几天', '每月指定几号', '每隔 N 天', '指定日期一次']
const weekCn = ['一', '二', '三', '四', '五', '六', '日']

const form = ref<Partial<Reminder>>({
  title: '',
  speak_text: '',
  rtype: 0,
  schedule_kind: 1,
  times_of_day: ['08:00'],
  weekdays: [],
  month_days: [],
  interval_days: 2,
  once_date: '',
  start_date: '',
  end_date: '',
  dose: '',
  require_confirm: true,
  repeat_interval_min: 10,
  repeat_times: 3,
  escalate_after_min: 30,
  confirm_methods: ['voice', 'button'],
  enabled: true,
})

const timesText = ref('08:00')
const monthText = ref('')
const methods = ref<Record<string, boolean>>({ voice: true, button: true, app: false })

function pickType(e: any) { form.value.rtype = Number(e.detail.value) }
function pickKind(e: any) { form.value.schedule_kind = Number(e.detail.value) }

function toggleWeek(i: number) {
  const list = new Set(form.value.weekdays || [])
  const v = i + 1
  if (list.has(v)) list.delete(v)
  else list.add(v)
  form.value.weekdays = [...list].sort((a, b) => a - b)
}

function toggleMethod(k: string) {
  methods.value[k] = !methods.value[k]
}

async function load() {
  if (!editId.value) return
  const all = await listReminders(devicePk.value)
  const hit = all.find(x => x.id === editId.value)
  if (!hit) return
  form.value = { ...hit }
  timesText.value = (hit.times_of_day || []).join(', ')
  monthText.value = (hit.month_days || []).join(',')
  methods.value = {
    voice: (hit.confirm_methods || []).includes('voice'),
    button: (hit.confirm_methods || []).includes('button'),
    app: (hit.confirm_methods || []).includes('app'),
  }
}

async function save() {
  const f = form.value
  if (!f.title) {
    uni.showToast({ title: '请填写标题', icon: 'none' })
    return
  }
  const times = timesText.value.split(/[,，\s]+/).filter(Boolean)
  if (times.length === 0) {
    uni.showToast({ title: '至少填一个触发时刻', icon: 'none' })
    return
  }
  for (const t of times) {
    if (!/^\d{1,2}:\d{2}$/.test(t)) {
      uni.showToast({ title: `时刻格式不对：${t}`, icon: 'none' })
      return
    }
  }

  const payload: Partial<Reminder> = {
    ...f,
    times_of_day: times,
    month_days: monthText.value.split(/[,，\s]+/).filter(Boolean).map(Number),
    confirm_methods: Object.keys(methods.value).filter(k => methods.value[k]),
    once_date: f.once_date || null,
    start_date: f.start_date || null,
    end_date: f.end_date || null,
  }

  try {
    if (editId.value) await updateReminder(editId.value, payload)
    else await createReminder(devicePk.value, payload)
    uni.showToast({ title: '已保存', icon: 'success' })
    setTimeout(() => uni.navigateBack(), 600)
  }
  catch { /* 已提示 */ }
}

onLoad((options: any) => {
  devicePk.value = Number(options?.devicePk || 0)
  editId.value = Number(options?.id || 0)
  if (editId.value) load()
})
</script>

<template>
  <view class="page">
    <view class="card">
      <text class="label">标题</text>
      <input v-model="form.title" class="input" placeholder="如：降压药" />

      <text class="label">播报文案（留空则读标题）</text>
      <input v-model="form.speak_text" class="input" placeholder="该吃降压药了，一片" />

      <text class="label">剂量 / 备注</text>
      <input v-model="form.dose" class="input" placeholder="1 片，饭后" />

      <text class="label">类型</text>
      <picker mode="selector" :range="typeOptions" :value="form.rtype" @change="pickType">
        <view class="picker">{{ typeOptions[form.rtype || 0] }}</view>
      </picker>

      <text class="label">重复方式</text>
      <picker mode="selector" :range="kindOptions" :value="form.schedule_kind" @change="pickKind">
        <view class="picker">{{ kindOptions[form.schedule_kind || 0] }}</view>
      </picker>
    </view>

    <view class="card">
      <text class="label">触发时刻（24 小时制，逗号分隔）</text>
      <input v-model="timesText" class="input" placeholder="08:00, 12:00, 20:00" />

      <template v-if="form.schedule_kind === 2">
        <text class="label">星期（可多选）</text>
        <view class="chips">
          <text
            v-for="(w, i) in weekCn" :key="i"
            :class="['chip', (form.weekdays || []).includes(i + 1) ? 'on' : '']"
            @click="toggleWeek(i)">{{ w }}</text>
        </view>
      </template>

      <template v-if="form.schedule_kind === 3">
        <text class="label">每月几号（逗号分隔）</text>
        <input v-model="monthText" class="input" placeholder="1,15" />
      </template>

      <template v-if="form.schedule_kind === 4">
        <text class="label">每隔天数</text>
        <input v-model="form.interval_days" class="input" type="number" placeholder="2" />
      </template>

      <template v-if="form.schedule_kind === 0">
        <text class="label">日期</text>
        <input v-model="form.once_date" class="input" placeholder="2026-10-15" />
      </template>

      <text class="label">生效开始（可选）</text>
      <input v-model="form.start_date" class="input" placeholder="2026-01-01" />
      <text class="label">生效结束（可选）</text>
      <input v-model="form.end_date" class="input" placeholder="2027-12-31" />
    </view>

    <view class="card">
      <text class="label">确认与超时策略</text>
      <view class="line">
        <text class="l">需要确认</text>
        <switch :checked="form.require_confirm" color="#2563eb" @change="form.require_confirm = $event.detail.value" />
      </view>
      <view class="line">
        <text class="l">未确认重复间隔（分钟）</text>
        <input v-model="form.repeat_interval_min" class="inline" type="number" />
      </view>
      <view class="line">
        <text class="l">超时升级（分钟）</text>
        <input v-model="form.escalate_after_min" class="inline" type="number" />
      </view>
      <text class="label">允许的确认方式</text>
      <view class="chips">
        <text :class="['chip', methods.voice ? 'on' : '']" @click="toggleMethod('voice')">语音</text>
        <text :class="['chip', methods.button ? 'on' : '']" @click="toggleMethod('button')">按键</text>
        <text :class="['chip', methods.app ? 'on' : '']" @click="toggleMethod('app')">手机端</text>
      </view>
      <view class="tip">按键方式：长按 3 秒听到提示后松开，30 秒内再短按一次即视为完成</view>
    </view>

    <button class="btn save" @click="save">保 存</button>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; }
.card { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; }
.label { font-size: 24rpx; color: #6b7280; display: block; margin: 12rpx 0 8rpx; }
.input { border: 1rpx solid #e3e8ef; border-radius: 10rpx; padding: 16rpx; font-size: 28rpx; background: #fff; }
.inline { width: 160rpx; border: 1rpx solid #e3e8ef; border-radius: 8rpx; padding: 8rpx; font-size: 26rpx; text-align: right; }
.picker { border: 1rpx solid #e3e8ef; border-radius: 10rpx; padding: 16rpx; font-size: 28rpx; }
.line { display: flex; justify-content: space-between; align-items: center; padding: 12rpx 0; }
.l { font-size: 26rpx; color: #374151; }
.chips { display: flex; flex-wrap: wrap; }
.chip { padding: 10rpx 24rpx; margin: 8rpx 12rpx 0 0; border-radius: 28rpx; background: #f1f3f6; color: #6b7280; font-size: 26rpx; }
.chip.on { background: #2563eb; color: #fff; }
.tip { font-size: 22rpx; color: #8a8f99; margin-top: 12rpx; }
.btn { background: #2563eb; color: #fff; border-radius: 12rpx; font-size: 30rpx; padding: 22rpx 0; }
.save { margin-top: 12rpx; }
</style>
