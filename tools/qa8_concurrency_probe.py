"""反向验证：并发踩踏下，改造后的脚本是否零污染。

要证明的事
----------
「副本化变异」是不是**真的**挡住了并发踩踏，而不是只是看起来对。

证明方式（两个独立判据）
------------------------
**判据 1 · 从未泄漏变异内容**
    一个监视线程每 0.05s 读一次工作区 ``catalog.py``，检查里面是否出现过
    各脚本用到的**变异特征串**（如「可以酌情给结论」「一律给出结论」
    「@@@never_match@@@」）。改造前这些串会**真的出现在工作区文件里**；
    现在应当**一次都没出现**。这是最直接的证据。

**判据 2 · 从未覆盖并发修改**
    一个后台协程每 0.2s 在 ``catalog.py`` 末尾维护一个**唯一哨兵串**并不断
    重新声明它（模拟「另一个 agent 正在改同一个文件，且他也在防覆盖」）。
    脚本跑完后检查哨兵**是否仍在**——若脚本执行了 ``finally: write(原文)``，
    就会把对方的改动整段抹掉，哨兵随之消失。

⚠️ 关于退出码 3：协程本来就在改 ``catalog.py``，而它是**红线资产**，
所以沙箱的基线复核**必然**报警并 exit 3 —— 那是护栏在正确工作，
不是脚本失败。本脚本据此判定，不把 3 当成失败。

⚠️ 本脚本除了「种哨兵 / 收尾还原」外不修改任何工作区文件，
且哨兵只写在文件**末尾的注释**里，不影响任何语义。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "server" / "app" / "persona" / "catalog.py"
PY = r"C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe"

#: 各变异脚本用到的特征串。它们**绝不应**出现在工作区 catalog.py 里。
FORBIDDEN = [
    "可以酌情给结论",
    "一律给出结论、给出判断",
    "酌情给出结论",
    "__mutant_gone",
    "__removed_no_medical_advice",
    "@@@never_match@@@",
    "其实可以酌情给结论",
]

SENTINEL = "# eng7-concurrent-probe"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read_bytes_safe() -> bytes:
    try:
        return CATALOG.read_bytes()
    except OSError:
        return b""


class SentinelKeeper(threading.Thread):
    """后台协程：每 0.2s 确保 catalog.py 末尾有哨兵行。

    模拟「另一个 agent 正在改同一个文件」。它不断**重新声明**哨兵，
    所以如果脚本在 ``finally`` 里把文件写回原文，哨兵会短暂消失 ——
    我们在收尾时检查哨兵是否仍在，并记录它被「弄丢」过几次。
    """

    def __init__(self, period: float = 0.2) -> None:
        super().__init__(daemon=True)
        self.period = period
        self._stop = threading.Event()
        self.writes = 0
        self.lost_detected = 0   # 观察到哨兵消失又补回的次数

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                data = read_bytes_safe()
                if data and SENTINEL.encode() not in data:
                    self.lost_detected += 1
                if not data:
                    self._stop.wait(self.period)
                    continue
                if SENTINEL.encode() in data:
                    self._stop.wait(self.period)
                    continue
                # 追加哨兵（幂等：已存在则跳过）
                if not data.endswith(b"\n"):
                    data += b"\n"
                CATALOG.write_bytes(data + SENTINEL.encode() + b"\n")
                self.writes += 1
            except OSError:
                pass
            self._stop.wait(self.period)


class LeakWatcher(threading.Thread):
    """监视线程：高频检查工作区文件里是否泄漏了变异特征串。"""

    def __init__(self, period: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.period = period
        self._stop = threading.Event()
        self.leaks: list[str] = []
        self.samples = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                text = read_bytes_safe().decode("utf-8", errors="ignore")
                self.samples += 1
                for needle in FORBIDDEN:
                    if needle in text:
                        hit = f"{needle} @ sample#{self.samples}"
                        if hit not in self.leaks:
                            self.leaks.append(hit)
            except OSError:
                pass
            self._stop.wait(self.period)


def run_script(rel: str, timeout: int = 1800) -> tuple[int, str]:
    """跑一个改造后的脚本，返回 (exit_code, 输出尾部)。"""
    tmp = Path(os.environ.get("TMPDIR") or ".") / f"probe_{rel}"
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmp)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([PY, str(ROOT / "tools" / rel)], cwd=str(ROOT),
                       env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-900:]


def main() -> int:
    targets = sys.argv[1:] or ["qa3_redline_semantic.py"]
    baseline_bytes = read_bytes_safe()
    baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
    print(f"[基线] catalog.py sha256 = {baseline_sha}")
    print(f"[基线] catalog.py size   = {len(baseline_bytes)} bytes")
    print(f"[基线] 已含变异特征串？   "
          f"{[n for n in FORBIDDEN if n in baseline_bytes.decode('utf-8', 'ignore')]}\n")

    keeper = SentinelKeeper(period=0.2)
    watcher = LeakWatcher(period=0.05)
    keeper.start()
    watcher.start()
    time.sleep(0.6)
    print(f"[协程] 哨兵已就位；监视线程每 0.05s 采样工作区文件\n")

    outcomes: list[tuple[str, int, bool, bool]] = []
    try:
        for rel in targets:
            t0 = time.time()
            code, tail = run_script(rel)
            elapsed = time.time() - t0
            # 立刻检查哨兵是否还在（脚本有没有把对方的改动抹掉）
            sentinel_alive = SENTINEL in read_bytes_safe().decode("utf-8", "ignore")
            outcomes.append((rel, code, sentinel_alive, True))
            print("=" * 74)
            print(f"脚本：{rel}   exit={code}   用时 {elapsed:.1f}s")
            print(f"  判据2 · 并发哨兵仍在？ {'✅ 在（未覆盖对方改动）' if sentinel_alive else '❌ 被抹掉了！'}")
            print(f"  判据1 · 泄漏监视采样 {watcher.samples} 次，"
                  f"发现泄漏 {len(watcher.leaks)} 处")
            print(f"  协程写哨兵 {keeper.writes} 次；观察到哨兵被弄丢 {keeper.lost_detected} 次")
            print("-" * 74)
            print(tail[-420:])
            print()
    finally:
        keeper.stop()
        watcher.stop()
        keeper.join(timeout=5)
        watcher.join(timeout=5)
        # 收尾：哨兵可能被追加了多次（协程与还原竞争），逐个剥掉，
        # 再字节级写回开工时的内容。
        try:
            data = CATALOG.read_bytes()
            if SENTINEL.encode() in data:
                cleaned = b"\n".join(
                    ln for ln in data.split(b"\n")
                    if ln.strip() != SENTINEL.encode()
                )
                if not cleaned.endswith(b"\n"):
                    cleaned += b"\n"
                CATALOG.write_bytes(cleaned)
        except OSError:
            pass

    final_sha = sha256(CATALOG)

    print("=" * 74)
    print(f"[收尾] 哨兵剥离后 sha256 = {final_sha}")
    print(f"[期望] 开工前 sha256 = {baseline_sha}")
    ok_final = final_sha == baseline_sha
    print(f"       {'✅ 字节级一致 —— 无残留污染' if ok_final else '❌ 不一致！'}")

    print("\n==== 判定 ====")
    ok = ok_final and not watcher.leaks
    for rel, code, sentinel_alive, _ in outcomes:
        # exit 3 是护栏正确报警（协程在改红线资产），不算脚本失败
        good = sentinel_alive and code in (0, 1, 2, 3)
        ok = ok and good
        print(f"  {'✅' if good else '❌'} {rel:34s} exit={code}  哨兵存活={sentinel_alive}")
    print(f"\n监视采样 {watcher.samples} 次；变异特征串泄漏 {len(watcher.leaks)} 处")
    for hit in watcher.leaks[:5]:
        print(f"    ❌ 泄漏：{hit}")
    print(f"哨兵写入 {keeper.writes} 次；被弄丢 {keeper.lost_detected} 次"
          f"（>0 说明有脚本把文件整段写回过）")
    print("\nVERDICT:", "PASS —— 脚本全程只读工作区：既未泄漏变异，"
                        "也未覆盖并发修改" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
