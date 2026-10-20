"""运行库（SQLite）默认位置的**唯一裁决处** —— 纯函数，无副作用、无日志。

为什么单独一个模块（而不是写在 ``config.py`` 里）
--------------------------------------------------
1. **纯函数才能在冒烟里进程内直调**。本项目的服务端是 subprocess，测试进程里
   打桩无效，所以「路径解析」必须在**同一进程**里被喂各种输入
   （``tools/smoke_db_path.py``）。逻辑一旦长在 ``config.py`` 的 import 期代码里，
   就只能靠「改环境变量 + 起子进程 + 看落在哪」来观测，那是最容易假绿的一种形态。
2. **判据不能依赖它正在检验的那个机制**：解析结果做成**返回值**（``Resolution``），
   日志只是它的渲染。若把「选了哪条分支」写进日志再由测试去 grep，改一次文案
   就假红/假绿。
3. 与仓库既有形态一致：``app/macs.py`` 的 MAC 归一化、``firmware_meta`` 的
   OTA 段裁决都走「纯函数 + 唯一真源」。

契约（改这里之前先读 ``deliverables/sprint-2026-10-20/design-p2.md`` §B.2）
-----------------------------------------------------------------------
* ``resolve()`` **不读** ``os.environ``（由调用方传入 ``env``）；
* ``resolve()`` **不写盘**（建目录由调用方显式调 ``ensure_dir()`` ——
  CI 与本机走不同分支比假绿更坏，见 §B.6）；
* ``resolve()`` **不 raise**：两个都有数据的库并存时以 ``conflict`` 返回值表达，
  由调用方决定「拒启动」（fail-closed）。

「有数据」的判据是 **``is_file() and st_size > 0``**，不是 ``exists()``。
本仓库真实存在 4 个 0 字节的 ``.db``（``elder.db`` / ``_lead_check.db`` /
``_uitest.db`` / ``server/_uitest.db``）。用 ``exists()`` 判据会让**每个**路径都被
判成「有旧库」⇒ 新逻辑永不生效，且 ``conflict`` 恒真 ⇒ 服务永远起不来。
0 字节文件里没有任何数据，忽略它没有任何信息损失。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

__all__ = [
    "APP_DIR_NAME",
    "CONTAINER_DATA_DIR",
    "DB_FILENAME",
    "Resolution",
    "default_data_dir",
    "ensure_dir",
    "has_data",
    "resolve",
    "sqlite_path_of",
]

#: 库文件名。全仓唯一真源 —— 不要在别处再拼一遍 "elder.db"。
DB_FILENAME = "elder.db"

#: 非容器形态下的应用数据目录名（Windows / macOS / Linux 通用）。
APP_DIR_NAME = "voice-avatar-box"

#: 容器形态的持久化目录（``docker-compose.yml`` 把命名卷 ``elder-data`` 挂到
#: ``/data``）。**逐字不变**：改了它会直接改变生产容器的库位置。
CONTAINER_DATA_DIR = Path("/data")

#: 只有这两种来源需要（也才允许）在磁盘上建目录：
#:   * ``data_dir``     —— 显式 ``ELDER_DATA_DIR``，建不出来就启动失败（不回落）；
#:   * ``new_default``  —— 全新机器，新默认目录还不存在。
#: ``legacy_*`` 与 ``data_dir_default`` 的目录本来就存在；``explicit`` 更不该被碰
#: （它的父目录兜底在 ``db.py::_ensure_sqlite_parent_dir``）。
_CREATE_SOURCES = ("data_dir", "new_default")

#: 旧库候选的固定顺序与标签。顺序不能改：``cwd`` 优先于 ``server_dir`` 是历史
#: 行为（``cd server && uvicorn`` 时两者同路径，见 ``_dedup``）。
_LEGACY_TAGS = ("legacy_cwd", "legacy_server")


@dataclass(frozen=True)
class Resolution:
    """一次默认库路径解析的**完整结论**（调用方只许读这些字段）。

    Attributes:
        url: 可直接交给 ``create_engine`` 的 SQLAlchemy 连接串。
        path: SQLite 库文件路径；非 sqlite 连接串（如 MySQL）时为 ``None``。
        source: 这个结论是哪条分支给的，取值见 ``SOURCE_*`` 常量。
        conflict: 非空 = **两个都有数据的库并存**，元素是新默认路径 + 命中的旧库
            路径。调用方必须拒启动，不许替用户猜（猜错的后果是「服务起来了但
            数据是空的」，且运行期零报错）。
        legacy_seen: 本次**探测过**的旧库路径（去重后，顺序固定）。
            用来在日志里说清「我看过哪些地方」，即使最终没选它。
    """

    url: str
    path: Path | None
    source: str
    conflict: tuple[Path, ...] = field(default=())
    legacy_seen: tuple[Path, ...] = field(default=())


# ``source`` 的全部取值。写成模块级常量而不是裸字符串，避免各处拼错后
# 「断言恒绿」（拼错的 tag 与拼对的一样能通过 in 判断，但它们不相等）。
SOURCE_EXPLICIT = "explicit"
SOURCE_DATA_DIR = "data_dir"
SOURCE_DATA_DIR_DEFAULT = "data_dir_default"
SOURCE_LEGACY_CWD = "legacy_cwd"
SOURCE_LEGACY_SERVER = "legacy_server"
SOURCE_NEW_DEFAULT = "new_default"


# ---------------------------------------------------------------- 小工具
def _clean(value: str | None) -> str:
    """``None`` / 空白串统一成空串；其余去首尾空白。"""
    return (value or "").strip()


def _as_abs(path: Path, base: Path) -> Path:
    """相对路径按 ``base`` 展开成绝对路径（**不**访问文件系统，不 ``resolve()``）。

    为什么不用 ``Path.resolve()``：它会做符号链接解析，在 Windows 上还会把
    8.3 短名展开成长名 —— 那会让「返回值 == 输入」这类断言莫名其妙地变红。
    """
    return path if path.is_absolute() else (base / path)


def _dedup_key(path: Path) -> str:
    """去重/比较用的路径键：绝对化 + 大小写归一（Windows 上 ``G:\\a`` 与 ``g:/a``
    是同一个文件，不做归一会把「自己和自己」判成两个候选）。"""
    return os.path.normcase(os.path.abspath(str(path)))


def _dedup(pairs: Sequence[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """按路径去重，**保留第一次出现的顺序与标签**。

    ``cwd == server_dir`` 是常态（``cd server && uvicorn app.main:app``），
    不去重会让 ``legacy_seen`` 出现两份、``conflict`` 判据把自己和自己比出
    「两个库并存」⇒ 服务起不来。
    """
    seen: dict[str, tuple[Path, str]] = {}
    for path, tag in pairs:
        key = _dedup_key(path)
        if key not in seen:
            seen[key] = (path, tag)
    return list(seen.values())


def has_data(path: Path) -> bool:
    """这个文件算不算「有数据的库」：**必须是普通文件且大小 > 0**。

    ⚠️ 别改成 ``exists()`` —— 见模块 docstring 最后一段（0 字节 ≠ 没有文件）。
    """
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        # 权限不足 / 路径过长等：按「没有数据」处理。理由：这个判据只影响
        # 「选哪条分支」，判错最多是回到「用新默认目录」；而在这里抛异常会让
        # **服务起不来**，代价与收益完全不对等。
        return False


def _url_for(path: Path) -> str:
    """把库文件路径拼成 SQLAlchemy 的 SQLite 连接串。

    四条斜杠的来历：``sqlite:///`` 是 scheme 分隔（三条）+ ``/data/...`` 的
    绝对路径首斜杠 ⇒ ``sqlite:////data/elder.db``。写成三条就是相对路径，
    容器里会悄悄落到 ``/app`` 的可写层（重建即丢）。这条被 CI 的
    ``make_url()`` 步骤与冒烟 B2 双重守着。
    """
    return f"sqlite:///{path.as_posix()}"


def sqlite_path_of(url: str, *, cwd: Path | None = None) -> Path | None:
    """从连接串里取出 SQLite 库文件路径；非 sqlite / 内存库返回 ``None``。

    刻意**不用** ``urllib.parse.urlparse``：``sqlite:///C:/a/b.db`` 经它解析后
    path 是 ``/C:/a/b.db``，``Path()`` 会解释成 ``\\C:\\a\\b.db``，再 mkdir 直接
    抛 ``WinError 123``（``db.py`` 里踩过同一个坑，那里改用 SQLAlchemy 的
    ``make_url()``；本模块是叶子模块，不引 SQLAlchemy，故自己按 scheme 切）。
    """
    if not url:
        return None
    sep = url.find("://")
    if sep < 0:
        return None
    if not url[:sep].lower().startswith("sqlite"):
        return None
    rest = url[sep + 3:]
    # 查询串 / 片段不是路径的一部分（VACUUM / mode=rwc 之类都可能出现在这里）
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    # 四条斜杠 = 绝对路径：切掉一条，剩下的首斜杠才是路径自己的
    if rest.startswith("/"):
        rest = rest[1:]
    if not rest or rest == ":memory:" or rest.startswith("file::memory:"):
        return None
    path = Path(rest)
    if cwd is not None:
        path = _as_abs(path, cwd)
    return path


def default_data_dir(
    *,
    env: Mapping[str, str],
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """非容器形态下的默认应用数据目录（**不建目录**，只算路径）。

    刻意不引 ``platformdirs``（``requirements.txt`` 里没有，为三个平台加一个
    依赖不值当）；三个平台的分支加起来不到 20 行。

    Args:
        env: 环境变量映射（由调用方传入，本函数不读 ``os.environ``）。
        platform: ``sys.platform`` 形态的值；``None`` 时取当前进程的。
        home: 用户主目录；``None`` 时取 ``Path.home()``。

    Returns:
        应用数据目录（尚未创建）。
    """
    plat = (platform or sys.platform).lower()
    base_home = Path(home) if home is not None else Path.home()

    if plat.startswith("win"):
        # %LOCALAPPDATA% 优先（本机私有数据不随漫游配置同步），
        # 拿不到再退 %APPDATA%，最后按 Windows 的固定布局猜一个。
        root = _clean(env.get("LOCALAPPDATA")) or _clean(env.get("APPDATA"))
        base = Path(root) if root else (base_home / "AppData" / "Local")
        return base / APP_DIR_NAME

    if plat.startswith("darwin"):
        return base_home / "Library" / "Application Support" / APP_DIR_NAME

    # Linux / 其他 POSIX：遵守 XDG 规范（相对路径的 XDG_DATA_HOME 按规范**忽略**）
    xdg = _clean(env.get("XDG_DATA_HOME"))
    if xdg:
        xdg_path = Path(xdg)
        if xdg_path.is_absolute():
            return xdg_path / APP_DIR_NAME
    return base_home / ".local" / "share" / APP_DIR_NAME


# ---------------------------------------------------------------- 主解析
def resolve(
    *,
    env: Mapping[str, str],
    cwd: Path,
    server_dir: Path,
    platform: str | None = None,
    home: Path | None = None,
    data_dir: Path | None = None,
) -> Resolution:
    """按固定优先级解析出「这次该用哪个库」。

    分支顺序**就是优先级**（``design-p2`` §B.2 的分支表，改顺序前先读那张表）::

        1. ELDER_DB 非空          -> 原样使用（source="explicit"）
        2. ELDER_DB 空串/未设      -> 继续往下（不是空连接串）
        3. ELDER_DATA_DIR 非空     -> 用它（source="data_dir"）
        4. data_dir 已存在且可写   -> data_dir/elder.db（source="data_dir_default"）
        5. 新默认有数据 且 旧库有数据 -> conflict 非空（调用方拒启动）
        6. 新默认有数据            -> 用它（source="new_default"）
        7. 某个旧库有数据          -> 用它，**不迁移**（source="legacy_cwd"/"legacy_server"）
        8. 都没有                  -> 新默认目录（source="new_default"）

    第 1~4 条都在探测旧库之前**直接返回**，这是刻意的：容器（第 4 条）与显式
    配置（第 1、3 条）的行为必须与历史逐字一致，不能让「仓库里恰好有个旧库」
    改变生产容器的落点。

    Args:
        env: 环境变量映射（``os.environ`` 或测试构造的 dict）。
        cwd: 进程当前工作目录（旧库候选之一、相对 ``ELDER_DATA_DIR`` 的基准）。
        server_dir: ``server/`` 目录（旧库候选之二）。
        platform: 见 ``default_data_dir``。
        home: 见 ``default_data_dir``。
        data_dir: 容器持久化目录的候选，默认 ``/data``。**冒烟脚本注入假路径**，
            不依赖 runner 上真实的 ``/data`` 是否存在 —— 否则 Ubuntu runner
            （``/`` 可写）与本机（Windows）会走不同分支，各自自洽，比假绿更坏。

    Returns:
        ``Resolution``。``conflict`` 非空时 ``url``/``path`` 只是「候选」，
        调用方必须拒启动，不许静默采用。
    """
    env = env or {}
    # 先把两个基准目录绝对化：后面所有路径都以它们为锚点拼接，
    # 若它们本身是相对路径（例如直接传 Path("server")），拼出来的结果会再被
    # ``_as_abs`` 叠一层基准，变成 ``server/server/elder.db`` 这种鬼路径。
    base_cwd = Path(os.path.abspath(str(cwd)))
    base_server = Path(os.path.abspath(str(server_dir)))
    container_dir = Path(data_dir) if data_dir is not None else CONTAINER_DATA_DIR

    # ---- 1/2. 显式 ELDER_DB：**逐字**使用，不做任何加工（不 resolve、不拼路径）
    explicit = _clean(env.get("ELDER_DB"))
    if explicit:
        return Resolution(
            url=explicit,
            path=sqlite_path_of(explicit, cwd=base_cwd),
            source=SOURCE_EXPLICIT,
        )

    # 旧库候选：顺序固定（cwd 先、server 后），去重但保留首次出现的标签
    legacy = _dedup([
        (_as_abs(base_cwd / DB_FILENAME, base_cwd), _LEGACY_TAGS[0]),
        (_as_abs(base_server / DB_FILENAME, base_cwd), _LEGACY_TAGS[1]),
    ])
    legacy_seen = tuple(path for path, _ in legacy)

    # ---- 3. ELDER_DATA_DIR 显式：用它，**建不出来就抛给调用方，不回落**
    # 历史行为是「不存在就静默退回 CWD」⇒ 又写进仓库树，等于本特性失效。
    env_dir = _clean(env.get("ELDER_DATA_DIR"))
    if env_dir:
        directory = _as_abs(Path(env_dir), base_cwd)
        target = directory / DB_FILENAME
        return Resolution(
            url=_url_for(target),
            path=target,
            source=SOURCE_DATA_DIR,
            legacy_seen=legacy_seen,
        )

    # ---- 4. 容器形态：/data 已存在且可写 ⇒ 逐字不变的老行为。
    # 判据**只读不建**：绝不 mkdir /data（CI runner 上 / 可写，建出来就会让
    # CI 与本机走不同分支）。
    try:
        container_ok = container_dir.is_dir() and os.access(container_dir, os.W_OK)
    except OSError:
        container_ok = False
    if container_ok:
        target = container_dir / DB_FILENAME
        return Resolution(
            url=_url_for(target),
            path=target,
            source=SOURCE_DATA_DIR_DEFAULT,
            legacy_seen=legacy_seen,
        )

    # ---- 5~8. 新默认目录 vs 仓库里的旧库
    new_dir = default_data_dir(env=env, platform=platform, home=home)
    new_db = _as_abs(new_dir / DB_FILENAME, base_cwd)
    new_has_data = has_data(new_db)
    legacy_hit = [(path, tag) for path, tag in legacy if has_data(path)]

    if new_has_data and legacy_hit:
        # 两份真源比没有更危险：不替用户猜「哪个更新」。猜错的后果是
        # 「服务起来了但数据是空的」，且运行期零报错。
        return Resolution(
            url=_url_for(new_db),
            path=new_db,
            source=SOURCE_NEW_DEFAULT,
            conflict=(new_db, *(path for path, _ in legacy_hit)),
            legacy_seen=legacy_seen,
        )

    if new_has_data:
        return Resolution(
            url=_url_for(new_db),
            path=new_db,
            source=SOURCE_NEW_DEFAULT,
            legacy_seen=legacy_seen,
        )

    if legacy_hit:
        # **沿用而不迁移**（design-p2 §B.4）：启动期无法可靠判断另一个进程是否
        # 正在写这个库；半搬一半比不搬更糟，而且「自动迁移」会让回滚不再免费。
        path, tag = legacy_hit[0]
        return Resolution(
            url=_url_for(path),
            path=path,
            source=tag,
            legacy_seen=legacy_seen,
        )

    # 全新机器：用新默认目录（目录由 ensure_dir 建出）
    return Resolution(
        url=_url_for(new_db),
        path=new_db,
        source=SOURCE_NEW_DEFAULT,
        legacy_seen=legacy_seen,
    )


def ensure_dir(resolution: Resolution) -> Path | None:
    """按解析结论建出库文件所在目录（**唯一**会写盘的地方）。

    Args:
        resolution: ``resolve()`` 的结论。

    Returns:
        实际创建/校验过的目录；不需要（或不允许）动文件系统时返回 ``None``。

    Raises:
        OSError: 目录建不出来。调用方必须把它变成「启动失败」，**不许回落**到
            当前工作目录 —— 静默回落正是「运行库又写进仓库树」的成因。
    """
    if resolution.path is None:
        return None
    if resolution.source not in _CREATE_SOURCES:
        return None
    if resolution.conflict:
        # 冲突场景调用方会直接拒启动，此时建目录只是留垃圾
        return None
    directory = resolution.path.parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory
