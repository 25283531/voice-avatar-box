<route lang="json">
{
  "style": { "navigationBarTitleText": "登录" }
}
</route>

<script setup lang="ts">
import { ref } from 'vue'
import { elderLogin, elderRegister } from '@/api/elder'
import { setElderToken, setElderUser } from '@/http/elder'

const mode = ref<'login' | 'register'>('login')
const form = ref({ username: '', password: '', again: '', nickname: '', email: '' })
const agreed = ref(false)
const busy = ref(false)

function switchMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
}

async function onSubmit() {
  if (busy.value) return
  const { username, password, again, nickname, email } = form.value

  if (!username.trim() || !password) {
    uni.showToast({ title: '请输入账号和密码', icon: 'none' })
    return
  }
  if (mode.value === 'register') {
    if (password !== again) {
      uni.showToast({ title: '两次输入的密码不一致', icon: 'none' })
      return
    }
    if (password.length < 6) {
      uni.showToast({ title: '密码至少 6 位', icon: 'none' })
      return
    }
    if (!agreed.value) {
      uni.showToast({ title: '请先同意用户协议和隐私政策', icon: 'none' })
      return
    }
  }

  busy.value = true
  try {
    if (mode.value === 'register') {
      await elderRegister({
        username: username.trim(),
        password,
        nickname: nickname.trim(),
        email: email.trim(),
      })
      uni.showToast({ title: '注册成功，请登录', icon: 'success' })
      mode.value = 'login'
      form.value.again = ''
      return
    }

    const r = await elderLogin(username.trim(), password)
    setElderToken(r.access_token)
    setElderUser({ id: r.user_id, username: r.username, is_admin: r.is_admin })
    uni.showToast({ title: '登录成功', icon: 'success' })
    setTimeout(() => uni.reLaunch({ url: '/pages/home/index' }), 300)
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || (mode.value === 'login' ? '登录失败' : '注册失败'), icon: 'none' })
  }
  finally {
    busy.value = false
  }
}

function openDoc(url: string) {
  uni.navigateTo({ url })
}
</script>

<template>
  <view class="page">
    <view class="brand">
      <image class="logo" src="/static/icons/192x192.png" mode="aspectFit" />
      <view class="brand-name">
        老人陪伴助手
      </view>
      <view class="brand-sub">
        让关心随时在线
      </view>
    </view>

    <view class="card">
      <view class="tabs">
        <text :class="['tab', mode === 'login' ? 'on' : '']" @click="mode = 'login'">登录</text>
        <text :class="['tab', mode === 'register' ? 'on' : '']" @click="switchMode">注册</text>
      </view>

      <!-- placeholder-class 必填：APP 端原生 input 的占位符不继承 .input 的颜色，
           不指定就用系统默认灰。要求老人也能看清提示文字。 -->
      <input v-model="form.username" class="input" placeholder-class="input-placeholder" placeholder="账号" maxlength="32" />
      <input v-model="form.password" class="input" password placeholder-class="input-placeholder" placeholder="密码" maxlength="32" />
      <template v-if="mode === 'register'">
        <input v-model="form.again" class="input" password placeholder-class="input-placeholder" placeholder="确认密码" maxlength="32" />
        <input v-model="form.nickname" class="input" placeholder-class="input-placeholder" placeholder="称呼（可选）" maxlength="32" />
        <input v-model="form.email" class="input" placeholder-class="input-placeholder" placeholder="邮箱（可选，用于找回密码）" maxlength="64" />
      </template>

      <button class="btn" :disabled="busy" @click="onSubmit">
        {{ busy ? '请稍候…' : (mode === 'login' ? '登 录' : '注 册') }}
      </button>

      <view v-if="mode === 'register'" class="agree">
        <text :class="['checkbox', agreed ? 'on' : '']" @click="agreed = !agreed" />
        <text class="agree-text">我已阅读并同意</text>
        <text class="agree-link" @click="openDoc('/pages/login/user-agreement-zh')">《用户协议》</text>
        <text class="agree-text">和</text>
        <text class="agree-link" @click="openDoc('/pages/login/privacy-policy-zh')">《隐私政策》</text>
      </view>

      <view v-if="mode === 'login'" class="tip">
        忘记密码可在「我的 → 修改密码」处理，或联系管理员重置
      </view>
    </view>
  </view>
</template>

