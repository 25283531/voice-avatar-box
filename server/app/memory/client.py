"""记忆与人格模块依赖的模型客户端（薄适配层）。

为什么要有这一层
----------------
记忆系统需要两件模型能力：把对话抽成结构化事实（chat），把文本变成向量（embed）。
真正的调用实现在 ``server/app/llm_gateway.py``（由模型网关模块负责，本模块不碰它）。

但直接 ``from .. import llm_gateway`` 会带来三个问题：

  1. **耦合时序**：那个文件可能还没写完 / 正在改，一 import 就把整个记忆模块拖挂，
     连「离线跑单元测试」都做不到；
  2. **测试不可注入**：冒烟测试需要一个假的模型（返回预设 JSON、返回确定性向量），
     硬依赖真实网关就没法离线验证；
  3. **替换成本**：将来换成别的调用层（本地 llama.cpp、批处理队列）要改所有调用点。

所以这里只定义一个**极薄的接口** ``ModelClient``，默认实现去桥接 llm_gateway，
并且允许外部用 ``set_default_client()`` 整个替换掉。桥接是**懒加载**的：
只有真正要发请求时才 import，失败时给一句人话错误，而不是抛一个看不懂的 ImportError。

期望 llm_gateway 提供的签名（与现有实现对齐，见本文件末尾的常量说明）：

    chat(kind, messages, **kw) -> str          # 返回纯文本
    embed(texts: list[str]) -> list[list[float]]

``kind`` 既接受用途名（``light`` / ``chat`` / ``embed``），也接受模型分层名
（``llm_light`` / ``llm_main`` / ``embedding``），本模块内部做一次映射，
这样将来网关改口径，这里改一行即可。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("elder.memory.client")

# 本模块用到的用途名。与 app/model_catalog.py 的 USAGE_* 保持一致，
# 单独在这里再写一遍是为了让记忆模块**不依赖**模型网关的内部常量，
# 网关改名时这里只动一行映射表。
USAGE_LIGHT = "light"    # 轻量任务：本模块只用于记忆事实抽取（不带 tools）
USAGE_CHAT = "chat"      # 正式对话
USAGE_EMBED = "embed"    # 文本向量化

# kind/usage 别名 -> 网关 usage。网关现在只认 usage（"light"/"chat"/"embed"），
# 而架构文档第 5 节对外承诺的是 kind（"llm_light"/"llm_main"/"embedding"），
# 两种都收，避免调用方为了适配去记两套名字。
_KIND_ALIASES: dict[str, str] = {
    "llm_light": USAGE_LIGHT,
    "llm_main": USAGE_CHAT,
    "llm": USAGE_CHAT,
    "embedding": USAGE_EMBED,
}


class ModelClientNotReady(RuntimeError):
    """模型网关不可用（未实现 / 未配置 / 导入失败）。

    刻意定义成专门的异常类型：调用方（记忆提取）可以精确地「吞掉它并降级为空结果」，
    而不是用 ``except Exception`` 把真正的 bug 也一起吞掉。
    """


# ---------------------------------------------------------------- 用量记账
def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _record(db: Any, *, usage: str, result: Any = None, ok: bool = True,
            error: str = "", elapsed_ms: int = 0,
            device_id: int | None = None, user_id: int | None = None) -> None:
    """给一次记忆侧的模型调用落一行 model_usage（source="memory"）。

    为什么记账写在这一层而不是 ``service.memorize``
    ---------------------------------------------
    这里才是**真正发请求的地方**：只有这里知道网关最终落到哪条配置、
    返回的 provider_name / model 是什么、耗时多少、失败原因是什么。
    写到上层就只能拿到"调用了一次"，厂商与耗时全是猜的。

    为什么必须显式收 device_id / user_id
    ----------------------------------
    用户端按**设备**过滤（``usage.recent_usage_rows(device_ids=...)``），
    行上没有 device_id 就等于「记了但家属永远看不到」。而
    ``memorize_background`` 是裸 ``threading.Thread``，contextvars
    **不会**传播过去 —— 所以只能从调用方一路显式传下来，不能图省事用
    contextvar。

    任何异常都只记 warning：记账是旁路，绝不能让记忆写入失败。
    """
    try:
        from .. import usage as usage_svc  # 局部导入：本模块要能在网关缺失时被 import

        if result is not None:
            usage_svc.record_from_result(
                db, result, source="memory", usage=usage,
                device_id=device_id, user_id=user_id,
                request_id=usage_svc.new_request_id(),
            )
        else:
            usage_svc.record_model_usage(
                db, source="memory", usage=usage,
                device_id=device_id, user_id=user_id,
                ok=ok, error=error[:255], elapsed_ms=elapsed_ms,
                request_id=usage_svc.new_request_id(),
            )
    except Exception as exc:  # noqa: BLE001  记账失败绝不能影响记忆写入
        log.warning("记忆侧用量记账失败（不影响记忆写入）：%s: %s",
                    type(exc).__name__, exc)


@runtime_checkable
class ModelClient(Protocol):
    """记忆模块需要的最小模型能力面。

    只有两个方法是有意为之：多一个方法（比如 rerank）就多一处要替测试造假的地方。
    将来真要接 rerank，新增一个可选方法 + ``getattr`` 判空即可，不必改协议。
    """

    def chat(self, kind: str, messages: list[dict], **kw: Any) -> str:
        """发一次对话，返回**纯文本**（不是 ChatResult 之类的包装对象）。"""
        ...

    def embed(self, texts: list[str], **kw: Any) -> list[list[float]]:
        """把一批文本变成向量，返回与输入等长、同序的二维列表。"""
        ...


class GatewayClient:
    """默认实现：桥接 ``server/app/llm_gateway.py``。

    **不在模块级 import 网关**，只在第一次真正调用时导入（见 ``_gateway()``）。
    这样即使网关文件缺失或正在被别人改，``import app.memory`` 依然是安全的。
    """

    def __init__(self) -> None:
        self._mod: Any = None

    # ------------------------------------------------------------ 懒加载
    def _gateway(self) -> Any:
        if self._mod is None:
            try:
                from .. import llm_gateway  # 局部导入：见类注释
            except Exception as exc:  # noqa: BLE001  任何导入失败都要转成可读错误
                raise ModelClientNotReady(
                    "模型网关尚未就绪（server/app/llm_gateway.py 不可用），"
                    "请先用 memory.client.set_default_client() 注入一个客户端；"
                    f"原始错误：{type(exc).__name__}: {exc}"
                ) from exc
            self._mod = llm_gateway
        return self._mod

    @staticmethod
    def _usage_of(kind: str) -> str:
        return _KIND_ALIASES.get((kind or "").strip(), (kind or "").strip())

    # ------------------------------------------------------------ 接口实现
    def chat(self, kind: str, messages: list[dict], **kw: Any) -> str:
        gw = self._gateway()
        usage = self._usage_of(kind)
        db = kw.pop("db", None)
        # ⚠️ 这两个是**记账上下文**，不是厂商参数：必须在转发前 pop 掉，
        # 否则会被 _chat_payload 原样塞进 HTTP 报文（厂商多半直接 400，
        # 而症状是「记忆抽取突然全失败」，极难联想到记账）。
        device_id = kw.pop("device_id", None)
        user_id = kw.pop("user_id", None)
        # 网关签名是 chat(db, usage, messages, ...)：它要一个会话去查 ai_providers。
        # 这里允许调用方自带 db（HTTP 请求内复用同一个会话），否则自己开一个短会话，
        # 用完立刻关掉，避免长事务占着 SQLite 的文件锁。
        if db is None:
            from ..db import SessionLocal  # 局部导入，避免循环依赖

            with SessionLocal() as own:
                return self._chat(own, gw, usage, messages, kw,
                                  device_id=device_id, user_id=user_id)
        return self._chat(db, gw, usage, messages, kw,
                          device_id=device_id, user_id=user_id)

    @staticmethod
    def _chat(db: Any, gw: Any, usage: str, messages: list[dict], kw: dict,
              *, device_id: int | None = None,
              user_id: int | None = None) -> str:
        started = time.monotonic()
        try:
            res = gw.chat(db, usage, list(messages), **kw)
        except Exception as exc:
            # 失败也记一行（ok=False + error）：排查「为什么轻量模型的用量是 0」
            # 时，「调用过但全失败」与「从来没调用过」必须可区分。
            _record(db, usage=usage, ok=False, device_id=device_id,
                    user_id=user_id, elapsed_ms=_elapsed_ms(started),
                    error=f"{type(exc).__name__}: {exc}")
            raise
        _record(db, usage=usage, result=res, device_id=device_id,
                user_id=user_id, elapsed_ms=_elapsed_ms(started))
        # 网关返回的是 ChatResult 数据类；这里只要文本，
        # 但也要能容忍将来网关直接返回 str（别把兼容性钉死在一个类型上）。
        text = getattr(res, "text", None)
        return text if isinstance(text, str) else (res if isinstance(res, str) else "")

    def embed(self, texts: list[str], **kw: Any) -> list[list[float]]:
        gw = self._gateway()
        if not texts:
            return []
        device_id = kw.pop("device_id", None)
        user_id = kw.pop("user_id", None)
        db = kw.pop("db", None)
        from ..db import SessionLocal  # 局部导入，避免循环依赖

        # 网关的 embed 也要 db（查 ai_providers）。这里自己开一个短会话并在 finally
        # 里关掉 —— 记忆写入可能一次 embed 几十条，会话不能一直挂着，
        # 否则 SQLite 的写锁会被读连接长时间占住。
        if db is None:
            with SessionLocal() as own:
                return self._embed(own, gw, texts, device_id=device_id,
                                   user_id=user_id)
        return self._embed(db, gw, texts, device_id=device_id, user_id=user_id)

    @staticmethod
    def _embed(db: Any, gw: Any, texts: list[str],
               *, device_id: int | None = None,
               user_id: int | None = None) -> list[list[float]]:
        started = time.monotonic()
        try:
            res = gw.embed(db, list(texts), usage=USAGE_EMBED)
        except Exception as exc:
            _record(db, usage=USAGE_EMBED, ok=False, device_id=device_id,
                    user_id=user_id, elapsed_ms=_elapsed_ms(started),
                    error=f"{type(exc).__name__}: {exc}")
            raise
        # ⚠️ VectorResult 上**没有** token 字段（见 llm_gateway 的类注释：
        # embed/rerank 的 usage 段本轮未接），所以这一行 total_tokens 恒为 0。
        # 用户端看到的是「向量化模型 · N 次调用」，这正是按次口径该有的样子。
        _record(db, usage=USAGE_EMBED, result=res, device_id=device_id,
                user_id=user_id, elapsed_ms=_elapsed_ms(started))
        vectors = getattr(res, "vectors", None)
        if isinstance(vectors, list):
            return [list(map(float, v)) for v in vectors]
        # 老口径：网关直接返回 list[list[float]]
        return [list(map(float, v)) for v in (res or [])]


# ---------------------------------------------------------------- 默认客户端
_default_client: ModelClient | None = None
_lock = threading.Lock()


def set_default_client(client: ModelClient | None) -> None:
    """替换默认客户端（测试注入 / 将来换调用层都走这里）。

    传 None 等价于重置回「懒加载真实网关」。
    """
    global _default_client
    with _lock:
        _default_client = client
    log.info("记忆模块模型客户端已%s", "替换为自定义实现" if client else "重置为默认网关")


def reset_default_client() -> None:
    """恢复成默认的 llm_gateway 桥接（主要用于测试之间清理状态）。"""
    set_default_client(None)


def get_default_client() -> ModelClient:
    """取当前客户端；没注入过就用 GatewayClient（此时**不会**校验网关是否可用）。"""
    global _default_client
    with _lock:
        if _default_client is None:
            _default_client = GatewayClient()
        return _default_client
