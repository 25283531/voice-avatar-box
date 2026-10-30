<route lang="json">
{
  "style": { "navigationBarTitleText": "设备" }
}
</route>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'
import {
  applyAvatar,
  bindAgentRole,
  bindDevice,
  deleteDevice,
  getDeviceWifi,
  listAgentRoles,
  listAvatars,
  listDevices,
  setDeviceWifi,
  updateDevice,
} from '@/api/elder'
import type { Avatar, Device, DeviceWifiState } from '@/api/elder'
import { fmtDateTime } from '@/utils/time'
import { getElderToken } from '@/http/elder'
import { installBackBridge, pushBackCloser } from '@/utils/back-guard'
import { scanWifi } from '@/utils/wifi-scan'

const devices = ref<Device[]>([])
const avatars = ref<Avatar[]>([])
// 平台预设与我的角色分开存，模板才能分区渲染（PRD §11.1 线框）。
// roles 是两者的合并，给 onPickRole 与空态判断用。
const presetRoles = ref<any[]>([])
const myRoles = ref<any[]>([])
const roles = ref<any[]>([])
const loading = ref(false)

const showBind = ref(false)
const bindForm = ref({ code: '', alias: '' })

// 激活码契约：**6 位随机数字 + 1 位校验位 = 7 位纯数字**。
// 与服务端 generate_activation_code()（server/app/security.py）逐位一致。
// ⚠️ 2026-10-20 从「1 字母 + 12 数字」改成全数字：设备内置音效表只有数字音，
// 旧格式的首字母在语音播报时被静默跳过，老人只听得到 12 位数字、照输必被拒。
const CODE_LEN = 7

// 已输入的有效位数：过滤短横线等分隔符后按字符数算。
// 屏幕上的码是 123-4567 分组形态，用户照抄会带 -，
// 按输入框原长度算会虚高，必须按「将要提交的形态」计。
const codeLen = computed(() => bindForm.value.code.replace(/\D/g, '').length)

/** 正在配置哪台设备：null=关闭 / 'net'=配网 / 'agent'=绑角色 */
const panel = ref<null | { devicePk: number, mode: 'net' | 'agent' }>(null)

/* ---------------- 安卓返回键 ---------------- */
/**
 * 关掉本页最上层的浮层；没东西可关返回 false（交回壳的默认行为）。
 *
 * ⚠️ 本页弹层同样是 `v-if` + `.mask` 自己画的，框架不知道有东西要关；
 * 而本项目手机端是 Android WebView 薄壳，返回键由壳的 onBackPressed 处理，
 * 壳只会 `canGoBack() ? goBack() : 退出`，tabBar 页 history 为空 ⇒ 直接退出。
 * 完整根因见 `utils/back-guard.ts` 与 profile 页「安卓返回键」一节。
 */
function closeTopOverlay(): boolean {
  if (showBind.value) {
    showBind.value = false
    return true
  }
  if (panel.value) {
    closePanel()
    return true
  }
  return false
}

// tabBar 页长驻，注册一次即可；装晚了壳那边会拿到 0，照样退出应用。
installBackBridge()
pushBackCloser(closeTopOverlay)

async function refresh() {
  if (!getElderToken()) return
  loading.value = true
  try {
    devices.value = await listDevices()
    avatars.value = await listAvatars().catch(() => [])
    // 角色已拆分到 /api/roles，返回 {presets, mine} 分区。
    // 这里要的是「这台设备能绑的全部角色」= 平台预设 + 我的，所以两个分区合并。
    // ⚠️ 绝不能再把平台 AI 配置当角色渲染 —— 改造前 roles 直接来自
    // /api/providers/safe，用户会在"选角色"里看到一堆 provider/endpoint/model，
    // 而且点删除会把平台 LLM 配置真删掉（P0-1/P0-2）。
    const rl = await listAgentRoles().catch(() => ({ presets: [], mine: [] }) as any)
    presetRoles.value = (rl.presets || []).filter((r: any) => !r.deleted_at)
    myRoles.value = (rl.mine || []).filter((r: any) => !r.deleted_at)
    roles.value = [...presetRoles.value, ...myRoles.value]
  }
  catch { /* 请求层已提示 */ }
  finally {
    loading.value = false
  }
}

