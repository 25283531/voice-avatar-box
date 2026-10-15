# VENDOR — xiaozhi-esp32 上游源码同步说明

本目录是 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 上游源码的**瘦身镜像**（vendor 方式，非 submodule），
用于「老人陪伴 & 提醒助手」项目的 ESP32-S3 固件基线。

| 项目 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/78/xiaozhi-esp32 |
| 上游 tag | **v2.0.5** |
| 上游 commit | `860d12a12c7df9e933ff710b6f9061bf58d61ab2` |
| 同步日期 | 2026-10-15 |
| 同步文件数 | 1295 |
| 同步体积 | 5.0MB |

## 为什么不是整棵搬
上游整仓约 567MB，其中：
- `.git/` 上游历史
- `managed_components/` 约 509MB，是 IDF 组件管理器按 `dependencies.lock`
  自动拉取的第三方依赖（LVGL / esp_emote_gfx / esp-sr / codec 等）
- `build/` 约 43MB，构建产物

这些都**不进入本仓库**：`managed_components` 在首次 `idf.py build` 时由组件管理器
依据 `main/idf_component.yml` + 根目录 `dependencies.lock` 自动下载还原。

## 过滤规则
同步脚本：`tools/sync_xiaozhi.py`

保留：
- 根目录：`CMakeLists.txt`、`LICENSE`、`README*.md`、`.gitignore`、
  `dependencies.lock`、`sdkconfig.defaults*`
- `main/`（固件源码、板卡目录、assets 提示音）
- `partitions/`（分区表 CSV）
- `scripts/`（构建相关脚本，剔除其中的 PC 端工具子目录）

排除：
- 目录：`.git/`、`build/`、`managed_components/`、`docs/`、`.github/`
- `scripts/` 子目录：`Image_Converter/`、`ogg_converter/`、`acoustic_check/`、`p3_tools/`
- 后缀：`.mp3 .wav .png .jpg .gif .mp4 .zip .exe .pdf …`（大体积素材）
- 单文件 > 8MB

### 关于音频资源
`main/assets/**/*.ogg` **全部保留**：它们是开机提示音、激活提示音、配网提示音，
由 `main/CMakeLists.txt` 的 `EMBED_FILES`/`build_default_assets.py` 编进固件。
按语言分目录存放，编译时只取 `CONFIG_LANGUAGE_DIR` 指定的一种，单文件均 < 20KB。

## 如何更新
```bash
# 1) 在上游仓库切到目标 tag
cd D:/espdev/src/xiaozhi-esp32 && git checkout v2.0.5

# 2) 预览差异（不写盘）
cd G:/workbuddy/语音助手 && python tools/sync_xiaozhi.py --dry-run

# 3) 同步（幂等，重复执行只做增量）
cd G:/workbuddy/语音助手 && python tools/sync_xiaozhi.py

# 4) 更新本文件的 tag / commit / 日期字段后提交
```

若上游升级了大版本（更换 `main/idf_component.yml` 依赖），需清空
`firmware/xiaozhi/build/managed_components` 让组件管理器重新解析。

## 与本项目自研代码的关系
- 自研组件在 `firmware/components/elder/`，通过 `tools/merge_to_xiaozhi.py`
  拷贝进 `firmware/xiaozhi/components/elder/` 后一起编译。
- 上游代码**只读**，任何业务修改都应放在 `components/elder/` 或 `main/` 的
  独立文件中，不要直接改上游文件。
