<script setup lang="ts">
import { onHide, onLaunch, onShow } from '@dcloudio/uni-app'
import 'abortcontroller-polyfill/dist/abortcontroller-polyfill-only'
import { bootstrapTimezone } from '@/utils/time'

/**
 * 老人陪伴助手 App 入口。
 *
 * 说明：不复用小智（xiaozhi）的账号体系、配置中心与语言商店，
 * 业务后端地址在 src/http/elder.ts 里代码级写死，不提供任何网络设置入口。
 */
onLaunch(() => {
  console.log('App Launch')
  // 展示时区按**本机 IP 所在国家/地区**判定（用户 2026-10-21 纠偏：
  // 不统一北京时间，也不盲信设备自报的设备时区）。
  // 这里刻意**不 await**：最坏要等两三秒（服务端可能要去查 IP 归属地），
  // 而首屏完全可以先按上次缓存/设备时区画出来 —— 值回来之后
  // startOfToday() 会因为 tzVersion 变化而重算，页面自动跟上。
  void bootstrapTimezone()
})

onShow(() => {
  console.log('App Show')
})

onHide(() => {
  console.log('App Hide')
})
</script>

<style lang="scss">
swiper,
scroll-view {
  flex: 1;
  height: 100%;
  overflow: hidden;
}

image {
  width: 100%;
  height: 100%;
  vertical-align: middle;
}
</style>