async function doBind() {
  // 只留数字：屏幕上显示的是 123-4567，用户粘进来会带短横线。
  const code = bindForm.value.code.replace(/\D/g, '')
  if (code.length !== CODE_LEN) {
    // 带上实际位数：「还差 N 位」比一句笼统的「格式不正确」可操作得多。
    uni.showToast({
      title: `激活码应为 ${CODE_LEN} 位数字，当前 ${code.length} 位`,
      icon: 'none',
    })
    return
  }
  try {
    const r = await bindDevice({ code, alias: bindForm.value.alias })
    uni.showToast({ title: `已添加 ${r.name}`, icon: 'success' })
    showBind.value = false
    bindForm.value = { code: '', alias: '' }
    await refresh()
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || '添加失败', icon: 'none' })
  }
}

function onDelete(d: Device) {
  uni.showModal({
    title: '删除设备',
    content: `确定删除「${d.name}」？删除后该设备的提醒也会一并移除。`,
    success: async (res) => {
      if (!res.confirm) return
      try {
        await deleteDevice(d.id)
        uni.showToast({ title: '已删除', icon: 'success' })
        await refresh()
      }
      catch { /* 已提示 */ }
    },
  })
}

function openPanel(d: Device, mode: 'net' | 'agent') {
  panel.value = { devicePk: d.id, mode }
  if (mode === 'net') {
    resetWifiPanel()
    // 只在设备在线时才去问状态：离线时这条请求必然拿到旧的/空的状态，
    // 而离线本来就不允许远程改 WiFi，问了也没用。
    if (d.online) void loadWifiStatus(d.id)
  }
}

function closePanel() {
  // ⚠️ 必须停轮询：面板关了还在打接口，用户离开页面后还会继续跑
  stopWifiPoll()
  panel.value = null
}

/* ---------------- 改 WiFi（设备已联网时） ---------------- */
/**
 * 与「首次配网」是**两条完全不同的路**，别混：
 *
 *   · 首次配网 —— 设备还没有任何 WiFi 配置，会自动起热点（XiaoZhi-XXXX）
 *     并用语音 + 字幕报出来，手机连上那个热点，在 device-config 页填密码。
 *     这条路上设备**没有网络**，APP 根本够不着它，只能走热点。
 *   · 改 WiFi —— 设备已经联网，APP 通过服务端下发指令。所以必须先判设备
 *     在不在线，离线就只能走上面那条。
 *
 * ⚠️ 提交成功 ≠ 已生效。设备每 300 秒才连一次服务器（服务端只在有待下发
 * 指令时把这次间隔临时压到 15 秒），所以状态要轮询着看，不能提交完就报
 * 「已切换」。见 api/elder 里 DeviceWifiState 的注释。
 */
const wifiForm = ref({ ssid: '', password: '' })
const wifiList = ref<{ ssid: string, level: number, secured: boolean }[]>([])
const wifiScanMsg = ref('')
const wifiScanning = ref(false)
const wifiStatus = ref<DeviceWifiState | null>(null)
const wifiBusy = ref(false)

/** 轮询句柄。面板一关就必须清掉，否则离开页面还在打接口 */
let wifiPollTimer: ReturnType<typeof setTimeout> | null = null

function stopWifiPoll() {
  if (wifiPollTimer) {
    clearTimeout(wifiPollTimer)
    wifiPollTimer = null
  }
}

function resetWifiPanel() {
  stopWifiPoll()
  wifiForm.value = { ssid: '', password: '' }
  wifiList.value = []
  wifiScanMsg.value = ''
  wifiScanning.value = false
  wifiStatus.value = null
  wifiBusy.value = false
}

/** 只有这两个中间态还需要等，到了终态就停轮询 */
function needsPoll(s: DeviceWifiState | null) {
  return !!s && (s.state === 'pending' || s.state === 'delivered')
}

