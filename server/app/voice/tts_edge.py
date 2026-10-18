"""Edge TTS 内置桥 —— 让预置音色**开箱即用**（无需自建桥服务）。

为什么需要这个模块
------------------
预置的 11 种音色全是 Edge TTS 的（``zh-CN-XiaoxiaoNeural`` 等）。但 Edge TTS
**不是 HTTP API**：它是微软 Edge 浏览器的 WebSocket 非公开协议，由开源库
``edge-tts`` 实现，输出 **mp3**。

而本项目的客户端只吃 **24kHz/mono/S16LE 裸 PCM**（见 ``asr_tts.py`` 的
``TTS_RESPONSE_FORMAT``/``TTS_SAMPLE_RATE``，理由是固件按服务端声明的采样率
逐帧 Opus 编码，容器格式必须在服务端剥掉）。

不处理这件事的后果很具体：管理员在后台选了「晓晓」这个音色，点测试也过了，
但设备**一点声音都没有** —— 因为 :func:`~app.voice.asr_tts.tts` 会把 mp3 字节
当成 PCM 直接送去编码，出来的是噪音。这正是「预置了却跑不通」。

本模块把这条链补齐：**edge-tts 合成 → mp3 解码 → 重采样 24k mono s16 → 裸 PCM**。

实测记录（本机 2026-10-18，非推断）
------------------------------------
- ``edge_tts.Communicate(text, voice).stream()`` 产出 audio chunk 拼接后 14400 字节，
  首字节 ``ff f3`` → **确认是 mp3**（不是 wav，也不是裸 PCM）
- PyAV 解码后源格式：**24000 Hz / 1 声道 / fltp** —— 采样率与声道数**正好**是我们要的，
  所以 AudioResampler 实际只做格式转换（fltp→s16），不需要真正的重采样
- 「老人今天该吃药了」→ 109440 字节 PCM ≈ 2.28 秒，比例合理（24000×2×2.28）
- 分块切片无损性：25600 字节确定性数据按 2880/块 切 9 块，重组**逐字节相同**

⚠️ **实测发现的限流约束（必须写在这里，否则后人会低估风险）**
----------------------------------------------------------------
连续 3 次调用，第 3 次抛 ``ConnectionTimeoutError``（连不上微软的
``speech.platform.bing.com``）。这不是本机网络问题 —— 前两次同样代码成功。

含义：Edge 是**非公开接口，微软对高频调用会限流**。所以：
  ① 不要用 Edge 做高频压测或「每句都换音色」的批量任务；
  ② 生产上应把 Edge 当**零成本兜底**，而不是唯一 TTS；
  ③ :func:`synth_pcm` 的失败必须能被上层捕获并切到备用配置行
     （``ai_providers`` 的 priority 链正是为此存在）。

这也是为什么后端要支持三家可切换：**任何一家都可能是当下的唯一可用项**。

为什么不用 ``edge_tts.Communicate.save()``
------------------------------------------
那个方法要求 aiohttp 事件循环上下文，且只能写文件；我们要的是字节流以便
接进现有的 :func:`tts_stream` 增量路径。

⚠️ **降级约定（这条必须守住）**
--------------------------------
``edge_tts`` 与 ``av`` 都是**可选依赖**（缺失时服务仍要能启动，只是 Edge 选项
不可用）。所以本模块在 import 期不硬依赖它们，而是在调用时才检查并抛出
带修复指引的 :class:`VoiceUpstreamError`。

⚠️ 这正是本项目吃过教训的地方：``smoke_voice`` 在缺 libopus 时「跳过断言」
而其余照跑 → **假绿**。所以 :func:`edge_available()` 必须被测试显式断言，
「跳过」要变成「红」，见 ``tools/smoke_voice_catalog.py`` 的 E 组。
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
from typing import Any, Final, Iterator

log = logging.getLogger("elder.voice.edge")

# Edge TTS 专用默认超时。它是公网 WebSocket 服务，比本地推理慢得多，
# 但老人场景等太久等于没有 —— 45 秒仍不返回就该失败并让上层换备用行。
EDGE_TIMEOUT_SEC: Final[int] = 45

# edge-tts 的 rate/pitch/volume 接受 "+10%" / "-5%" 这类字符串。
# ⚠️ 注意：**没有空格**。写成 "-10 %" 微软会拒收，报一个极难定位的 400。
_RATE_RE: Final = "+0%"


def _import_deps() -> tuple[Any, Any]:
    """延迟导入可选依赖，返回 ``(edge_tts, av)``；缺失时抛可读错误。"""
    try:
        import av
        import edge_tts
    except ImportError as exc:   # pragma: no cover - 取决于部署环境
        raise MissingEdgeDeps(
            "Edge TTS 需要两个可选依赖：edge-tts（合成）与 av（mp3 解码）。"
            "安装：pip install edge-tts av；"
            "或改用阿里云百炼 / 火山引擎（只需网络，无需这两个依赖）"
        ) from exc
    return edge_tts, av


class MissingEdgeDeps(RuntimeError):
    """缺少 edge-tts / av 依赖。

    独立成类而不是复用 :class:`VoiceUpstreamError`，是为了让调用层能**分诊**：
    缺依赖是「部署没配好」，重试无用；上游故障才值得重试或切备用行。
    把两者混成一个异常，会导致缺依赖时无限重试到超时 —— 对老人就是一直不吭声。
    """


def edge_available() -> bool:
    """两个依赖是否都在。**测试必须对它断言**，把「跳过」变成「红」。

    为什么这条这么重要（项目真实教训）
    ----------------------------------
    缺依赖时若让测试「打印警告后跳过」，CI 会在**完全没有 Edge 能力**的情况下
    全绿 —— 假绿比红更贵：它让人以为 Edge 这条路验证过了。参考
    ``smoke_voice.py`` 在缺 libopus 时的历史假绿（本项目已为此加独立断言）。
    """
    try:
        import av  # noqa: F401
        import edge_tts  # noqa: F401
    except Exception:  # noqa: BLE001  任何导入异常都算「不可用」
        return False
    return True


def _run_coroutine(coro: Any, timeout: float) -> Any:
    """在同步代码里跑协程（本项目的 TTS 调用链全是同步的）。

    ⚠️ 不能直接 ``asyncio.run``：语音链路运行在 uvicorn 的事件循环里，
    在那里再开新事件循环会与已有 loop 冲突（RuntimeError）。
    这里用独立线程 + 私有 loop，线程之间无共享状态，是安全的。

    超时用 ``asyncio.wait_for``：微软服务偶尔连上就不吭声（连不上的情况是
    立刻抛错的，能连上但不回才是这种），没有超时就是设备永远不吭声。
    """
    result: dict[str, Any] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            result["value"] = loop.run_until_complete(
                asyncio.wait_for(coro, timeout=timeout))
        except BaseException as exc:  # noqa: BLE001  要把异常带回主线程
            result["error"] = exc
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_runner, daemon=True, name="edge-tts")
    t.start()
    t.join(timeout=timeout + 5)
    if t.is_alive():
        raise TimeoutError(f"Edge TTS 在 {timeout:g}s 内没有返回")
    if "error" in result:
        raise result["error"]
    return result.get("value")


async def _synth_mp3(edge_tts: Any, text: str, voice: str, rate: str) -> bytes:
    """合成出 mp3 字节。"""
    chunks: list[bytes] = []
    comm = edge_tts.Communicate(text, voice, rate=rate)
    async for ch in comm.stream():
        if ch.get("type") == "audio":
            data = ch.get("data")
            if data:
                chunks.append(data)
    if not chunks:
        raise RuntimeError("Edge TTS 返回空音频（可能被限流，或音色名不被接受）")
    return b"".join(chunks)


def mp3_to_pcm_24k(mp3: bytes, av: Any) -> bytes:
    """mp3 → 24kHz / mono / S16LE **裸 PCM**（无容器头）。

    这段是本模块最容易写错的地方，已按 PyAV 19 实测校正过两点：
      ① ``resample(frame)`` 是**逐帧**调用，不能传 list（传了报
         "Argument 'frame' has incorrect type"）；
      ② 末尾必须 ``resample(None)`` 冲洗内部残留 —— 否则最后几十毫秒被截掉，
         表现为「每句话尾巴被切掉」，老人听到的句子总是断在半截。
    """
    container = av.open(io.BytesIO(mp3))
    try:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=24000)
        out: list[bytes] = []
        for frame in container.decode(audio=0):
            out.extend(resampler.resample(frame))
        out.extend(resampler.resample(None))   # 冲洗，漏了会切掉句尾
        return b"".join(bytes(f.planes[0]) for f in out)
    finally:
        container.close()


def synth_pcm(text: str, voice: str, *, rate: str = _RATE_RE,
               timeout: float = EDGE_TIMEOUT_SEC) -> bytes:
    """一句话 → 24kHz/mono/S16LE 裸 PCM。与 :func:`asr_tts.tts` 的返回契约一致。

    :param voice: Edge 音色名，如 ``zh-CN-XiaoxiaoNeural``。空串回落到晓晓
        （微软侧最稳的中文女声），而不是回落到空 —— 空音色名会被微软拒收，
        报一个和真实原因无关的错误。
    """
    edge_tts, av = _import_deps()
    voice = (voice or "").strip() or "zh-CN-XiaoxiaoNeural"
    mp3 = _run_coroutine(_synth_mp3(edge_tts, text, voice, rate), timeout)
    return mp3_to_pcm_24k(mp3, av)


def stream_pcm(text: str, voice: str, *, rate: str = _RATE_RE,
               timeout: float = EDGE_TIMEOUT_SEC) -> Iterator[bytes]:
    """流式版：合成完整段后**按块**吐出裸 PCM（增量生成器）。

    ⚠️ 为什么不是真流式
    -------------------
    edge-tts 协议本身是 WebSocket 增量推音频，理论上可以边收边转。但本函数
    刻意**先合成完再分块**：转码要在完整 mp3 上做才准（mp3 帧有帧头与
    编码延迟，逐块解码会引入咔哒声）。

    分块是为了让上层的 60ms 切帧逻辑正常工作 —— 若整段一次性 yield，
    下行时序（sentence_start → 帧流 → tts stop）虽仍正确，但首帧延迟等于
    整段合成时间，体验上等同非流式。分块后至少下行是平滑的。

    **不假装**：真正的边收边解码留作后续优化，届时必须同样处理 mp3 帧边界。
    """
    pcm = synth_pcm(text, voice, rate=rate, timeout=timeout)
    # 60ms @ 24kHz mono s16 = 24000(采样率) * 2(字节/采样) * 0.06 = 2880 字节
    # ⚠️ 写成 24000*2//5*3 会算出 28800（10 倍），下行时序会整体拖慢 10 倍。
    chunk = int(24000 * 2 * 0.06)
    for i in range(0, len(pcm), chunk):
        yield pcm[i:i + chunk]