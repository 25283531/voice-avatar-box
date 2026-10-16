<route lang="json">
{
  "style": { "navigationBarTitleText": "找回密码" }
}
</route>

<script setup lang="ts">
/**
 * 找回密码（两步：邮箱收验证码 -> 填验证码 + 新密码）。
 *
 * 这条链路存在的意义：**用户登不进来才需要找回密码**，所以它必须完全匿名可用，
 * 不能要求先登录，也不能把人推给"我的 → 修改密码"（那是登录后才有的东西）。
 * 所有请求都走 auth:false。
 */
import { computed, onUnmounted, ref } from 'vue'
import { isResetFail, requestPasswordReset, verifyPasswordReset } from '@/api/elder'
import type { ElderRawResult } from '@/http/elder'

/** 1 填邮箱 | 2 验证码+新密码 | 3 完成 */
const step = ref<1 | 2 | 3>(1)
const email = ref('')
const code = ref('')
const pwd = ref('')
const pwdAgain = ref('')

const sending = ref(false)
const verifying = ref(false)
/** 页面上直接显示的错误话术（不用 toast：老人看不清也记不住一闪而过的字） */
const err = ref('')
/** 验证码有效期提示，服务端给多少分钟就显示多少 */
const expireText = ref('')
/** 重发倒计时剩余秒数，> 0 时按钮置灰 */
const cooldown = ref(0)
const resetUser = ref('')

let timer: ReturnType<typeof setInterval> | null = null

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

const canResend = computed(() => cooldown.value <= 0 && !sending.value)

function startCooldown(sec: number) {
  stopCooldown()
  cooldown.value = Math.max(1, Math.floor(sec))
  timer = setInterval(() => {
    cooldown.value -= 1
    if (cooldown.value <= 0) stopCooldown()
  }, 1000)
}

function stopCooldown() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  cooldown.value = 0
}

onUnmounted(stopCooldown)

/** 后端 400 的 detail 可能是字符串、对象，也可能是 pydantic 的校验数组，统一掏一句人话出来 */
function messageOf(res: ElderRawResult<any>): string {
  const body: any = res.data
  const detail = body?.detail
  if (Array.isArray(detail)) return '填写的内容不对，请检查后再试'
  if (typeof detail === 'string') return detail
  if (detail?.message) return String(detail.message)
  if (body?.message) return String(body.message)
  return ''
}

/** 从"请 42 秒后再试"这种文案里掏出秒数，用于把按钮继续按住 */
function secondsOf(text: string): number {
  const m = String(text).match(/(\d+)\s*秒/)
  return m ? Number(m[1]) : 0
}

/** 第 1 步 / 第 2 步换一个邮箱时清掉上一步的残留输入 */
function clearCode() {
  code.value = ''
  pwd.value = ''
  pwdAgain.value = ''
  err.value = ''
}

async function onSend() {
  if (sending.value || !canResend.value) return
  const mail = email.value.trim()
  err.value = ''
  if (!EMAIL_RE.test(mail)) {
    err.value = '请先填写正确的邮箱地址，例如 abc@163.com'
    return
  }

  sending.value = true
  try {
    const res = await requestPasswordReset(mail)
    if (res.statusCode >= 200 && res.statusCode < 300) {
      const data = res.data
      const minutes = Math.round((data?.expires_in_sec || 900) / 60)
      expireText.value = `验证码 ${minutes} 分钟内有效`
      startCooldown(data?.cooldown_sec || 60)
      clearCode()
      step.value = 2
      return
    }
    // 429 冷却：按钮同步进入倒计时，避免让用户干猜还要等多久
    if (res.statusCode === 429) {
      const msg = messageOf(res)
      err.value = msg ? `${msg}（每 60 秒只能发一次，防止被人用来刷邮件）` : '验证码刚发过，请稍等一会儿再试'
      startCooldown(secondsOf(msg) || 60)
      return
    }
    // 503：平台没配 SMTP，用户再怎么试也收不到，必须直接说清楚 + 给出口（工单）
    if (res.statusCode === 503) {
      err.value = '平台还没有开通邮件服务，验证码发不出去。请点下面的"找管理员帮忙"提交问题，我们会尽快处理。'
      return
    }
    err.value = messageOf(res) || '验证码发送失败，请稍后再试'
  }
  catch {
    err.value = '网络连接不上，请确认手机有网后再试'
  }
  finally {
    sending.value = false
  }
}