async function loadWifiStatus(pk: number) {
  try {
    wifiStatus.value = await getDeviceWifi(pk)
  }
  catch {
    // 读不到历史状态不影响「改 WiFi」本身，静默即可
    // （http 层已经弹过 toast 了，这里再弹一次就是重复）
  }
  if (needsPoll(wifiStatus.value)) scheduleWifiPoll(pk)
}

function scheduleWifiPoll(pk: number) {
  stopWifiPoll()
  // 5 秒一拍：服务端在有 pending 时把设备心跳压到 15 秒，5 秒足够及时，
  // 又不至于把手机和服务器打成高频轮询。
  wifiPollTimer = setTimeout(async () => {
    wifiPollTimer = null
    // 面板已关 / 已换成别的设备 ⇒ 停止
    if (!panel.value || panel.value.devicePk !== pk) return
    try {
      wifiStatus.value = await getDeviceWifi(pk)
    }
    catch {
      // 切换过程中设备会短暂断网，接口抖一下很正常，下一拍再试
    }
    if (needsPoll(wifiStatus.value)) scheduleWifiPoll(pk)
  }, 5000)
}

async function onScanWifi() {
  wifiScanning.value = true
  wifiScanMsg.value = ''
  try {
    const r = await scanWifi()
    if (r.ok) {
      wifiList.value = r.list
      if (!r.list.length) wifiScanMsg.value = '附近没搜到 WiFi，可以直接在下面输入名称'
    }
    else {
      wifiList.value = []
      wifiScanMsg.value = scanErrorText(r.error)
    }
  }
  finally {
    wifiScanning.value = false
  }
}

/**
 * 把原生错误码翻译成**能行动**的话。
 *
 * ⚠️ 只说「扫描失败」等于没说：安卓在「定位」总开关关着时扫描返回**空列表
 * 且不抛异常**，用户看到「附近没有 WiFi」会以为家里 WiFi 坏了。
 */
function scanErrorText(e: string) {
  switch (e) {
    case 'unsupported':
      return '这台手机不支持自动搜索，请直接在下面输入 WiFi 名称'
    case 'location_off':
      return '请先打开手机的「定位」开关再搜索（安卓系统的要求，和定位无关）'
    case 'no_permission':
      return '没有 WiFi 权限，请到系统设置里允许本应用使用「附近的设备」'
    case 'busy':
      return '搜索太频繁，请等几秒再点一次'
    case 'empty':
      return '附近没搜到 WiFi，可以直接在下面输入名称'
    default:
      return '搜索失败，可以直接在下面输入名称'
  }
}

function onPickWifi(w: { ssid: string }) {
  wifiForm.value.ssid = w.ssid
}

async function onSubmitWifi() {
  if (!panel.value) return
  const ssid = wifiForm.value.ssid.trim()
  if (!ssid) {
    uni.showToast({ title: '请选择或输入 WiFi 名称', icon: 'none' })
    return
  }
  const pwd = wifiForm.value.password
  // 开放网络（无密码）允许空；有密码时 8..63 是 WPA 的合法区间。
  // 在这里先挡一道，比等服务端 422 再解释更省事。
  if (pwd && (pwd.length < 8 || pwd.length > 63)) {
    uni.showToast({ title: 'WiFi 密码一般是 8 位以上', icon: 'none' })
    return
  }
  wifiBusy.value = true
  try {
    wifiStatus.value = await setDeviceWifi(panel.value.devicePk, { ssid, password: pwd })
    scheduleWifiPoll(panel.value.devicePk)
  }
  catch {
    // 设备离线时服务端回 409，detail 已经是「设备当前不在线，等它连上网再改
    // WiFi」这种能行动的话，http 层已经弹出来了 —— 这里不要再弹第二个。
  }
  finally {
    wifiBusy.value = false
  }
}

/** 状态 → 给家属看的一句话。服务端已按 state 派生 message，这里只补服务端
 *  不可能知道的「大概要等多久」，因为那是 APP 侧才知道的心跳节奏。 */
