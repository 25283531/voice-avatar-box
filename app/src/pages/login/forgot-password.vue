<route lang="json">
{
  "style": { "navigationBarTitleText": "找回密码" }
}
</route>

<script setup lang="ts">
/**
 * 找回密码 —— 五步：用户名 → 绑定邮箱 → 验证码 → 自己设新密码 → 完成。
 *
 * 为什么是「用户名」而不是「邮箱」开头（2026-10-19 用户要求）
 * ----------------------------------------------------------
 * 老人常常只记得自己注册时用的**用户名**；邮箱在哪张卡片上、拼对没有，
 * 都说不清。若让他先填邮箱，等于要求「必须先知道邮箱才能找回密码」，
 * 而忘了密码的人恰恰最可能同时忘了邮箱。
 * 所以用户名放在最前面，邮箱降级为**第二道确认**。
 *
 * ⚠️ 第 4 步的语义在 2026-06-19 改过：原先是「服务端把新密码寄到邮箱」，
 * 现在改为「用户自己在 APP 里设新密码」，邮件只发验证码。
 * 理由：明文口令躺在收件箱里（转发/备份/邮件网关都会留存），而且老人
 * 还得把它抄到纸上才能登录，抄错一位就登不进来 —— 这套流程反人性。
 * 改成自设后，**口令从不出现在邮件里**，也不再需要「抄」这个动作。
 *
 * 与服务端那套「邮箱 → 验证码 → 自己设新密码」并存：
 * 那条对知道邮箱的人更省事（少一步邮箱确认），本页多花一步但起点是用户名。
 *
 * ⚠️ 三条不能在前端"优化"的规则（都是账号枚举防护，见服务端同名注释）：
 *   1. email_bound=false **不能**翻译成「这个用户名不存在」——
 *      服务端对「未绑邮箱」和「用户名不存在」返回同一个 false，
 *      前端若替它区分，就等于前端替服务端开了个枚举洞。
 *      本页统一显示「没有绑定邮箱 → 联系管理员」。
 *   2. matched=false 时**不要**显示任何"正确邮箱是…"的提示 —— 服务端根本没给，
 *      前端也不该猜。
 *   3. 验证码错误/过期/账号不存在，服务端返回**同一句话**，前端照原样显示。
 *
 * 所有请求都走 auth:false：用户此刻登不进来，这条链路必须完全匿名可用。
 *
 * ⚠️ 跳转一律用 script 里的具名函数，不在模板里写 uni.xxx：
 *    模板表达式里的 `uni.*` 不会被插件改写，产物会变成 o.uni.xxx，
 *    TypeError 被 Vue 事件处理器静默吞掉 —— 构建全绿但点击没反应。
 */
import { computed, onUnmounted, ref } from 'vue'
import { issueResetCode, lookupAccount, resetPassword } from '@/api/elder'
import type { ElderRawResult } from '@/http/elder'

/** 1 用户名 | 2 邮箱确认 | 3 验证码 | 4 新密码 | 5 完成 */
const step = ref<1 | 2 | 3 | 4 | 5>(1)
const username = ref('')
const email = ref('')
const code = ref('')
const newPwd = ref('')
const newPwd2 = ref('')

/** 密码输入框是原生控件，类型选错会露出明文；这里统一在模板里绑定 password */
const busy = ref(false)
/** 页面上直接显示的错误话术—— 不用 toast：老人看不清也记不住一闪而过的字 */
const err = ref('')
/** 提示性文案（不是错误），比如"验证码已发送" */
const tip = ref('')
/** 服务端给的验证码有效期 / 冷却秒数 */
const expireText = ref('')
const cooldown = ref(0)
/** 服务端回传的管理员邮箱（未绑邮箱时展示，不在前端硬编码） */
const adminEmail = ref('')

let timer: ReturnType<typeof setInterval> | null = null

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

const canResend = computed(() => cooldown.value <= 0 && !busy.value)

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

/** 后端 400 的 detail 可能是字符串、对象，也可能是 pydantic 的校验数组，统一掏一句人话 */
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

