"""冒烟脚本默认端口的**唯一真源**（single source of truth）。

## 为什么要有这个文件

早先每个 smoke 脚本各自写死一个默认端口，结果大面积撞车::

    smoke_home / smoke_roles / smoke_memory   都是 8097
    smoke_models / smoke_mail                 都是 8096

这些脚本都会真的起一个 HTTP 子进程，所以**并行跑必然端口冲突**。
症状极具误导性，两种都出现过：

1. 显性失败 —— 后起的脚本直接 ``Address already in use``，服务起不来；
2. **隐性假失败** —— 上一次残留的进程还占着端口，新脚本连上去「跑通了」，
   断言数诡异地从 87 条掉到 3 条，看起来像代码回归，实际只是连错了服务。

第 2 种最坑：它会让人去查根本不存在的代码 bug。本文件把端口收敛成一张表，
让「并行跑」从一个陷阱变成一件确定的事。

## 约定

* **每个会起真实 HTTP 子进程的冒烟脚本，在这里独占一个端口**；
* 新增冒烟脚本必须先在这里登记端口，否则不允许写死端口号；
* 端口段选在 ``8100-8106``（业务冒烟）与 ``8110-8120``（QA / 运营后台 /
  语音链路 / 用量）：既避开历史上被撞过的 ``8096-8099``（可能还有
  残留进程占着），也避开 QA 脚本已占用的 ``8123/8127/8155/8177/8188``；
* 命令行仍然可以覆盖默认值（位置参数或 ``--port N``），
  CI 就是靠这个显式指定端口的。

## 用法

脚本内::

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from smoke_ports import resolve_port
    PORT = resolve_port("smoke_roles")

自检（确认所有默认端口都没被占用）::

    python tools/smoke_ports.py --check

================================ 本模块解决了什么、没解决什么 =================================
这一节是**资产**。加新脚本 / 改端口分配前请先读，否则会像本轮architect 那样
被「已经改过了」的错觉带偏。

✅ **已消除**：不同脚本之间的**默认端口重复**
    早先 smoke_home/smoke_roles 都是 8097、smoke_models/smoke_mail 都是 8096。
    收敛成一张表 + ``_assert_unique()`` 在 import 期自检，表内不可能重复。

❌ **未消除（务必知道）**：**同一脚本被两个 agent 并发跑**
    ``resolve_port()`` 是**静态查表**，它保证「表内不重复」，
    **不保证「此刻这个端口没被别人占着」**。11 个 agent 并行是常态，
    同一脚本被跑两次照样撞。

    → 正因为如此才需要 ``guard_port()`` 三段式守卫（见下）。
    **不要因为「端口都走smoke_ports.py 了」就认为并发跑已经安全。**

⚠️ 由此引出本轮最重要的一条教训（团队血泪，换来的）
------------------------------------------------------------------
``is_port_free()`` 早就写好了，只是**没人调用**；``resolve_port()`` 早就改好了，
但它给人「并发安全」的**错觉**。

> **一个消除了某类冲突的自动化改进，如果不说清它没消除哪一类，比没有改进更危险**
> —— 因为它让人省掉了真正该做的那一步。

推论：**每次修完这类"基础设施"问题，都要在注释里写清边界**，
否则下一个人会把「部分解决」当成「已解决」，等问题换个马甲再来一次。

⚠️ 退出码分类（区分「环境失败」与「真回归」）留待 **P2**，本轮刻意不做
------------------------------------------------------------------
理由：**先消除假绿，再谈分类。** 端口预检上线后，「环境失败」不再以
假绿形式出现 —— 被占就直接 return 1 并给出可执行提示。
那时 CI 红了就一定是真问题，再区分两种退出码才有意义。
现在做等于**在还没消除的失败模式之上再加一层装饰**，反而更让人误判。
"""
from __future__ import annotations

import socket
import sys
import time
from typing import Iterable