function wifiStateHint(s: DeviceWifiState | null) {
  if (!s || !s.state) return ''
  if (s.state === 'pending') return '设备最长要 5 分钟才会来取这条指令'
  if (s.state === 'delivered') return '设备正在切换，会短暂断网，请不要关电源'
  return ''
}


/** 选择形象 / agent 角色并绑定到设备 */
async function onPickAvatar(a: Avatar) {
  if (!panel.value) return
  try {
    await applyAvatar(a.id, panel.value.devicePk)
    uni.showToast({ title: `已绑定角色「${a.name}」`, icon: 'success' })
    closePanel()
    await refresh()
  }
  catch { /* 已提示 */ }
}

/** 绑定自建 agent 角色（后端支持时可用） */
async function onPickRole(r: any) {
  if (!panel.value) return
  try {
    await bindAgentRole(panel.value.devicePk, r.id)
    uni.showToast({ title: `已绑定角色「${r.name}」`, icon: 'success' })
    closePanel()
    await refresh()
  }
  catch (e: any) {
    uni.showToast({ title: e?.message || '绑定失败', icon: 'none' })
  }
}

async function onRename(d: Device) {
  uni.showModal({
    title: '重命名设备',
    editable: true,
    placeholderText: d.name,
    success: async (res) => {
      const name = (res.content || '').trim()
      if (!res.confirm || !name) return
      try {
        await updateDevice(d.id, { name })
        await refresh()
      }
      catch { /* 已提示 */ }
    },
  })
}

function currentDevice() {
  return devices.value.find(d => d.id === panel.value?.devicePk) || null
}

// ⚠️ 跳转必须写在 script 里，不能写成 `@click="uni.navigateTo(...)"`。
// 模板内联写法不会被 @dcloudio/uni-app 插件改写，产物里是
// `i.uni.navigateTo(...)`——那个 `uni` 不是 API 对象，调用抛 TypeError
// 又被 Vue 的事件处理器静默吞掉 ⇒「点不动」且构建不报错。
// 详见 pages/profile/index.vue 里 goAvatar 上方的完整说明。
function goDeviceConfig() {
  uni.navigateTo({ url: '/pages/device-config/index' })
}

onShow(() => { refresh() })
</script>

