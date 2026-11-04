# elder-lx04

小爱触屏音箱 **LX04** 设备端客户端 —— 「老人陪伴提醒助手」的 LX04 移植版。

> 上游项目：仓库根目录的 `voice-avatar-box`（ESP32-S3 版）。
> 本目录是**独立的 Android 应用**，不是 `android/`（家属端 WebView 壳）的附属品。

---

## 为什么是 Android 应用而不是固件

LX04 是 **Android 设备**（MTK MT8167A / Android 8.1 / 480×800），不是 ESP32。
所以移植 = 写一个原生 App + 把系统换成最小集，**不是**重编固件。

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
| 开机直接进我们的系统，不进小爱桌面 | Manifest 注册 `MAIN+HOME+DEFAULT` + `BootReceiver` + `deploy.sh takeover` | ✅ 代码就绪，待实机验证 |
| 可以直接语音唤醒 | 自建 `AudioRecord` 采集 + 唤醒词（不走小爱的私有唤醒引擎） | 🚧 阶段 1 |
| 音量± 保留、中间键 = 服药确认 | `dispatchKeyEvent`：音量键**不消费**（交给系统），中间键吃掉事件并驱动确认状态机 | ✅ 代码就绪，**中间键 KeyCode 待实机确认** |
| 取消事件灯/状态灯，改为屏幕两侧 5mm 色带 | `LedBandView` 左右各画一条；灯效语义 1:1 移植自固件 | ✅ 代码就绪 |

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

### 1. 中间键的 KeyCode 必须实测

**不能猜。** 猜成 `KEYCODE_POWER` 的后果是老人长按服药键把音箱关了。
猜成 `KEYCODE_HOME` 的后果是我们的默认桌面被自己触发。

```bash
adb shell getevent -lt
# 依次按 音量+ → 音量- → 中间键，记录 keycode
```

然后填进 `TopKeys.CONFIRM_KEY_CODES`（收敛为单一值）。

### 2. 小爱桌面包名必须实测

```bash
adb shell pm list packages | grep -iE "launcher|xiaomi|voice|mico"
```

填进 `deploy.sh` 的 `XIAOMI_LAUNCHER_PACKAGES`：

```bash
XIAOMI_LAUNCHER_PACKAGES='com.xiaomi.xxx,com.yyy.zzz' ./scripts/deploy.sh takeover
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
| 0 | 实机探测（按键 KeyCode / 包名 / 麦克风 / 触摸方向） | 📋 [`实机验证清单.md`](../docs/lx04/实机验证清单.md) |
| 1 | AudioRecord 采集 + Opus + WebSocket + 唤醒词 | 🚧 |
| 2 | system 分区瘦身，彻底移除小爱 | 📋 [`LX04移植方案.md`](../docs/lx04/LX04移植方案.md) §2 |
