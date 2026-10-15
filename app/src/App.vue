<script setup lang="ts">
import { onHide, onLaunch, onShow } from '@dcloudio/uni-app'
import 'abortcontroller-polyfill/dist/abortcontroller-polyfill-only'

/**
 * 老人陪伴助手 App 入口。
 *
 * 说明：不复用小智（xiaozhi）的账号体系、配置中心与语言商店，
 * 业务后端地址在 src/http/elder.ts 里代码级写死，不提供任何网络设置入口。
 */
onLaunch(() => {
  console.log('App Launch')
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