<template>
  <view class="page">
    <view class="head">
      <view>
        <view class="head-title">
          我的设备
        </view>
        <view class="head-sub">
          共 {{ devices.length }} 台 · 在线 {{ devices.filter(d => d.online).length }} 台
        </view>
      </view>
      <text class="add" @click="showBind = true">+ 添加设备</text>
    </view>

    <view v-if="loading" class="tip">
      加载中…
    </view>
    <view v-else-if="devices.length === 0" class="card empty">
      <view class="empty-title">
        还没有设备
      </view>
      <view class="empty-sub">
        打开设备电源，屏幕上会显示 7 位数字激活码，点右上角「添加设备」输入即可绑定
      </view>
    </view>

    <view v-for="d in devices" :key="d.id" class="card">
      <view class="row between">
        <text class="dname">{{ d.name }}</text>
        <text :class="['tag', d.online ? 'ok' : '']">{{ d.online ? '在线' : '离线' }}</text>
      </view>
      <view class="sub">标识 {{ d.device_id }}</view>
      <view class="sub">最近在线 {{ fmtDateTime(d.last_seen_at) }}</view>
      <view class="sub">固件 {{ d.fw_version || '—' }}{{ d.ip ? ` · ${d.ip}` : '' }}</view>

      <view class="row ops">
        <button class="btn small" @click="openPanel(d, 'net')">
          配网
        </button>
        <button class="btn small" @click="openPanel(d, 'agent')">
          绑定角色
        </button>
        <button class="btn small ghost" @click="onRename(d)">
          重命名
        </button>
        <button class="btn small danger" @click="onDelete(d)">
          删除
        </button>
      </view>
    </view>

    <!-- 添加设备 -->
    <view v-if="showBind" class="mask" @click="showBind = false">
      <view class="modal" @click.stop>
        <view class="title">
          添加设备
        </view>
        <view class="tip left">
          在设备屏幕上可以看到形如 123-4567 的 7 位数字激活码，10 分钟内有效
        </view>
        <input v-model="bindForm.code" class="input big" placeholder="123-4567" />
        <view class="code-count">
          已输入 {{ codeLen }}/{{ CODE_LEN }} 位
        </view>
        <view v-if="codeLen < CODE_LEN" class="code-tip">
          还差 {{ CODE_LEN - codeLen }} 位：全部是数字，屏幕上按 3+4 分组显示
        </view>
        <view v-else-if="codeLen > CODE_LEN" class="code-tip">
          超出 {{ codeLen - CODE_LEN }} 位：激活码共 {{ CODE_LEN }} 位数字，请核对屏幕
        </view>
        <input v-model="bindForm.alias" class="input" placeholder="给设备起个名字（可选）" />
        <button class="btn" @click="doBind">
          绑定设备
        </button>
        <!-- 换绑的前置条件必须写在绑定卡片里：家属最常见的误解是「再输一次码
             就能把设备挪到我名下」，而服务端对已绑定设备已不再下发激活码
             （voice.ota_check 的已绑定分支不含 activation 段），重复绑定只会失败。
             ⚠️ 这里刻意**不写具体邮箱**：APP 没有配置类接口，硬编码一个地址意味着
             换邮箱要重新发版；网页端（user.html）从 /api/support-contact 取。 -->
        <view class="tip left">
          已经绑定过的设备不会重复下发激活码：换绑需要先在原账户里解绑。
          忘记用户名、无法自行解绑的，请联系平台管理员。
        </view>
        <button class="btn ghost" @click="showBind = false">
          取消
        </button>
      </view>
    </view>

    <!-- 配网 / 绑定角色 面板 -->
    <view v-if="panel" class="mask" @click="closePanel">
      <view class="modal" @click.stop>
        <template v-if="panel.mode === 'net'">
          <view class="title">
            给「{{ currentDevice()?.name }}」配网
          </view>

          <!-- ============ 设备在线：远程改 WiFi ============ -->
          <template v-if="currentDevice()?.online">
            <!-- 上一次/本次改 WiFi 的结果。状态由服务端派生，APP 只补一句
                 「大概要等多久」—— 那是 APP 侧才知道的心跳节奏。 -->
            <view v-if="wifiStatus && wifiStatus.state" class="wifi-state" :class="`st-${wifiStatus.state}`">
              <text class="wifi-state-main">{{ wifiStatus.message }}</text>
              <text v-if="wifiStateHint(wifiStatus)" class="wifi-state-sub">{{ wifiStateHint(wifiStatus) }}</text>
              <text v-if="wifiStatus.ssid" class="wifi-state-sub">目标 WiFi：{{ wifiStatus.ssid }}</text>
            </view>

            <view class="tip left">
              设备已经联网，可以直接在这里换成别的 WiFi。切换时设备会短暂断网；
              <text class="em">如果连不上新 WiFi，它会自动回到现在这个</text>，不会失联。
            </view>

            <view class="group-label">WiFi 名称</view>
            <view class="row-between">
              <input v-model="wifiForm.ssid" class="input" placeholder="选择下面的 WiFi，或直接输入名称" />
              <button class="btn small ghost scan" :disabled="wifiScanning" @click="onScanWifi">
                {{ wifiScanning ? '搜索中…' : '搜索' }}
              </button>
            </view>
            <view v-if="wifiScanMsg" class="wifi-scan-msg">
              {{ wifiScanMsg }}
            </view>
            <!-- 扫描列表。搜不到/没权限时这里就是空的，用户仍然能走上面的
                 输入框 —— 手输兜底是**必须**的，不是可选优化。 -->
            <view v-if="wifiList.length" class="wifi-list">
              <view v-for="w in wifiList" :key="w.ssid" class="pick"
                    :class="{ 'pick-on': wifiForm.ssid === w.ssid }" @click="onPickWifi(w)">
                <text class="pick-name">{{ w.ssid }}</text>
                <text class="pick-sub">{{ w.secured ? '需要密码' : '开放网络（无密码）' }}</text>
              </view>
            </view>

            <view class="group-label">WiFi 密码</view>
            <input v-model="wifiForm.password" class="input" placeholder="没有密码就留空" />

            <button class="btn" :disabled="wifiBusy" @click="onSubmitWifi">
              {{ wifiBusy ? '提交中…' : '切换到这个 WiFi' }}
            </button>
            <view class="tip left">
              提交后设备最长要 5 分钟才会来取这条指令，可以先把手机放一边，回来再看结果。
            </view>
          </template>

          <!-- ============ 设备离线：只能走设备热点 ============ -->
          <template v-else>
            <view class="tip left">
              设备现在不在线，没法远程改 WiFi。请让设备进入配网模式：它第一次开机、
              或者连不上 WiFi 时会自己进入，屏幕和语音会报出热点名字（形如 XiaoZhi-1A2B）。
              用手机连上这个热点，再点下面的按钮。
            </view>
            <button class="btn" @click="goDeviceConfig">
              开始配网
            </button>
          </template>
        </template>

        <template v-else>
          <view class="title">
            为「{{ currentDevice()?.name }}」绑定角色
          </view>
          <view class="tip left">
            每台设备可以绑定不同的角色。角色决定说话方式与音色，形象决定屏幕上的外观
          </view>

          <view v-if="presetRoles.length" class="group-label">平台推荐</view>
          <view v-for="r in presetRoles" :key="`p${r.id}`" class="pick" @click="onPickRole(r)">
            <text class="pick-name">{{ r.name }}</text>
            <text class="pick-sub">{{ r.description || '平台预设角色' }}</text>
          </view>

          <view v-if="myRoles.length" class="group-label">我的角色</view>
          <view v-for="r in myRoles" :key="`r${r.id}`" class="pick" @click="onPickRole(r)">
            <text class="pick-name">{{ r.name }}</text>
            <text class="pick-sub">{{ r.description || '自定义角色' }}</text>
          </view>

          <view class="group-label">系统形象</view>
          <view v-for="a in avatars" :key="`a${a.id}`" class="pick" @click="onPickAvatar(a)">
            <text class="pick-name">{{ a.name }}</text>
            <text class="pick-sub">{{ a.description || a.tts_voice || '' }}</text>
          </view>

          <view v-if="!roles.length && !avatars.length" class="tip">
            暂无可选角色，可到「我的 → 创建角色」新建
          </view>
        </template>

        <button class="btn ghost" @click="closePanel">
          关闭
        </button>
      </view>
    </view>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; padding-bottom: 40rpx; }
