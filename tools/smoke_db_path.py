"""默认数据库路径解析的冒烟守护（**纯函数直调，不起服务、不建 uvicorn**）。

为什么是纯函数直调
------------------
本项目的服务端是 subprocess，测试进程里打桩无效 —— 「路径解析」如果只能通过
「改环境变量 + 起子进程 + 看库落在哪」来观测，那它天然假绿：起不来的原因太多，
而「落在哪」要靠人去猜。所以 `app/dbpath.py` 被刻意写成纯函数，这里直接用各种
输入喂它，观测 `Resolution` 的**返回值**（不是日志文本）。

覆盖（对应 design-p2 §B.6 的 B1~B8，另加三条本条文件的补充断言）
--------------------------------------------------------------
  B1  显式 ELDER_DB ⇒ source=="explicit"，url **逐字**等于输入
  B1b 显式 ELDER_DB 时**不去碰** ELDER_DATA_DIR（不建目录）⇒ 证明 explicit 分支是早返回
  B2  容器目录可用 ⇒ source=="data_dir_default"，连接串能被 make_url 还原成同一路径
  B3  只有 cwd/elder.db 有数据 ⇒ source=="legacy_cwd"，且**新目录不被创建**
  B4  只有 server_dir/elder.db 有数据 ⇒ source=="legacy_server"
  B5  legacy 是 **0 字节** ⇒ source=="new_default"（「有数据」判据的哨兵）
  B6  新库与 legacy 都有数据 ⇒ conflict 含两个路径，且**真的起不来**（SystemExit）
  B7  默认目录**不在**仓库树内，且能被 ELDER_DATA_DIR 覆盖（双向）
  B8  ELDER_DATA_DIR 指向不存在目录 ⇒ 建出来并用它（不回落 CWD）
  B9  cwd == server_dir（`cd server && uvicorn` 的常态）⇒ 候选去重，不自己和自己冲突

⚠️ 三条最容易做错的地方，写在这里防下一个人「顺手修好」
-------------------------------------------------------
1. **「有数据」是 `st_size > 0`，不是 `exists()`**。本仓库真实存在 4 个 0 字节的
   `.db`；改成 `exists()` 会让新逻辑永不生效、conflict 恒真、服务永远起不来。
   B5 就是这条的哨兵（把 `>` 改成 `>=` 必须让它变红）。
2. **`/data` 的判据只读不建**，而且 B2 用**注入的假路径**测 —— 绝不依赖 runner 上
   真实的 `/data` 是否存在。否则 Ubuntu runner（`/` 可写）与本机（Windows）会走
   不同分支、各自自洽，那比假绿更坏。
3. **平台与 home 一律注入固定值**（`platform="linux"` + 临时 home），B3/B5 才会在
   CI 与本机走**同一条**分支。

用法：
    python tools/smoke_db_path.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

# 让本脚本能 import app.dbpath（app/__init__.py 只有一句 docstring，不会连带
# import config，所以这一步不会触发任何启动自检 / 数据库连接）。
sys.path.insert(0, str(SERVER))

from app import dbpath  # noqa: E402

# 注入用的固定平台：B3/B5 期望的新默认目录必须与本机操作系统无关，
# 否则 CI（Linux）与本机（Windows）会走不同分支，两份报告各自"自洽"。
FIXED_PLATFORM = "linux"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记一条断言。**不抛异常**：一条红不该顶掉后面的断言。"""
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return bool(ok)


