# voice-avatar-box · 老人陪伴 & 提醒助手

面向老年人的语音陪伴与服药提醒助手，硬件基于 **立创·实战派 ESP32-S3**，
后续移植 **ESP32-S3-BOX-3 / ESP-VoCat**。

**语音链路是自研的**（`server/app/voice/`：WebSocket 服务端 + Opus 编解码 + 会话编排 + OTA），
与业务 API 同进程同端口，运行时**不依赖任何第三方语音服务器**。
固件侧借鉴开源 [xiaozhi-esp32](https://github.com/xinnan-tech/xiaozhi-esp32) 的
**公开协议**（设备侧 WebSocket 帧格式、OTA 响应结构）与固件设计，
从而在固件零改动的前提下自研服务端——**协议兼容 ≠ 调用它的服务器**：
[xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 只作为协议与固件设计的参考，
本项目不部署、不调用它（早期曾跑过它的容器，已删除）。设备出厂 OTA 地址默认指向本项目自己的域名。

本项目自己做的「业务面」：设备绑定、提醒与回执、形象/提示词、AI 平台配置、交互统计、
**双界面**（浏览器管理后台 `/` + 浏览器用户端 `/user`，配网也不需要 APP）、手机端 APP。

⚠️ **两件反直觉的事实，先知道能省很多排查时间**：

1. **「零配置即可提供服务」不成立**。全新空库下 ASR / TTS / VAD / Chat 四项全未就绪，
   `resolve_provider` 直接返 503。管理后台「服务就绪度」面板的作用是**如实反映未就绪**。
2. **平台会员制已取消**（commit `e264200`）。已授权设备陪聊恒为无限量，没有订单、
   没有核销、没有会员期限。

## 目录

| 路径 | 说明 |
| --- | --- |
| `server/` | **单一镜像**的服务端：FastAPI 业务后端 + 自研语音链路 + 记忆与人格模块 + 多模型网关 + Docker 部署文件（`docker-compose.local.yml` 是可选的本地 ASR/VAD profile） |
| `app/` | 手机端家属端 APP（uni-app v3 + Vue3），四个栏目：首页 / 设备 / 日程 / 我的；出 H5 / 微信小程序 / Android APK |
| `android/` | Android 原生壳工程（WebView + `WebViewAssetLoader`），把 H5 打成 APK，不依赖 DCloud |
| `firmware/` | ESP32-S3 固件（`components/elder` 为本项目新增组件，编译前由 `tools/merge_to_xiaozhi.py` 合入 xiaozhi 源码树） |
| `tools/` | 辅助脚本：`smoke_*.py` 后端冒烟（端口唯一真源 `tools/smoke_ports.py`）、`ci_gate_check.py` 本地门禁判据、`merge_to_xiaozhi.py` 注入 elder 组件与 sdkconfig 覆盖、`make_firmware.py` 合并烧录 bin、`idfwin.py` / `winrun.py` Windows 下的 IDF 包装 |
| `docs/` | [`架构设计.md`](docs/架构设计.md) 平台整体架构与选型 · [`DEPLOY.md`](docs/DEPLOY.md) 部署与 MySQL 接入 · [`决策记录.md`](docs/决策记录.md) 用户拍板的决策及其边界 · [`免费语音服务商选型.md`](docs/免费语音服务商选型.md) ASR/TTS 厂商选型 · [`架构设计-角色模型与人格能力.md`](docs/架构设计-角色模型与人格能力.md) · [`架构设计-人格预设运营后台.md`](docs/架构设计-人格预设运营后台.md) · [`烧录指南.md`](docs/烧录指南.md) · [`手机APP开发说明.md`](docs/手机APP开发说明.md) |
| `老人陪伴提醒助手-技术方案-v1.md` | 完整技术方案（硬件、形象引擎、激活码算法、风险清单） |

## 端口与地址约定

| 项 | 值 | 备注 |
| --- | --- | --- |
| 容器内监听 | `8080` | `ELDER_PORT` 可覆盖 |
| **对外发布** | **`8064`** | 镜像只发布这一个端口，`-p 8064:8080`。HTTP API、语音 WebSocket、OTA 全复用它 |
| 语音 WebSocket | `ws(s)://<地址>/api/voice/v1/ws` | **自研链路**，与 API 同源同端口，不需要额外开端口 |
| 生产地址 | `https://152.136.230.132` | **IP 直连**（2026-10-18 起）。原因：域名 `voice.250886.xyz` 未备案，被云厂商按 SNI 阻断（连 IP 不带 SNI 则 TLS 畅通，已实证）；APP 与固件已改为固化此 IP。**前提：服务器须持有 SAN 含该 IP 的 IP 证书**（Let's Encrypt 已支持，见 DEPLOY.md §4.3），否则固件/APP 报证书名不符 |
| 管理后台 | `https://152.136.230.132/` | 源码在 `server/app/static/index.html`，按 `is_admin` 过滤入口。账号 `admin` 的口令**不再有硬编码默认值**（`admin/admin123` 已从代码移除，commit `bb5ca98`）：首次启动必须经 `ELDER_SEED_ADMIN_PASSWORD` 显式提供，否则随机化并打进启动日志 `ELDER-INITIAL-PASSWORD=<pwd>` |
| 用户端 | `https://152.136.230.132/user` | 源码在 `server/app/static/user.html`，**浏览器直接用，不需要装 APP**；绑定设备等操作全在 web 完成 |
| 接口文档 | `https://152.136.230.132/docs` | FastAPI 自动生成 |
| 原域名（备用） | `https://voice.250886.xyz` | 备案恢复前，域名访问会被厂商按 SNI 杀连接（时通时断=QUIC 加密了 SNI 时通）。**域名不变、代码不改**：把服务器证书/IP 证书部署形态切回域名证书即可，APP 与固件需重打包/重烧 |

## GitHub Actions 工作流

| 工作流 | 触发 | 作用 |
| --- | --- | --- |
| `pr-gate.yml` | push 到 `main`/`master` + **每个 PR**（刻意不加 paths 过滤） | 常驻门禁「守门人自检」：CODEOWNERS 覆盖 + workflows 可解析。push 时自动跑，结果可见但**不再拦截推送**（见下方流程变更） |
| `server-ci.yml` | push/PR 改动 `server/`、`tools/`、`firmware/components/elder/`；可手动 | 装依赖（含 `libopus0`）后跑 **8 个 `smoke_*.py` 冒烟脚本 + 6 个 QA/审计脚本**（清单见下方），另有 5 个 job：elder 组件宿主测试、MySQL 真实路径冒烟、Compose 文件校验、Dockerfile 语法检查（hadolint） |
| `docker-build.yml` | push/PR 改动 `server/`；可手动 | 构建镜像 → 起容器校验 `/api/health`；**push 到 `main` 且容器冒烟通过后**才推 `ghcr.io/25283531/voice-avatar-box` 的 `latest` + 短 SHA（冒烟失败不推，`latest` 不被污染）；PR 只构建不推送；手动触发勾选 `publish` 可覆盖 tag 发布 |
| `firmware-build.yml` | push 改动 `firmware/`、`tools/merge_to_xiaozhi.py` 或本文件；可手动 | 在 `espressif/idf:v5.5.1` 容器里编译 ESP32-S3 固件 → 合并单文件烧录镜像 → 上传 artifact。**每次构建都会打印生效的 `CONFIG_OTA_URL` 并在偏离本项目地址（IP `152.136.230.132` / 域名 `voice.250886.xyz`）时告警**；手动触发可自选板卡与后端地址 |
| `app-build.yml` | push/PR 改动 `app/`；可手动 | 构建 H5 / 微信小程序并上传产物；校验后端地址已内联 |
| `android-build.yml` | push/PR 改动 `app/`、`android/`；可手动 / 发 Release | **一次产出可安装的 Android APK**（H5 + WebView 壳，全程在 runner 内，不用 DCloud 账号） |
| `app-pack.yml` | **仅手动**（workflow_dispatch）/ 发 Release | ⚠️ **已废弃**：uni-app 云打包，DCloud 账号登录失败且强制实名认证。保留文件只为留档，现走 `android-build.yml`（见下文「Android APK」） |

#### `server-ci.yml` 实际跑哪些冒烟

⚠️ **不是只有一个 `smoke_server.py`**。数口径时要分清三件事：**脚本个数 ≠ 调用次数**：

- `smoke:` job 里跑 **8 个不同的 `smoke_*.py`**（端口已登记在 `tools/smoke_ports.py`，
  那是唯一真源，新脚本先登记再写死）：

| 脚本 | 端口 | 守什么 |
| --- | --- | --- |
| `smoke_server.py` | 8100 | 业务闭环：注册/登录/设备/激活码(Luhn 与固件一致)/绑定/提醒复杂规则/排期/同步/回执/统计/鉴权 |
| `smoke_auth.py` | 8121 | 注册鉴权、公开开关、初始管理员引导 |
| `smoke_roles.py` | 8101 | 角色域 + P0 越权断言 |
| `smoke_admin_presets.py` | 8118 | 人格预设运营后台：红线不出库 + 审计同事务 + 重启不变式 |
| `smoke_quota.py` | 8106 | 陪聊计量 + 「授权设备无限量」不变量（原 `smoke_membership`，会员端点须已下线） |
| `smoke_usage.py` | 8120 | token 用量：网关提取 + 双端接口对账 + 越权隔离 |
| `smoke_voice_catalog.py` | 8124 | 音色目录单一真源 + **就绪度面板如实反映零配置未就绪** |
| `smoke_voice.py` | 8119 | 自研语音链路：WS 协议逐字段对固件 + Opus 编解码 + OTA 签发口径 |

- 另有 **6 个 QA / 审计脚本**同在 `smoke:` job 里，它们**不属于 `smoke_*.py` 系列**，
  别数漏也别数重：

| 脚本 | 端口 | 守什么 |
| --- | --- | --- |
| `qa_audit_rollback.py` | 不占端口（进程内直调端点） | 审计写失败时业务写必须回滚 |
| `eval_persona.py` | 不占端口 | 人格 eval 门禁，医疗安全类硬失败 |
| `qa3_r2_restart.py` | 8113 | 重启不变式：回填不得改写角色归属 |
| `qa3_migration.py` | 不起服务 | 迁移完整性：ORM 列与 `schema_patch.NEW_COLUMNS` 对账 |
| `qa3_device_binding.py` | 8114 | 设备绑定归属：重启不被绑上平台预设 |
| `qa3_drop_tables.py` | 不占端口 | 会员遗留五张表必须真删，**掏空实现必变红** |

- `mysql-smoke:` job **复跑 2 个**：`smoke_server.py 8100` 与 `smoke_roles.py 8101`
  走真实 MySQL 往返，另加 `eval_persona.py` 与 `qa3_drop_tables.py`。
  **这是同一批脚本的方言复跑，不是新脚本** —— 所以全仓「不同脚本数」是 8 + 6 = **14 个**，
  而 CI 里 `python tools/smoke_*.py` 的**调用次数**是 8（SQLite）+ 2（MySQL）= **10 次**。

⚠️ 其余 4 个 `qa3_*` 脚本**刻意不进 MySQL job**——它们直连 `sqlite3` 且 `boot()` 里
无条件 `env["ELDER_DB"] = f"sqlite:///{DB}"`，会**覆盖掉 CI 传入的 MySQL 连接串**。
硬塞进去的话，步骤名写着 mysql+pymysql、实际跑的是 SQLite ——
那正是本项目最该防的「假绿」，比不接更坏。

#### CI 发布策略

- push 到 `main` 且**触及 `server/**`** → 构建 + 容器冒烟，**通过后**才推
  `ghcr.io/25283531/voice-avatar-box:latest` + 短 SHA tag。
  **冒烟失败不推镜像，`latest` 不被污染。**
- PR **只构建不推送**（PR 代码可能从未被 review，但触发的构建一旦推上 ghcr
  就是公共产物；且 `pull_request` 事件下 `GITHUB_TOKEN` 只读，`packages: write` 不生效）。
- ⚠️ **本机无 GitHub API 凭据**（未装 `gh`、无 PAT），只有 git SSH ⇒ **无法用 API 触发 workflow**。
  固件构建靠 push 触及 `firmware/**` 自动触发；需要自选板卡时在 GitHub UI 点
  Actions → firmware-build → Run workflow（**推荐，可自选板卡**）。

增量触发：每个工作流都带 `paths` 过滤，推送只跑受影响的那条
（例如只改 `docs/` 不会触发任何构建；改 `tools/` 只跑 `server-ci`；
改 `firmware/components/elder/` 会同时触发 `server-ci` 的宿主测试 job 和
`firmware-build`——两者守的是不同的东西）。

手动产出最终产物：**Actions → 对应工作流 → Run workflow**。

- `docker-build`：合入 `main` 即自动发布 `latest`（iStoreOS 上 `docker pull ghcr.io/25283531/voice-avatar-box:latest` 即用）；也可手动触发勾选 `publish`、填 `tag`（如 `v0.1.0`）发布指定版本，产物在 ghcr 包列表
- `app-build`：选 `both` / `h5` / `mp-weixin` → 产物在 Actions 运行页的 Artifacts
- `android-build`：选 `self`（仓库证书签名）/ `debug` → 产物为可直接安装的 APK
- `firmware-build`：产物 artifact 名 `elder-firmware-esp32s3`，**日常只下 `elder-firmware-esp32s3-merged-flash.bin`**（offset `0x0`，esptool 单文件直烧）；其余 bin 是按 `flasher_args.json` 分地址烧写的备选

### 手机 APP 栏目

APP 是给家属用的，不接入小智任何服务，业务后端地址在代码级写死
（`app/src/http/elder.ts`，生产 IP `https://152.136.230.132`），**不提供任何网络设置入口**。

⚠️ **APP 不是唯一入口**：绑定设备等操作在浏览器打开 `/user` 同样能做完，
固件配网也不需要 APP（见下文「双界面与配网」）。

| 栏目 | 内容 |
| --- | --- |
| 首页 | 已绑定设备、设备状态（网络连接 / 设备时间）、当日服药状态（待服数量、待服时间点、已确认次数） |
| 设备 | 添加设备（激活码）、删除设备、重命名、给设备配网、为每台设备绑定不同 agent 角色 |
| 日程 | 服药提醒与日程提醒：新增 / 编辑 / 暂停 / 代老人确认 / 删除 |
| 我的 | 账户名、修改密码、更换绑定邮箱、创建自有 agent 角色、系统形象库、缓存管理、隐私政策、用户协议 |

> **平台会员制已取消**（commit `e264200`）：会员状态、开通/续费会员、订单与人工核销
> 全部不存在，相关五张表由 `server/app/schema_patch.py::drop_membership_tables()`
> 在启动期真正 DROP。**已授权（已绑定）的设备陪聊恒为无限量**；时长计量仍在，
> 但只用于展示与对账，不再拦人（`server/app/chat_quota.py`）。
> 代码里「授权」的判据就是**设备是否已绑定到某个用户**（`dev.owner_id is not None`），
> 绑定动作本身在 `/user` 或 APP 里输激活码完成，**没有在线支付环节**。

## 分支保护、门禁与实际提交流程

> ⚠️ **先读这一句免得白忙**：本项目**不开分支、不开 PR，所有改动直接提交并推送到 `main`**
> （用户 2026-10-16 拍板，下称 D5）。本节描述的是 **GitHub 仓库侧的分支保护设置**
> 与**推送前的本地门禁**，不是「你需要提 PR」。下面那段历史（曾经要求 PR + 评审）
> 只作为「为什么规则被改成现在这样」的记录保留。

仓库默认分支是 **`main`**（本地还有一个落后 19 个提交的僵尸 `master`，不要往它推）。
远端 `github` = `git@github.com:25283531/voice-avatar-box.git`（HTTPS 在本机被网络阻断），
推送必须显式指定 key：

```bash
GIT_SSH_COMMAND="ssh -i C:/Users/Administrator/.ssh/id_ed25519_25283531 -o StrictHostKeyChecking=no" \
  git push github main
```

### 为什么没有 PR

⚠️ **流程已变更（2026-10-16，决策 D5）**：早期是「功能分支 + PR + 评审批准」，
实测跑不通后改为**直接提交并推送到 `main`**。原因是单协作者仓库的自审被 GitHub
拒绝（`POST /pulls/{n}/reviews` 返回 422、合并返回 405「At least 1 approving
review is required」），而"过严的守门人等于没有守门人"—— 保护规则挡住了
唯一一个本该干活的人，代码因此长期停在分支上进不了 main，连部署都拿不到。

现在的流程固定为：

```
改代码 → 本地跑门禁判据（exit 0）→ commit → 直接 push 到 main
```

**main 上仍保留的保护**：禁强推、禁删分支、`enforce_admins`（管理员也不豁免）。
**已移除**：必需状态检查「守门人自检」—— 它是唯一会拦截直接 push 的规则，
留着就直接推不上去。`pr-gate` 改为 push 后运行：仍然每次都跑、结果在 PR /
commit 页可见，**只是不再拦截**。若将来恢复多人协作，把它加回来即可
（`PATCH /repos/{o}/{r}/branches/main/protection`，改名前先改 GitHub 仓库设置，
改必需状态检查名 = 保护失效）。

### 推送前的本地门禁

推 main 前，本地先跑一遍门禁判据（与 CI 同一个脚本，exit 0 才推）：

```bash
python tools/ci_gate_check.py
```

> **为什么移除状态检查后还留着 CODEOWNERS 与 pr-gate**：它们的作用从"合并前的
> 自动关卡"变成了"push 后的自动体检 + 危险路径所有权的登记簿"。危险路径清单
> 本身仍然有效（见下一节）—— 它回答的是"改这些地方时该找谁、该多想一层"，
> 这个价值与走不走 PR 无关。

### 危险路径清单（改这些路径前必须自己多想一层）

登记在 `.github/CODEOWNERS`（默认行 `* @25283531` + 下列显式条目）。
「出事就是数据损坏」不是形容词，是事故链路的第一环：

| 路径 | 为什么危险 |
| --- | --- |
| `server/app/schema_patch.py` | **启动期写表**：backfill / dedupe / `_patch_version` 登记跑在每次进程启动时，判据决定「哪些行会被改写」 |
| `server/app/main.py` | 启动 seed 人格预设：平台预设角色的唯一来源（改一次发版一次，运营无法调人格） |
| `server/app/db.py` | `init_db` / `wait_for_db`：建表与连库等待，是回填的执行环境 |
| `server/app/persona/` | 人格红线目录：5 条硬红线（一律不给结论、不做判断、不推荐具体疗法或药品等）。红线被改坏不会自己变红 |
| `server/evals/` | 红线黄金副本基准：评测判据的权威数据，与 `catalog.py` 必须同时改才自洽 |
| `.github/workflows/` | CI 本体：改这里 = 改「什么能进 main」 |

事故链路（真实发生过的 P0，不是假想）：

```
启动回填判据写错 → 重启时改写用户角色归属 → 用户派生角色被永久刷成 preset
→ 角色变只读 + 全平台可见 = 跨用户数据泄漏
```

没有报警、没有堆栈，用户侧看不出任何异常，等发现时数据已被改写多轮。

⚠️ **这些路径出事时没有任何自动信号**。流程改为直接推 main 后（D5），
「合并前有人看过 diff」这道关卡已经不存在了，所以唯一的防线变成了
**动手前的自查**。改这些文件时逐条问自己：

1. 这次改动会不会让某个**判据**（哪些行算 preset / 是否已执行过）的含义变化？
2. 如果判据写错，**重启时**会发生什么？会不会无信号地改写用户数据？
3. 有没有一条断言能在判据写错时**变红**？如果没有，先补断言再改判据。

第 3 条是本项目用真实 P0 换来的纪律：`schema_patch.py` 的启动回填曾把用户派生
角色永久刷成 preset（跨用户数据泄漏），当时**没有任何测试能发现它**。

### 守门人自检守得住什么、守不住什么

| 守得住（机械可判，CI 红） | 守不住（没有机械判据，靠人） |
| --- | --- |
| CODEOWNERS 存在且每条规则语法合法 | **同时篡改代码与基准的自洽绕过**（改 `catalog.py` 再重新生成黄金副本，diff 两处都变、门禁全绿）—— 靠人工 review 看 diff 全貌 |
| 关键危险路径仍被显式（非通配）规则覆盖，删行就红 | **语义正确性**（判据写错但语法合法）—— 靠人工 review + 变异测试「证明它会红」的纪律 |
| `.github/workflows/*.yml` 全部可被 `yaml.safe_load` 解析 | **覆盖 ≠ 真的有人看**——分支保护是否强制 review 是 GitHub 侧设置，CI 只代表文件层面完好 |

本项目哲学：**不写清边界的断言是负债**。每个断言旁边都必须写清它守不住
什么，否则下一个人会把「门禁是绿的」当成「代码是对的」。

## 服务器部署

默认**单一镜像**：业务、语音链路、角色与记忆、邮件——全部平台级能力都在同一个容器里，
默认内置 SQLite，不需要任何外部数据库或额外的语音服务容器。

```bash
cd server
cp .env.example .env            # 至少改 ELDER_JWT_SECRET
docker compose up -d            # 对外 http://<host>:8064
```

> 语音链路是自研的（`server/app/voice/`），与本服务同进程、**同端口 8064**，
> 设备连 `ws(s)://<域名>/api/voice/v1/ws`，地址由服务端自动下发。
> **不再依赖任何第三方语音服务端**，镜像也不需要第二个端口。

⚠️ **`server/.env.example` 里全是本地示例值，一个都不能照抄进生产**。
下表是这个文件里**全部未被注释、真正生效**的 7 个值：

| 变量 | 示例值 | 生产怎么办 |
| --- | --- | --- |
| `ELDER_JWT_SECRET` | `please-change-this-secret-in-production` | **必须**换成随机长字符串，否则任何人可伪造 token |
| `ELDER_CORS` | `*` | 收窄到 `https://152.136.230.132`（IP 直连生产形态；域名形态则为 `https://voice.250886.xyz`） |
| `MYSQL_ROOT_PASSWORD` | `123456` | 仅 MySQL 变体用得到，改强口令 |
| `ELDER_MAIL_FAKE` | `0` | ⚠️ **必须留 `0`**。这是唯一一个「留着默认值就可能出事」的：设为 `1` 时 `send_mail()` 不碰网络，邮件全写进进程内发件箱——用户点「找回密码」永远收不到邮件。上表其余几项不改只是「不安全」，这一项不改是**功能直接坏掉** |
| `ELDER_TZ` | `Asia/Shanghai` | **建议别动**。服药提醒是时间强相关业务，服务端/设备端/展示层必须同一时区；改了整体影响提醒的时间计算 |
| `ELDER_HTTP_PORT` | `8064` | 宿主机发布端口。改它要同步改反代的 `proxy_pass` |
| `ELDER_DB_WAIT_SEC` | `180` | 只在 MySQL 形态生效（等数据库就绪），SQLite 用不到 |

> `ELDER_ORDER_TTL_HOURS` 曾在这个文件里，是会员制残留（人工核销订单有效期），
> 已随会员制取消（commit `e264200`）连同 example 里的示例一并清掉。
> **没有任何代码读它**，旧 `.env` 里还留着的话直接删掉，无副作用。

⚠️ **`.env.example` 里没有、也不该有管理员口令示例**。服务端**已经不再有硬编码弱口令**
（`admin/admin123` 于 commit `bb5ca98` 移除），所以「照着示例填 `admin123`」这条路
根本不存在——不设 `ELDER_SEED_ADMIN_PASSWORD` 时种子口令**随机化**，日志打一次
`ELDER-INITIAL-PASSWORD=<pwd>`，你必须从日志里捞出来登录，否则登不进去。
**已经在跑的生产库不受种子改动影响**，存量口令要么登录后走 `/api/auth/password` 改，
要么设 `ELDER_RESET_ADMIN_PASSWORD` 重启一次（日志出现 `ELDER-ADMIN-PASSWORD-ROTATED`
后**立刻删掉这个变量**，否则每次重启都会改写）。

⚠️ **隧道形态必须显式配对外地址**：镜像的 CMD 没加 `--proxy-headers`，
`X-Forwarded-Proto` 不被采纳，uvicorn 看到的 scheme 恒为 http ⇒ 设备拿到 `ws://域名`
去打 443，表现为**能心跳但不能说话**（最难归因的一类故障）。生产形态是
iStoreOS（OpenWrt 软路由）+ Docker 对外 `8064`（容器内 `8080`）+ Cloudflare Tunnel，
`.env` / compose 里必须给：

```bash
ELDER_DEPLOY_MODE=tunnel
ELDER_PUBLIC_BASE_URL=https://152.136.230.132
# WS 想用单独域名再加：
# ELDER_WS_PUBLIC_BASE_URL=wss://voice-ws.250886.xyz
```

旧变量 `ELDER_VOICE_PUBLIC_BASE` 仍向后兼容（语义逐字未变），新变量在任何模式下
都校验 scheme。⚠️ **不要配 uvicorn `--forwarded-allow-ips=*`**：那会取
`X-Forwarded-For` 最左段（可伪造），登录限速器形同虚设。真实客户端 IP 默认只认
`request.client.host`，可信反代用 `ELDER_TRUSTED_PROXY_IPS` 显式配置
（cloudflared 在宿主机时，容器看到的 TCP 对端是 Docker 网桥网关，不是 `127.0.0.1`）。

只有在用户量上来之后才需要接 MySQL（**仅换数据库，不拆分服务**），
详见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

```bash
# MySQL 变体是叠加文件，必须和主文件一起加载
docker compose -f docker-compose.yml -f docker-compose.mysql.yml up -d
```

nginx 反代片段（IP 直连形态：**不带 SNI 的请求落到 default_server，必须配 IP 证书**；完整双证书配置见 DEPLOY.md §4.3）：

```nginx
server {
    listen 443 ssl default_server;
    server_name 152.136.230.132;
    # IP 直连必须用 SAN 含该 IP 的证书（Let's Encrypt 已支持 IP 证书）
    ssl_certificate     /etc/letsencrypt/live/152.136.230.132/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/152.136.230.132/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8064;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;      # 管理后台长轮询
    }
}
```

### 本地语音服务（可选 compose profile）

`server/docker-compose.local.yml` 提供两个**可选**旁路服务，只含 CPU 跑得动的部分：

| 服务 | 内容 | 端口（只绑 `127.0.0.1`） |
| --- | --- | --- |
| `local-asr` | FunASR / SenseVoice 中文识别，纯 CPU，约 254MB 模型 | `10095` |
| `local-vad` | Silero VAD 端点检测，2MB 级 ONNX | `8081`（容器内 `8080`） |

```bash
cd server
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

⚠️ **刻意不含本地中文 TTS**，这是从部署形态倒推的结论而不是偷懒：生产是 iStoreOS
软路由、**无独立显卡**，而 CosyVoice 2 推理需要 8GB 显存起步，塞进去的结果是
「容器能起、合成一句话要几十秒」，对老人场景比用云端 TTS 更糟。

这两个服务**没有任何鉴权**，所以端口刻意只绑 `127.0.0.1`，主服务在同一 compose
网络内用 `http://local-asr:10095` 访问。⚠️ 起来之后**仍需在管理后台「AI 接入」里
手工添加两行**（项目刻意不自动写库）：`kind=asr` 指向 `http://local-asr:10095/v1`，
`kind=vad` 指向 `http://local-vad:8080`。首次启动要下 254MB 模型，冷启动 30~60 秒
是正常的。

## 语音能力：TTS 三家预置 + 服务就绪度

### 三家 TTS 预置，服务端自动分发

音色目录的**唯一真源**是 `server/app/voice/catalog.py`，管理后台的音色下拉框通过
`GET /api/voice-catalog` 拉取，不在前端另抄一份。三家厂商预置：

| key | api_style | endpoint | model | 要 Key | 预置音色数 |
| --- | --- | --- | --- | --- | --- |
| `edge` | `edge` | （空，走内置桥） | — | 否 | 11 |
| `volcengine` | `volcengine` | `https://openspeech.bytedance.com` | `volc.tts_async` | 是（AK:SK，英文冒号分隔） | 5 |
| `aliyun` | `openai_compatible` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-tts` | 是（`sk-` 开头） | 4 |

`VOICE_CATALOG` 的 11 条 Edge 音色按**老人友好度**排序（温和在前、活泼在后），
出厂默认音色 `DEFAULT_PRESET_TTS_VOICE = "zh-CN-XiaoxiaoNeural"`（这个值不能改，
管理后台的「恢复出厂默认」依赖它）。

**服务端自动切换**在 `server/app/voice/asr_tts.py::_tts_dispatch_style()`：
判据顺序是 **provider 优先于 api_style**（预填的 api_style 会截胡，是个真实修过的
bug），都不匹配则回落 `openai_compatible`。`tts_stream()` 内部做了同样分发 ——
否则 Edge 会出现"非流式能出声、流式不出声"。

三家各自的坑：

- **Edge TTS 不是 HTTP API**，是微软 Edge 浏览器的非公开 WebSocket 协议，只出 mp3。
  桥在 `server/app/voice/tts_edge.py`，用 PyAV 解码重采样。
  ⚠️ **实测限制：连续调用 3 次，第 3 次会 ConnectionTimeoutError**（上游限流），
  非代码问题。且无商业授权，只适合当零成本兜底。
- **火山引擎是私有协议**：AK/SK + 四级派生 HMAC-SHA256 签名，自定义报文，
  实现在 `server/app/voice/tts_volc.py`。⚠️ **签名无法本地实测**（需真实 AK/SK），
  首次使用必须用后台的「真实测试」按钮验证。
- **阿里云百炼 `qwen-tts`** 是真 OpenAI 兼容，endpoint 到 `compatible-mode/v1` 为止一级。

**TTS 音频契约（硬约束）**：客户端只吃 24kHz / mono / S16LE **裸 PCM**
（`asr_tts.TTS_RESPONSE_FORMAT="pcm"`、`TTS_SAMPLE_RATE=24000`），固件按服务端
声明的采样率逐帧 Opus 编码。

### 非 TTS 厂商预置：硅基流动的向量化与重排是免费的

记忆检索要两种模型：**向量化**（`kind=embedding`）与**重排**（`kind=rerank`）。
这不是可选项 —— 没有向量模型时记忆检索整体退化为「按关键词硬匹配」。

`server/app/provider_presets.py` 是这张预置表的**唯一真源**，通过
`GET /api/providers/catalog` 的 `provider_presets` 下发给前端。当前 5 条：
硅基流动 / DeepSeek / 智谱 / 阿里云百炼 / OpenAI 官方。

**首推硅基流动 SiliconFlow**（`api_style=openai_compatible`，
endpoint `https://api.siliconflow.cn/v1`），因为它是唯一一家把记忆检索要用的两个
模型都做成永久免费的：

| 能力（kind） | 模型 | 费用 |
| --- | --- | --- |
| `embedding` | `BAAI/bge-m3` | 永久免费 |
| `rerank` | `BAAI/bge-reranker-v2-m3` | 永久免费 |
| `llm_main` | `Qwen/Qwen2.5-7B-Instruct` | 永久免费（9B 及以下） |
| `llm_light` | `Qwen/Qwen2.5-1.5B-Instruct` | 永久免费 |

**要配成两行，不是一行两个模型**：能力选 `embedding` 填 bge-m3 保存，再新增一行
能力选 `rerank` 填 bge-reranker-v2-m3。两行可以同厂商同 key。

⚠️ 重排端点是 `/v1/rerank`（**不是** OpenAI 那套 `/rerankings`），
`server/app/llm_gateway.py::rerank()` 已按此实现，**不需要改调用层**；
`_extract_scores()` 同时认 `relevance_score` / `score`，两家格式都能解。
换 9B 以上的模型就开始计费。

⚠️ 这张表**只管 OpenAI 兼容这一族**。TTS 厂商在 `server/app/voice/catalog.py`
的 `VENDOR_CATALOG`（字段集不同：要音色、要 AK:SK 两段密钥、有私有协议）。
**两张表不要合并** —— 合并后必然出现一半条目没有 `voices` 的空壳。

### 服务就绪度面板：「零配置即可服务」不成立

管理后台 AI 接入页顶部有「服务就绪度」卡片（`GET /api/providers/readiness`），
返回四层事实：`configured` / `resolved`（脱敏）/ `hint` / `critical`。

- **关键项**（`critical`）：`asr` / `tts` / `chat` —— 缺任一则无法提供完整服务。
  `vad` **不算**关键项：断句已由设备端 esp-sr 承担。
- ⚠️ **全新空库下 asr/tts/vad/chat 四项全部未就绪**，`resolve_provider` 返 503。
  也就是说**「零配置即可提供服务」这个假设不成立**——面板的作用正是**如实反映未就绪**，
  不是粉饰。配完 AI 平台行之后才会转绿。

`POST /api/providers/test-voice` 会**真打一次** TTS 合成并回 `audio_bytes` /
`duration_ms`；旧的 `POST /api/providers/test` **不覆盖** asr/tts，会掉进兜底逻辑
返回"请先创建角色"，别拿它判断语音配置对不对。

## 双界面与配网（不需要 APP）

| 入口 | 路径 | 源码 |
| --- | --- | --- |
| 管理后台 | `/` | `server/app/static/index.html`，按 `is_admin` 过滤 |
| 用户端 | `/user` | `server/app/static/user.html`，8 个 tab：我的设备 / 提醒 / 记忆管理 / 形象 / 人格预设 / 用量统计 / 交互统计 / 帮助与工单 |

绑定设备的后端端点早就存在且只挂普通用户鉴权（`POST /api/devices/bind`），
缺的只是 `user.html` 里的调用——所以**用户端不推去 APP，浏览器就能绑**。

**WiFi 配网固件已原生支持浏览器配网，不需要 APP**：ESP32 自开热点
（前缀 `XiaoNuan-` + MAC 后两位，**不是上游项目的 `Xiaozhi-`**），屏显热点名与
`http://192.168.4.1`，DNS 强制门户，HTML 页在
`firmware/xiaozhi/managed_components/78__esp-wifi-connect/assets/wifi_configuration.html`。
APP 只是另一层封装。

## 语音链路要点

- 语音链路自研（`server/app/voice/`，xiaozhi 公开协议服务端），
  **不得依赖任何第三方服务器**：固件 `CONFIG_OTA_URL` 默认已指向自己的 `/api/voice/ota`
  （默认值在 `firmware/xiaozhi/main/Kconfig.projbuild`；`sdkconfig` 是构建产物、被 gitignore）。
  OTA 只给已绑定设备签发 token，未绑定只返激活码。
- Opus 编解码 = **ctypes 直连系统 libopus**（零新增 Python 依赖；
  Docker / CI 都 apt 装 `libopus0`）。
- **提醒工具调用走服务端 function calling，不走设备 MCP 通道**：固件的 "MCP" 是
  设备本地 JSON-RPC 工具（音量/电量/摄像头），提醒数据在服务端 `reminders` 表，
  设备本地没有。工具在 `server/app/voice/tools.py`。
  **歧义时间必须反问**（「三点」不猜上下午、「每天」缺时间）——静默建错时间的提醒
  对老人最糟。
- **流式 TTS**：`tts_stream()` 增量吐 PCM；厂商不发流式时降级为非流式、不假装。
  下行时序 `stt → tts start → sentence_start×N → tts stop` 不能改（设备靠它回落 idle）。
- **老人语速 / 停顿是默认值即改善，不需运维调参**：
  `ELDER_VOICE_SILENCE_MS` 默认 `2000`（句末停顿）、`ELDER_VOICE_SILENCE_MAX_MS`
  默认 `6000`（句中最长停顿自适应 = 最长句中停顿 ×1.5 + 500ms）。

### 登录限速

`server/app/ratelimit.py`：默认 5 秒冷却 / 每分钟 6 次 / 每小时 20 次，
**只计失败次数**，同时按 IP 和按用户名两道闸门。关键设计：**口令正确的请求不过闸门**
——先验口令，对了直接发证不看配额。所以「被锁死」只会作用于不知道口令的人，
不会变成 DoS 武器。计数在进程内存里，不落库。

## 手机端

```bash
cd app
pnpm install
pnpm build:h5          # 产物 app/dist/build/h5
pnpm build:mp-weixin   # 产物 app/dist/build/mp-weixin，用微信开发者工具上传
```

- 后端地址**代码级写死** `https://152.136.230.132`（`app/src/http/elder.ts` 的 `BASE_URL` 常量）
- 页面入口：`src/pages/elder/`（绑定设备、提醒列表与编辑、统计、形象选择）

### Android APK（CI 一次出包，不依赖 DCloud）

`android-build.yml` 的流程：构建 H5（`VITE_APP_PUBLIC_BASE=./`，资源走相对路径）
→ 拷进 `android/app/src/main/assets/web` → Gradle 打 Release 包 → 上传 APK。

页面由 `android/app/src/main/java/com/elder/companion/MainActivity.kt` 里的 WebView 承载，
用 `androidx.webkit.WebViewAssetLoader` 把 assets 映射成虚拟域 `https://elder.local/assets/web/index.html`
——这样页面有正常 origin，`localStorage` 与跨域请求行为和线上 H5 完全一致。

之所以不走 uni-app 云打包：uni-app 的 App 端原生运行时是 DCloud 闭源 AAR，只有云打包能出 APK，
而且 HBuilderX 3.9+ 起强制要求登录 + 绑定手机号 + 实名认证，免费账号每月仅 5 次配额。
本项目手机端不依赖 App 原生能力（唯一用到 `plus.runtime.restart()` 的地方已有 H5 降级分支），
所以 WebView 壳即可完整运行，构建也完全在 GitHub Actions 里完成。

签名（Secret，可选；不配会自动回落到 debug 签名，依然可安装）：

| Secret | 用途 |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | 证书文件 base64（当前这份实际是 PKCS12，Gradle 会按文件 magic 自动识别） |
| `ANDROID_KEY_ALIAS` | 别名，当前 `__uni__g9c80343` |
| `ANDROID_KEY_PASSWORD` / `ANDROID_STORE_PASSWORD` | 密钥与密钥库口令 |

跑一次：**Actions → android-build → Run workflow**，
产物在 Artifacts：`elder-app-android-apk`。

> iOS：没有 Apple 开发者证书（p12 + mobileprovision 缺一不可），暂不支持。

> `app-pack.yml`（uni-app 云打包）**已废弃**：DCloud 账号登录失败且强制实名认证，
> 现已改走 `android-build.yml` 的 H5 + WebView 壳方案，全程在 GitHub runner 内出 APK。
> 两个 Secret，且该账号必须已在 DCloud 注册并实名。

## 固件

**硬件基线**：立创·实战派 ESP32-S3，板型符号 `CONFIG_BOARD_TYPE_LICHUANG_DEV_S3`
（板目录 `lichuang-dev`），ESP-IDF v5.5.1。板上关键器件：ES8311（音频 codec）、
ES7210（ADC）、FT5x06（触摸）、**GC0308（摄像头，DVP）**。

`firmware/components/elder` 是本项目新增的组件（编译前由
`tools/merge_to_xiaozhi.py` 合入 xiaozhi 源码树），已实现：RGB 指示灯与情绪同步、
长按 3s + 30s 内短按的服药确认状态机、离线提醒（DS3231 + NVS）、
13 位激活码（**1 位字母 + 12 位数字，第 12 位为 Luhn 校验位**，与后端算法一致；
屏显按 `A-3829-4471-0056` 四段分组，12 位连写在 320×240 上抄错率太高）。

显示走**标准 LVGL 路径**（`lv_init()` + `lv_display_get_default()` +
`SpiLcdDisplay`），不是 `esp_emote_gfx` 自绘——开 emote 路径时它根本不创建
LVGL display，真机表现为全黑屏 + `font context is NULL`。

**对话界面 70/30 布局**（2026-10-19 用户拍板）：屏幕上下分割——字幕区占屏幕
**高度 70%**（全宽、紧贴顶部状态栏，文字在区内居中），**右下角 30%×30%** 是
图片区（表情图标 `emoji_box_` 与服务端预览图 `preview_image_` 共用同一块区域，
两者互斥显示），顶部状态栏（WiFi/静音/电量）原样保留。低电量弹窗移到左下角
65% 宽，避免压图。图片缩放用 LVGL 原生 `LV_IMAGE_ALIGN_CONTAIN`（等比缩放进区），
不再手算 scale。

⚠️ 该布局是**直接改上游 `main/display/lcd_display.cc` 默认风格分支**
（`CONFIG_USE_WECHAT_MESSAGE_STYLE` 气泡分支未启用、未动）。上游
`sync_xiaozhi.py --force` 会冲掉它 ⇒ 文件内有 `elder-layout-70-30` 标记，
`tools/merge_to_xiaozhi.py --check`（CI 每次构建都跑）会校验该标记，
缺失即构建失败。重贴步骤：对照 git 历史里 `lcd_display.cc` 的默认分支
（`#else` 与 `#endif` 之间）恢复 6 处几何改动——`emoji_box_`/`preview_image_`
改 `LV_ALIGN_BOTTOM_RIGHT` + `lv_pct(30)` + CONTAIN、`bottom_bar_` 改
`lv_obj_align_to(top_bar_, LV_ALIGN_OUT_BOTTOM_LEFT)` + 高度 70% 减状态栏、
`low_battery_popup_` 改左下角 65%。

**摄像头驱动已真正编进固件**，初始化改为「探到硬件才起来」：先用 SCCB 探地址，
探不到就打一条 WARNING 跳过，既不占 DVP 的 11 个 IO 也不刷 ERROR；接上排线重启
即自动启用。

### 唤醒词与离线命令词

**唤醒词是「你好，小暖」**（拼音 `ni hao xiao nuan`），在设备端**离线识别**，
不联网也能响应。识别引擎是乐鑫 esp-sr 的 **MultiNet** 中文命令词模型
（不是上游默认的 WakeNet 专用唤醒模型），识别阈值 20%（越小越灵敏）。
配置的真源是 `tools/merge_to_xiaozhi.py` 的 `SDKCONFIG_OVERLAY`（由它生成
`sdkconfig.defaults` 的 overlay 段，手动段只留注释——**两处都写以后定义覆盖先定义，
手动段即成死配置**），改词只需改 merge 脚本后重跑 merge。

📌 2026-10-18 用户拍板把唤醒词从「你好小智」改为「你好，小暖」（带逗号）。
⚠️ **拼音↔汉字必须同步改**：MultiNet 的注册键是拼音串，`DISPLAY` 只影响屏显，
改了拼音忘改汉字会出现「喊 A 显示 B」的割裂。另注意 `main/boards/` 下 4 块
演示板的 ASR 按钮直接引用 `CONFIG_CUSTOM_WAKE_WORD_DISPLAY` 宏，自动跟随。

🚨 **改了配置但固件没变？先查 `sdkconfig` 残留**
2026-10-19 真实事故：唤醒词明明改对了，编译烧的固件**仍然只认「你好小智」**。
原因是 ESP-IDF 里 `sdkconfig`（本地生成、不入库）的优先级**高于**
`sdkconfig.defaults`，后者只在**首次生成**时参与，而 kconfig 对已存在的选项
**默认不覆盖**——全程**不报错、不告警**，症状与「模型没烧进分区」完全一样。

⇒ 改完配置按这个顺序走，别跳步：

```bash
# 1) 改 firmware/xiaozhi/sdkconfig.defaults（或改 merge 脚本后重跑 merge）
# 2) 删 sdkconfig（不入库，会按 defaults 重新生成）——★ 最容易漏的一步
rm -f firmware/xiaozhi/sdkconfig
# 3) 删 build 并**先 set-target**（不 set-target 会按 esp32 编译，
#    esp_video 等依赖的 rule 会把摄像头组件判成无用，见决策记录 D29）
rm -rf firmware/xiaozhi/build
python tools/idfwin.py set-target esp32s3
# 4) 编译前的必过自检（把上面这些坑全变成会红的断言）
python tools/prebuild_check.py
# 5) 编译
python tools/idfwin.py build
```

CI 里第 4 步已自动执行（`firmware-build.yml`），本地不会漏。
自检项与判据见 `tools/prebuild_check.py` 模块 docstring 与决策记录 D28。

除唤醒词外还有 **6 条离线命令词，无需先说唤醒词，直接说即生效**：

| 命令词 | 拼音（MultiNet 注册用） | 作用 |
| --- | --- | --- |
| 新建早上吃药提醒 | `xin jian zao shang chi yao ti xing` | 建每天早上服药提醒 |
| 新建中午吃药提醒 | `xin jian zhong wu chi yao ti xing` | 建每天中午服药提醒 |
| 新建晚上吃药提醒 | `xin jian wan shang chi yao ti xing` | 建每天晚上服药提醒 |
| 新建吃药提醒 | `xin jian chi yao ti xing` | 按默认时段建提醒 |
| 吃过了 | `chi guo le` | 确认本次服药 |
| 我吃过了 | `wo chi guo le` | 确认本次服药 |

词表本体在 `firmware/components/elder/include/elder_cmd_words.h`，
**短语与拼音逐条对齐是铁律**：改了短语忘了改拼音，老人说那句话就没反应
（现场排查时，看启动日志打印的拼音比看汉字有用）。断网时「吃过了」是老人
唯一能自助完成的确认动作，与硬件按钮确认（长按 3s + 30s 内短按）互为备份。

⚠️ `idf.py build` **不读**板级 `config.json`（那是上游 `scripts/release.py` 做
多板发布时才读的）。驱动必须经 `tools/merge_to_xiaozhi.py` 里的
`SDKCONFIG_OVERLAY` 真源启用——写进 `config.json` 属于死配置，CI 产物里根本没编进来。

### ⚠️ 打包固件前必须 `rm -rf firmware/xiaozhi/build`

`firmware/xiaozhi/build/config/sdkconfig.h` 是**未入库的本地构建产物**
（`.gitignore` 里 `firmware/xiaozhi/build/` 被忽略），里面的 `CONFIG_OTA_URL`
可能仍是上游的 `api.tenclass.net`。不清理就打包，打出来的设备会去连别人的服务器。

**症状**：设备**能启动、能联网、能心跳，但语音链路永远不通** —— 因为
WebSocket 地址与 token 都由第三方在 OTA 响应里下发，我们没有任何侧信道能通知
设备改地址。而第三方一旦限流或切断，全部设备同时失声。

```bash
rm -rf firmware/xiaozhi/build
```

正确的默认值在 `firmware/xiaozhi/main/Kconfig.projbuild`：
`CONFIG_OTA_URL` 默认 `https://152.136.230.132/api/voice/ota`。

CI 不受影响：`firmware-build.yml` 走 `idf.py set-target` 重新生成 `sdkconfig`，
并且**每次构建都打印生效的 `CONFIG_OTA_URL`**，偏离本项目地址时告警。
固件运行时还可被 NVS（`settings("wifi")` 的 `ota_url`）覆盖，不必重新烧录。

固件烧录步骤见 [`docs/烧录指南.md`](docs/烧录指南.md)。

## 后续开发计划

以下三项已排定方向、**均未开工**，写在这里是为了让贡献者知道演进路线与边界。

### 1. 固件迁移到 ESP32-P4

当前硬件基线是立创·实战派 ESP32-S3（`CONFIG_BOARD_TYPE_LICHUANG_DEV_S3`）。
P4 的算力与 PSRAM 更适合本地形象渲染（EAF 序列帧）与更大的 MultiNet 命令词表。
上游源码树已带 `sdkconfig.defaults.esp32p4`，唤醒词引擎依赖
（`USE_CUSTOM_WAKE_WORD`）也已覆盖 P4 目标，迁移工作量集中在**板级驱动**
（音频 codec / 触摸 / 摄像头）与分区表，语音链路本身不用动。

### 2. 接入 Home Assistant（MCP 能力）

计划给服务端增加 **MCP（Model Context Protocol）能力**，把设备与提醒能力
以 MCP 工具形式暴露给 Home Assistant，让「提醒老人吃药」这类动作可以编进
HA 的自动化（门磁触发、夜间场景联动等）。
⚠️ 边界：提醒数据的单一真源仍是服务端 `reminders` 表，MCP 只是新的暴露面，
**不引入第二份数据**；设备的本地 MCP（音量/电量/摄像头）保持现状。

### 3. 独立药盒硬件（ESP + 实时时钟）

解决「提醒响了但药盒不在手边、事后无法证明吃没吃」的问题：

- **硬件**：药盒内置 ESP 模块 + **实时时钟芯片（RTC）**，断网也走得准；
- **日程下发**：药盒 ESP 通过**局域网 WiFi** 从主机下载服药日程
  （时段 → 格口映射），不依赖公网；
- **指示灯**：到达服药时段，对应格口亮起指示灯，老人不用记「哪个盒」；
- **开盒通讯**：打开药盒后 ESP 模块立即与主机通讯，记录取药时间；
- **语音确认**：关闭药盒后，**主机在 5 分钟内语音向用户确认本次服药是否
  完成**（复用现有语音链路与确认状态机；超时未确认按未服药处理）。

## 参考与致谢

### 源代码与协议参考

| 来源 | 用途 | 说明 |
| --- | --- | --- |
| [xinnan-tech/xiaozhi-esp32](https://github.com/xinnan-tech/xiaozhi-esp32) | 固件源码树与设备侧协议 | `firmware/xiaozhi/` 即在其源码树上合入本项目 `components/elder` 组件（`tools/merge_to_xiaozhi.py`） |
| [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) | 协议与固件设计参考 | **不部署、不调用**（早期跑过它的容器，已删除）；本项目语音链路为自研实现 |
| [78/esp-wifi-connect](https://github.com/78/esp-wifi-connect) | 浏览器配网 | `managed_components/78__esp-wifi-connect`，配网热点 `http://192.168.4.1` |
| [espressif/esp-sr](https://github.com/espressif/esp-sr) | 离线命令词识别 | MultiNet 中文模型：唤醒词「你好，小暖」+ 6 条服药命令词 |
| [78/xiaozhi-assets-generator](https://github.com/78/xiaozhi-assets-generator) | 自定义资产生成 | 唤醒词 / 字体 / 表情的在线编辑与打包 |

### 致谢

- **WorkBuddy** —— 本项目的主力开发协作者：服务端、固件、APP、CI 与全套冒烟测试均在它上面完成。
- **Edge TTS** —— 微软 Edge 浏览器的语音合成协议，本项目的零成本 TTS 兜底（`server/app/voice/tts_edge.py`）；⚠️ 非公开协议且无商业授权，正式部署建议另配商业 TTS。
- **乐鑫 ESP-IDF / esp-sr / esp-ml307** —— 固件框架与离线语音识别。
- **LVGL** —— 设备端显示与形象渲染。
- **FunASR / SenseVoice 与 Silero VAD** —— 可选的本地 ASR / VAD 旁路服务。
- **PyAV** —— Edge TTS 的 mp3 解码与重采样。
