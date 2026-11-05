# elder-lx04

小爱触屏音箱 **LX04** 设备端客户端 —— 「老人陪伴提醒助手」的 LX04 移植版。

> 上游项目：仓库根目录的 `voice-avatar-box`（ESP32-S3 版）。
> 本目录是**独立的 Android 应用**，不是 `android/`（家属端 WebView 壳）的附属品。

---

## 为什么是 Android 应用而不是固件

LX04 是 **Android 设备**（MTK MT8167A / Android 8.1 / **800×480 横屏 / 240dpi**），不是 ESP32。
所以移植 = 写一个原生 App + 把系统换成最小集，**不是**重编固件。

> 硬件事实（屏幕、按键 KeyCode、包名、麦克风占用）**只认**
> [`docs/lx04/实机验证结果.md`](../docs/lx04/实机验证结果.md)。
> 移植前的调研资料写的是 480×800 竖屏，实机已推翻，别再照它算布局。

详见 [`docs/lx04/LX04移植方案.md`](../docs/lx04/LX04移植方案.md)。

---

## 目录

```
lx04/
├── app/
│   └── src/main/java/com/elder/lx04/
│       ├── MainActivity.kt      全屏 UI + 装配 + 按键分发
│       ├── BootReceiver.kt      开机自启（BOOT_COMPLETED 兜底）
│       ├── light/
│       │   ├── Led.kt           灯效语义模型（移植自固件 elder_rgb.h）
│       │   ├── LedEngine.kt     灯效引擎（移植自固件 rgb.c）
│       │   ├── LedBandView.kt   屏幕两侧 5mm 竖向色带的绘制
│       │   └── BandRender.kt    渲染产物
│       └── input/
│           └── TopKeys.kt       顶部三键映射 + 长按判定
└── scripts/
    └── deploy.sh                部署：安装 / 接管 / 探测 / 状态 / 还原
```

---

## 四项需求的落点

| 需求 | 实现 | 状态 |
| --- | --- | --- |
| 开机直接进我们的系统，不进小爱桌面 | Manifest 注册 `MAIN+HOME+DEFAULT` + `BootReceiver` + `deploy.sh takeover` | ✅ 代码就绪；HOME 接管**尚未在实机执行** |
| 可以直接语音唤醒 | 自建 `AudioRecord` 采集 + 唤醒词（不走小爱的私有唤醒引擎） | 🚧 采集/Opus/WS 已接上，**唤醒词模型未接**；当前是按住中间键说话（PTT）。2026-11-05 定位并修复了「按了没答复」的两个断点（编码器并发踩踏 + 服务端待机看门狗），**待装机复验**，详见 `docs/lx04/实机验证结果.md` 第九节 |
| 音量± 保留、中间键 = 服药确认 | `dispatchKeyEvent`：音量键**不消费**（交给系统），中间键吃掉事件并驱动确认状态机 | ✅ 实机确认：原厂上报 `KEY_POWER`，已用 keylayout 重映射为 **`KEYCODE_F1`**，App 只认 F1 |
| 取消事件灯/状态灯，改为屏幕两侧 5mm 色带 | `LedBandView` 左右各画一条；灯效语义 1:1 移植自固件 | ✅ 实机 800×480 / 240dpi ⇒ 5mm ≈ **47px**，按 density 动态折算 |

---

## 灯效语义与固件的对应

LX04 **没有任何物理 LED**，所以灯效落在屏幕上。但**语义不变** —— 颜色与节奏是
老人和家属约定好的信号，换硬件不等于换约定。

| 固件（`elder_rgb.h`） | Android 侧 |
| --- | --- |
| 待机闪烁星光 TWINKLE | 色带逐段点亮、单色渐隐，2 秒一格 |
| 唤醒聆听 #00BFFF 55% | 色带整体常亮深天蓝 |
| TTS 说话 #FF8C00 70% | 色带整体常亮深橙 |
| 网络传输中 METEOR | 亮头带拖尾沿色带跑过 |
| 服药未确认 | 红色快闪 |
| 按住渐变 #00E5FF | 色带自下而上填充（**渐变即进度条**） |
| 确认成功 #00BFFF | 天蓝闪 3 次 |
| 中途松开 #FF69B4 | 粉红闪 5 次 |

