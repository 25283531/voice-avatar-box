"""登录失败限速：5 秒冷却 / 每分钟 6 次 / 每小时 20 次。

需求来源（主理人 2026-10-17 拍板，三条规则**全部只针对登录失败**）：

    1. 密码错一次之后，该主体必须等 **5 秒** 才能再试，期间尝试直接拒绝；
    2. 每分钟最多 **6 次** 失败；
    3. 每小时最多 **20 次** 失败。

这个文件回答了四件事，改动前请先读，别把其中任何一条"顺手优化"掉。

================================================================ 一、限流的键
**同时按 IP 和按用户名限，请求必须同时过两道闸门**（先到先拦，取更长的等待时间）。

* **IP 键**：撞库的典型形态是**同一个来源**反复试（试同一个账号的口令、
  或拿一批口令扫一批用户名）。只按用户名限的话，换代理/换出口 IP 就绕过去了。
* **用户名键**：只按 IP 限的话，攻击者拿一堆 IP（僵尸网络 / 代理池）对
  ``admin`` 各试一次就能无限试下去。用户名键让"**针对某个账号**的总失败次数"
  在**跨 IP**时也封顶。

⚠️ 只按用户名限的经典事故：**攻击者把 admin 的 20 次/小时打满，真正的管理员
自己登不进去** —— 限速器变成 DoS 武器。本实现用下面这条规则把这个风险归零：

    ★ **口令正确的请求不过闸门**：先校验口令，对了就发证（并清掉该账号的
      失败记录），**根本不看配额**。配额只在"这次确实是失败"时才判定。

    于是"被锁死"这件事**只会作用在不知道口令的人身上** —— 他本来也登不进去。
    这同时保住了 ``tools/smoke_mail.py:458-461`` 那种「先故意输错一次、紧接着
    用新口令登录」的既有冒烟（改回"先查配额再验口令"会让它从 200 变 429）。

================================================================ 二、只在失败时记账
成功登录**不计入**任何配额（否则正常使用会被误伤）。被 **429 拒绝**的那一笔
也**不记账** —— 否则攻击者只要持续敲，冷却期就被无限续期，5 秒冷却会退化成
"永久锁死"，那正是上一条要消灭的形态。

================================================================ 三、三个窗口共一份逻辑
冷却本质上就是「窗口 = 5 秒、上限 = 1 次」，和「60 秒 / 6 次」「3600 秒 / 20 次」
是**同一种结构**。所以每个主体只存一份**失败时间戳队列**，三条规则是三个
``(窗口, 上限)``，用同一个判定函数跑一遍 —— 不是三份重复代码。

================================================================ 四、存在哪、怎么清理
**进程内存（不落库）**。理由：

* 限速是**速度挡板**不是审计：它只需要"最近一小时谁失败了几次"，
  精度要求是秒级，不需要持久化；服务重启后计数归零是可接受的
  （真要不可绕过，靠的是口令强度 + 不泄露用户名，不是靠这张表）。
* 落库会让**每一次失败都变成一次写**，等于给攻击者一条"用失败登录刷 I/O"
  的放大路径；而且那张表会随失败**持续增长**，必须再配一套清理任务 ——
  多一张表、多一条清理路径、多一处能失效的东西。
* 本镜像是**单进程 uvicorn**（server/Dockerfile 的 CMD 没有 ``--workers``），
  不存在"多 worker 不共享"的问题。

⚠️ 代价要写清楚：将来若改成多 worker / 多副本，**限额会变成每份各算一次**
（N 个副本 = 放宽到 N 倍）。那时应换成 Redis 之类的共享计数，别靠调大阈值糊过去。

内存增长的防线有三道，缺一不可：

1. 每次访问该键时，把滑出**最长窗口（1 小时）**的时间戳丢掉；
2. 时间戳空了的键直接删；
3. 键数量超过 ``ELDER_LOGIN_RATE_LIMIT_MAX_KEYS``（默认 10000）按 **LRU** 淘汰最久
   没动静的键 —— 挡住"拿一万个不存在的用户名各错一次"把 dict 撑爆。

并发安全：uvicorn 把同步端点丢进线程池跑，所以这里用 ``threading.Lock``
包住所有读写，时间戳用 ``time.monotonic``（不受系统改表影响）。
"""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from .config import (
    LOGIN_FAIL_COOLDOWN_SEC,
    LOGIN_FAIL_PER_HOUR,
    LOGIN_FAIL_PER_MINUTE,
    LOGIN_RATE_LIMIT_ENABLED,
    LOGIN_RATE_LIMIT_MAX_KEYS,
    TRUSTED_PROXY_IPS,
)