/* ------------------------------------------------------------ 第1 步：用户名 */
async function onLookup() {
  if (busy.value) return
  const name = username.value.trim()
  err.value = ''
  tip.value = ''
  if (!name) {
    err.value = '请先填写你的账号名'
    return
  }
  busy.value = true
  try {
    const res = await lookupAccount(name)
    if (res.statusCode >= 200 && res.statusCode < 300) {
      const data = res.data
      if (data.email_bound) {
        // 有邮箱 → 进第 2 步问邮箱
        step.value = 2
        return
      }
      //⚠️ 不管服务端是「未绑邮箱」还是「用户名不存在」，
      //    这里都显示同一句。用户分不出二者正是设计意图。
      adminEmail.value = data.admin_email || ''
      err.value = '这个账号没有绑定邮箱，无法自己重置密码。'
      tip.value = adminEmail.value
        ? `请联系管理员重置，发送邮件至 ${adminEmail.value}`
        : '请联系管理员重置'
      return
    }
    if (res.statusCode === 503) {
      err.value = '平台服务暂时不可用，请稍后再试或联系管理员'
      return
    }
    err.value = messageOf(res) || '查询失败，请稍后再试'
  }
  catch {
    err.value = '网络连接不上，请确认手机有网后再试'
  }
  finally {
    busy.value = false
  }
}

/* ------------------------------------------------------------ 第2 步：邮箱 */
async function onIssue() {
  if (busy.value || !canResend.value) return
  const mail = email.value.trim()
  err.value = ''
  tip.value = ''
  if (!EMAIL_RE.test(mail)) {
    err.value = '请先填写正确的邮箱地址，例如 abc@163.com'
    return
  }
  busy.value = true
  try {
    const res = await issueResetCode(username.value.trim(), mail)
    if (res.statusCode === 429) {
      const msg = messageOf(res)
      err.value = msg || '验证码刚发过，请稍等一会儿再试'
      startCooldown(secondsOf(msg) || 60)
      return
    }
    if (res.statusCode === 503) {
      err.value = '平台还没有开通邮件服务，验证码发不出去。请联系管理员帮忙处理。'
      return
    }
    if (res.statusCode === 502) {
      err.value = '验证码邮件发送失败，请稍后再试或联系管理员'
      return
    }
    if (res.statusCode >= 200 && res.statusCode < 300) {
      if (!res.data.matched) {
        // ⚠️ 只说"不匹配，请重新填"，不给正确邮箱（服务端也没给）
        err.value = '邮箱与账号不匹配，请重新填写这个账号注册时使用的邮箱'
        return
      }
      const minutes = Math.round((res.data.expires_in_sec || 900) / 60)
      expireText.value = `验证码 ${minutes} 分钟内有效`
      startCooldown(res.data.cooldown_sec || 60)
      code.value = ''
      err.value = ''
      tip.value = '验证码已经发到你的邮箱，请查收（也可能在垃圾邮件箱里）'
      step.value = 3
      return
    }
    err.value = messageOf(res) || '验证码发送失败，请稍后再试'
  }
  catch {
    err.value = '网络连接不上，请确认手机有网后再试'
  }
  finally {
    busy.value = false
  }
}

/* --------------------------------------------------------- 第3 步：验证码 */
function onCodeNext() {
  if (busy.value) return
  const c = code.value.trim()
  err.value = ''
  if (!/^\d{6}$/.test(c)) {
    err.value = '请照着邮件里的 6 位数字验证码填一遍'
    return
  }
  // 只做本地格式校验就往下走：验证码的真伪由服务端在第 4 步提交时校验。
  // 在这里就调接口会让用户为"填错一位"多消耗一次尝试次数
  // （服务端是 5 次上限，用完就得重发验证码）。
  newPwd.value = ''
  newPwd2.value = ''
  step.value = 4
}

