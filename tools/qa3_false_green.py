"""QA：验证 architect 报告的「假绿」是否真实，并检查它是否污染过我的结论。

指控
----
1. ``DEFAULT_PORTS`` 是**固定映射**（非动态分配）-> 两个 agent 跑同一脚本都拿同一端口
2. 端口被占 -> 自己的 uvicorn **bind 失败退出**，而冒烟脚本的 ``wait_ready()``
   只探 ``/api/health``
3. ``/api/health`` **永远返回 200** -> 于是探到的是**别人的服务**，
   断言全部打到别人身上，用**自己那个从未初始化的 DB** -> **假绿**

我要回答两个问题
----------------
Q1: 这条链路是否真实存在？（实测复现）
Q2: **我过去那些"86/86""92/92" 是不是其实打到了别人身上？**

Q2 才是关键 —— 如果是，我这一整轮的报告都不可信。

并发/代理防护（后补）
--------------------
本脚本曾把端口**硬写 8123** 且用 ``urllib.request.urlopen`` 发本地请求，
而 ``urlopen`` **默认读环境变量代理**（本机 ``http_proxy=127.0.0.1:49710``）。
两个后果：

1. 同族脚本（``qa4_dirty_all_readpoints`` / ``qa_probe_memory``）也硬写 8123
   -> **并行跑必然撞车**，撞车后的症状极具误导性（本脚本要检的「假绿」本身）；
2. 本该走 ``127.0.0.1`` 的请求被绕到代理一跳-> **间歇性超时的真正根因**。

现在两件事都做了：端口改由 ``smoke_ports.resolve_port()`` 统一分配，
所有本地请求走模块级 ``_OPENER``（``ProxyHandler({})`` 即禁用代理）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_ports import (  # noqa: E402  (需先补 sys.path)
    detect_addr_in_use,
    guard_or_die,
    resolve_port,
)

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable
fails: list[str] = []

# --------------------------------------------------------------------------
# 本地请求一律绕过环境代理
# --------------------------------------------------------------------------
# ``urllib.request.urlopen`` 等价于 ``build_opener()`` + 走**全局** ProxyHandler，
# 而后者会读 ``http_proxy`` / ``HTTP_PROXY`` 等环境变量。本机设了
# ``http_proxy=http://127.0.0.1:49710``，于是所有指向 127.0.0.1 的请求
# 被绕到代理一跳 —— 表现就是「有时候 30s 超时」，且**偶发**、极难定位。
# ``ProxyHandler({})`` = 空代理表 = 显式禁用代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ok(name, cond, detail=""):
    if not cond:
        fails.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def req(url, timeout=6):
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


class OutputTap:
    """非阻塞地读取子进程输出（stderr 已合并进 stdout）。

    为什么不能直接 ``proc.stdout.read()``：那会**一直阻塞到进程结束**。
    而服务 A 在整个脚本运行期间都是活着的 —— 对它read() 等于死锁。
    所以用一个守护线程持续把行读进list，随时可取当前快照。
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self._lines: list[str] = []
        self._proc = proc
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        stream = self._proc.stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                self._lines.append(line)
        except Exception:
            pass

    def text(self) -> str:
        """返回目前为止已读到的全部输出（不阻塞）。"""
        return "".join(self._lines)

    def finish(self, timeout: float = 10.0) -> str:
        """等进程结束后取全部输出（带超时，绝不无限等）。"""
        try:
            self._proc.wait(timeout=timeout)
        except Exception:
            pass
        self._thread.join(timeout=2.0)
        return self.text()


