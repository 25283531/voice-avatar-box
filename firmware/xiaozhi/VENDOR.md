# VENDOR — xiaozhi-esp32 上游源码同步说明

本目录是 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 上游源码的**瘦身镜像**（vendor 方式，非 submodule），
用于「老人陪伴 & 提醒助手」项目的 ESP32-S3 固件基线。

| 项目 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/78/xiaozhi-esp32 |
| 上游 tag | **v2.0.5（已冻结）** |
| 上游 commit | `860d12a12c7df9e933ff710b6f9061bf58d61ab2` |
| 初次同步日期 | 2026-10-15 |
| **冻结日期** | **2026-10-19（不再跟随上游更新，见下节）** |
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

## ⛔ 版本冻结（2026-10-19 起，本项目拍板）

**本目录自 2026-10-19 起固定在 v2.0.5，不再跟随上游更新。**

原因：本项目并非「所有定制都走 overlay 注入」，而是有一部分**直接改上游文件**：

| 文件 | 定制内容 |
| --- | --- |
| `main/display/lcd_display.cc` | 对话界面 70/30 布局（6 处几何改造，带 `elder-layout-70-30` 标记） |
| `main/main.cc` | elder_boot 任务 + 未绑定设备绑定引导任务 |
| `main/boards/bread-compact-esp32/esp32_bread_board.cc` | 唤醒词改宏引用 |
| `main/boards/bread-compact-esp32-lcd/esp32_bread_board_lcd.cc` | 同上 |
| `main/boards/esp32-cgc-144/esp32_cgc_144_board.cc` | 同上 |
| `main/boards/esp32-cgc/esp32_cgc_board.cc` | 同上 |
| `main/boards/common/wifi_board.cc` | 唤醒词模型注释同步 |
| `sdkconfig.defaults` | 唤醒词段落去重（消除死配置） |

一次 `sync`（含 `--force` / `--prune`）会把上面全部**静默覆盖**——因为同步脚本
只做「与上游内容比对」，**不认识「本地已定制」**。后果不是报错，而是「布局悄悄
回退 / 唤醒词又变回你好小智」，排查成本极高。

### 机制：sync 脚本默认拒绝同步

```
$ python tools/sync_xiaozhi.py
⛔ 上游同步已冻结（2026-10-19 …）
[sync] 已拒绝：加 --unlock 才会真正写盘。   # exit code 3
```

解锁需**三道门**同时通过（缺一不可，且非交互环境一律拒绝）：
1. 显式 `--unlock` 开关；
2. `--confirm 'I-ACCEPT-OVERWRITE-CUSTOM-PATCHES'` 逐字短语；
3. 交互式终端里再手敲一次 `sync-xiaozhi`。

`--dry-run`（只读统计）不受冻结限制。

### 解冻流程（将来真要升级上游时）

```bash
# 1) 先把定制 diff 存下来，否则同步后无从对照
git diff -- firmware/xiaozhi/ > /tmp/custom-before-upgrade.patch

# 2) 上游仓库切到目标 tag
cd D:/espdev/src/xiaozhi-esp32 && git checkout <目标tag>

# 3) 预览差异（不写盘，安全）
python tools/sync_xiaozhi.py --dry-run

# 4) 同步（三道门全过才会写盘）
python tools/sync_xiaozhi.py --unlock \
    --confirm 'I-ACCEPT-OVERWRITE-CUSTOM-PATCHES'

# 5) 重跑 merge 注入自研组件
python tools/merge_to_xiaozhi.py && python tools/merge_to_xiaozhi.py --check

# 6) 按 README「上游定制补丁」节，把上表 8 个文件的定制重新贴回去
#    （merge --check 会因 elder-layout-70-30 标记缺失而红，正是提醒信号）

# 7) 同步 VENDOR.md 的 tag / commit / 同步日期字段，并更新
#    tools/sync_xiaozhi.py 顶部的 PINNED_TAG / PINNED_COMMIT / PINNED_ON
```

若上游升级了大版本（更换 `main/idf_component.yml` 依赖），需清空
`firmware/xiaozhi/build/managed_components` 让组件管理器重新解析。

## 与本项目自研代码的关系
- 自研组件在 `firmware/components/elder/`，通过 `tools/merge_to_xiaozhi.py`
  拷贝进 `firmware/xiaozhi/components/elder/` 后一起编译。**这类新增文件不会被
  sync 影响**（sync 只覆盖上游清单内的文件）。
- 上游文件**允许直接改**（本项目已在上表列出全部直接改动点），但每处都必须：
  ① 加可被 `merge --check` 或标记扫描识别的锚点；
  ② 在 README「上游定制补丁」节写明重贴步骤；
  ③ 在本文件上表登记。
- 新增代码仍优先放 `components/elder/`——只有「改上游逻辑本身」才动上游文件。
