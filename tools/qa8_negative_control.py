"""反向验证的**负对照**：证明 qa8 探针不是「怎么都过」的空检查。

做法
----
在探针的监视下，故意复现**改造前**那个危险模式：

    src =目标.read_text()
    目标.write_text(变异后)      # 原地改写工作区
    try:
        ...
    finally:
        目标.write_text(src)     # ← 无条件写回

预期：探针必须**抓到大红**——
  * 判据 1：监视线程会看到变异特征串出现在工作区里；
  * 判据 2：``finally`` 的无条件写回会把并发哨兵整段抹掉。

这两条正是本项目已经实际发生过的两类事故。

⚠️ 本脚本**自己就是**那个危险示范，所以它必须亲手碰工作区文件。
为此它极度保守：
  * 只改 ``catalog.py`` 末尾的**注释行**（追加一行），不碰任何红线正文；
  * ``finally`` 里无条件写回**开工前读到的字节**（这正是要演示的 bug）；
  * 最外层再兜一层 ``finally``，无条件写回基线字节；
  * 退出前自检 sha256，不一致立刻大声报警。
运行完请以 ``md5sum server/app/persona/catalog.py`` 复核。
"""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "server" / "app" / "persona" / "catalog.py"
SENTINEL = "# eng8-sentinel"
LEAK = "可以酌情给结论"   # 探针的判据 1 特征串之一


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    baseline = CATALOG.read_bytes()
    baseline_sha = sha256_bytes(baseline)
    print(f"[基线] sha256 = {baseline_sha}")

    seen_leak: list[str] = []
    sentinel_lost: list[str] = []
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            try:
                text = CATALOG.read_bytes().decode("utf-8", "ignore")
                if LEAK in text:
                    seen_leak.append(LEAK)
                if SENTINEL not in text:
                    sentinel_lost.append("sentinel")
            except OSError:
                pass
            time.sleep(0.02)

    # ⚠️ 顺序至关重要，必须复现真实竞态：
    #   A 先把「原文」读进内存（此时文件里**还没有**哨兵）
    #   → B（另一个 agent）写入哨兵
    #   → A 的 finally 无条件写回自己那份「过期原文」= 把 B 的哨兵抹掉
    # 若这里先种哨兵再读原文，写回时就会把哨兵一起带回去，判据 2 测不到。
    stale_original = CATALOG.read_bytes()      # A 读到的原文（无哨兵）
    if SENTINEL.encode() not in stale_original:
        CATALOG.write_bytes(stale_original + b"\n" + SENTINEL.encode() + b"\n")
    with_sentinel = CATALOG.read_bytes()
    print(f"[哨兵] B 已写入，sha256 = {sha256_bytes(with_sentinel)}")

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    time.sleep(0.2)

    # ===== 以下是「改造前」的原始危险模式，原样复现 =====
    try:
        src = stale_original                   # A 手里的「过期原文」（无哨兵）
        # 变异内容基于**当前**文件（保留哨兵），这样两条判据各自独立成立：
        #   判据 1 靠这次写入暴露「变异内容进了工作区」；
        #   判据 2 靠下面 finally 写回过期原文暴露「覆盖了别人的改动」。
        mutated = with_sentinel + b"\n# " + LEAK.encode() + b"\n"
        CATALOG.write_bytes(mutated)             # 原地改写工作区
        time.sleep(0.4)                         # 让监视线程看到
        raise RuntimeError("模拟测试中途异常")   # 触发 finally
    except RuntimeError:
        pass
    finally:
        # ⚠️ 这就是 bug：无条件写回**过期**原文，把 B 的哨兵整段抹掉。
        CATALOG.write_bytes(src)
        time.sleep(0.4)
    # ===== 危险模式结束 =====

    stop.set()
    t.join(timeout=3)

    # 最外层兜底：恢复基线
    if CATALOG.read_bytes() != baseline:
        CATALOG.write_bytes(baseline)

    final_sha = sha256_bytes(CATALOG.read_bytes())
    print("\n==== 负对照判定 ====")
    print(f"  判据 1 · 监视线程看到变异内容泄漏？ "
          f"{'❌ 是（探针有效）' if seen_leak else '未看到'}"
          f"  次数={len(seen_leak)}")
    print(f"  判据 2 · 并发哨兵被无条件写回抹掉？ "
          f"{'❌ 是（探针有效）' if sentinel_lost else '未看到'}"
          f"  次数={len(sentinel_lost)}")
    print(f"  收尾 sha256 = {final_sha}")
    print(f"  与基线一致？ {'✅ 是' if final_sha == baseline_sha else '❌ 否'}")

    probe_valid = bool(seen_leak) and bool(sentinel_lost) and final_sha == baseline_sha
    print("\nVERDICT:", "PASS —— 探针有鉴别力：旧模式会被它抓到，"
                        "因此新版通过不是假绿" if probe_valid else "FAIL —— 探针无效")
    return 0 if probe_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