/* ------------------------------------------------------- 第4 步：设置新密码 */
async function onReset() {
  if (busy.value) return
  const c = code.value.trim()
  const p1 = newPwd.value
  const p2 = newPwd2.value
  err.value = ''
  // 下限 6 位，与服务端 PasswordSendIn.new_password 的 min_length 一致。
  // 这里先挡一道是为了不让用户白等一次网络往返。
  if (p1.length < 6) {
    err.value = '新密码至少要 6 位，请再长一点'
    return
  }
  if (p1 !== p2) {
    err.value = '两次输入的密码不一样，请再核对一遍'
    return
  }
  busy.value = true
  try {
    const res = await resetPassword(username.value.trim(), email.value.trim(), c, p1)
    if (res.statusCode >= 200 && res.statusCode < 300) {
      stopCooldown()
      err.value = ''
      step.value = 5
      // 留 4 秒让用户看清「密码已重置」，然后自动回登录页
      setTimeout(() => uni.reLaunch({ url: '/pages/login/index' }), 4000)
      return
    }
    const msg = messageOf(res)
    // attempts_left 要转达：老人反复输错时，「还能试几次」比「错了」有用得多
    const detail: any = (res.data as any)?.detail
    const left = Number(detail?.attempts_left ?? 0)
    if (left > 0) {
      err.value = `验证码不对或已经过期，你还可以试 ${left} 次`
      // 验证码可疑 ⇒ 退回第 3 步重输，别让他在第 4 步反复改密码
      step.value = 3
    }
    else if (res.statusCode === 422) {
      err.value = '新密码太短了，至少要 6 位'
    }
    else if (res.statusCode === 400) {
      err.value = msg || '验证码错误或已过期，请回到上一步重新发送'
      step.value = 3
    }
    else {
      err.value = msg || '操作失败，请稍后再试'
    }
  }
  catch {
    err.value = '网络连接不上，请确认手机有网后再试'
  }
  finally {
    busy.value = false
  }
}

/** 第4 步的「返回上一步」：回验证码页重输，不清 code（省得再抄一遍） */
function backToStep3() {
  if (busy.value) return
  newPwd.value = ''
  newPwd2.value = ''
  err.value = ''
  step.value = 3
}

function backToStep1() {
  stopCooldown()
  code.value = ''
  email.value = ''
  newPwd.value = ''
  newPwd2.value = ''
  err.value = ''
  tip.value = ''
  step.value = 1
}
</script>

<template>
  <view class="page">
    <view class="card">
      <view class="steps">
        <text class="step" :class="{ on: step === 1 }">1 账号</text>
        <text class="sep">›</text>
        <text class="step" :class="{ on: step === 2 }">2 邮箱</text>
        <text class="sep">›</text>
        <text class="step" :class="{ on: step === 3 }">3 验证码</text>
        <text class="sep">›</text>
        <text class="step" :class="{ on: step === 4 || step === 5 }">4 新密码</text>
      </view>

      <!-- 第 1 步：填用户名 -->
      <view v-if="step === 1" class="pane">
        <view class="lead">
          先填你的账号名，我们会用它找到你注册的邮箱。
        </view>
        <input v-model="username" class="input" placeholder="账号名" :maxlength="64" />
        <view v-if="err" class="err">
          {{ err }}
        </view>
        <view v-if="tip" class="tip-box">
          {{ tip }}
        </view>
        <button class="btn" :disabled="busy" @click="onLookup">
          {{ busy ? '正在查询…' : '下一步' }}
        </button>
      </view>

      <!-- 第 2 步：确认绑定邮箱 -->
      <view v-else-if="step === 2" class="pane">
        <view class="lead">
          请再填一下这个账号注册时用的邮箱，用来确认是你本人。
        </view>
        <view class="mail-row">
          <text class="mail-label">账号</text>
          <text class="mail">{{ username }}</text>
          <text class="mail-edit" @click="backToStep1">换一个</text>
        </view>
        <input v-model="email" class="input" placeholder="邮箱地址，例如 abc@163.com" :maxlength="64" />
        <view v-if="err" class="err">
          {{ err }}
        </view>
        <button class="btn" :disabled="busy || cooldown > 0" @click="onIssue">
          {{ busy ? '正在发送…' : (cooldown > 0 ? `请等 ${cooldown} 秒再点` : '下一步') }}
        </button>
      </view>

      <!-- 第 3 步：验证码 -->
      <view v-else-if="step === 3" class="pane">
        <view class="sent">
          {{ tip }}
        </view>
        <view class="mail-row">
          <text class="mail">{{ email }}</text>
          <text class="mail-edit" @click="backToStep1">换一个账号</text>
        </view>
        <view class="sub">
          验证码 {{ expireText }}
        </view>
        <input v-model="code" class="input" type="number" placeholder="邮件里的 6 位验证码" :maxlength="6" />
        <view v-if="err" class="err">
          {{ err }}
        </view>
        <button class="btn" :disabled="busy" @click="onCodeNext">
          下一步（设置新密码）
        </button>
        <button class="btn ghost" :disabled="!canResend" @click="onIssue">
          {{ cooldown > 0 ? `重新发送（${cooldown} 秒后可以再发）` : '没收到？重新发送验证码' }}
        </button>
      </view>

      <!-- 第 4 步：自己设新密码（口令不再经过邮件） -->
      <view v-else-if="step === 4" class="pane">
        <view class="lead">
          请自己想一个新的密码。它不会被发到任何邮箱，只存在这台手机上。
        </view>
        <view class="mail-row">
          <text class="mail-label">账号</text>
          <text class="mail">{{ username }}</text>
        </view>
        <input v-model="newPwd" class="input" password placeholder="新密码，至少 6 位" :maxlength="128" />
        <input v-model="newPwd2" class="input" password placeholder="再输一遍新密码" :maxlength="128" />
        <view v-if="err" class="err">
          {{ err }}
        </view>
        <button class="btn" :disabled="busy" @click="onReset">
          {{ busy ? '正在保存…' : '确定，修改密码' }}
        </button>
        <button class="btn ghost" :disabled="busy" @click="backToStep3">
          返回上一步
        </button>
      </view>

      <!-- 第 5 步：完成 -->
      <view v-else class="pane">
        <view class="done-title">
          密码已重置
        </view>
        <view class="done-sub">
          请用刚才设置的新密码登录。密码只存在这台手机上，没有发到任何邮箱。
        </view>
        <view class="done-tip">
          正在自动返回登录页…
        </view>
      </view>
    </view>
  </view>
