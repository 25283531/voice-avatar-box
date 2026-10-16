"""分组指纹守卫：**在跑测试之前，先回答「工作区是不是稳定的」。**

为什么需要这个工具
------------------
本项目出现过一次很难查的现象：`redline_matches_baseline` **偶发红**，
连跑 6 次里红1 次绿 5 次，看起来像随机 flake。

真因是**双文件编辑竞态**：

    catalog.py（红线正文）与 redline_baseline.json（黄金副本）
    必须**同时**改才自洽。而普通的「16 文件指纹清单」把它们当
    **两个独立条目**，于是「有人正在改」这件事在指纹上表现为
    「某一个文件变了」—— 与「随机 flake」在观测上无法区分。

代价很实在：QA 只能去翻mtime，靠「我的测试窗口 02:32–02:34
恰好落在 catalog.py 的 mtime 02:34:01 里」才反推出因果。**20 分钟。**

所以这里做两件事
--------------
1. **分组**：`catalog.py` 与 `redline_baseline.json` 放进同一个
   「红线原子组」，组内标注 ``atomic = True``。
2. **给出的是「编辑中」信号，而不是「不一致」信号**：
   指纹不一致本身既可能是「有人正在改」，也可能是「改完了、
   该重跑测试了」—— 这两者对QA 的行动指令完全相反。
   本工具只回答前者：**是否有人在编辑中**。

⚠️ 本工具**不判断代码对错**，也不替代任何测试
------------------------------------------------
它唯一的职责是：**在跑测试之前，先拿到一个「现在测有意义吗」的答案。**
指纹一致 ≠ 代码正确（这一点必须说清楚，否则它会被当成门禁用）。

用法
----
    python tools/qa_fingerprint.py            # 打印分组指纹 + 是否可测
    python tools/qa_fingerprint.py --json     # 机器可读
    python tools/qa_fingerprint.py --record   # 把当前值记为新基线（需人工确认）

退出码
------
    0 = **可测**：所有原子组都不处于「半改」状态
    2 = **有人在编辑中**：至少一个原子组半改。**此时跑测试会得到
        不可复现的结果**，建议等 30~60 秒重试，或先确认归属再动手
    3 = 工具自身出错（文件缺失等）

为什么要分组而不是给一个总 md5
------------------------------
总 md5 有用，但**粒度太粗**：它在「任何人改了一个文件」时都会变，
于是「有人在编辑」与「刚改完」都表现为「指纹变了」，同样不可区分。
分组保留了「改的是哪一组」这一信息量，才可能区分「半改」与「全改」。

「半改」为什么是可判定信号
--------------------------
对 atomic 组（组内文件必须一起改），存在三种状态：

    全同 = 组内所有文件都等于基线 -> 空闲
    全异 = 组内所有文件都变了     -> 改完了，可以测
    半异 = 组内**一部分**变了     -> **正在改，别测**

第三种正是本项目踩过的坑。它可判定是因为 atomic 组内文件数 >= 2，
而「全同/全异/半异」三态互斥且穷尽。单文件组不可能出现半异
（那要么全同要么全异），所以单文件组天然只区分前两态。

设计上的三条自我约束（踩过的坑）
--------------------------------
1. **不要在测试脚本里 kill 进程**：本工具只读文件，绝不碰进程。
   团队约定「任何清理进程的建议必须先附进程清单与归属判断」。
2. **只读，不写**：除非显式 ``--record``，否则不修改任何文件。
   变异脚本更必须如此 —— 改到一半被中断会留污染（见模块末尾）。
3. **指纹是线索不是结论**：一致不代表对，不一致不代表错。

⚠️⚠️ 为什么人工核对不可靠：「两次采样」vs「一次一致性快照」
----------------------------------------------------------
**这是本工具存在的第二个理由，且比"分组"更根本。**

人工核对指纹的天然做法是跑两次命令再看结果：

    md5sum catalog.py        # 第一次采样
    ...过一会儿...
    md5sum catalog.py        # 第二次采样 -> 拿两个值对比

**这个方法有固有缺陷**：两次采样**不在同一时刻**。若这中间发生了一次
「保存 -> 回滚」（编辑器保存、agent 改完又撤、git checkout 复原），
那么两次采样会看到**同一个值**，你会得出「没变化」的结论 ——
**而实际上工作区在那几秒内是脏的。**

**本项目真实发生过**：QA 用``md5sum`` 测得 `catalog.py` = `8565167a`，
同一时刻 ``qa_fingerprint.py`` 报「全同」。查mtime 才发现是 engineer-3
在 **2 秒内保存又回滚** —— 工具是对的，人工核对是错的。

反过来也成立：人工看到变化时，可能变化早已消失，于是误以为"有人在改"。

> **人工核对 = 两次采样（跨时刻，可能被"保存又回滚"骗过）；
> 本工具 = 一次一致性快照（组内文件同一时刻读取）。**

这不是人的疏忽，是**方法的固有缺陷**：任何"跑两次再对比"的方案都有这个洞。
所以本工具把「一组文件的指纹」放在**同一次调用**里算完，不跨时刻。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ============================================================ 分组定义
# atomic=True 的组：组内文件必须**同时**改才自洽。
# 半改 = 有人在编辑中= 别测。
GROUPS: list[dict] = [
    {
        "name": "红线原子组",
        "atomic": True,
        "why": "catalog.py 的红线正文与 redline_baseline.json 的黄金副本"
               "必须逐字一致。只改其一时redline_matches_baseline 必红，"
               "且表现为**随机 flake**（本项目真实踩过）。",
        "files": [
            "server/app/persona/catalog.py",
            "server/evals/redline_baseline.json",
            "server/evals/redline_baseline.py",
        ],
    },
    {
        "name": "门禁",
        # 门禁各文件可独立演进（改断言不必同时改套件），故 atomic=False。
        # 但它们**共同决定** eval 的判定，改动后必须重跑 eval 才算数。
        "atomic": False,
        "why": "决定 eval 判定口径。改动后旧结论全部失效，必须重跑。",
        "files": [
            "server/evals/assertions.py",
            "server/evals/runner.py",
            "server/evals/perm_invariants.py",
            "server/evals/suites/xiaozhi.json",
            "server/evals/suites/invariants.json",
        ],
    },
    {
        "name": "角色域",
        "atomic": False,
        "why": "角色读写链路。改动后 smoke_roles 的旧结论失效。",
        "files": [
            "server/app/routers/roles.py",
            "server/app/persona/guardrails.py",
            "server/app/persona/spec.py",
            "server/app/persona/compiler.py",
            "server/app/schemas.py",
        ],
    },
    {
        "name": "启动与迁移",
        "atomic": False,
        "why": "seed/_seed_migration 会改写用户数据，改动后必须跑"
               "smoke_roles 的重启不变式那组。",
        "files": [
            "server/app/main.py",
            "server/app/schema_patch.py",
            "server/app/models.py",
        ],
    },
]

BASELINE_PATH = ROOT / "tools" / ".qa_fingerprint_baseline.json"


def fp(path: Path) -> str:
    """文件内容的 md5 前 8 位。文件不存在返回 'MISSING'。"""
    if not path.exists():
        return "MISSING"
    return hashlib.md5(path.read_bytes()).hexdigest()[:8]


def load_baseline() -> dict[str, str]:
    """读基线文件；不存在返回空 dict（首次使用）。

    基线里有一个 ``_meta`` 键存元信息（记录时间 / verified_green）。
    它**不是文件路径**，比较时必须跳过 —— 否则 ``_meta`` 会被当成
    一个"文件"去找，恒为 MISSING（上一版没这个键时不会出问题，
    加了之后就会。这里显式过滤，别删）。
    """
    if not BASELINE_PATH.exists():
        return {}
    try:
        raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception:      # 基线坏了不该让工具整体不可用
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def evaluate() -> tuple[int, dict]:
    """算出每组状态与全局判定。

    :return: (exit_code, 详情)
    """
    base = load_baseline()
    if not base:
        return 3, {
            "error": "尚无基线。请先运行：python tools/qa_fingerprint.py --record",
            "groups": [],
        }

    out_groups = []
    editing: list[str] = []
    for g in GROUPS:
        items = []
        changed = 0
        for rel in g["files"]:
            cur = fp(ROOT / rel)
            old = base.get(rel)
            is_changed = (old is not None and cur != old)
            if is_changed:
                changed += 1
            items.append({
                "file": rel,
                "cur": cur,
                "base": old or "(无基线)",
                "changed": is_changed,
            })
        n = len(g["files"])
        if changed == 0:
            state = "全同"
        elif changed == n:
            state = "全异"
        else:
            state = "半异"
        # 半异只在 atomic 组才有意义（组内>=2 且要求同步）
        if state == "半异" and g["atomic"]:
            editing.append(g["name"])
        out_groups.append({
            "name": g["name"],
            "atomic": g["atomic"],
            "why": g["why"],
            "state": state,
            "changed_count": changed,
            "total": n,
            "items": items,
        })

    code = 2 if editing else 0
    meta = {}
    if BASELINE_PATH.exists():
        try:
            raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            meta = raw.get("_meta") or {}
        except Exception:
            meta = {}
    return code, {
        "editing_groups": editing,
        "groups": out_groups,
        "baseline_meta": meta,
        "baseline_file": str(BASELINE_PATH.relative_to(ROOT)),
    }


def render(detail: dict) -> str:
    lines: list[str] = []
    if "error" in detail:
        return f"[fingerprint] {detail['error']}"

    lines.append("=" * 74)
    meta = detail.get("baseline_meta") or {}
    if meta:
        tag = "已声明全套通过" if meta.get("verified_green") else "**未**声明全套通过"
        lines.append(f"基线记录于{meta.get('recorded_at', '?')}  ·  {tag}")
    lines.append("工作区指纹（分组）—— 只回答「现在测有意义吗」，不判断代码对错")
    lines.append("=" * 74)
    for g in detail["groups"]:
        tag = "原子同步" if g["atomic"] else "可独立改"
        lines.append(f"\n[{g['name']}]  状态={g['state']}  "
                     f"({g['changed_count']}/{g['total']} 变化)  {tag}")
        for it in g["items"]:
            mark = "*" if it["changed"] else " "
            lines.append(f"   {mark} {it['file']:46s} {it['cur']}  (基线 {it['base']})")
        lines.append(f"   ↳ {g['why']}")

    lines.append("")
    lines.append("-" * 74)
    if detail["editing_groups"]:
        lines.append("⛔ **有人在编辑中**：" + "、".join(detail["editing_groups"])
                     + " 处于「半改」状态。")
        lines.append("")
        lines.append("   这几个组里的文件必须**一起**改才自洽。现在只改了一部分，")
        lines.append("   此时跑测试会得到**不可复现**的结果（看起来像随机 flake）。")
        lines.append("")
        lines.append("   建议：")
        lines.append("     · 等 30~60 秒后重跑本工具，确认变成「全异」再测；")
        lines.append("     · 或先用 `Get-CimInstance Win32_Process -Filter "
                     "\"Name='python.exe'\"`")
        lines.append("       确认是否有人在跑（团队约定：不许裸kill，见模块docstring）。")
        lines.append("")
        lines.append("   ⚠️ 若你就是那个正在编辑的人：这正是正常状态，改完再测。")
        return "\n".join(lines)

    lines.append("✅ 可测：没有原子组处于「半改」状态。")
    lines.append("")
    lines.append("   注意：这**只**说明工作区不在编辑中，不代表代码正确 ——")
    lines.append("   该跑的测试仍要跑。指纹是「测之前的前置条件」，不是门禁。")
    return "\n".join(lines)


def record() -> int:
    """把当前值记为新基线。

    ``verified_green`` 记录的是**记录那一刻**的验证状态，而不是一个可以
    长期复用的保证。它的作用是让 review 的人看到：
    「这份基线是在全套测试通过的时候记的」，而不是一堆裸hash。

    ⚠️ ``--record`` 故意**不自动跑测试**。理由：
    在多人并发的工作区里，「record 时恰好全绿」有两种可能——
    真的绿，或者**恰好撞上没人编辑的窗口**。把后者记成"已验证"就是在
    给基线盖一个不实的章。所以由人显式传 ``--verified`` 来声明，
    工具只负责**如实记录时间与 git commit**，不替人做判断。
    """
    import datetime

    data = {
        "_meta": {
            # 这份基线声称是在「全套测试通过」的状态下记录的。
            # 只表示记录那一刻的状态，不表示之后仍然成立。
            "verified_green": "--verified" in sys.argv,
            "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "note": "verified_green 只描述记录那一刻；改动代码后它不会自动失效，"
                    "所以 review 时仍需看下面的 hash 与当前 diff 是否一致。",
            "how_to_verify": "python tools/qa_fingerprint.py  # 应输出「可测」；"
                             "再跑 python tools/eval_persona.py # 应 exit 0",
        }
    }
    for g in GROUPS:
        for rel in g["files"]:
            data[rel] = fp(ROOT / rel)
    missing = [k for k, v in data.items()
               if k != "_meta" and v == "MISSING"]
    if missing:
        print("❌ 以下文件缺失，未记录基线：")
        for m in missing:
            print("   - " + m)
        return 3
    BASELINE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    n = len(data) - 1
    print(f"✅ 已记录 {n} 个文件的基线 -> "
          f"{BASELINE_PATH.relative_to(ROOT)}")
    print(f"   verified_green = {data['_meta']['verified_green']}"
          f"   recorded_at = {data['_meta']['recorded_at']}")
    if not data["_meta"]["verified_green"]:
        print()
        print("   ⚠️ 本次**未**声明「记录时全套测试通过」。")
        print("      确认绿之后建议重新执行并加上 --verified：")
        print("        python tools/eval_persona.py        # 须 exit 0")
        print("        python tools/qa_fingerprint.py --record --verified")
    else:
        print("   ⚠️ 记录基线等于声明「当前状态是已知良好态」。")
        print("      基线入库后，任何人改动它都会产生 diff —— 这正是它该受 review 的原因。")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--record" in argv:
        return record()
    code, detail = evaluate()
    if "--json" in argv:
        print(json.dumps(detail, ensure_ascii=False, indent=2))
    else:
        print(render(detail))
    return code


if __name__ == "__main__":
    raise SystemExit(main())