# 脚本名（不含 .py）-> 默认端口。**唯一真源**，改这里等于改全部脚本 + CI + 文档。
DEFAULT_PORTS: dict[str, int] = {
    "smoke_server": 8100,      # 业务闭环：注册/登录/设备/激活码/提醒
    "smoke_roles": 8101,      # 角色域 + P0 越权断言
    "smoke_home": 8102,       # 首页聚合 / 设备状态 / 导出
    "smoke_memory": 8103,     # 记忆抽取 / 检索 / 去重
    "smoke_models": 8104,     # 模型接入点 + 本地假厂商
    "smoke_mail": 8105,       # 邮件 / SMTP / 工单
    "smoke_quota": 8106,      # 陪聊计量 + 「授权设备无限量」不变量（原 smoke_membership）
    # ---- QA 独立验证脚本（QA 轮次产出）----
    # 也登记进来而不是各写一份：QA 曾手动给 smoke_roles 指定 8155，
    # 而 qa3_evidence.py 的默认值恰好也是 8155 —— 撞车症状是
    #「TimeoutError + 零断言失败」，极易被误判成代码回归。
    # 登记之后这类撞车在**调用前**就不可能发生（resolve_port 会分到不同端口）。
    "qa3_independent": 8110,   # P0 + 角色不变式 + guardrails + 设备下发
    "qa3_evidence": 8111,      # 运行时取证（哈希 / 红线产物 / 设备下发）
    "qa3_r2_e2e": 8112,        # Bug1 端到端：改库 -> 重启 -> 真实 HTTP
    "qa3_r2_restart": 8113,    # 重启不变式：启动流程不得改写用户角色
    "qa3_device_binding": 8114,  # 设备绑定回填：解绑持久 + 存量回填
    # ---- 假绿诊断类（第三批）----
    # 这三个此前硬写 8123（且三者相同 → 并行必撞），已统一登记。
    "qa3_false_green": 8115,       # 复现「连到别人的服务却全绿」
    "qa4_dirty_all_readpoints": 8116,  # 全读点 × 脏形态 + role_diff 兼容
    "qa_probe_memory": 8117,       # 记忆域探针
    # ---- 业务域（本仓自有CI 脚本）----
    # 人格预设运营后台：97 条断言里有多条要**重启服务**（T14/T27），
    # 且它会直连 SQLite 改库注入脏数据（T08），跑得比其他脚本久，
    # 所以排在 QA 段之后独占 8118。
    "smoke_admin_presets": 8118,   # 管理端 · 平台人格预设运营后台
    "smoke_voice": 8119,           # 自研语音链路：WS 协议 + Opus + 假上游 + 用量埋点
    "smoke_usage": 8120,           # token 用量：网关提取 + 双端接口对账
    "smoke_auth": 8121,            # 注册/鉴权：公开开关 + 初始管理员引导（问题 A/B 的回归网）
    "smoke_deploy": 8122,          # 部署模式：tunnel/direct/auto 三模式地址推导 + 启动自检
    "smoke_local_reminders": 8123, # 断网自建提醒上报：鉴权 + 幂等 + sync 可见性
    "smoke_voice_catalog": 8124,   # 音色目录 + 服务就绪度：预置清单单一真源 + 零配置如实反映
    "smoke_proxy_headers": 8125,   # 可信反代：ProxyHeaders 算法 + 307 Location 的 scheme 推导
    # 硬件型号截断护栏：守 device_api.sync 的 hw[:64]（SQLite 不强制列宽，
    # 删掉那行切片时**没有任何别的测试会红**，所以必须单独一条）。
    # 它内部会起两次服务（原始 + 变异自检），故端口与 8125 相邻但独立。
    "smoke_hw_model_truncation": 8126,
    # OTA 端点「新建激活码」配额：证明超配额 429、且**复用路径不被误伤**
    # （设备每 15s 轮询，若复用也计数，正常设备会被自己的轮询打满）。
    "smoke_ota_ratelimit": 8127,
    # 激活码 10 分钟 TTL / 服务端锁定 / 已绑定不发码 / 管理员解绑
    "smoke_activation_ttl": 8128,
    # QA-B 第二层独立验证（不引用实现者的冒烟断言，独立直连 SQLite 复核）
    "qa_b_activation_gate": 8129,
    # pk 复用 → 已删除设备的旧 token 复活（P0）：必须起真实服务走真实 HTTP/WS
    # （缺陷依赖「删→建→旧 token」三跳的跨请求状态，进程内打桩测不出来）。
    "smoke_pk_reuse": 8130,
    # 求助邮箱解析：env → 管理端 SMTP → 空串（不许写死地址）。
    # 两个阶段共用同一个库文件，故不与其他脚本共享端口。
    "smoke_support_email": 8131,
    # MQTT+UDP 传输（F1）：broker 协议 + AES-128-CTR 音频面 + 端到端一轮。
    # 进程内还会额外占两个口（MQTT 18183 / UDP 18884，见 smoke_mqtt_udp.py
    # 顶部注释），与 8132 错开且不在上游默认段（1883/8884）上，免得撞到
    # 本机真装了的 mosquitto。
    "smoke_mqtt_udp": 8132,
    # 腾讯云语音（TTS + ASR）：TC3 签名对齐官方文档向量 + 假上游端到端验签。
    # 进程内另起一个假上游 HTTP 服务（本脚本用 8134，变异子进程用 +100），
    # 与 8132/8133 错开。
    "smoke_tencent_voice": 8134,
    # 声纹认人（F3）：提供者契约 + 说话人登记 CRUD + 端到端个性化注入。
    # 进程内还会额外占一个假声纹服务的口（默认 18099，见 smoke_voiceprint.py
    # 顶部注释），与 8133 错开；不在任何常见服务端口段上。
    "smoke_voiceprint": 8133,
    # 展示时区判定（跨时区 2026-10-21）：IP → 国家 → 时区 五层判定 + 提醒
    # 按规则自身时区展开。进程内另起一个假 IP 地理位置服务（默认 18100，
    # 见 smoke_geo.py 顶部注释），由 ELDER_GEO_API 指过去 —— 这样断言
    # **不依赖公网**，也不会因为真实服务抖动而假红。
    "smoke_geo": 8135,
    # 用户端音色下拉：按**生效厂商**给清单 + 失效音色回落默认。
    # 它会直连同一份库造 tts 配置行（含换厂商），故独占一个端口。
    "smoke_voice_options": 8136,
    # 提醒软删除 + 操作审计流水：守「删了还在响」与「删了还能看到历史」。
    # 它会直连同一个库文件数 PENDING 实例（问接口等于让被测方自证），
    # 故独占一个端口。
    "smoke_reminder_audit": 8137,
}