</template>

<style scoped>
.page { padding: 48rpx 40rpx; background: #f5f7fa; min-height: 100vh; }
.card { background: #fff; border-radius: 24rpx; padding: 36rpx 32rpx; }

/* 步骤条 */
.steps { display: flex; flex-direction: row; align-items: center; justify-content: center; margin-bottom: 32rpx; }
.step { font-size: 26rpx; color: #8a8f99; }
.step.on { color: #e8590c; font-weight: 700; }
.sep { font-size: 26rpx; color: #c8cdd4; margin: 0 10rpx; }

.pane { display: flex; flex-direction: column; }
.lead { font-size: 30rpx; color: #1f2937; line-height: 1.6; margin-bottom: 24rpx; }
.sent { font-size: 28rpx; color: #9a3412; background: #fff7ed; border: 1rpx solid #fed7aa; border-radius: 12rpx; padding: 20rpx; line-height: 1.6; margin-bottom: 20rpx; }
.mail-row { display: flex; flex-direction: row; align-items: center; margin-bottom: 20rpx; }
.mail-label { font-size: 28rpx; color: #8a8f99; margin-right: 12rpx; }
.mail { flex: 1; font-size: 30rpx; color: #1f2937; font-weight: 600; word-break: break-all; }
.mail-edit { font-size: 28rpx; color: #e8590c; }
.sub { font-size: 26rpx; color: #8a8f99; margin-bottom: 16rpx; }

/* ⚠️ 输入框必须给显式 height/line-height，不能靠 padding 撑：
   APP 端<input> 是原生控件，内容盒高度与 padding 无关，
   只压 padding 会出现「占位符正常但输入内容被裁掉上半截」（登录页踩过）。 */
.input {
  border: 1rpx solid #e3e8ef; border-radius: 12rpx;
  height: 88rpx; line-height: 88rpx;
  padding: 0 22rpx; margin-bottom: 20rpx;
  font-size: 30rpx; background: #fff; width: auto;
}
.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 32rpx; height: 88rpx; line-height: 88rpx; padding: 0; font-weight: 600; }
.btn[disabled] { background: #f0b48a; }
.btn.ghost { background: #fff; color: #e8590c; border: 1rpx solid #e8590c; margin-top: 16rpx; font-weight: 500; }

.err { font-size: 28rpx; color: #dc2626; line-height: 1.6; background: #fef2f2; border: 1rpx solid #fecaca; border-radius: 12rpx; padding: 18rpx 20rpx; margin-bottom: 20rpx; }
.tip-box { font-size: 28rpx; color: #1f2937; line-height: 1.6; background: #eff6ff; border: 1rpx solid #bfdbfe; border-radius: 12rpx; padding: 18rpx 20rpx; margin-bottom: 20rpx; }

.done-title { font-size: 38rpx; font-weight: 700; color: #16a34a; text-align: center; margin-top: 24rpx; }
.done-sub { font-size: 28rpx; color: #1f2937; line-height: 1.7; text-align: center; margin-top: 24rpx; }
.done-tip { font-size: 26rpx; color: #8a8f99; text-align: center; margin-top: 24rpx; }
</style>