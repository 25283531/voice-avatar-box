# 固件发布 2026-10-27 —— 灯效重定义 + 两个崩溃修复

- 提交：`0982930`（固件）+ `3a184fd`（CI 门禁跟进）
- CI：firmware-build ✅ / server-ci ✅ / pr-gate ✅
- 产物：`elder-firmware-esp32s3-merged-flash.bin`
  SHA256 `e169b1b69ce648e50013c8be799fa74744c2ee9be458bae454acadf98041ac35`
- 烧录：已写入设备 `COM11`，**只写 app 分区 0x20000**（NVS 里的提醒、音量、WiFi 全部保留）

## 一、语音调音量必现重启（已修，根因确定）

不是栈、不是供电，是**确定性 abort**：

1. 服务端下发 `self.audio_speaker.set_volume` 时，设备处于 listening，ES8311 输出是**关着**的；
2. `esp_codec_dev_set_out_vol()` 走 `_verify_codec_setting → _verify_codec_ready`，
   发现 `is_open == false`，返回 `ESP_CODEC_DEV_WRONG_STATE`；
3. `BoxAudioCodec::SetOutputVolume()` 用 `ESP_ERROR_CHECK` 包着它 ⇒ `abort()` ⇒ PANIC 重启。

修法：改成检查返回值 + 打 WARN。**音量不会丢** —— `AudioCodec::SetOutputVolume()`
照样更新 `output_volume_` 并落 NVS，而 `EnableOutput(true)` 打开 codec 之后
本来就会按 `output_volume_` 补写一次（那行代码原本就在）。下次开口即生效。

## 二、灯效整套重定义

`#2~#9`：

| 场景 | 颜色 | 亮度 | 效果 |
|---|---|---|---|
| 待机 | 天蓝 `#87CEFA` | 30% | 呼吸 |
| 唤醒后收听 | 蓝 `#0000FF` | 100% | 常亮 |
| 网络传输处理中 | 五彩 | 80% | 低速流水（60°/秒，逐颗 45°） |
| TTS 输出 | 深橙 `#FF8C00` | 100% | 常亮 |

`#1`（常态**不亮**）：

| 条件 | 表现 |
|---|---|
| 无待确认提醒 | 熄灭 |
| 有待确认提醒 | 深橙 `#FF8C00` 100% 呼吸 |
| 按住确认键 | 浅绿 `#90EE90` → 深绿 `#006400`，3 秒走完 |
| 确认完成 | 深蓝绿 `#00CED1` 闪 3 次 |
| 中途松开 | 深粉红 `#FF1493` 闪 5 次 |

**"网络传输处理中"在固件里没有对应状态**：realtime 模式全程停在
`kDeviceStateListening`（`CONFIG_USE_DEVICE_AEC=y`），是服务端 VAD 决定何时开口。
所以新增 `elder_on_vad_change(bool)`，用"本轮是否已开口"区分：

- 还没开口 + 静音 → 蓝（等你说话）
- 开过口 + 静音 → 五彩流水（在等回复）

## 三、长按确认改一步 + 提示音挪出按键任务

- 长按满 3 秒**即确认完成**，删掉"挂起 → 30 秒内再短按"两步
  （与灯的渐变语义冲突：灯走到最深绿了却还要再按一下，一犹豫就超时）。
- `OnPlayTone` 原先**就地** `PlaySound`，而 `AudioService::PlaySound()` 是同步的
  （解 OGG 页 + `codec->EnableOutput(true)` + 推 I2S），被 `elder_input`(4096)
  与 `esp_event` 两条小栈路径同步调用 —— 长按确认偶发重启的嫌疑链。
  改为 `Application::Schedule()` 到主事件循环。
- `elder_input` 栈 4096 → **8192**，并在每次按键事件后打**栈余量**日志。

## 需要真机确认的三件事

1. 说「把音量调小一点」「把屏幕调暗一点」—— **不应该再重启**，且音量/亮度真的变了。
2. 长按 GPIO11 满 3 秒 —— 灯从浅绿走到深绿、松手后闪深蓝绿 3 次；
   串口里看 `elder_input 栈余量 xxxx 字节`，**接近 0 就是栈问题**。
3. 四个灯效状态是否与描述一致（尤其"流水"是逐颗流动、不是整条同色）。

## 观察项（不是缺陷）

`#2~#9` 在"说话/收听"是 100% 亮度，8 颗橙最坏约 250mA。若设备从弱 USB 口取电，
同时开背光 + 大音量时可能触发欠压复位（串口会打
`复位原因=BROWNOUT`）。真出现的话把这两个 100% 降一档即可，一行常量的事。