function backToStep1() {
  stopCooldown()
  clearCode()
  step.value = 1
}

async function onSubmitReset() {
  if (verifying.value) return
  err.value = ''

  if (!/^\d{6}$/.test(code.value.trim())) {
    err.value = '请照着邮件里的 6 位数字验证码填一遍'
    return
  }
  if (pwd.value.length < 6) {
    err.value = '新密码至少要 6 位'
    return
  }
  if (pwd.value !== pwdAgain.value) {
    err.value = '两次输入的新密码不一样，请重新输入'
    return
  }

  verifying.value = true
  try {
    const res = await verifyPasswordReset({
      email: email.value.trim(),
      code: code.value.trim(),
      new_password: pwd.value,
    })

    if (res.statusCode >= 200 && res.statusCode < 300) {
      stopCooldown()
      resetUser.value = (res.data as any)?.username || ''
      err.value = ''
      step.value = 3
      // 留 2 秒让用户看清"已经改好了"，然后自动回登录页
      setTimeout(() => uni.reLaunch({ url: '/pages/login/index' }), 2000)
      return
    }

    if (isResetFail(res)) {
      const left = Number(res.data.attempts_left || 0)
      if (left > 0) {
        err.value = `验证码不对或已经过期，你还可以试 ${left} 次，用完就要重新发送了`
      }
      else {
        err.value = '验证码已经不能再用了，请回到上一步重新发送一封'
        setTimeout(backToStep1, 1200)
      }
      return
    }

    if (res.statusCode === 429) {
      err.value = '操作太频繁了，请等一分钟后再试'
      return
    }
    if (res.statusCode === 503) {
      err.value = '平台还没有开通邮件服务，请点下面的"找管理员帮忙"提交问题。'
      return
    }
    err.value = messageOf(res) || '密码重置失败，请稍后再试'
  }
  catch {
    err.value = '网络连接不上，请确认手机有网后再试'
  }
  finally {
    verifying.value = false
  }
}

function openTickets() {
  uni.navigateTo({ url: '/pages/profile/tickets' })
}
</script>

