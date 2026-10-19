import path from 'node:path'
import process from 'node:process'
// manifest.config.ts
import { defineManifestConfig } from '@uni-helper/vite-plugin-uni-manifest'
import { loadEnv } from 'vite'

// 手动解析命令行参数获取 mode
function getMode() {
  const args = process.argv.slice(2)
  const modeFlagIndex = args.findIndex(arg => arg === '--mode')
  return modeFlagIndex !== -1 ? args[modeFlagIndex + 1] : args[0] === 'build' ? 'production' : 'development' // 默认 development
}
// 获取环境变量的范例
const env = loadEnv(getMode(), path.resolve(process.cwd(), 'env'))
const {
  VITE_APP_TITLE,
  VITE_UNI_APPID,
  VITE_WX_APPID,
  VITE_APP_PUBLIC_BASE,
  VITE_FALLBACK_LOCALE,
} = env

export default defineManifestConfig({
  'name': VITE_APP_TITLE,
  'appid': VITE_UNI_APPID,
  'description': '老人陪伴与服药提醒助手（家属端）',
  // ⚠️ versionCode 必须**严格递增**，否则安卓侧无法把它识别成新版（老人装不上）。
  // 2026-10-19 发布 1.1.0：本版含忘记密码四步流程（用户名→绑定邮箱→验证码→新密码
  // 发邮箱）与角色创建页六处交互修复（下拉选择、音色试听、返回键只关弹窗等）。
  'versionName': '1.1.0',
  'versionCode': '110',
  'transformPx': false,
  'locale': VITE_FALLBACK_LOCALE, // 'zh-Hans'
  'h5': {
    router: {
      // base: VITE_APP_PUBLIC_BASE,
    },
  },
  /* 5+App特有相关 */
  'app-plus': {
    usingComponents: true,
    nvueStyleCompiler: 'uni-app',
    compilerVersion: 3,
    compatible: {
      ignoreVersion: true,
    },
    splashscreen: {
      alwaysShowBeforeRender: true,
      waiting: true,
      autoclose: true,
      delay: 0,
    },
    /* 模块配置 */
    modules: {},
    /* 应用发布信息 */
    distribute: {
      /* android打包配置 */
      android: {
        minSdkVersion: 24,
        targetSdkVersion: 33,
        abiFilters: ['armeabi-v7a', 'arm64-v8a'],
        // 只保留本项目真正用到的：网络、提醒震动、音频播放、头像选择
        permissions: [
          '<uses-permission android:name="android.permission.INTERNET"/>',
          '<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE"/>',
          '<uses-permission android:name="android.permission.CHANGE_NETWORK_STATE"/>',
          '<uses-permission android:name="android.permission.ACCESS_WIFI_STATE"/>',
          '<uses-permission android:name="android.permission.CHANGE_WIFI_STATE"/>',
          '<uses-permission android:name="android.permission.VIBRATE"/>',
          '<uses-permission android:name="android.permission.WAKE_LOCK"/>',
          '<uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS"/>',
          '<uses-permission android:name="android.permission.CAMERA"/>',
          '<uses-permission android:name="android.permission.READ_MEDIA_IMAGES"/>',
          '<uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE"/>',
          '<uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE"/>',
          '<uses-permission android:name="android.permission.REQUEST_INSTALL_PACKAGES"/>',
          '<uses-feature android:name="android.hardware.camera"/>',
          '<uses-feature android:name="android.hardware.camera.autofocus"/>',
        ],
      },
      /* ios打包配置 */
      ios: {},
      /* SDK配置 */
      sdkConfigs: {},
      /* 图标配置 */
      icons: {
        android: {
          hdpi: 'static/icons/72x72.png',
          xhdpi: 'static/icons/96x96.png',
          xxhdpi: 'static/icons/144x144.png',
          xxxhdpi: 'static/icons/192x192.png',
        },
        ios: {
          appstore: 'static/icons/1024x1024.png',
          ipad: {
            'app': 'static/icons/76x76.png',
            'app@2x': 'static/icons/152x152.png',
            'notification': 'static/icons/20x20.png',
            'notification@2x': 'static/icons/40x40.png',
            'proapp@2x': 'static/icons/167x167.png',
            'settings': 'static/icons/29x29.png',
            'settings@2x': 'static/icons/58x58.png',
            'spotlight': 'static/icons/40x40.png',
            'spotlight@2x': 'static/icons/80x80.png',
          },
          iphone: {
            'app@2x': 'static/icons/120x120.png',
            'app@3x': 'static/icons/180x180.png',
            'notification@2x': 'static/icons/40x40.png',
            'notification@3x': 'static/icons/60x60.png',
            'settings@2x': 'static/icons/58x58.png',
            'settings@3x': 'static/icons/87x87.png',
            'spotlight@2x': 'static/icons/80x80.png',
            'spotlight@3x': 'static/icons/120x120.png',
          },
        },
      },
    },
  },
  /* 快应用特有相关 */
  'quickapp': {},
  /* 小程序特有相关 */
  'mp-weixin': {
    // 微信小程序 appid：需使用自己在 mp.weixin.qq.com 申请的，
    // 不要复用其它项目的 appid。为空时仅能在微信开发者工具里预览（无法上传发布）。
    appid: VITE_WX_APPID || '',
    setting: {
      urlCheck: false,
      // 是否启用 ES6 转 ES5
      es6: true,
      minified: true,
    },
    optimization: {
      subPackages: true,
    },
    usingComponents: true,
    // __usePrivacyCheck__: true,
    permission: {
      'scope.userLocation': {
        desc: 'WiFi配网功能需要获取位置权限',
      },
    },
    requiredPrivateInfos: ['getLocation'],
  },
  'mp-alipay': {
    usingComponents: true,
    styleIsolation: 'shared',
  },
  'mp-baidu': {
    usingComponents: true,
  },
  'mp-toutiao': {
    usingComponents: true,
  },
  'uniStatistics': {
    enable: false,
  },
  'vueVersion': '3',
})