__all__ = [
    "RATE_LIMIT_DETAIL_PREFIX",
    "clear_login_failures_for",
    "client_ip",
    "login_gate",
    "record_login_failure",
]

# ============================================================ 规则表（统一结构）
#: 冷却 = 「窗口 5 秒、上限 1 次」，与另外两个窗口**同一种结构**。
_RULES: tuple[tuple[float, int], ...] = (
    (float(LOGIN_FAIL_COOLDOWN_SEC), 1),
    (60.0, LOGIN_FAIL_PER_MINUTE),
    (3600.0, LOGIN_FAIL_PER_HOUR),
)
#: 窗口 <= 0 或上限 <= 0 的规则视为"关掉"（配置文件里把某条设成 0 即停用该窗口）
_ACTIVE_RULES: tuple[tuple[float, int], ...] = tuple(
    r for r in _RULES if r[0] > 0 and r[1] > 0
)
#: 最长的窗口 —— 它就是"一条失败记录最长要保留多久"
_WINDOW_SEC: float = max((r[0] for r in _RULES), default=1.0)

#: 429 文案的前缀。冒烟据此判断"命中限速"而不必逐字匹配整句
#: （整句里含"还要等多久"，会随时间变）。
#: ⚠️ 这个串在 tools/smoke_auth.py 里有一份副本，改了那边会红。
RATE_LIMIT_DETAIL_PREFIX = "登录失败次数过多"


@dataclass(frozen=True)
class Verdict:
    """闸门判定结果。``retry_after`` 是还要等的秒数（向上取整，至少 1）。"""

    retry_after: int
    detail: str


def _human_wait(sec: int) -> str:
    """把"还要等多久"说成人话。

    只给秒数在小时窗口上会输出「请 3487 秒后再试」—— 老人用户看不懂，
    运维看日志也难判断严重性，所以按量级换成分钟 / 小时。
    """
    if sec < 60:
        return f"{sec} 秒"
    minutes = max(1, round(sec / 60))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


