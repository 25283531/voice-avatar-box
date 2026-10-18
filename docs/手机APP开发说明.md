# 手机端（含小程序）开发说明

> ⚠️ **APP 不是必需路径（2026-10-16 用户拍板）**
> 本项目的服务端已提供完整的**网页用户端** `/user`（`server/app/static/user.html`），
> **设备绑定与付费开通在网页上全部可以完成，不需要 APP**。
> 关键事实：后端端点早就存在且只挂 `get_current_user`
> （如 `POST /api/devices/bind`），缺的只是网页端的调用。
>
> 此外，**WiFi 配网也不需要 APP**：固件原生支持浏览器配网（ESP32 自开热点
> `XiaoNuan-XXXX`，屏显热点名与 `http://192.168.4.1`，DNS 强制门户拦截），
> 页面在 `firmware/xiaozhi/managed_components/78__esp-wifi-connect/assets/wifi_configuration.html`。
> APP 只是这层浏览器配网的另一层封装。
>
> 所以本文档描述的是**可选增强渠道**（子女想要更顺手时的选择），
> 不是产品的必要组成部分。管理后台另有 `/`（按 `is_admin` 过滤）。

## 1. 来源与二次开发策略

手机端直接基于小智官方的 **`xiaozhi-esp32-server / main/manager-mobile`** 二次开发：

- 技术栈：**uni-app v3 + Vue 3 + TypeScript + Vite + wot-design-uni**，MIT 协议
- 一套代码同时产出：**H5 / Android / iOS / 微信小程序**（可选支付宝、百度小程序、鸿蒙）
- 保留原有小智功能（配网、智能体、音色、对话历史、声纹），**新增老人陪伴业务模块**

> 之所以不用 Flutter 版 `xiaozhi-android-client`：uni-app 版是官方后端同源项目，
> 且能同时出小程序——子女不愿装 App 时直接用微信小程序，这解决了「装 App 是最大流失点」的问题。

## 2. 新增文件

```
app/src/
├── http/elder.ts                  业务后端请求层（uni.request 封装，独立 token）
├── api/elder/index.ts             业务 API 与类型定义
├── pages/elder/
│   ├── index.vue                  设备列表 + 登录 + 激活码绑定
│   ├── reminder.vue               提醒规则列表 + 执行回执 + 代确认
│   ├── reminder-edit.vue          复杂规则编辑（按周/按月/隔天/一天多次）
│   ├── stats.vue                  交互次数统计（日/周/月）
│   └── avatar.vue                 形象选择与应用
└── layouts/fg-tabbar/tabbarList.ts  底部导航新增「陪伴」入口（已改）
```

请求层为什么不复用项目自带的 alova 实例：那个实例绑定小智 Java 后端
（`{code,msg,data}` 响应结构 + 自己的 token 体系），我们的 FastAPI 返回裸数据，
混用会互相污染，因此独立一套、互不干扰，也便于单独切换服务地址。

## 3. 配置

`app/env/.env`（及 `.env.production`）新增一行：

```bash
VITE_ELDER_BASEURL = 'http://192.168.1.10:8080'    # 改成实际后端地址
```

App 内也支持运行时修改：设备列表页登录框下方「保存服务地址」会写入本地存储，优先级高于编译期配置。

**微信小程序注意事项**
- 小程序要求 HTTPS，且域名需在微信公众平台配置为合法 request 域名
- 开发阶段可在微信开发者工具勾选「不校验合法域名」
- 个人主体小程序可以给别人使用，但「服药 / 健康」容易被归入医疗类目被驳回，
  建议选「工具类 / 生活服务」类目，页面文案避开「问诊、处方、健康档案」

## 4. 构建命令

```bash
cd app
pnpm i                    # 安装依赖（Node >= 18, pnpm >= 7.30）

pnpm dev:h5               # H5 开发调试，浏览器打开日志里的地址
pnpm dev:mp               # 微信小程序，用开发者工具导入 dist/dev/mp-weixin
pnpm dev:app-android      # Android 真机调试
pnpm dev:app-ios          # iOS 真机调试

pnpm build:h5             # 产出 dist/build/h5，可直接放 Nginx
pnpm build:mp-weixin      # 产出 dist/build/mp-weixin
pnpm build:app            # App 资源包，再用 HBuilderX 云打包 / 离线打包
```

## 5. 功能对照

| 需求 | 页面 | 说明 |
| --- | --- | --- |
| 绑定手机 APP（需求 6） | `elder/index` | 输入设备屏幕上的 13 位激活码完成绑定 |
| 添加日程 / 服药提醒（需求 7/9） | `elder/reminder-edit` | 支持每天 / 每周 / 每月 / 隔天 / 一次性，一天多时刻 |
| 查看客户端交互次数 | `elder/stats` | 今日交互、对话轮次、按时确认、超时未确认 + 每日柱状图 |
| 选择角色和形象 | `elder/avatar` | 列出 EAF 形象包，一键应用到设备 |
| 查看服药是否按时 | `elder/reminder` 回执页 | 状态、确认方式（语音/按键/手机端）、确认时间 |
| 子女代老人确认 | `elder/reminder` | 老人不方便按键时由子女端确认，设备红灯熄灭 |

## 6. 与设备端的约定

- 设备每 5 分钟 `POST /api/device/sync` 拉配置，形象切换最迟 5 分钟内生效
- 提醒由设备本地 RTC 触发，联网后回传 `/api/device/events`，APP 看到的就是对账结果
- 交互事件由设备 `POST /api/device/interactions` 上报，统计页按天聚合
- **OTA 只给已绑定设备签发 token**；未绑定只返激活码。
  固件 `CONFIG_OTA_URL` 默认已指向自己的 `/api/voice/ota`
  （真源在 `Kconfig.projbuild`），**不依赖任何第三方服务器**。

## 7. 音频契约（写流式播放代码前必读）

- 下行音频是 **24kHz / mono / S16LE裸 PCM**，不是 wav/mp3
  （`TTS_RESPONSE_FORMAT="pcm"`、`TTS_SAMPLE_RATE=24000`）
- 固件按服务端声明的采样率**逐帧**做 Opus 编码
- ⚠️ 服务端下行时序 `stt → tts start → sentence_start×N → tts stop` **不能改**——
  设备靠它回落 idle 状态
- 连续多次快速合成可能被上游限流（Edge TTS 实测第 3 次会超时），
  客户端重试要有退避，不要立即连打
