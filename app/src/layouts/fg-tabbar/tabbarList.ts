import type { TabBar } from '@uni-helper/vite-plugin-uni-pages'

type FgTabBarItem = TabBar['list'][0] & {
  icon: string
  iconType: 'uiLib' | 'unocss' | 'iconfont'
}

/**
 * tabbar 选择的策略，更详细的介绍见 tabbar.md 文件
 * 0: 'NO_TABBAR' `无 tabbar`
 * 1: 'NATIVE_TABBAR'  `完全原生 tabbar`
 * 2: 'CUSTOM_TABBAR_WITH_CACHE' `有缓存自定义 tabbar`
 * 3: 'CUSTOM_TABBAR_WITHOUT_CACHE' `无缓存自定义 tabbar`
 *
 * 温馨提示：本文件的任何代码更改了之后，都需要重新运行，否则 pages.json 不会更新导致错误
 */
export const TABBAR_MAP = {
  NO_TABBAR: 0,
  NATIVE_TABBAR: 1,
  CUSTOM_TABBAR_WITH_CACHE: 2,
  CUSTOM_TABBAR_WITHOUT_CACHE: 3,
}
// TODO：通过这里切换使用tabbar的策略
export const selectedTabbarStrategy = TABBAR_MAP.NATIVE_TABBAR

// selectedTabbarStrategy==NATIVE_TABBAR(1) 时，需要填 iconPath 和 selectedIconPath
// selectedTabbarStrategy==CUSTOM_TABBAR(2,3) 时，需要填 icon 和 iconType
// selectedTabbarStrategy==NO_TABBAR(0) 时，tabbarList 不生效
export const tabbarList: FgTabBarItem[] = [
  {
    iconPath: 'static/tabbar/home.png',
    selectedIconPath: 'static/tabbar/homeHL.png',
    pagePath: 'pages/home/index',
    text: '首页',
    icon: 'home',
    iconType: 'uiLib',
  },
  {
    iconPath: 'static/tabbar/device.png',
    selectedIconPath: 'static/tabbar/device_activate.png',
    pagePath: 'pages/device/index',
    text: '设备',
    icon: 'i-carbon-devices',
    iconType: 'uiLib',
  },
  {
    iconPath: 'static/tabbar/example.png',
    selectedIconPath: 'static/tabbar/exampleHL.png',
    pagePath: 'pages/schedule/index',
    text: '日程',
    icon: 'i-carbon-calendar',
    iconType: 'uiLib',
  },
  {
    iconPath: 'static/tabbar/personal.png',
    selectedIconPath: 'static/tabbar/personalHL.png',
    pagePath: 'pages/profile/index',
    text: '我的',
    icon: 'i-carbon-user',
    iconType: 'uiLib',
  },
]

// NATIVE_TABBAR(1) 和 CUSTOM_TABBAR_WITH_CACHE(2) 时，需要tabbar缓存
export const cacheTabbarEnable = selectedTabbarStrategy === TABBAR_MAP.NATIVE_TABBAR
  || selectedTabbarStrategy === TABBAR_MAP.CUSTOM_TABBAR_WITH_CACHE

const _tabbar: TabBar = {
  // 只有微信小程序支持 custom。App 和 H5 不生效
  custom: selectedTabbarStrategy === TABBAR_MAP.CUSTOM_TABBAR_WITH_CACHE,
  color: '#9aa0a6',
  selectedColor: '#e8590c',
  backgroundColor: '#fff',
  borderStyle: 'white',
  // 适老化：tabbar 加高、字号加大，方便老人点击
  height: '58px',
  fontSize: '12px',
  iconWidth: '26px',
  spacing: '4px',
  list: tabbarList as unknown as TabBar['list'],
}

// 0和1 需要显示底部的tabbar的各种配置，以利用缓存
export const tabBar = cacheTabbarEnable ? _tabbar : undefined