<template>
  <view class="page">
    <view class="card">
      <view class="steps">
        <text class="step" :class="{ on: step === 1 }">1 填写邮箱</text>
        <text class="sep">›</text>
        <text class="step" :class="{ on: step === 2 }">2 设置新密码</text>
        <text class="sep">›</text>
        <text class="step" :class="{ on: step === 3 }">3 完成</text>
      </view>

      <!-- 第 1 步：填邮箱 -->
      <view v-if="step === 1" class="pane">
        <view class="lead">
          填写你注册时用过的邮箱，系统会发一个 6 位验证码给你。
        </view>
        <input v-model="email" class="input" placeholder="邮箱地址，例如 abc@163.com" maxlength="64" />
        <view v-if="err" class="err">
          {{ err }}
        </view>
        <button class="btn" :disabled="sending || cooldown > 0" @click="onSend">
          {{ sending ? '正在发送…' : (cooldown > 0 ? `请等 ${cooldown} 秒再点` : '发送验证码') }}
        </button>
      </view>

      <!-- 第 2 步：验证码 + 新密码 -->
      <view v-else-if="step === 2" class="pane">
        <!-- 措辞必须带"如果"：未注册邮箱的响应完全一致，不能暗示邮箱存不存在 -->
        <view class="sent">
          如果该邮箱已注册，验证码已经发出去了，请打开邮箱查看（也可能在垃圾邮件箱里）。{{ expireText }}
        </view>

        <view class="mail-row">
          <text class="mail">{{ email }}</text>
          <text class="mail-edit" @click="backToStep1">换一个邮箱</text>
        </view>

        <input v-model="code" class="input" type="number" placeholder="邮件里的 6 位验证码" maxlength="6" />
        <input v-model="pwd" class="input" password placeholder="新的密码（至少 6 位）" maxlength="32" />
        <input v-model="pwdAgain" class="input" password placeholder="再输入一次新密码" maxlength="32" />

        <view v-if="err" class="err">
          {{ err }}
        </view>

        <button class="btn" :disabled="verifying" @click="onSubmitReset">
          {{ verifying ? '正在提交…' : '确认修改密码' }}
        </button>
        <button class="btn ghost" :disabled="!canResend" @click="onSend">
          {{ cooldown > 0 ? `重新发送（${cooldown} 秒后可以再发）` : '没收到？重新发送验证码' }}
        </button>
      </view>

      <!-- 第 3 步：完成 -->
      <view v-else class="pane">
        <view class="done-title">
          密码已经改好了
        </view>
        <view class="done-sub">
          马上回到登录页，用账号{{ resetUser ? ` ${resetUser} ` : '' }}和新密码登录就可以了。
        </view>
        <view class="done-tip">
          正在自动返回登录页…
        </view>
      </view>
    </view>

    <view class="help" @click="openTickets">
      还是弄不好？点这里找管理员帮忙
    </view>
  </view>
</template>

<style scoped>
.page { padding: 48rpx 40rpx; background: #f5f7fa; min-height: 100vh; }
.card { background: #fff; border-radius: 24rpx; padding: 36rpx 32rpx; }

/* 步骤条 */
.steps { display: flex; flex-direction: row; align-items: center; justify-content: center; margin-bottom: 32rpx; }
.step { font-size: 28rpx; color: #8a8f99; }
.step.on { color: #e8590c; font-weight: 700; }
.sep { font-size: 28rpx; color: #c8cdd4; margin: 0 12rpx; }

.pane { display: flex; flex-direction: column; }
.lead { font-size: 30rpx; color: #1f2937; line-height: 1.6; margin-bottom: 24rpx; }
.sent { font-size: 30rpx; color: #9a3412; background: #fff7ed; border: 1rpx solid #fed7aa; border-radius: 12rpx; padding: 20rpx; line-height: 1.6; margin-bottom: 24rpx; }
.mail-row { display: flex; flex-direction: row; align-items: center; justify-content: space-between; margin-bottom: 20rpx; }
.mail { font-size: 30rpx; color: #1f2937; font-weight: 600; }
.mail-edit { font-size: 30rpx; color: #e8590c; }

.input { border: 1rpx solid #e3e8ef; border-radius: 12rpx; padding: 22rpx; margin-bottom: 20rpx; font-size: 30rpx; background: #fff; width: auto; }
.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 32rpx; padding: 22rpx 0; font-weight: 600; }
.btn[disabled] { background: #f0b48a; }
.btn.ghost { background: #fff; color: #e8590c; border: 1rpx solid #e8590c; margin-top: 16rpx; font-weight: 500; }

.err { font-size: 30rpx; color: #dc2626; line-height: 1.6; background: #fef2f2; border: 1rpx solid #fecaca; border-radius: 12rpx; padding: 18rpx 20rpx; margin-bottom: 20rpx; }

.done-title { font-size: 40rpx; font-weight: 700; color: #16a34a; text-align: center; margin-top: 24rpx; }
.done-sub { font-size: 30rpx; color: #1f2937; line-height: 1.7; text-align: center; margin-top: 24rpx; }
.done-tip { font-size: 28rpx; color: #8a8f99; text-align: center; margin-top: 24rpx; }

.help { font-size: 30rpx; color: #e8590c; text-align: center; margin-top: 32rpx; text-decoration: underline; }
</style>