.head { display: flex; flex-direction: row; align-items: center; justify-content: space-between; margin-bottom: 20rpx; }
.head-title { font-size: 34rpx; font-weight: 700; }
.head-sub { font-size: 24rpx; color: #8a8f99; margin-top: 4rpx; }
.add { font-size: 28rpx; color: #e8590c; font-weight: 600; }
.card { background: #fff; border-radius: 20rpx; padding: 28rpx; margin-bottom: 24rpx; }
.dname { font-size: 32rpx; font-weight: 600; }
.sub { font-size: 24rpx; color: #8a8f99; margin-top: 8rpx; display: block; }
.row { display: flex; flex-direction: row; align-items: center; }
.between { justify-content: space-between; }
.ops { margin-top: 20rpx; flex-wrap: wrap; }
.btn { background: #e8590c; color: #fff; border-radius: 12rpx; font-size: 28rpx; padding: 18rpx 0; margin-top: 8rpx; }
.btn.small { flex: 1; min-width: 40%; margin-right: 12rpx; margin-top: 12rpx; padding: 14rpx 0; font-size: 26rpx; }
.btn.ghost { background: #fff; color: #e8590c; border: 1rpx solid #e8590c; }
.btn.danger { background: #fff; color: #dc2626; border: 1rpx solid #f3c7c7; margin-right: 0; }
.tag { font-size: 22rpx; color: #8a8f99; background: #f1f3f6; border-radius: 20rpx; padding: 4rpx 16rpx; }
.tag.ok { color: #16a34a; background: #e8f7ee; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; padding: 40rpx 0; }
.tip.left { text-align: left; padding: 12rpx 0 20rpx; }
.empty { text-align: center; padding: 60rpx 28rpx; }
.empty-title { font-size: 32rpx; font-weight: 600; }
.empty-sub { font-size: 26rpx; color: #8a8f99; margin-top: 16rpx; line-height: 1.6; }
.mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 99; }
.modal { width: 86%; max-height: 80vh; overflow-y: auto; background: #fff; border-radius: 20rpx; padding: 32rpx; }
.title { font-size: 32rpx; font-weight: 600; margin-bottom: 8rpx; }
.input { border: 1rpx solid #e3e8ef; border-radius: 10rpx; padding: 18rpx; margin-bottom: 16rpx; font-size: 28rpx; background: #fff; }
.input.big { font-size: 36rpx; letter-spacing: 2rpx; text-align: center; }
.code-count { font-size: 24rpx; color: #8a8f99; text-align: center; margin: -8rpx 0 12rpx; }
.code-tip { font-size: 24rpx; color: #e8590c; text-align: center; margin: -8rpx 0 12rpx; }
.group-label { font-size: 26rpx; color: #8a8f99; margin: 20rpx 0 8rpx; }
.pick { border: 1rpx solid #e3e8ef; border-radius: 12rpx; padding: 20rpx; margin-bottom: 12rpx; }
.pick-name { font-size: 28rpx; font-weight: 600; display: block; }
.pick-sub { font-size: 24rpx; color: #8a8f99; display: block; margin-top: 6rpx; }

/* ---------- 改 WiFi（设备在线时的远程切换） ---------- */
/* 输入框 + 搜索按钮同一行。
   ⚠️ 搜索按钮**不能**复用 .btn.small：那个类带 `flex: 1`，是给并排两个
   等宽按钮用的，放进这一行会把输入框挤没。 */
.row-between { display: flex; flex-direction: row; align-items: center; }
.row-between .input { flex: 1; }
.btn.scan {
  flex: none; width: 150rpx;
  margin: 0 0 16rpx 16rpx; padding: 18rpx 0; font-size: 26rpx;
}
/* 扫描失败/降级的说明。用暖色而不是灰色 —— 这句话是「你需要做点什么」，
   不是「一切正常」。 */
.wifi-scan-msg {
  font-size: 24rpx; color: #b45309; background: #fff8f0;
  border-radius: 8rpx; padding: 14rpx 16rpx; margin-bottom: 16rpx; line-height: 1.5;
}
/* 扫描列表限高，避免 20 个 WiFi 把密码框和提交按钮顶出屏幕 */
.wifi-list { max-height: 320rpx; overflow-y: auto; margin-bottom: 8rpx; }
.pick-on { border-color: #e8590c; background: #fff8f0; }

/* 改 WiFi 的状态条：按 state 上色，家属要能一眼分清
   「还在等」/「成功了」/「失败了已回滚」三种处境。 */
.wifi-state { border-radius: 12rpx; padding: 18rpx 20rpx; margin-bottom: 16rpx; background: #f7f8fa; }
.wifi-state-main { display: block; font-size: 27rpx; color: #1f2937; line-height: 1.5; }
.wifi-state-sub { display: block; font-size: 23rpx; color: #8a8f99; margin-top: 6rpx; line-height: 1.5; }
.wifi-state.st-applied { background: #f0fdf4; }
.wifi-state.st-applied .wifi-state-main { color: #15803d; }
.wifi-state.st-rolled_back, .wifi-state.st-failed { background: #fef2f2; }
.wifi-state.st-rolled_back .wifi-state-main,
.wifi-state.st-failed .wifi-state-main { color: #b91c1c; }
.wifi-state.st-pending, .wifi-state.st-delivered { background: #fff8f0; }
.wifi-state.st-pending .wifi-state-main,
.wifi-state.st-delivered .wifi-state-main { color: #b45309; }

.em { color: #b45309; }
</style>