**亮度口径沿用固件**：0~255 的*感知亮度*（人眼看着几成亮），不是百分比、不是线性值。
`LedEngine.applyLevel` 里保留了这条口径的说明。

---

## 快速开始

```bash
cd lx04

# 1) 构建（需要 Android SDK；CI 里用官方镜像）
./gradlew assembleRelease

# 2) 连上设备，先探测（★ 必做：确认按键 KeyCode / 包名 / 麦克风占用）
./scripts/deploy.sh probe

# 3) 安装 + 接管
./scripts/deploy.sh all

# 4) 看状态
./scripts/deploy.sh status
```

---

## ⚠️ 部署前必读

### 1. 中间键：结论已实测，别再猜（2026-11-04）

原厂上报 `KEY_POWER`，**应用层拦不住**（`interceptKeyBeforeQueueing` 就吃掉了），
长按会弹关机菜单。已通过 `/system/usr/keylayout/mtk-pmic-keys.kl` 重映射成
`KEYCODE_F1`，`TopKeys.CONFIRM_KEY_CODES` 只认 F1。

**装机前必须确认 keylayout 真的刷进去了** —— 没刷的话按确认键毫无反应
（这是刻意的：宁可没反应，也不能按一下就熄屏）：

```bash
adb shell dumpsys input | grep -A2 mtk-pmic-keys   # KeyLayoutFile 应指向 mtk-pmic-keys.kl
adb shell getevent -lt /dev/input/event1          # 内核仍上报 116 属正常
```

### 2. 小爱桌面与麦克风占用：结论已实测

```bash
adb shell pm list packages | grep -iE "launcher|xiaomi|voice|mico"
```

- 桌面包名 `com.xiaomi.micolauncher` 已填进 `deploy.sh` 的
  `XIAOMI_LAUNCHER_PACKAGES`；**小爱语音没有独立包**，就在这个进程里
- ⚠️ 麦克风被 init 服务 **`mivpm`**（`/system/bin/MiVpmService`）常驻占用。
  `am force-stop micolauncher` **不能**释放它，必须 `stop mivpm` 才能录音。
  永久禁用见 `deploy.sh takeover` 与 `实机验证结果.md` 第四节

```bash
XIAOMI_LAUNCHER_PACKAGES='com.xiaomi.micolauncher' ./scripts/deploy.sh takeover
```

### 3. 刷机前整盘备份

4GB eMMC 刷多了会坏。详见 [`docs/lx04/实机验证清单.md`](../docs/lx04/实机验证清单.md) §0。

### 4. 架构只能是 armeabi-v7a

LX04 是 32 位用户空间。`build.gradle.kts` 里已锁 `abiFilters`，
**装 arm64 的 libopus 会 crash**。

---

## 还原

```bash
./scripts/deploy.sh restore
```

会重新启用小爱桌面并清除我们的 HOME 优先权，重启后按 Home 会重新询问。

---

## 阶段划分

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 0 | 实机探测（按键 KeyCode / 包名 / 麦克风 / 触摸方向） | ✅ 12 项测完，见 [`实机验证结果.md`](../docs/lx04/实机验证结果.md)；仅 App 内点按坐标待装机复核 |
| 1 | AudioRecord 采集 + Opus + WebSocket + 唤醒词 | 🚧 采集/编码/WS 已接上；「按了没答复」的两个断点已定位并修复（见 [`实机验证结果.md`](../docs/lx04/实机验证结果.md) 第九节），**待装机复验**；唤醒词未接；麦克风须先停 `mivpm` |
| 2 | 装机接管 HOME + system 分区瘦身，彻底移除小爱 | 📋 [`LX04移植方案.md`](../docs/lx04/LX04移植方案.md) §2（**未实机执行**） |
