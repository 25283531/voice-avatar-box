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
* 端口段选在 ``8100-8106``：既避开历史上被撞过的 ``8096-8099``（可能还有
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
    "smoke_membership": 8106, # 会员订单
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


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """检测端口当前是否空闲（能否 bind）。

    仅用于**诊断**：端口被占用说明有残留进程或有人正在并行跑同一个脚本，
    此时冒烟结果不可信（可能连到了别人的服务上）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Windows 上不设 SO_EXCLUSIVEADDRUSE 时，SO_REUSEADDR 会让 bind
        # 对「已被占用」的端口也成功，那样就测不出冲突了。
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
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
#   1. 新脚本的 uvicorn socket bind 失败（WinError 10048），**但进程不退出**
#      —— 它还活着，只是没在监听；
#   2. 冒烟脚本的 wait_ready() 只探 /api/health；
#   3. 而 /api/health **永远返回 200**（main.py 的注释明写"只表达进程活着"）；
#   4. 于是 wait_ready 探到的是**别人的服务** → 认定"我的已就绪"；
#   5. 全部断言打到别人的服务上，用**自己那个从未 seed 过的 DB**。
#
# 实测（QA 用两个 uvicorn 同端口验证）：
#   A: 进程活着=True   自己 DB 已 seed = True   (184320 bytes)
#   B: 进程活着=True   自己 DB 已 seed = False  (65536 bytes)
#   → B 的"测试"实际跑在 A 的数据上，而 B 自己的库从未初始化。
#
# ⚠️ 三段式守卫**仍然不是绝对保证**
# ---------------------------------
# 「事前检查」与「真正 bind」之间存在不可消除的竞态窗口：检查刚通过，
# 别人就把端口抢走了。本模块把这段距离压到最小，但**压到最小 ≠ 消除**。
# 真正的绝对保证是让服务**自证身份**（启动时生成 run_id、/api/health 回传、
# 冒烟比对是否一致），那需要改生产代码，留给下一轮。
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
            f"  `resolve_port()` 是**静态查表**（表内不重复 ≠ 此刻没被占）。\n"
            f"  若继续跑，`/api/health` 会把**别人的服务**报成 200，\n"
            f"  于是全部断言打到别人身上、用自己从未初始化的 DB —— **假绿**。\n"
            f"  处置：换端口重跑 —— python tools/<本脚本>.py <其它端口>\n"
            f"  或先查占用方：python tools/smoke_ports.py --check"
        )
    # 阶段②：立刻占住。bind 但不 listen，且用独占地址选项
    # （Windows 上不设它，SO_REUSEADDR 会让 bind 对已占用端口也成功）。
    hold = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            hold.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
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