def same(a: Path | None, b: Path | None) -> bool:
    """路径等价比较：绝对化 + Windows 大小写归一。

    Windows 上 `Path("G:/x")` 与 `Path("G:\\x")` 字符串不同但是同一路径，
    直接用 `==` 比较会让断言在本地红、在 CI 绿（或反过来）。
    """
    if a is None or b is None:
        return False
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def touch_db(path: Path, size: int) -> Path:
    """造一个「库」文件：`size > 0` 表示有数据，`0` 表示空壳。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * max(0, size - 16) if size else b"")
    return path


def base_env(**overrides: str) -> dict[str, str]:
    """构造一份**干净的** env：不带 ELDER_DB / ELDER_DATA_DIR / LOCALAPPDATA。

    不继承 `os.environ` 是刻意的 —— CI 的 job 级 env 里有 ELDER_DB，
    继承进来会让 `explicit` 分支永远先命中，本组断言集体失效（而且是**绿**的）。
    """
    env: dict[str, str] = {}
    env.update(overrides)
    return env


def resolve_in(workspace: Path, env: dict[str, str], *, cwd: Path | None = None,
               server_dir: Path | None = None, data_dir: Path | None = None,
               home: Path | None = None) -> dbpath.Resolution:
    """在 workspace 里按固定 platform/home 解析一次。

    ⚠️ `data_dir` **默认注入一个不存在的假目录**，绝不落到真实的 `/data`：
    Ubuntu runner 上 `/` 可写，若真有人建过 `/data`，本组断言会在 CI 上走
    `data_dir_default` 分支、在本机走 `new_default` —— 两条分支各自"自洽"，
    比假绿更坏。要用容器分支的断言（B2）必须**自己显式注入**。
    """
    return dbpath.resolve(
        env=env,
        cwd=cwd if cwd is not None else workspace / "cwd",
        server_dir=server_dir if server_dir is not None else workspace / "server",
        platform=FIXED_PLATFORM,
        home=home if home is not None else workspace / "home",
        data_dir=data_dir if data_dir is not None else workspace / "container-absent",
    )


def new_default_for(workspace: Path) -> Path:
    """与 `resolve_in` 同参数下的新默认库路径（硬编码推导，不从被测实现抄）。"""
    return workspace / "home" / ".local" / "share" / dbpath.APP_DIR_NAME / dbpath.DB_FILENAME


# ---------------------------------------------------------------- B1 / B1b
def t_explicit(workspace: Path) -> None:
    """B1：显式 ELDER_DB **逐字**使用；B1b：早返回、不碰其它目录。"""
    raw = "sqlite:///somewhere/elder.db"
    res = resolve_in(workspace, base_env(ELDER_DB=raw))
    check("B1 显式 ELDER_DB 的 url 逐字等于输入（不加工、不 resolve）",
          res.url == raw, f"got={res.url!r}")
    check("B1 显式 ELDER_DB 的 source 为 explicit",
          res.source == dbpath.SOURCE_EXPLICIT, f"got={res.source!r}")
    check("B1 显式 ELDER_DB 不产生 conflict",
          res.conflict == (), f"got={res.conflict!r}")

    want_dir = workspace / "explicit-must-not-be-created"
    res2 = resolve_in(
        workspace,
        base_env(ELDER_DB=raw, ELDER_DATA_DIR=str(want_dir / "sub")),
    )
    check("B1b 显式 ELDER_DB 时不去建 ELDER_DATA_DIR 目录（explicit 分支必须早返回）",
          res2.url == raw and not (want_dir / "sub").exists(),
          f"url={res2.url!r} created={(want_dir / 'sub').exists()}")

    # 空串必须**回落**到默认解析（今天的行为是拿到空连接串、启动崩）
    res3 = resolve_in(workspace, base_env(ELDER_DB="   "))
    check("B1b ELDER_DB 为空白串 ⇒ 回落到默认解析（不是空连接串）",
          res3.source != dbpath.SOURCE_EXPLICIT and bool(res3.url.strip()),
          f"source={res3.source!r} url={res3.url!r}")


# ---------------------------------------------------------------- B2
def t_container_dir(workspace: Path) -> None:
    """B2：容器形态（注入的假 /data）⇒ data_dir_default，且连接串可被还原。"""
    fake_data = workspace / "fake-data"
    fake_data.mkdir(parents=True, exist_ok=True)
    res = resolve_in(workspace, base_env(), data_dir=fake_data)
    expected = fake_data / dbpath.DB_FILENAME
    check("B2 容器目录可写 ⇒ source 为 data_dir_default",
          res.source == dbpath.SOURCE_DATA_DIR_DEFAULT, f"got={res.source!r}")
    check("B2 容器形态的库路径是 <data_dir>/elder.db",
          same(res.path, expected), f"got={res.path}")
    check("B2 连接串逐字是 sqlite:/// + posix 路径（斜杠数不能少）",
          res.url == f"sqlite:///{expected.as_posix()}", f"got={res.url!r}")

    # 用 SQLAlchemy 自己的 make_url 反解，钉死「三斜杠/四斜杠」这类抄错
    try:
        from sqlalchemy.engine import make_url

        got_db = make_url(res.url).database
        check("B2 make_url(url).database 还原成同一个文件路径",
              same(Path(got_db or ""), expected), f"got={got_db!r}")
    except Exception as exc:  # noqa: BLE001
        check("B2 make_url(url).database 还原成同一个文件路径", False,
              f"{type(exc).__name__}: {exc}")

    if os.name == "posix":
        check("B2 POSIX 下绝对路径必须是四条斜杠 sqlite:////...",
              res.url.startswith("sqlite:////"), f"got={res.url!r}")

    # 判据只读不建：容器目录**不存在**时必须**一路走到新默认目录**，
    # 而不是替它把 /data 造出来（CI runner 上 / 可写，造出来就分叉了）。
    absent = workspace / "absent-data"
    res2 = resolve_in(workspace, base_env(), data_dir=absent)
    check("B2 容器目录不存在 ⇒ 既不判成 data_dir_default，也不 mkdir 它",
          res2.source != dbpath.SOURCE_DATA_DIR_DEFAULT and not absent.exists(),
          f"source={res2.source!r} created={absent.exists()}")


# ---------------------------------------------------------------- B3 / B4
def t_legacy(workspace: Path) -> None:
    """B3：cwd 有旧库 ⇒ legacy_cwd 且新目录不建；B4：server 有旧库 ⇒ legacy_server。"""
    w1 = workspace / "b3"
    cwd, srv = w1 / "cwd", w1 / "server"
    touch_db(cwd / dbpath.DB_FILENAME, 4096)
    res = resolve_in(w1, base_env(), cwd=cwd, server_dir=srv)
    check("B3 只有 cwd 有旧库 ⇒ source 为 legacy_cwd",
          res.source == dbpath.SOURCE_LEGACY_CWD, f"got={res.source!r}")
    check("B3 legacy_cwd 的路径 == cwd/elder.db",
          same(res.path, cwd / dbpath.DB_FILENAME), f"got={res.path}")
    check("B3 走 legacy 时**不建**新默认目录（否则等于偷偷搬家）",
          not new_default_for(w1).exists(), f"got={new_default_for(w1)}")

    w2 = workspace / "b4"
    cwd2, srv2 = w2 / "cwd", w2 / "server"
    srv2.mkdir(parents=True, exist_ok=True)
    touch_db(srv2 / dbpath.DB_FILENAME, 4096)
    res2 = resolve_in(w2, base_env(), cwd=cwd2, server_dir=srv2)
    check("B4 只有 server 有旧库 ⇒ source 为 legacy_server（tag 与路径必须同时对上）",
          res2.source == dbpath.SOURCE_LEGACY_SERVER
          and same(res2.path, srv2 / dbpath.DB_FILENAME),
          f"source={res2.source!r} path={res2.path}")


# ---------------------------------------------------------------- B5
def t_zero_byte(workspace: Path) -> None:
    """B5：0 字节的旧库**不算**「有数据」——本组最重要的一条。"""
    w = workspace / "b5"
    cwd, srv = w / "cwd", w / "server"
    touch_db(cwd / dbpath.DB_FILENAME, 0)
    touch_db(srv / dbpath.DB_FILENAME, 0)
    res = resolve_in(w, base_env(), cwd=cwd, server_dir=srv)
    check("B5 旧库都是 0 字节 ⇒ source 为 new_default（0 字节不等于有数据）",
          res.source == dbpath.SOURCE_NEW_DEFAULT, f"got={res.source!r}")
    check("B5 0 字节场景下选中的是新默认目录",
          same(res.path, new_default_for(w)), f"got={res.path}")
    check("B5 但两个 0 字节文件仍然出现在 legacy_seen 里（说明探测过，只是不算数）",
          len(res.legacy_seen) == 2, f"got={res.legacy_seen!r}")


# ---------------------------------------------------------------- B6
def t_conflict(workspace: Path) -> None:
    """B6：两个都有数据的库并存 ⇒ conflict 非空，且**真的起不来**。"""
    w = workspace / "b6"
    cwd, srv = w / "cwd", w / "server"
    touch_db(new_default_for(w), 4096)
    touch_db(cwd / dbpath.DB_FILENAME, 4096)
    srv.mkdir(parents=True, exist_ok=True)

    res = resolve_in(w, base_env(), cwd=cwd, server_dir=srv)
    check("B6 新库与 legacy 都有数据 ⇒ conflict 含新默认路径与 cwd 路径",
          same(new_default_for(w), res.conflict[0] if res.conflict else None)
          and any(same(p, cwd / dbpath.DB_FILENAME) for p in res.conflict),
          f"got={res.conflict!r}")

    # 真的起一遍 config（**不是** uvicorn，只是 import），确认是 SystemExit 而非
    # 静默采用。子进程的退出码与 stderr 是「服务起不来」的唯一可靠观测点。
    # ⚠️ 「新默认目录」在三套平台布局上都埋一份，并把 HOME / XDG_DATA_HOME /
    # LOCALAPPDATA 全部指到同一个根 —— 否则这条断言在 CI（Linux）与本机（Windows）
    # 会走不同分支，各自自洽。
    planted = w / "newroot"
    for candidate in (
        planted / "voice-avatar-box",
        planted / "Library" / "Application Support" / "voice-avatar-box",
        planted / "AppData" / "Local" / "voice-avatar-box",
    ):
        touch_db(candidate / dbpath.DB_FILENAME, 4096)
    env = {k: v for k, v in os.environ.items()
           if k not in ("ELDER_DB", "ELDER_DATA_DIR", "LOCALAPPDATA", "APPDATA",
                        "XDG_DATA_HOME", "HOME", "USERPROFILE")}
    env["LOCALAPPDATA"] = str(planted)
    env["XDG_DATA_HOME"] = str(planted)
    env["HOME"] = str(planted)
    env["USERPROFILE"] = str(planted)
    env["PYTHONPATH"] = str(SERVER)
    env["PYTHONIOENCODING"] = "utf-8"

    # 先在同一套输入上**预测**一次（platform 用真实值、home 显式传 planted，
    # 三个平台布局都与子进程一致），再拿子进程的实际结果去比。
    # 这样断言既不依赖 runner 上有没有 /data（有 /data 时容器分支优先，
    # 冲突分支根本不适用），又仍然能抓住「dbpath 说冲突、config 却不理」：
    # 那种情况下预测=冲突、子进程 rc=0 ⇒ 红。
    prediction = dbpath.resolve(
        env=env, cwd=cwd, server_dir=SERVER, home=planted,
    )
    expect_abort = bool(prediction.conflict)
    try:
        proc = subprocess.run(
            [PY, "-c", "import app.config"],
            cwd=str(cwd), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        first = next((ln for ln in (proc.stderr or "").splitlines() if ln.strip()), "")
        if expect_abort:
            check("B6 dbpath 判定双库冲突 ⇒ import config 以非 0 退出（不静默挑一个）",
                  proc.returncode != 0, f"rc={proc.returncode} first={first[:60]!r}")
            check("B6 失败文案首行是【启动失败】并把库路径打出来",
                  first.startswith("【启动失败】") and "elder.db" in out,
                  f"first={first[:80]!r}")
        else:
            # 预测没有冲突（本机 /data 可用时容器分支优先，冲突分支根本不适用）
            # ⇒ 服务必须正常起来。「无冲突也拒启动」同样是事故形态，所以这一半
            # 不是"跳过"，而是一条反向断言。
            check("B6 无冲突时 import config 必须成功（不许把正常场景误判成冲突）",
                  proc.returncode == 0, f"rc={proc.returncode} {first[:120]!r}")
            check("B6 无冲突时不得出现【启动失败】（否则等于全站起不来）",
                  not first.startswith("【启动失败】"), f"first={first[:80]!r}")
    except Exception as exc:  # noqa: BLE001
        check("B6 双库冲突场景的 import config 行为符合预期", False,
              f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- B7
def t_no_repo_tree(workspace: Path) -> None:
    """B7（双向）：默认目录不在仓库树内 **且** 能被 ELDER_DATA_DIR 覆盖。

    这是**面向目的**的断言：防将来有人「为了简单」把默认值改回 `./elder.db`。
    只做前一半不够 —— 覆盖点被删掉时前一半照样绿。
    """
    real_default = dbpath.default_data_dir(env=os.environ)
    resolved_default = Path(os.path.abspath(str(real_default)))
    repo = Path(os.path.abspath(str(ROOT)))
    inside = (same(resolved_default, repo)
              or repo in resolved_default.parents
              or resolved_default in repo.parents)
    check("B7 本机真实默认目录**不在**仓库树内（父/子路径都不许）",
          not inside, f"default={resolved_default} repo={repo}")
    check("B7 默认目录名是 voice-avatar-box/elder.db 的父目录",
          resolved_default.name == dbpath.APP_DIR_NAME, f"got={resolved_default.name!r}")

    override = workspace / "b7-override"
    res = resolve_in(workspace, base_env(ELDER_DATA_DIR=str(override)))
    check("B7 ELDER_DATA_DIR 能覆盖默认目录（覆盖点没被删）",
          res.source == dbpath.SOURCE_DATA_DIR
          and same(res.path, override / dbpath.DB_FILENAME),
          f"source={res.source!r} path={res.path}")

    # 第二问的强化版：**在仓库根裸跑**（`uvicorn app.main:app` 的常见形态）时，
    # 若解析结论落到 new_default，它必须落在仓库之外。
    # `server_dir` 故意指向空目录，让「命中旧库」这条分支不成立 —— 命中旧库时
    # 这条断言不适用（沿用旧库是设计决定的，见 §B.4），故用条件式表达。
    empty_server = workspace / "b7-empty-server"
    repo_cwd = dbpath.resolve(
        env=base_env(), cwd=ROOT, server_dir=empty_server,
        platform=FIXED_PLATFORM, home=workspace / "b7-home",
        data_dir=workspace / "b7-absent-data",
    )
    picked = Path(os.path.abspath(str(repo_cwd.path))) if repo_cwd.path else repo_cwd.path
    inside_picked = bool(picked) and (
        same(picked, repo) or repo in picked.parents or picked in repo.parents)
    check("B7 在仓库根裸跑时，new_default 不会落回仓库树（防「默认值改回 ./elder.db」）",
          repo_cwd.source != dbpath.SOURCE_NEW_DEFAULT or not inside_picked,
          f"source={repo_cwd.source!r} path={picked}")


# ---------------------------------------------------------------- B8
def t_data_dir_created(workspace: Path) -> None:
    """B8：ELDER_DATA_DIR 指向不存在目录 ⇒ 建出来并用它，**不回落 CWD**。"""
    w = workspace / "b8"
    cwd = w / "cwd"
    # 故意在 cwd 放一个**有数据**的旧库：旧实现会「目录不存在就回落 CWD」，
    # 有了这个文件，回落与不回落的结果差别才可观测。
    touch_db(cwd / dbpath.DB_FILENAME, 4096)
    target = w / "does" / "not" / "exist"
    res = resolve_in(w, base_env(ELDER_DATA_DIR=str(target)), cwd=cwd)
    check("B8 显式 ELDER_DATA_DIR 优先于 CWD 里的旧库（不回落）",
          res.source == dbpath.SOURCE_DATA_DIR and same(res.path, target / dbpath.DB_FILENAME),
          f"source={res.source!r} path={res.path}")
    check("B8 resolve() 自己不写盘（目录此刻还不存在）", not target.exists(),
          f"got={target}")
    created = dbpath.ensure_dir(res)
    check("B8 ensure_dir() 把不存在的目录建出来",
          target.is_dir() and same(created, target), f"created={created}")

    # 不需要动文件系统的来源一律返回 None（legacy / 容器 / 显式 ELDER_DB）
    legacy_res = resolve_in(w, base_env(), cwd=cwd, server_dir=w / "server")
    check("B8 legacy 来源不触发 mkdir（ensure_dir 返回 None）",
          dbpath.ensure_dir(legacy_res) is None, f"source={legacy_res.source!r}")


# ---------------------------------------------------------------- B9
def t_dedup(workspace: Path) -> None:
    """B9：cwd == server_dir 时候选去重，不许自己和自己冲突。"""
    w = workspace / "b9"
    both = w / "same"
    touch_db(both / dbpath.DB_FILENAME, 4096)
    res = dbpath.resolve(
        env=base_env(),
        cwd=both,
        server_dir=both,
        platform=FIXED_PLATFORM,
        home=w / "home",
        data_dir=w / "no-such-data-dir",
    )
    check("B9 cwd == server_dir ⇒ legacy_seen 只有一份（去重生效）",
          len(res.legacy_seen) == 1, f"got={res.legacy_seen!r}")
    check("B9 cwd == server_dir ⇒ 不产生 conflict",
          res.conflict == (), f"got={res.conflict!r}")
    check("B9 cwd == server_dir ⇒ 仍然用这个库，source 为 legacy_cwd",
          res.source == dbpath.SOURCE_LEGACY_CWD and same(res.path, both / dbpath.DB_FILENAME),
          f"source={res.source!r} path={res.path}")


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="smoke_db_path_"))
    workspace = tmp_root / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        t_explicit(workspace)
        t_container_dir(workspace)
        t_legacy(workspace)
        t_zero_byte(workspace)
        t_conflict(workspace)
        t_no_repo_tree(workspace)
        t_data_dir_created(workspace)
        t_dedup(workspace)
    finally:
        # 清理是卫生问题，不该污染正确性结论（smoke_common 模块 docstring 缺陷 1）
        shutil.rmtree(tmp_root, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
