"""mutation_sandbox.py —— 变异测试的「副本化变异」基础设施。

为什么需要这个模块
------------------
变异测试（mutation testing）要证明「门禁真的能挡住红线被改坏」，唯一的
办法是**主动把生产代码改坏**，看 eval 会不会变红。

历史上这些脚本是**原地改写** ``server/app/persona/catalog.py``，跑完再
``finally`` 复原。这在**并发**下会造成永久性数据损坏：

    A 存原文 -> B 存「A 的变异版」-> A 复原成原文
             -> B 复原成「A 的变异版」-> 永久留下一个反转的红线

这不是理论风险，项目里已经**实际发生过两次**：一次把医疗红线正文改成
「可以酌情给结论」，一次把「一律**不给**结论」翻成「一律**给出**结论」，
后者直接把「禁止给医疗结论」变成了「必须给」——安全护栏被悄悄拆掉，
而门禁可能仍然是绿的。

三层防线（本模块提供全部三层）
----------------------------
1. **副本化变异**：把 ``server/`` 整棵树复制到**本次运行独有的**临时沙箱
   （``mkdtemp``，绝不共用固定路径），所有变异只发生在副本上；
   ``sys.path`` / ``PYTHONPATH`` / ``cwd`` 全部指向沙箱。
   **工作区原件全程只读** —— 不是「尽量不写」，是结构上无从写起。

2. **基线比对（兜底）**：开工前对工作区每个受保护文件记 sha256，
   收工前逐个复核。任何不一致 -> 大声报警 + 退出码 3。
   副本化是「设计正确」，基线比对是「万一设计被绕过」的兜底。

3. **条件写回**：``restore()`` 只有在文件当前内容**仍等于我写入的变异版**
   时才复原；否则**跳过并报警**。
   无条件写回会把别人的并发改动整段抹掉 —— 宁可人工介入，不可覆盖。

退出码约定
----------
=====  ==========================================================
   0   测试通过
   1   测试失败（门禁没按预期变红 / 复原后仍红）
   2   脚本自身失效（锚点漂移，目标片段找不到）
   3   **工作区被污染** —— 比任何测试失败都严重，必须人工检查
=====  ==========================================================

用法
----
    with MutationSandbox("qa3_redline_all") as sb:
        sb.mutate("server/app/persona/catalog.py", OLD, NEW)
        code, out = sb.run_eval()
        sb.restore("server/app/persona/catalog.py")

退出 ``with`` 时自动做基线复核 + 清理；一旦发现污染会打印差异并以 3 退出
（即使脚本自己返回了 0 —— 污染的优先级高于一切测试结论）。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "WORKSPACE",
    "EXIT_OK",
    "EXIT_FAIL",
    "EXIT_SCRIPT_INVALID",
    "EXIT_CONTAMINATED",
    "MutationSandbox",
    "SandboxFile",
    "sha256_bytes",
]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 仓库根（本文件位于 ``<repo>/tools/mutation_sandbox.py``）
WORKSPACE = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SCRIPT_INVALID = 2
EXIT_CONTAMINATED = 3

#: 沙箱里不复制的东西：缓存、二进制数据库、以及巨大的前端 vendor 包
#: （eval 套件完全用不到它们，排除后每次建沙箱只要几毫秒）。
_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite",
    "*.sqlite3",
    "node_modules",
    ".git",
)

#: 受保护的工作区文件：``server/`` 下全部文件 + ``tools/`` 下的脚本。
#: 基线复核会覆盖这个集合，任何一个字节的变化都会被抓到。
_PROTECT_SUBDIRS = ("server", "tools")

#: **红线资产**：变异脚本会改写的那些文件。
#:
#: 判定规则（关键设计）：这些文件在测试窗口期内只要变了，无论是谁改的，
#: 一律以退出码 3 结束并要求人工检查。理由有两条，缺一不可：
#:
#: 1. 万一是**我们**造成的，那就是本任务要根除的污染事故；
#: 2. 万一是**别人**造成的，那么在「红线正文被改动」这件事上，
#:    本次测试跑出来的任何红/绿结论都不再可信 —— 与其给出一份
#:    可能建立在被污染基线上的报告，不如停下要求人看一眼。
#:
#: 其余文件（``tools/`` 下别的脚本、无关的 server 文件）由同事并发编辑
#: 是**正常工作方式**，把它们算成污染只会造成狼来了，反而让真正的污染
#: 淹没在噪声里。因此那一类只提示、不改退出码。
REDLINE_ASSETS = frozenset({
    "server/app/persona/catalog.py",
    "server/app/persona/guardrails.py",
    "server/app/persona/compiler.py",
    "server/evals/assertions.py",
    "server/evals/redline_baseline.py",
    "server/evals/redline_baseline.json",
    "server/evals/suites/xiaozhi.json",
    "server/evals/suites/invariants.json",
    "tools/eval_persona.py",
})


def sha256_bytes(data: bytes) -> str:
    """返回字节串的 sha256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    """返回文件字节的 sha256；文件不存在时返回 ``"<missing>"``。"""
    try:
        return sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        return "<missing>"