<!--
  一屏显示 + 输入框可见（2026-10-19 两轮用户实测）

  第一轮（只为一屏）：安卓 APP 注册页装不下，底部按钮和协议要滚动才看得到。
  按 rpx 算原布局注册态总高 ≈ 1326rpx，而典型安卓视口可用高约 1200rpx
  （16:9 扣掉状态栏/导航栏）。做法是压「非信息」的垂直开销：
  brand 装饰、card/page 内边距、注册态隐藏登录态的「忘记密码」提示。
  ⚠️ 当时**顺手把输入框上下内边距从 22 压到 16** —— 这一步是错的，
  直接导致下面的输入框文字被裁。

  第二轮（用户截图反馈）：内容框被压成扁带、输入的文字只露上半截，
  而占位符「邮箱（可选，用于找回密码」显示完整。
  ⇒根因不是「整体压太扁」，而是**只用 padding 撑高度对APP 端原生<input>
  无效**，其内容盒高度由原生样式决定；line-height 又没固定，
  默认行高大于内容盒 ⇒ 文字上沿被裁。占位符由原生 hint 视图单独绘制，
  所以不受影响——「占位符完整但输入内容被裁」就是这个根因的指纹。

  修正：输入框改成height/line-height 显式固定（见 .input），
  上下padding 去掉只留左右；品牌区与内边距**适度回收**，
  因为实测截图里卡片下方仍有大片空白，一屏并不紧张。
  仍不写 overflow:hidden —— 万一极小屏装不下，用户连滚动都没得滚。
-->
<style scoped>
.page { padding: 28rpx 32rpx; background: #f5f7fa; min-height: 100vh; box-sizing: border-box; }
.brand { text-align: center; margin-bottom: 28rpx; }
.logo { width: 120rpx; height: 120rpx; border-radius: 28rpx; }
.brand-name { font-size: 42rpx; font-weight: 700; margin-top: 14rpx; }
.brand-sub { font-size: 26rpx; color: #8a8f99; margin-top: 8rpx; }
.card { background: #fff; border-radius: 24rpx; padding: 28rpx; box-sizing: border-box; }
.tabs { display: flex; margin-bottom: 20rpx; }
.tab { flex: 1; text-align: center; padding: 16rpx 0; font-size: 32rpx; color: #6b7280; }
.tab.on { color: #e8590c; font-weight: 700; border-bottom: 4rpx solid #e8590c; }
/*
  输入框必须显式给 height + line-height（2026-10-19 用户实测截图）

  现象：内容框被压成扁带，输入的文字**只露出上半截**
        （占位符「邮箱（可选，用于找回密码」却是完整的）。
  根因：只用 padding 撑高度时，APP 端 `<input>` 是**原生控件**，
        它的内容盒高度由原生样式决定、与 padding 无关；
        而 line-height 未固定，默认行高算出来大于内容盒高度 ⇒ 文字上沿被裁。
        占位符由原生 hint 视图单独绘制，所以它不受影响 ——
        **「占位符完整但输入内容被裁」正是这个根因的指纹**，
        遇到时直接怀疑 line-height/height，而不是「整体压太扁」。

  修法：height 用 rpx 固定给足，line-height 与 height 相同让文字垂直居中，
        padding 只保留左右（上下 padding 对原生控件的高度无效，还会挤内容）。

  一屏显示仍要保证：靠brand 装饰压缩 + 注册态隐藏登录态提示，
  不再靠压输入框 —— 上一轮正是压输入框导致这个 bug。
*/
.input {
  border: 1rpx solid #e3e8ef;
  border-radius: 12rpx;
  /* 88rpx ≈ 44px：对老人字号（30rpx ≈ 15px）足够，且仍属紧凑 */
  height: 88rpx;
  line-height: 88rpx;
  padding: 0 24rpx;
  margin-bottom: 20rpx;
  font-size: 30rpx;
  background: #fff;
  color: #1f2430;
  width: auto;
  box-sizing: border-box;
}
.input-placeholder { color: #a8aeb8; }
.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 34rpx; padding: 24rpx 0; font-weight: 600; margin-top: 8rpx; }
.btn[disabled] { background: #f0b48a; }
.agree { display: flex; flex-direction: row; align-items: center; flex-wrap: wrap; margin-top: 24rpx; font-size: 26rpx; color: #6b7280; line-height: 1.6; }
.checkbox { width: 34rpx; height: 34rpx; border: 2rpx solid #c8cdd4; border-radius: 6rpx; margin-right: 10rpx; }
.checkbox.on { background: #e8590c; border-color: #e8590c; }
.agree-text { color: #6b7280; }
.agree-link { color: #e8590c; }
.tip { font-size: 26rpx; color: #8a8f99; text-align: center; margin-top: 24rpx; line-height: 1.6; }
</style>