# ============================================================ 存储
class _FailureStore:
    """``主体 -> 失败时间戳队列``，带 LRU 上限与锁。"""

    def __init__(self, max_keys: int) -> None:
        self._lock = threading.Lock()
        self._max_keys = max(1, max_keys)
        self._items: "OrderedDict[str, deque[float]]" = OrderedDict()

    def blocked_for(self, key: str, now: float) -> float:
        """该主体还要等多少秒；``0`` = 没触发。

        **不记账** —— 被拦下的尝试不算一次失败，否则冷却期会被持续续期。
        """
        with self._lock:
            q = self._items.get(key)
            if not q:
                return 0.0
            self._prune(q, now)
            if not q:
                del self._items[key]
                return 0.0
            self._items.move_to_end(key)
            wait = 0.0
            for window, limit in _ACTIVE_RULES:
                if len(q) < limit:
                    continue
                # 队列升序；要等"第 (len-limit+1) 老"的那一笔滑出窗口
                wait = max(wait, q[len(q) - limit] + window - now)
            return wait if wait > 0 else 0.0

    def record(self, key: str, now: float) -> None:
        with self._lock:
            q = self._items.get(key)
            if q is None:
                q = deque()
                self._items[key] = q
            self._prune(q, now)
            q.append(now)
            self._items.move_to_end(key)
            while len(self._items) > self._max_keys:
                # LRU：淘汰最久没动静的键（不是"失败最多的键"——
                # 那会让攻击者靠刷别的键把正在被撞的那个键挤掉之后自己又进来）
                self._items.popitem(last=False)

    def clear(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    @staticmethod
    def _prune(q: "deque[float]", now: float) -> None:
        cut = now - _WINDOW_SEC
        while q and q[0] <= cut:
            q.popleft()


_STORE = _FailureStore(LOGIN_RATE_LIMIT_MAX_KEYS)


def _ip_key(ip: str) -> str:
    return "ip:" + ip


def _user_key(username: str) -> str:
    """用户名键做 ``casefold`` 归一。

    登录本身是**逐字**匹配用户名的，若不归一，攻击者拿 ``Admin`` / ``admin ``
    轮换着试就能每次都开一个新键，用户名闸门等于没装。归一后它们都算到
    ``admin`` 头上 —— 这里归一的是"限速键"，不影响鉴权口径。
    """
    return "u:" + (username or "").strip().casefold()


# ============================================================ 客户端 IP
def _norm_ip(raw: str) -> str:
    """归一化一个 IP 串；解析不了的（攻击者可以乱填）原样返回，绝不抛异常。"""
    text = (raw or "").strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return text
    mapped = getattr(addr, "ipv4_mapped", None)
    return str(mapped) if mapped else str(addr)


def _parse_trusted(raw: str) -> tuple[object, ...]:
    out: list[object] = []
    for part in (raw or "").split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.append(ipaddress.ip_network(p, strict=False))
        except ValueError:
            out.append(p)  # 非 IP/CIDR：留作逐字比较（例如主机名）
    return tuple(out)


_TRUSTED = _parse_trusted(TRUSTED_PROXY_IPS)


def _is_trusted(ip: str) -> bool:
    norm = _norm_ip(ip)
    for entry in _TRUSTED:
        if isinstance(entry, str):
            if norm == entry:
                return True
            continue
        try:
            if ipaddress.ip_address(norm) in entry:  # type: ignore[operator]
                return True
        except ValueError:
            continue
    return False


def client_ip(request) -> str:
    """取限速用的客户端 IP。

    ⚠️ **默认不信任 X-Forwarded-For**（``ELDER_TRUSTED_PROXY_IPS`` 为空时）。
    那个头是**请求方可以随便写**的，盲目信任等于让攻击者每次换个头就换一个
    主体 —— 限速器形同虚设。只有 TCP 对端本身在可信代理名单里时才解析它，
    并且**从右往左**扫：

        ``X-Forwarded-For: <攻击者伪造>, <真实客户端>, <可信代理>``

    每经过一跳可信代理就在右边追加一段，所以从右往左跳过所有可信代理后拿到的
    第一个不可信地址就是**真实客户端**；伪造的那几段在左边，正好被跳过了。
    """
    peer = ""
    try:
        peer = (request.client.host if request.client else "") or ""
    except Exception:  # noqa: BLE001 —— 取不到对端也不能让登录接口 500
        peer = ""
    peer = _norm_ip(peer) or "unknown"

    if not _TRUSTED or not _is_trusted(peer):
        # 直连 / 未声明可信代理：只认 TCP 对端（可被伪造不了）
        return peer

    raw = request.headers.get("x-forwarded-for", "") or ""
    chain = [p.strip() for p in raw.split(",") if p.strip()]
    for cand in reversed(chain):
        if not _is_trusted(cand):
            return _norm_ip(cand)
    return peer


# ============================================================ 对外的三个动作
def login_gate(request, username: str) -> Verdict | None:
    """失败路径的闸门。**成功登录不要调用它**（成功不过闸门）。"""
    if not LOGIN_RATE_LIMIT_ENABLED or not _ACTIVE_RULES:
        return None
    now = time.monotonic()
    wait_ip = _STORE.blocked_for(_ip_key(client_ip(request)), now)
    wait_user = _STORE.blocked_for(_user_key(username), now)
    wait = max(wait_ip, wait_user)
    if wait <= 0:
        return None
    # 向上取整，且至少 1 秒：否则 0.4 秒会被显示成"请 0 秒后再试"
    sec = max(1, int(wait + 0.999))
    return Verdict(sec, f"{RATE_LIMIT_DETAIL_PREFIX}，请 {_human_wait(sec)} 后再试")


def record_login_failure(request, username: str) -> None:
    """记一笔失败（IP 与用户名各一份）。只在**确实失败**时调用。"""
    if not LOGIN_RATE_LIMIT_ENABLED or not _ACTIVE_RULES:
        return
    now = time.monotonic()
    ip = client_ip(request)
    _STORE.record(_ip_key(ip), now)
    _STORE.record(_user_key(username), now)


def clear_login_failures_for(username: str) -> None:
    """登录成功时清掉**该账号**自己的失败记录。

    ⚠️ **刻意不清 IP 键**。IP 键描述的是"这个来源的行为"：若一次成功就把它清零，
    手里握着一个有效口令的人就能反复登录来给同一出口 IP 续配额，继续撞别的账号。
    而"清用户名键"不会削弱防护 —— 要拿到清除权就得先证明自己知道口令，
    那时候本来就已经进来了。

    ⚠️ 会不会让"撞库撞累了换一次对的"重置计数？会，但**没有安全代价**：
    能重置的前提就是口令正确，而口令一旦正确，配额挡不挡都已经没意义了。
    反过来看收益：老人在自己家 IP 上输错 5 次被限，下一秒想起来正确口令
    登进去，计数立刻归零 —— 这才是"限速器不误伤自己人"。
    """
    if not LOGIN_RATE_LIMIT_ENABLED:
        return
    _STORE.clear(_user_key(username))