def _assert_unique() -> None:
    """导入时自检：默认端口不允许重复。

    这是本文件存在的意义所在 —— 与其等到并行跑时才发现撞车，
    不如在 import 阶段就把「有人抄了一个已有端口」直接炸出来。
    """
    seen: dict[int, str] = {}
    dupes: list[str] = []
    for name, port in DEFAULT_PORTS.items():
        if port in seen:
            dupes.append(f"{name} 与 {seen[port]} 都是 {port}")
        else:
            seen[port] = name
    if dupes:
        raise RuntimeError(f"smoke_ports: 默认端口重复 -> {'; '.join(dupes)}")


_assert_unique()


def resolve_port(script: str, argv: Iterable[str] | None = None) -> int:
    """解析该脚本本次要用的端口。

    优先级（**保持向后兼容**，老调用方式一个都没删）：

    1. ``--port 8100`` 或 ``--port=8100``；
    2. 裸位置参数 —— CI 用的就是这种（``python tools/smoke_server.py 8100``）；
    3. 本表登记的默认端口。

    Args:
        script: 脚本名，带不带 ``.py`` 都行。
        argv: 命令行参数列表，默认取 ``sys.argv[1:]``。

    Returns:
        要使用的端口号。

    Raises:
        KeyError: ``script`` 未在 :data:`DEFAULT_PORTS` 中登记 ——
            这是刻意的：新增冒烟脚本必须先来这里登记端口。
    """
    key = script[:-3] if script.endswith(".py") else script
    if key not in DEFAULT_PORTS:
        raise KeyError(
            f"smoke_ports: {script!r} 未登记默认端口 —— "
            f"新增冒烟脚本请先在 tools/smoke_ports.py 的 DEFAULT_PORTS 里登记，"
            f"否则并行跑必然撞车。已登记: {sorted(DEFAULT_PORTS)}"
        )

    args = list(sys.argv[1:] if argv is None else argv)

    # 1) --port N / --port=N
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
        if arg.startswith("--port=") and arg.split("=", 1)[1].isdigit():
            return int(arg.split("=", 1)[1])

    # 2) 裸位置参数（CI 的用法）
    for arg in args:
        if arg.isdigit():
            return int(arg)

    # 3) 默认端口
    return DEFAULT_PORTS[key]