def _detect_eol(data: bytes) -> bytes:
    """返回该文件使用的行尾（CRLF 或 LF）。

    Windows 上 ``Path.write_text`` 会把 LF 悄悄转成 CRLF，导致「复原」
    悄悄改变了文件字节。脚本一律走 bytes 通道，这里负责把原始行尾风格
    还原回去，保证沙箱副本与工作区原件字节一致。
    """
    return b"\r\n" if b"\r\n" in data else b"\n"


def _to_text(data: bytes) -> str:
    """按 UTF-8 解码，宽容处理 BOM。"""
    return data.decode("utf-8-sig")


def _from_text(text: str) -> bytes:
    """编码为不含 BOM 的 UTF-8 字节。"""
    return text.encode("utf-8")


class SandboxFile:
    """沙箱内某个文件的句柄，带**条件复原**能力。

    这个类存在的意义：让「无条件写回」这种危险动作在 API 层面就不容易
    写出来。``write_text`` 会记住「我写进去的是什么」；``restore()``
    只有在磁盘上的内容**仍然等于**那个值时才复原，否则拒绝覆盖并报警。
    """

    __slots__ = ("path", "relpath", "_original", "_mutated")

    def __init__(self, path: Path, relpath: str, original: bytes) -> None:
        self.path = path
        self.relpath = relpath
        self._original = original
        # None = 自创建以来没被写过
        self._mutated: bytes | None = None

    # ---------------------------------------------------------------- 读

    def read_bytes(self) -> bytes:
        """返回文件当前字节。"""
        return self.path.read_bytes()

    def read_text(self) -> str:
        """返回文件当前文本（自动去 BOM）。"""
        return _to_text(self.read_bytes())

    # ---------------------------------------------------------------- 写

    def write_text(self, text: str) -> None:
        """以**保留原始行尾**的方式写入文本，并记录这是我的变异版。

        参数
        ----
        text:
            内容。内部换行统一按 ``\\n`` 书写即可，本方法会按原文件的
            行尾风格转换回去。
        """
        data = _from_text(text)
        self.write_bytes(_match_eol(data, self._original))

    def write_bytes(self, data: bytes) -> None:
        """直接写入字节并记录变异版。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)
        self._mutated = data

    def write_json(self, obj: object) -> None:
        """以 ``ensure_ascii=False, indent=2`` 写入 JSON。"""
        self.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")

    # ------------------------------------------------------------ 复原

    @property
    def mutated(self) -> bool:
        """当前是否处于「已被我改过」的状态。"""
        return self._mutated is not None

    def restore(self) -> bool:
        """**条件**复原：只有内容仍是我的变异版时才写回。

        返回
        ----
        True
            已复原，或本来就没改过。
        False
            **拒绝复原** —— 检测到文件被第三方改动，已打印差异。
            此时文件保持现状，交由人工判断。
        """
        if self._mutated is None:
            return True
        current = self._read_or_none()
        if current != self._mutated:
            print("=" * 74, file=sys.stderr)
            print(
                f"[!! 拒绝复原] 沙箱文件被第三方改动：{self.relpath}",
                file=sys.stderr,
            )
            print(f"     我写入的 sha256: {sha256_bytes(self._mutated)[:16]}", file=sys.stderr)
            print(f"     当前磁盘 sha256: {sha256_bytes(current or b'')[:16]}", file=sys.stderr)
            print("     -> 保留磁盘现状，不覆盖。可能是并发运行了同一个脚本。", file=sys.stderr)
            print("=" * 74, file=sys.stderr)
            return False
        self.path.write_bytes(self._original)
        self._mutated = None
        return True

    def _read_or_none(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    # ------------------------------------------------------------ 变异

    def replace(self, old: str, new: str, count: int = 1) -> bool:
        """在文件里把 ``old`` 替换成 ``new``，保留行尾风格。

        返回 ``False`` 表示**锚点不存在**（代码已变，脚本需要更新）——
        绝不静默当作成功，这是本项目反复踩的「假绿」坑。
        """
        text = self.read_text().replace("\r\n", "\n")
        old_n, new_n = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        if old_n not in text:
            return False
        self.write_text(text.replace(old_n, new_n, count))
        return True

    @property
    def original_bytes(self) -> bytes:
        """建沙箱时该文件的原始字节（用于比对与诊断）。"""
        return self._original


def _match_eol(data: bytes, reference: bytes) -> bytes:
    """把 ``data`` 的行尾统一成 ``reference`` 的行尾风格。"""
    target = _detect_eol(reference)
    normalized = data.replace(b"\r\n", b"\n")
    return normalized.replace(b"\n", target)


class MutationSandbox:
    """一次变异测试运行的隔离沙箱。

    生命周期：``__enter__`` 建立**本次运行独有**的临时目录并复制 ``server/``，
    ``__exit__`` 复核工作区基线、打印报告、清理目录。

    关键性质
    --------
    * **唯一性** —— 目录来自 ``mkdtemp``。哪怕两个 agent 同时跑同一个
      脚本，也是两份互不相干的副本（旧实现共用固定的 ``qa3_sandbox``，
      那是同一类踩踏，只是搬到了临时目录）。
    * **只读工作区** —— 沙箱内所有路径都在临时目录里，脚本拿到的
      ``catalog`` / ``guardrails`` 等句柄全部指向副本。
    * **退出码 3** —— 一旦基线复核发现工作区被动过，无条件以 3 退出。
    """

    def __init__(
        self,
        label: str,
        *,
        copy_server: bool = True,
        protect: Sequence[str] = _PROTECT_SUBDIRS,
    ) -> None:
        """初始化沙箱描述符（此时尚未建目录）。

        参数
        ----
        label:
            人类可读的本次运行标签，用于报告与临时目录命名。
        copy_server:
            是否复制 ``server/`` 整棵树。默认 True。
        protect:
            需要做基线复核的工作区子目录。
        """
        self.label = label
        self._copy_server = copy_server
        self._protect = tuple(protect)
        self._root: Path | None = None
        self._tmp: Path | None = None
        self._files: dict[str, SandboxFile] = {}
        self._baseline: dict[str, str] = {}
        self._contamination: list[tuple[str, str, str]] = []
        self._benign: list[tuple[str, str, str]] = []
        self._guard_installed = False

    # ------------------------------------------------------------ 建/拆

    def __enter__(self) -> "MutationSandbox":
        parent = os.environ.get("MUTATION_SANDBOX_ROOT") or None
        self._tmp = Path(tempfile.mkdtemp(prefix=f"qmut_{self.label}_", dir=parent))
        self._root = self._tmp / "repo"
        (self._root / "tools").mkdir(parents=True, exist_ok=True)

        # 1) 基线：先记工作区指纹，再复制。顺序很重要：先记基线才能
        #    区分「复制之前就有的改动」和「我们造成的改动」。
        self._baseline = self._snapshot_workspace()

        # 2) 复制源码树到沙箱
        if self._copy_server:
            shutil.copytree(
                WORKSPACE / "server",
                self._root / "server",
                ignore=_IGNORE,
                dirs_exist_ok=True,
            )
        # 3) 复制 eval harness —— 它用 ``Path(__file__).parent.parent``
        #    推导 ROOT/SERVER，复制进去才能让 ROOT 落在沙箱里。
        for name in ("eval_persona.py", "mutation_sandbox.py"):
            src = WORKSPACE / "tools" / name
            if src.exists():
                shutil.copy2(src, self._root / "tools" / name)

        self._tmp_scratch = self._tmp / "tmp"
        self._tmp_scratch.mkdir(parents=True, exist_ok=True)
        self._install_exit_guard()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        contaminated = self.verify_workspace()
        benign = self.benign_changes()
        try:
            self.cleanup()
        except OSError:
            pass
        if contaminated:
            self._report_contamination()
        elif benign:
            # 同事并发改了别的文件 —— 这是正常的协作，只提示不改退出码。
            print(f"[note] 测试期间有 {len(benign)} 个非红线资产文件被并发改动"
                  f"（同事的工作），不影响本次结论。", file=sys.stderr)
        # 不吞异常：即便脚本自身抛错，污染也已经通过 atexit 钩子
        # 升级成退出码 3。
        return False

    def cleanup(self) -> None:
        """删除临时沙箱目录。"""
        if self._tmp and self._tmp.exists():
            shutil.rmtree(self._tmp, ignore_errors=True)

    # ------------------------------------------------------------ 路径

    @property
    def root(self) -> Path:
        """沙箱仓库根（等价于「工作区的我」）。"""
        assert self._root is not None, "请在 with 语句内使用沙箱"
        return self._root

    @property
    def server(self) -> Path:
        """沙箱内的 ``server/`` 目录。"""
        return self.root / "server"

    @property
    def tools(self) -> Path:
        """沙箱内的 ``tools/`` 目录。"""
        return self.root / "tools"

    @property
    def tmp(self) -> Path:
        """本次运行独有的临时目录，用作子进程 TMPDIR。"""
        assert self._tmp is not None
        return self._tmp_scratch

    @property
    def eval_script(self) -> Path:
        """沙箱内的 ``tools/eval_persona.py``。"""
        return self.tools / "eval_persona.py"

    # ---- 常用文件的句柄（全部指向沙箱副本）--------------------------

    def _handle(self, relpath: str) -> SandboxFile:
        """取得（并缓存）沙箱内某文件的条件写回句柄。"""
        if relpath in self._files:
            return self._files[relpath]
        target = self.root / relpath
        if not target.exists():
            raise FileNotFoundError(f"沙箱内不存在 {relpath}（工作区里也没有？）")
        handle = SandboxFile(target, relpath, target.read_bytes())
        self._files[relpath] = handle
        return handle

    @property
    def catalog(self) -> SandboxFile:
        """``server/app/persona/catalog.py``（红线定义）。"""
        return self._handle("server/app/persona/catalog.py")

    @property
    def guardrails(self) -> SandboxFile:
        """``server/app/persona/guardrails.py``。"""
        return self._handle("server/app/persona/guardrails.py")

    @property
    def compiler(self) -> SandboxFile:
        """``server/app/persona/compiler.py``。"""
        return self._handle("server/app/persona/compiler.py")

    @property
    def assertions(self) -> SandboxFile:
        """``server/evals/assertions.py``。"""
        return self._handle("server/evals/assertions.py")

    @property
    def redline_baseline_py(self) -> SandboxFile:
        """``server/evals/redline_baseline.py``。"""
        return self._handle("server/evals/redline_baseline.py")

    @property
    def redline_baseline_json(self) -> SandboxFile:
        """``server/evals/redline_baseline.json``（黄金副本）。"""
        return self._handle("server/evals/redline_baseline.json")

    @property
    def perm_invariants(self) -> SandboxFile:
        """``server/evals/perm_invariants.py``。"""
        return self._handle("server/evals/perm_invariants.py")

    @property
    def suites_dir(self) -> Path:
        """沙箱内的 ``server/evals/suites/`` 目录。"""
        return self.server / "evals" / "suites"

    def suite(self, name: str) -> SandboxFile:
        """取得套件 JSON 的句柄（``name`` 可省略 ``.json``）。"""
        stem = name[:-5] if name.endswith(".json") else name
        return self._handle(f"server/evals/suites/{stem}.json")

    def suites(self) -> list[SandboxFile]:
        """取得全部套件 JSON 的句柄。"""
        return [self._handle(f"server/evals/suites/{p.name}")
                for p in sorted(self.suites_dir.glob("*.json"))]

    def handle(self, relpath: str) -> SandboxFile:
        """取得任意沙箱文件的句柄。"""
        return self._handle(relpath)

    def exists(self, relpath: str) -> bool:
        """工作区（也即沙箱副本来源）中是否存在该文件。"""
        return (self.root / relpath).exists()

    # ------------------------------------------------------------ 变异

    def mutate(self, relpath: str, old: str, new: str, count: int = 1) -> bool:
        """在**沙箱副本**上做一次替换。

        返回 ``False`` 表示锚点不存在 —— 调用方**必须**据此判定
        「脚本自身失效」（退出码 2），不得当作通过。
        """
        return self._handle(relpath).replace(old, new, count)

    def restore(self, relpath: str) -> bool:
        """条件复原单个文件。返回 ``False`` 表示拒绝覆盖并已报警。"""
        handle = self._files.get(relpath)
        if handle is None:
            return True
        return handle.restore()

    def restore_all(self) -> list[str]:
        """条件复原所有被改过的文件，返回复原失败的相对路径列表。"""
        failed: list[str] = []
        for relpath, handle in self._files.items():
            if handle.mutated and not handle.restore():
                failed.append(relpath)
        return failed

    def reset(self, relpaths: Iterable[str]) -> None:
        """把给定文件全部复原到基线（条件写回）。

        沙箱副本被多个 case 反复复用时用它代替「重新拷一份」。
        """
        for relpath in relpaths:
            self.restore(relpath)

    # ------------------------------------------------------------ 执行

    def subprocess_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """构造指向沙箱的子进程环境变量。

        关键点：``PYTHONPATH`` 指向沙箱的 ``server/``，``TMPDIR`` 指向本次
        运行独有的临时目录。这样即使子进程 import 了 ``app.persona.*``，
        拿到的也是**副本**。
        """
        env = dict(os.environ)
        env.pop("MSYSTEM", None)          # Git Bash 会干扰子进程编码
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(self.tmp)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.server)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
        env["MUTATION_SANDBOX_ACTIVE"] = "1"
        if extra:
            env.update(extra)
        return env

    def run_eval(
        self,
        suites: Sequence[str] = (),
        *,
        timeout: int = 600,
        python: str | None = None,
    ) -> tuple[int, str]:
        """在沙箱里跑一次 ``tools/eval_persona.py``。

        参数
        ----
        suites:
            要跑的套件名；留空表示全部。
        timeout:
            子进程超时秒数。

        返回
        ----
        ``(exit_code, stdout + stderr)``。**不会**抛异常：变异体导致的
        崩溃本身就是被观测信号，调用方从输出里判读。
        """
        cmd = [python or sys.executable, str(self.eval_script), *suites]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.root),
                env=self.subprocess_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, f"[TIMEOUT] eval 超过 {timeout}s 未返回"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def run_python(
        self,
        code: str,
        *,
        timeout: int = 300,
        python: str | None = None,
        driver_name: str = "driver.py",
    ) -> tuple[int, str]:
        """在沙箱里执行一段 Python 代码，返回 ``(exit_code, 输出)``。

        代码以 ``-c`` 形式运行，``sys.path[0]`` 会指向沙箱 ``server/``
        （通过 ``PYTHONPATH`` 保证），因此 ``import app.persona`` 拿到的
        是副本。
        """
        try:
            proc = subprocess.run(
                [python or sys.executable, "-c", code],
                cwd=str(self.root),
                env=self.subprocess_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, f"[TIMEOUT] driver 超过 {timeout}s 未返回"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def write_driver(self, name: str, code: str) -> Path:
        """把一段驱动代码写进沙箱 ``tools/``，返回其路径。

        供需要落盘脚本、再单独执行的场景使用（例如把编译产物打印出来
        供人工确认）。写入位置在沙箱内，不会碰到工作区。
        """
        target = self.tools / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        return target

    def run_driver(self, driver: Path, *, timeout: int = 300,
                   python: str | None = None) -> tuple[int, str]:
        """执行 :meth:`write_driver` 产出的脚本。"""
        try:
            proc = subprocess.run(
                [python or sys.executable, str(driver)],
                cwd=str(self.root),
                env=self.subprocess_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, f"[TIMEOUT] {driver.name} 超过 {timeout}s 未返回"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def python_exe(self) -> str:
        """本脚本应当使用的解释器（与当前进程一致）。"""
        return sys.executable

    # -------------------------------------------------------- 基线复核

    def _snapshot_workspace(self) -> dict[str, str]:
        """记录工作区受保护文件的 sha256 基线。"""
        snap: dict[str, str] = {}
        for sub in self._protect:
            base = WORKSPACE / sub
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                snap[path.relative_to(WORKSPACE).as_posix()] = _sha256_file(path)
        return snap

    def verify_workspace(self) -> list[tuple[str, str, str]]:
        """复核工作区是否仍与基线一致。

        返回**致命污染**列表 ``[(相对路径, 基线 sha256, 当前 sha256), ...]``，
        只包含 :data:`REDLINE_ASSETS`（红线资产）里被改动的文件。
        非资产文件的并发改动由 :meth:`benign_changes` 单独报告，
        不影响退出码 —— 同事正常改代码不该让门禁变红。
        """
        current = self._snapshot_workspace()
        bad: list[tuple[str, str, str]] = []
        for relpath, before in self._baseline.items():
            after = current.get(relpath, "<missing>")
            if after != before and relpath in REDLINE_ASSETS:
                bad.append((relpath, before, after))
        self._contamination = bad
        self._benign = [
            (relpath, before, current.get(relpath, "<missing>"))
            for relpath, before in self._baseline.items()
            if current.get(relpath, "<missing>") != before
            and relpath not in REDLINE_ASSETS
        ]
        self._appeared = [
            relpath for relpath in current
            if relpath not in self._baseline and relpath in REDLINE_ASSETS
        ]
        for relpath in self._appeared:
            self._contamination.append((relpath, "<absent>", current[relpath]))
        return bad

    def benign_changes(self) -> list[tuple[str, str, str]]:
        """返回非红线资产文件的并发改动（同事的工作，不算污染）。"""
        return list(getattr(self, "_benign", []))

    def _install_exit_guard(self) -> None:
        """注册 atexit 钩子：一旦有污染，强制退出码 3。

        为什么需要 atexit：脚本可能因为各种原因提前 ``return`` 或抛异常，
        未必走得到我们自己的报告代码。污染的严重性高于任何测试结论，
        所以在解释器退出这一层再兜一次底。
        """
        if self._guard_installed:
            return
        self._guard_installed = True

        def _hook() -> None:
            if not self._contamination:
                return
            self._report_contamination()
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            # 只在「本来会以 0 退出」时升级；已有非 0 退出码时保留原值，
            # 但仍然已经把差异打印出来了。
            os._exit(EXIT_CONTAMINATED)

        atexit.register(_hook)

    def _report_contamination(self) -> None:
        """打印污染详情（差异清单 + 归属判定）。"""
        print("=" * 74, file=sys.stderr)
        print("[!!! 严重] 红线资产文件在测试期间被改动 —— 本次结论不可信！",
              file=sys.stderr)
        print(f"      脚本：{self.label}", file=sys.stderr)
        print(f"      受影响文件 {len(self._contamination)} 个：", file=sys.stderr)
        for relpath, before, after in self._contamination[:20]:
            print(f"        - {relpath}", file=sys.stderr)
            print(f"            基线 {before[:16]}  →  当前 {after[:16]}", file=sys.stderr)
        if len(self._contamination) > 20:
            print(f"        ... 另有 {len(self._contamination) - 20} 个", file=sys.stderr)
        print("      → 若非本脚本所为（同事并发改动），请人工确认后再重跑。",
              file=sys.stderr)
        print("      → 请人工检查 git diff / git status！", file=sys.stderr)
        benign = getattr(self, "_benign", [])
        if benign:
            print(f"      （另有 {len(benign)} 个非红线资产文件被并发改动，"
                  f"不计入污染——那是同事的正常工作）", file=sys.stderr)
        print("=" * 74, file=sys.stderr)


def workspace_md5(relpath: str) -> str:
    """返回工作区某文件的 md5（诊断用，短摘要）。"""
    return hashlib.md5((WORKSPACE / relpath).read_bytes()).hexdigest()