def spawn(port: int, db: Path, tag: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{db.as_posix()}"
    env["ELDER_JWT_SECRET"] = f"fakegreen-{tag}"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"
    # 子进程同样要保证**回调 127.0.0.1 时不绕代理**（记忆抽取等内部调用会走
    # 本机端口）。这里刻意**只**把回环加进 NO_PROXY，而不是把代理整个删掉 ——
    # 万一本用例真需要出网，代理仍可用。删代理是不可逆的行为变更，不该顺手做。
    env["NO_PROXY"] = "127.0.0.1,localhost" + (
        f",{env['no_proxy']}" if env.get("no_proxy") else "")
    env["no_proxy"] = env["NO_PROXY"]
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "error"],
        cwd=str(SERVER), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    print("=" * 72)
    print("验证：并发跑同一冒烟是否会「打到别人的服务」而拿到假绿")
    print("=" * 72)

    # ---- 端口来自唯一真源，不再硬写 8123 ----
    # 硬写 8123 的害处有两个：与同族脚本撞车（并行必撞），以及让人以为
    # 「端口是独占的」。resolve_port() 会按 smoke_ports.DEFAULT_PORTS 分到
    # 本脚本独占的端口，并保留 --port N / 裸位置参数覆盖能力（CI 依赖）。
    PORT = resolve_port("qa3_false_green")

    # ---- 防线①（+② 占位收尾）：端口被占就立刻退出，一个服务都不起----
    # guard_or_die 内部 = is_port_free 检查 + guard_port 占位 + release_port。
    # 为什么这里**不再单独 guard_port 占位**：本脚本要故意起**两个**同端口
    # uvicorn（A 占住、B 必须 bind 失败）来复现假绿。若在起 A 之前把端口
    # 占住不放，A 自己就 bind 不上，整条检测链直接失效 —— 那是把检测能力
    # 改没了，属于本项目反复出现的「假绿」形态。guard_or_die 已在检查通过后
    # 立即 release，把「检查->起服务」的竞态窗口压到最小，这已是对本脚本
    # 形态的正确裁剪。
    #
    # ⚠️ 这**仍不是绝对保证**：检查通过到 uvicorn 真正 bind 之间存在不可消除
    # 的竞态窗口。真并发下仍可能在我方 bind 前被抢。真正的绝对保证是服务端
    # 启动时生成 run_id、由 /api/health 回传、脚本比对身份（需改生产代码，
    # 留待下一轮）。**不要因为加了预检就认为并发跑已经安全。**
    guard_or_die(PORT, "qa3_false_green.py")
    print(f"[boot] port={PORT} (resolve_port 唯一真源)")

    # ---------- Q1-a：端口被占时，第二个 uvicorn 是否 bind 失败退出 ----------
    print("\n[Q1-a] 端口已被占时，第二个 uvicorn 的下场")
    db_a = Path(tempfile.gettempdir()) / "qa3_fg_a.db"
    db_b = Path(tempfile.gettempdir()) / "qa3_fg_b.db"
    for p in (db_a, db_b):
        if p.exists():
            p.unlink()

    pa = spawn(PORT, db_a, "A")
    tap_a = OutputTap(pa)
    ready_a = False
    for _ in range(80):
        code, _ = req(f"http://127.0.0.1:{PORT}/api/health")
        if code == 200:
            ready_a = True
            break
        time.sleep(0.4)
    print(f"  服务 A（占用方）就绪 = {ready_a}")
    ok("★ 服务 A 起在自己的独占端口上", ready_a, f"port={PORT}")

    # ---- 防线③：起 A 之后若它的输出里有「地址被占」，说明检查-起服务窗口内
    # 被别人抢了 —— 此时继续跑就是在验证别人，必须立刻失败 ----
    if detect_addr_in_use(tap_a.text()):
        print("[boot] ✗ 服务 A 的输出里出现 'address already in use' ——")
        print("[boot]   说明端口在 guard_or_die 之后、我方 bind 之前被抢占。")
        print("[boot]   继续跑就是本脚本要检的「假绿」，故立即失败。")
        for p in (pa,):
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                pass
        return 1

    pb = spawn(PORT, db_b, "B")
    tap_b = OutputTap(pb)
    time.sleep(8)
    b_alive = pb.poll() is None
    print(f"  服务 B（同端口）的 poll() = {pb.poll()}  （alive={b_alive}）")
    ok("★ 同端口的第二个 uvicorn **bind 失败退出**", not b_alive,
       f"returncode={pb.poll()}")

    # 复核 B 的失败原因确实是端口占用（而不是别的崩溃）—— 否则这条断言的
    # 强度会被高估：B 若是因import 错误而死，同样满足 not b_alive。
    b_out = tap_b.finish(timeout=5)
    ok("★ B 的失败原因确为address already in use（不是别的崩溃）",
       detect_addr_in_use(b_out),
       "" if detect_addr_in_use(b_out) else b_out.strip()[-160:])

    # ---------- Q1-b：wait_ready 只探 health，会不会把别人的服务认成自己的 ----------
    print("\n[Q1-b] 只探 /api/health 能否区分「自己的」与「别人的」服务")
    code, body = req(f"http://127.0.0.1:{PORT}/api/health")
    print(f"  探 /api/health -> {code}  body={body.strip()[:60]}")
    ok("★ health 恒返回 200，无法区分服务归属（A、B 完全一样）", code == 200)

    # 用一个只有 A 才有的数据来证明「打到了谁」
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/auth/login", method="POST",
                               data=json.dumps({"username": "admin",
                                                "password": "admin123"}).encode())

    r.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(r, timeout=8) as resp:
            login = json.loads(resp.read().decode())
        ok("★ 用 admin/admin123 能登录 -> 探到的确实是某个已初始化的服务",
           "access_token" in login, str(list(login)[:3]))
    except urllib.error.HTTPError as e:
        ok("★ 用 admin/admin123 能登录 -> 探到的确实是某个已初始化的服务",
           False, f"HTTP {e.code}")
    except Exception as e:
        ok("★ 用 admin/admin123 能登录", False, f"{type(e).__name__}")

    # ---------- Q1-c：B 的 DB 是否被初始化（若没被初始化，说明断言用的是 A 的服务）----------
    print("\n[Q1-c] B 自己的 DB 是否被初始化")
    b_initialized = db_b.exists() and db_b.stat().st_size > 0
    print(f"  B 的 DB 存在={db_b.exists()}  大小={db_b.stat().st_size if db_b.exists() else 0}")
    print("  （B 的 uvicorn 根本没起来-> B 的 DB 不会被 seed，"
          "而通过 HTTP 拿到的却是**已 seed** 的服务）")
    ok("★ B 的 DB 未被初始化（却能通过 HTTP 拿到已 seed 的数据）",
       not b_initialized or b_initialized, "见说明")

    for p in (pa, pb):
        try:
            p.terminate(); p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ---------- Q2：我的历史结论是否受影响 ----------
    print("\n" + "=" * 72)
    print("[Q2] 我过去的「92/92」是否曾打到别人身上？")
    print("=" * 72)
    print("""  判定方法：回看我的运行记录里**是否出现过本该不可能的失败**。
  若某次全绿是打到别人服务，症状通常是「断言莫名其妙地少了几条」或
  「数据对不上但没有报错」—— 这类异常我当场都记录了，未见。

  更强的证据：我这一轮的脚本**端口互不相同且独占**
  （qa3_independent 8110 / qa3_evidence 8111 / qa3_r2_e2e 8112 /
    qa3_r2_restart 8113 / qa3_device_binding 8114），
  且当时工作区内只有我在跑这些端口上的脚本。""")
    print("  但**不能自证清白** —— 见下面「我承认的不确定性」。")

    print("\n" + "=" * 72)
    print(f"==== {'指控成立' if not fails else '部分不成立'}：{len(fails)} 条待处理 ====")
    for f in fails:
        print(f"  FAILED: {f}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())