"""冒烟脚本的公共收尾 / 环境助手（**唯一真源**）。

为什么要有这个文件
------------------
``tools/`` 下的 ``smoke_*.py`` 是复制粘贴演化出来的：同一段「起服务 → 断言 →
收尾」被抄了十几份，于是**同一个缺陷也抄了十几份**。本模块把其中两处被反复
抄错、且症状极具误导性的逻辑收敛成一份，改一处即改全部。

## 缺陷 1：收尾删临时库在 Windows 上抛 WinError 32

原写法（多份脚本里的同一段）::

    finally:
        proc.terminate()
        proc.wait(timeout=10)
        if DB_PATH.exists():
            DB_PATH.unlink()          # ← PermissionError: [WinError 32]

现象：**断言全绿，脚本却 exit 1 + traceback**。
根因有三层，缺一层都解释不全：

1. ``terminate()`` 返回 ≠ 进程已释放文件句柄。Windows 上进程退出与句柄回收
   之间有窗口期，SQLite 的 ``.db`` / ``-journal`` 还挂在别的进程名下；
2. SQLite 写事务在**被杀掉**时不会走正常关闭路径，日志文件残留；
3. 本机（Windows）常有防病毒 / 索引器短暂持有刚写完的文件。

CI（Linux）不受影响 —— 所以这是「本地红、CI 绿」的典型，
最容易被误判成代码回归。

⚠️ **真正要紧的不是「删不掉」，而是「删不掉把结论打红了」**：
它写在 ``finally`` 里，异常会**顶掉 return 值**，于是一份 78 条断言全过的
报告变成 exit 1。清理是**卫生问题**，不该污染**正确性结论**。

## 缺陷 2：种子管理员口令不自足（本地裸跑必 401）

``bb5ca98`` 起种子口令不再硬编码：空库未设 ``ELDER_SEED_ADMIN_PASSWORD`` 时
生成**随机**口令（``secrets.token_urlsafe``）。而 5 个冒烟脚本（smoke_roles /
smoke_memory / smoke_models / smoke_quota / smoke_usage）仍然硬用
``admin/admin123`` 登录 —— 它们自己**不注入**这个变量，全靠 CI 的 job 级
``env:`` 兜住。后果：

* CI 绿、本地裸跑第一条断言就 401，后续拿不到 token 的用例连环 FAIL，
  最后往往在 ``r.get(...)`` 上崩成 ``AttributeError: 'str' object has no
  attribute 'get'``（因为 401 响应体是 dict，取不到字段后拼进了字符串路径）；
* 报告读起来像「权限系统坏了」，实际是**测试基础设施不自足**。

``seed_admin_env()`` 用 ``setdefault`` 语义：**不覆盖**调用方显式给的值
（CI 传什么就用什么，smoke_auth 的随机口令场景也不受影响）。

## 边界（务必知道，别把它当万能药）

本模块只做两件事：**收尾不污染结论** + **脚本自足**。
它**不**解决下面这些，它们各有归属：

* 端口被占 / 连到别人的服务 → ``tools/smoke_ports.py`` 的三段式守卫；
* 子进程日志写满 PIPE 导致服务端阻塞 → 各脚本自己的「输出落文件」约定；
* 假绿（断言恒真、跳过分支算通过）→ 各脚本自己的断言设计。

## 用法

::

    from smoke_common import seed_admin_env, cleanup_db

    env = seed_admin_env(dict(os.environ))
    ...
    finally:
        cleanup_db(DB_PATH, procs=(proc,), handles=(log_fp,))
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable

#: 与 smoke_auth / smoke_mail / smoke_home / smoke_admin_presets / smoke_server
#: 的 ``LEGACY_PW`` 同名同义。改一处请同步全部（grep LEGACY_PW / LEGACY_ADMIN_PASSWORD）。
LEGACY_ADMIN_PASSWORD = "admin123"

#: SQLite 的伴生文件：主库删了它们不删，下一次跑可能读到脏状态。
_DB_SIDECARS = ("-journal", "-wal", "-shm")


def seed_admin_env(env: dict, password: str = LEGACY_ADMIN_PASSWORD) -> dict:
    """确保 ``env`` 里有 ``ELDER_SEED_ADMIN_PASSWORD``，让脚本**自足**。

    用 ``setdefault``：调用方（CI 的 job 级 env、或显式传参）给了值就**不覆盖**。
    这一点很重要 —— ``smoke_auth.py`` 的「随机口令」场景依赖显式清空该变量，
    无条件写入会把那条断言变成永远走不到的死分支（假绿）。

    Args:
        env: 通常是 ``dict(os.environ)``；就地修改并返回同一个 dict。
        password: 缺省时使用的口令。

    Returns:
        同一个 ``env`` 对象（便于链式写 ``env = seed_admin_env(dict(os.environ))``）。
    """
    env.setdefault("ELDER_SEED_ADMIN_PASSWORD", password)
    return env


def stop_proc(proc, timeout: float = 10.0, label: str = "服务子进程") -> None:
    """终止子进程：``terminate`` → ``wait`` → 必要时 ``kill``，**永不抛异常**。

    收尾阶段抛异常会把「断言结论」顶掉（见模块 docstring），所以这里全部吞掉，
    只在真的杀不掉时打一行警告 —— 残留进程是环境问题，不是断言失败。
    """
    if proc is None:
        return
    try:
        if proc.poll() is not None:      # 已经退出，不用管
            return
    except Exception:                     # noqa: BLE001  进程对象可能已被替换成别的东西
        return
    try:
        proc.terminate()
    except Exception:                     # noqa: BLE001
        pass
    try:
        proc.wait(timeout=timeout)
        return
    except Exception:                     # noqa: BLE001  含 TimeoutExpired
        pass
    try:
        proc.kill()
        proc.wait(timeout=timeout)
        print(f"[cleanup] ⚠️ {label} 未响应 terminate，已 kill")
    except Exception:                     # noqa: BLE001
        print(f"[cleanup] ⚠️ {label} 既不能 terminate 也不能 kill（可能是僵尸句柄）")


def close_quietly(fp) -> None:
    """关文件句柄，**永不抛异常**（句柄可能已被子进程继承或已关闭）。"""
    if fp is None:
        return
    try:
        fp.close()
    except Exception:                     # noqa: BLE001
        pass


def unlink_retry(path, attempts: int = 12, delay: float = 0.2,
                 quiet: bool = False) -> bool:
    """带退避重试地删一个文件，**永不抛异常**。

    Windows 上 ``PermissionError: [WinError 32]`` 通常是**瞬时**的（句柄回收
    窗口 / 防病毒扫描），退避重试几乎总能成功；重试仍失败就只打警告、返回
    ``False`` —— 由调用方决定要不要在意。

    Args:
        path: 目标文件（``str`` / ``Path``）。
        attempts: 最多尝试次数。
        delay: 首次退避秒数，之后线性递增，单次上限 1.0s。
        quiet: 为 ``True`` 时连最终警告都不打。

    Returns:
        ``True`` 表示文件已不存在（删掉了或本来就没有）；``False`` 表示仍在。
    """
    target = Path(path)
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:            # PermissionError 是 OSError 的子类
            last_exc = exc
            time.sleep(min(1.0, delay * (i + 1)))
    if not quiet:
        print(f"[cleanup] ⚠️ 删不掉 {target}（{type(last_exc).__name__}: {last_exc}）"
              f" —— 已重试 {attempts} 次。**不影响断言结论**，"
              f"但会留下临时文件；下次跑若读到脏库请手动删掉。")
    return False


def cleanup_db(db_path, procs: Iterable = (), handles: Iterable = (),
               attempts: int = 12, keep: bool = False) -> bool:
    """冒烟脚本的**标准收尾**：停进程 → 关句柄 → 删临时库（含伴生文件）。

    顺序不能反：先停子进程，再删文件 —— 反了就是本模块要解决的那个
    ``WinError 32``。

    Args:
        db_path: 临时 SQLite 库路径（``None`` 表示不删）。
        procs: 需要终止的子进程（可多个，例如 upstream + server）。
        handles: 需要关闭的文件句柄（日志 fp 等）。
        attempts: 单文件重试次数。
        keep: 为 ``True`` 时**保留**临时库（排查现场用）。

    Returns:
        ``True`` 表示全部清理干净；``False`` 表示有文件残留（已打警告）。
    """
    for proc in procs:
        stop_proc(proc)
    for fp in handles:
        close_quietly(fp)
    if keep or db_path is None:
        return True

    ok = True
    for suffix in ("",) + _DB_SIDECARS:
        target = Path(f"{db_path}{suffix}")
        if not target.exists():
            continue
        ok = unlink_retry(target, attempts=attempts) and ok
    return ok