def _set_bind_semantics(sock: socket.socket) -> None:
    """按平台设置 bind 语义。**Windows 与 POSIX 的选项是相反的**，必须分平台。

    Windows：设 ``SO_EXCLUSIVEADDRUSE``。
        不设它时 ``SO_REUSEADDR`` 会让 bind 对**已被占用**的端口也成功 ——
        那样就测不出冲突，守卫形同虚设。

    POSIX（Linux / macOS）：设 ``SO_REUSEADDR``。
        不设它时，端口上只要还有 **TIME_WAIT** 状态的 socket，bind 就直接
        ``EADDRINUSE``（Windows 的 TIME_WAIT 不挡 bind，所以这个问题**只在
        Linux/macOS 上发作**，本地 Windows 实跑永远复现不出来）。

        ⚠️ **为什么加它不会削弱守卫的核心能力**（本次修改的正当性来源）
        ------------------------------------------------------------------
        Linux 上 ``SO_REUSEADDR`` **只绕过 TIME_WAIT** —— 那些 socket 对应的
        监听进程**早已退出**，只是连接四元组还在冷却期。而要抢占一个**正在
        LISTEN** 的套接字，必须用 ``SO_REUSEPORT``，``SO_REUSEADDR`` 做不到。

        所以：「这个端口上有个别人的服务在跑」**仍然必然被测出来**，
        守卫的核心判据（识别别人正在跑）**完全不受影响**。

        ⚠️ 反过来，不设它就是**判据与服务端行为不一致**
        ----------------------------------------------------
        uvicorn / asyncio 建监听套接字时**自己就默认设 SO_REUSEADDR**，
        也就是说：**服务端能绑上的端口，我们的校验却判它「被占用」。**
        本项目已实测踩过：``smoke_roles.py`` 的 ``restart()`` terminate 自己
        的服务后，TIME_WAIT 让 ``is_port_free()`` 返回 False，守卫把
        **自己刚关掉的服务**当成别人的服务，重启直接失败
        （CI run 38838222327 实测；本地 Windows 因平台语义差异不复现）。
    """
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """检测端口当前是否空闲（能否 bind）。

    仅用于**诊断**：端口被占用说明有残留进程或有人正在并行跑同一个脚本，
    此时冒烟结果不可信（可能连到了别人的服务上）。

    ⚠️ 这里只回答「此刻能不能 bind」，**不区分**占用方是「别人的服务」还是
    「我自己刚 terminate 完、还挂在 TIME_WAIT 上的残留」。后者曾经导致
    restart() 误判自己 —— 靠 POSIX 分支的 ``SO_REUSEADDR`` 消除该误判，
    详见 :func:`_set_bind_semantics`。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        _set_bind_semantics(sock)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def check_all_ports(host: str = "127.0.0.1") -> list[tuple[str, int, bool]]:
    """逐个检测 :data:`DEFAULT_PORTS` 里的端口，返回 ``(脚本, 端口, 是否空闲)``。"""
    return [(n, p, is_port_free(p, host)) for n, p in sorted(DEFAULT_PORTS.items())]


# ================================================================ 三段式端口守卫
#
# 为什么不能只用 is_port_free（QA 第 4 轮实测的「假绿」）
# --------------------------------------------------------
# 端口被占时会发生这一串：
#   1. 新脚本的 uvicorn socket bind 失败（WinError 10048），**进程退出**；
#   2. 若 wait_ready() 只探 /api/health 而**不检查自己的进程存活**，
#      就会探到**别人的服务**的 200 —— 因为 /api/health 按设计
#      **永远返回 200**（main.py 注释明写"只表达进程活着"）；
#   3. 于是误判"我的已就绪"，全部断言打到别人的服务上，
#      用**自己那个从未 seed 过的 DB**。
#
# ⚠️ **一个流传过的错误描述，勿再照它改代码**
#   曾有版本写着「bind 失败但**进程苟活**（活着、只是没在监听），
#   所以 wait_ready 一定探到别人 → 假绿**必然**发生」。
#   **该描述已被实测推翻**：bind 失败后 uvicorn 约 14 秒退出（poll()=3，
#   stderr 含 10048）。QA 曾用「起两个进程 + 固定 sleep 6 秒就poll()」
#   来测，把「还没走到 bind 那一步」误读成了「苟活」——
#   **教训：测进程生命周期必须轮询到有结果，不能固定 sleep。**
#
#   正确的机制：进程退出 -> wait_ready 每轮先查 ``proc.poll()``
#   -> 已退出则立即 return False ->**根本不会去探别人的 health**。
#   也就是说：**假绿的前提是 wait_ready 不检查自己的进程存活**；
#   而 smoke_roles.py 的 wait_ready 已经检查了，**这条路已被堵死**。
#
# 那为什么还要预检？
#   因为第2 步那个前提**不能只靠 wait_ready 兜底**：
#   * wait_ready 的超时是 120 秒，占用者若是别人的常驻服务，
#     等120 秒才判失败，体验与日志都很差；
#   * 更重要的是**别把"结果不可信"这件事拖到断言阶段才发现** ——
#     预检让它在 boot 阶段就硬失败并给出可执行提示。
#   换句话说：**防线 #2（查进程存活）才是关键，#1 预检与 #3 stderr 校验
#   都是为了让失败更早、更可读。** 任何改动 wait_ready 的尝试**必须保住 #2**。
#
# ⚠️ 三段式守卫**仍然不是绝对保证**
# ---------------------------------
# 「事前检查」与「真正 bind」之间存在不可消除的竞态窗口：检查刚通过，
# 别人就把端口抢走了。本模块把这段距离压到最小，但**压到最小 ≠ 消除**。
#
# 但请注意**风险的边界**（QA 与 architect 实测裁决后的结论）：
#   这个窗口**不会导致假绿**。因为被抢 -> bind 失败 -> 进程退出 ->
#   ``wait_ready`` 里的 ``proc.poll()`` 检查（防线 #2）会立刻判定失败。
#   也就是说**真正的兜底是 #2，不是本模块的预检**。
#
# 那要不要再上 run_id（让服务自证身份）？architect 与 QA 都认为**不需要**：
#   * 它防的场景已被 #2 覆盖；
#   * 要改 main.py 的 health，会影响 Dockerfile HEALTHCHECK 与所有客户端；
#   * 随机 run_id 会让 /api/health 的响应不再稳定可比。
# 若将来 #2 被削弱（有人改 wait_ready 时漏掉 poll() 检查），**那时才需要 run_id**。
#
# **不要因为「加了预检」就认为并发跑已经安全** —— architect 这次栽的正是
# 这个坑：以为静态查表不重复就等于运行时没被占。
#
# 三段各做什么
# ------------
#   ① 事前检查 is_port_free()  —— 挡住 99% 的情况（残留进程 / 他人正在跑）
#   ② socket 占位               —— 检查通过后立刻 bind 占住，把窗口缩到最小
#   ③ stderr 校验               —— 占位没起作用时，uvicorn 会打
#                                 "address already in use"，据此**立即失败**
class PortBusy(RuntimeError):
    """端口已被占用，且不应继续跑（否则会拿到不可信的结果）。"""


def guard_port(port: int, host: str = "127.0.0.1") -> tuple[socket.socket | None, str]:
    """阶段①②：检查 + 占位。返回 ``(占位 socket 或 None, 可执行提示)``。

    占位 socket 必须由调用方在**起服务前** :func:`release_port` 释放 ——
    它的唯一作用是让"检查"到"bind"之间的距离尽可能短。

    用法::

        hold, hint = guard_port(PORT)
        if hold is None:
            print(hint)
            return 1
        try:
            proc = spawn(...)          # spawn 内部先 release_port(hold, PORT)
            ...
    """
    if not is_port_free(port, host):
        return None, (
            f"✗ 端口 {port} 已被占用。\n"
            f"  多 agent 环境无进程隔离，同一脚本并发跑必然撞：\n"
            f"  `resolve_port()` 是**静态查表**（表内不重复≠ 此刻没被占）。\n"
            f"  若继续跑，`/api/health` 会把**别人的服务**报成 200，\n"
            f"  于是全部断言打到别人身上、用自己从未初始化的 DB —— **假绿**。\n"
            f"  处置：指定另一个端口重跑（两种写法都支持）——\n"
            f"    python tools/<本脚本>.py --port <其它端口>\n"
            f"    python tools/<本脚本>.py <其它端口>\n"
            f"  或先查占用方：python tools/smoke_ports.py --check"
        )
    # 阶段②：立刻占住。bind 但不 listen。
    # 占位 socket 与 is_port_free() 必须用**同一套**平台语义，否则会出现
    # 「检查说空闲、占位却 bind 不上」的自相矛盾（TIME_WAIT 场景，见
    # _set_bind_semantics）。
    hold = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _set_bind_semantics(hold)
    except OSError:
        pass
    try:
        hold.bind((host, port))
    except OSError as exc:
        hold.close()
        return None, (
            f"✗ 端口 {port} 在检查通过后仍被抢占（{type(exc).__name__}）——\n"
            f"  说明有人刚好在同一瞬间抢占了它。请换端口重跑。"
        )
    return hold, ""


def release_port(hold: socket.socket | None, port: int,
                 host: str = "127.0.0.1") -> None:
    """阶段②的收尾：释放占位，让 uvicorn 能 bind 上来。"""
    if hold is not None:
        try:
            hold.close()
        except OSError:
            pass
    # Windows 上端口从"占用"到可 bind 之间有极短的残留窗口，等一下。
    for _ in range(20):
        if is_port_free(port, host):
            return
        time.sleep(0.05)


#: uvicorn bind 失败时会在输出里留下的痕迹（阶段③据此判定）
#:
#: ⚠️ **阶段③的残余风险**（architect 提出，QA 认可）：
#: 本阶段依赖"bind 错误被写进子进程输出"这个前提。若 uvicorn 因``--log-level``
#: 过低或输出缓冲策略而没把该错误刷出来，阶段③会**漏判**。
#: 漏判的后果**不是假绿**：wait_ready 仍会因进程退出而 return False ->
#: 走通用「服务未就绪」分支 -> 同样是 ``code=1`` 不绿。
#: 也就是说：**漏判只让报错信息不够指向性（少了"换端口"这句提示），
#: 不会让结果变得可信。** 目前 ``--log-level warning`` 下实测能抓到。
ADDR_IN_USE_MARKERS = (
    "address already in use",
    "only one usage of each socket address",
    "winerror 10048",
    "errno 98",
)


def detect_addr_in_use(text: str) -> bool:
    """阶段③：判断 uvicorn 输出里是否出现了"地址被占用"的痕迹。"""
    low = (text or "").lower()
    return any(m in low for m in ADDR_IN_USE_MARKERS)


def guard_or_die(port: int, script: str, stream=None) -> None:
    """冒烟脚本 ``main()`` 开头的**标准三段式守卫**（失败即退出）。

    给每个起真实 HTTP 子进程的冒烟脚本统一调用，省得各自重写一遍 ——
    之前 ``is_port_free`` 写好了却零调用，就是因为"每处自己写一遍"的
    代价高于"抽出来"。

    失败时**直接抛 SystemExit(1)**：此时服务一个子进程都还没起，
    不会有任何残留，退出码也干净。
    """
    out = stream or sys.stdout
    hold, hint = guard_port(port)
    if hold is None:
        out.write(f"[boot] {hint}\n")
        out.write(f"[boot]   （本脚本：tools/{script}）\n")
        out.flush()
        raise SystemExit(1)
    # 占位只是为了把竞态窗口压到最小；起服务前必须释放
    release_port(hold, port)


def _main(argv: list[str]) -> int:
    if "--check" in argv:
        print("默认端口占用检查：")
        busy = False
        for name, port, free in check_all_ports():
            mark = "空闲" if free else "⛔ 被占用（可能是残留进程，结果不可信）"
            if not free:
                busy = True
            print(f"  {name:<18} :{port}  {mark}")
        return 1 if busy else 0

    print("冒烟脚本默认端口表（唯一真源：tools/smoke_ports.py）")
    for name, port, free in check_all_ports():
        print(f"  {name:<18} :{port}{'' if free else '   ⛔ 被占用'}")
    print(f"\n共 {len(DEFAULT_PORTS)} 个脚本，端口互不重复。")
    print("命令行可覆盖：位置参数或 --port N（CI 依赖此能力）")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
