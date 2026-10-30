/**
 * 安卓物理返回键的「页面内拦截」。
 *
 * ⚠️ 为什么框架自带的 `onBackPress` 在本项目里**拦不住**返回键
 * （2026-10-30 用户报「创建我的角色页面按返回键直接退出 APP」）：
 *
 * 本项目手机端不是 DCloud 的 app-vue 原生运行时，而是自己写的 Android
 * WebView 薄壳（`android/app/src/main/java/com/elder/companion/MainActivity.kt`）
 * 加载编译好的 H5（`assets/web/index.html`）。壳里的返回处理只有两行：
 *
 *     if (web.canGoBack()) web.goBack() else super.onBackPressed()
 *
 * 而 tabBar 各页之间切换不产生 WebView 的 history 记录 ⇒ `canGoBack()` 恒为
 * false ⇒ 弹层开着时按返回键，**H5 连 popstate 都收不到**，Activity 直接被
 * finish（= 退出应用）。同理，`plus.key.addEventListener('backbutton')` 在
 * H5 里也没有 `plus` 对象（产物里 `plus.` 出现 0 次），是彻底的死代码。
 *
 * 所以真正的通路必须由壳主动发起：`MainActivity.onBackPressed()` 先
 * `evaluateJavascript` 问一次页面 —— 也就是本文件装上的
 * `window.__elderHandleBack()`：
 *   返回 1 ⇒ 这次返回键已被页面消化（壳什么都不做）
 *   返回 0 ⇒ 交回壳的默认行为（能 goBack 就 goBack，否则退出）
 *
 * 三端判据只有一份（`pushBackCloser` 注册的那个函数），任一条通路拦住即可，
 * 且各通路都幂等 —— 关一个已经关掉的弹层是 no-op，不会出现「关两次」。
 */

/** 关掉本页最上层的浮层；**关掉了返回 true**，没东西可关返回 false */
export type BackCloser = () => boolean

const closers: BackCloser[] = []

/**
 * 注册一个「关掉本页最上层浮层」的函数，返回注销函数。
 *
 * 后注册的先执行（后进先出）：tabBar 页长驻，多个页面可能同时留着注册项，
 * 让**当前活跃页面**的浮层优先被关掉。
 *
 * 判据必须写成「本页有没有浮层开着」而不是「本页是不是当前页」——
 * 后者要读路由，反而多一份真源；前者对已切走的页面恒返回 false，
 * 天然让位给当前页。
 */
export function pushBackCloser(fn: BackCloser): () => void {
  closers.push(fn)
  return () => {
    const i = closers.lastIndexOf(fn)
    if (i >= 0) closers.splice(i, 1)
  }
}

/** 供壳调用：返回 1 表示已消化，0 表示交回默认行为 */
function handleBack(): number {
  for (let i = closers.length - 1; i >= 0; i--) {
    let consumed = false
    try {
      consumed = closers[i]() === true
    }
    catch {
      // 单个 closer 抛异常不能让整个返回键失效（否则又变成「按了没反应」）
      consumed = false
    }
    if (consumed) return 1
  }
  return 0
}

/**
 * 把 `window.__elderHandleBack` 挂上去。幂等，可在每个页面的 onLoad/onShow 里调。
 *
 * H5 / 小程序里没有壳来调用它，挂上也无副作用（只是个没人调的函数）。
 */
export function installBackBridge(): void {
  const w = globalThis as any
  if (typeof w.__elderHandleBack === 'function') return
  w.__elderHandleBack = handleBack
}
