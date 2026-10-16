"""Opus 编解码：ctypes 直连系统 libopus，**零新增 Python 依赖**。

为什么不用 pip 包
----------------
1. 镜像里已经有 apt 的 ``libopus0``（Debian/Ubuntu 基础镜像自带，见
   Dockerfile），再装一个自带 vendor 的 wheel 等于把同一份 C 代码装两遍；
2. 纯 Python 的 Opus 实现不存在（Opus 的 MDCT/SILK/CELT 三层复杂度不可能
   被 Python 实时跑完），能选的只有 ``pyogg`` / ``opuslib`` 这类 ctypes 封装，
   而它们做的事与这里一模一样，却多一个依赖要审计、要跟着 CVE 更新；
3. 语音链路的关键路径是"每 60ms 一次编解码"，绑系统库可以直接吃到发行版的
   安全更新与 CPU 指令优化，不必等上游发版。

为什么编解码器是模块级函数而不是类
--------------------------------
**为了让冒烟测试能 monkeypatch**。会话编排（``session.py``）只依赖
:func:`decode_opus` / :func:`encode_opus` 这两个名字，不知道 ctypes 的存在；
本机没有 libopus 时冒烟脚本把这两个函数换成"静音直通"就能继续验协议，
不必为了测试在编排层开一个后门。同理 :func:`libopus_available` 是函数而非
import 期常量 —— 常量会在 import 时定死，monkeypatch 来不及。

帧长口径
--------
60ms @ 24kHz = 1440 样本/帧 = 2880 字节 S16LE，与握手里声明的
``VOICE_FRAME_DURATION_MS`` / ``VOICE_OUTPUT_SAMPLE_RATE`` 一致。
固件侧把每帧当独立包解码（audio_service.cc），所以帧边界必须精确，
补零截齐时按**帧长**取整，不是按"4 字节对齐"这种凑整口径。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import math
import struct
import threading
from typing import Final

log = logging.getLogger(__name__)

# Opus 应用类型：VOIP 侧重低延迟与语音清晰度（本项目就是实时对话），
# 而不是 AUDIO/RESTRICTED_LOWDELAY。
OPUS_APPLICATION_VOIP: Final[int] = 2048
# 目标码率 32kbps。高于它字节数涨但老人听力提升有限；低于它在嘈杂环境里
# 辅音先糊（"吃药"听成"去澡"）。
OPUS_TARGET_BITRATE: Final[int] = 32000

# 候选库名。find_library("opus") 在 Alpine/musl 与 Windows 上都会失败，
# 所以必须逐个试；Windows 上用户装的是 opus.dll，macOS 是 libopus.0.dylib。
_CANDIDATES: Final[tuple[str, ...]] = (
    "libopus.so.0", "libopus.so", "libopus-0.dll", "opus.dll",
    "libopus.0.dylib", "libopus.dylib",
)

_lib: ctypes.CDLL | None = None
_lib_checked = False
_lib_lock = threading.Lock()

# 编解码器实例按 (sample_rate, channels) 缓存。**按采样率分开建**不是洁癖：
# Opus 内部每个采样率有独立的模式表，复用错采样率的实例会得到被当作
# 另一个速率解释的音频（表现为"能解出声音但音调不对"），很难排查。
_decoders: dict[tuple[int, int], ctypes.c_void_p] = {}
_encoders: dict[tuple[int, int], ctypes.c_void_p] = {}
_codec_lock = threading.Lock()


class OpusUnavailableError(RuntimeError):
    """libopus 不可用。文案必须**可执行** —— 见下。"""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"libopus 不可用，无法{what}。修复方式（任选其一）：\n"
            f"  1) Debian/Ubuntu:  apt-get install -y libopus0\n"
            f"  2) macOS:          brew install opus\n"
            f"  3) Windows:        把 opus.dll 放到 PATH，或放进本目录\n"
            f"  4) 任意平台:       设环境变量 ELDER_LIBOPUS_PATH 指向库文件"
            f"（例如 C:/lib/opus.dll）\n"
            f"当前候选均已尝试：{', '.join(_CANDIDATES)}"
        )


def _load_lib() -> ctypes.CDLL | None:
    """按顺序找 libopus，找到就声明函数签名。线程安全，只做一次。"""
    global _lib, _lib_checked
    with _lib_lock:
        if _lib_checked:
            return _lib
        _lib_checked = True

        import os

        # 环境变量逃生舱放最前：运维/CI 明确知道库在哪时，不该被
        # find_library 的缓存（gconf 的负缓存）坑到。
        override = (os.getenv("ELDER_LIBOPUS_PATH") or "").strip()
        names: list[str] = [override] if override else []
        found = ctypes.util.find_library("opus")
        if found:
            names.append(found)
        names.extend(n for n in _CANDIDATES if n not in names)

        lib = None
        for name in names:
            if not name:
                continue
            try:
                lib = ctypes.CDLL(name)
                log.info("libopus 已加载：%s", name)
                break
            except OSError:
                continue
        if lib is None:
            log.warning(
                "libopus 未找到，语音编解码不可用（装 libopus0 或设 "
                "ELDER_LIBOPUS_PATH 后可恢复）。协议链路不受影响，"
                "音频将退化为静音/跳过。")
            return None

        # ---- 函数签名。restype 不声明时 ctypes 默认按 int 截断，
        # opus_decode 返回样本数（可达 5760）虽不会溢出，但
        # opus_encoder_create 返回指针（64 位）会被截成 32 位而崩溃。
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                    ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                    ctypes.c_char_p, ctypes.c_int]
        lib.opus_encoder_ctl.restype = ctypes.c_int
        lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_int]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        _lib = lib
        return lib


def libopus_available() -> bool:
    """当前进程能否真的做 Opus 编解码。

    是**函数**而不是 import 期常量：冒烟要在无 libopus 的机器上把它
    monkeypatch 成 True 再喂一段合法 Opus 包（见 smoke_voice 的说明）。
    """
    return _load_lib() is not None


def _decoder(sample_rate: int, channels: int) -> ctypes.c_void_p:
    with _codec_lock:
        key = (sample_rate, channels)
        if key in _decoders:
            return _decoders[key]
        lib = _load_lib()
        if lib is None:
            raise OpusUnavailableError("解码音频")
        err = ctypes.c_int(0)
        ptr = lib.opus_decoder_create(sample_rate, channels, ctypes.byref(err))
        if err.value != 0 or not ptr:
            raise OpusUnavailableError(
                f"创建 Opus 解码器（采样率 {sample_rate}，错误码 {err.value}）")
        _decoders[key] = ptr
        return ptr


def _encoder(sample_rate: int, channels: int) -> ctypes.c_void_p:
    with _codec_lock:
        key = (sample_rate, channels)
        if key in _encoders:
            return _encoders[key]
        lib = _load_lib()
        if lib is None:
            raise OpusUnavailableError("编码音频")
        err = ctypes.c_int(0)
        ptr = lib.opus_encoder_create(
            sample_rate, channels, OPUS_APPLICATION_VOIP, ctypes.byref(err))
        if err.value != 0 or not ptr:
            raise OpusUnavailableError(
                f"创建 Opus 编码器（采样率 {sample_rate}，错误码 {err.value}）")
        # OPUS_SET_BITRATE_REQUEST = 4003
        lib.opus_encoder_ctl(ptr, 4003, OPUS_TARGET_BITRATE)
        _encoders[key] = ptr
        return ptr


def _frame_samples(sample_rate: int, frame_ms: int = 60) -> int:
    return int(sample_rate * frame_ms / 1000)


def decode_opus(data: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """解一个 Opus 包成 S16LE PCM（单声道）。

    **两级失败语义不同，这是刻意的**
    --------------------------------
    - **包坏了/解不出**（``opus_decode`` 返回 <=0、抛异常）→ 返回**等长静音**。
      丢一帧就抛会让整个对话崩掉，而 60ms 的静音在语音里只是一次极短的
      断续，老人听不出来。等长是为了让"帧数与时长"的对应关系保持可预测
      （后续按帧算 RMS 做静音检测，帧数缩水会让兜底阈值失效）。
    - **库根本没装**（``OpusUnavailableError``）→ **抛出去**，不静默。
      这一条是反向要求：缺库时若也返回静音，服务端会"永远听不见"却
      看起来一切正常 —— 每一轮都有stt/tts 往返，只是内容全是空的，
      没有任何断言会红，而老人已经完全失声。**显式失败 + 可执行文案**
      才是这里该有的行为。调用方（``session._on_binary`` /
      ``session._speak_reply``）捕获它并降级（跳过本帧 / 只发文本）。

    ⚠️ 等长按**帧长**算（24kHz/60ms = 1440 样本），不是按输入字节数：
    Opus 是压缩格式，一个几十字节的包可以解出几十毫秒的音频。
    """
    if not data:
        return b""
    frame = _frame_samples(sample_rate)
    out_len = frame * 2 * channels
    try:
        dec = _decoder(sample_rate, channels)
        lib = _load_lib()
        assert lib is not None  # noqa: S101  _decoder 成功即必然非 None
        buf = ctypes.create_string_buffer(out_len)
        # 最后一个参数是 fec（前向纠错），传 0：固件不做 PLC 请求，
        # 我们也不必在服务端补（丢了就静音，见上）。
        n = lib.opus_decode(dec, data, len(data), buf, frame, 0)
        if n <= 0:
            log.warning("opus_decode 返回 %s（损坏/空包），按静音处理", n)
            return b"\x00" * out_len
        return buf.raw[: n * 2 * channels]
    except OpusUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001  编解码崩了不能让会话崩
        log.warning("Opus 解码异常，按静音处理：%s: %s", type(exc).__name__, exc)
        return b"\x00" * out_len


def encode_opus(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """把一段 PCM 编成**一个** Opus 包（S16LE 单声道，24kHz）。

    长度不是整帧时**补零截齐**到 60ms 整数倍。为什么补零而不是截断：
    截断会让最后一帧变短，固件按帧长解就得到一段被压缩的音频
    （听起来像快进）；补零解出来是末尾极短静音，无害。

    缓冲区按 Opus 规范取 ``1275 * 6`` —— 一个包的最大可能尺寸。
    """
    if not pcm:
        return b""
    frame = _frame_samples(sample_rate)
    want = frame * 2 * channels
    if len(pcm) % want:
        pcm = pcm + b"\x00" * (want - len(pcm) % want)
    try:
        enc = _encoder(sample_rate, channels)
        lib = _load_lib()
        assert lib is not None  # noqa: S101  _encoder 成功即必然非 None
        out = ctypes.create_string_buffer(1275 * 6)
        # 只编码第一帧：调用方按帧循环调本函数（会话编排里就是逐帧发）。
        # 多帧一次编会得到一个"多帧大包"，固件把它当一个包解，
        # 得到的是第一帧的内容 + 后续被丢弃 —— 直接表现成播报只有一帧。
        n = lib.opus_encode(enc, pcm[:want], frame, out, len(out))
        if n <= 0:
            log.warning("opus_encode 返回 %s，本帧丢弃", n)
            return b""
        return out.raw[:n]
    except OpusUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Opus 编码异常，本帧丢弃：%s: %s", type(exc).__name__, exc)
        return b""


def pcm_rms(pcm: bytes) -> float:
    """算一段 PCM 的 RMS（0.0 - 1.0）。供服务端静音兜底用。

    刻意用 RMS 而不是峰值：峰值一个采样点就能被一次点击/爆音顶上去，
    老人在屋里咳嗽一声就可能触发误判；RMS 反映的是能量。
    """
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    if n <= 0:
        return 0.0
    # 一次解出全部采样：struct.unpack 比逐样本 int.from_bytes 快一个量级，
    # 而这是每帧都要跑的（60ms 一次）。
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    total = 0
    for s in samples:
        total += s * s
    return math.sqrt(total / n) / 32768.0


def frame_bytes(sample_rate: int, channels: int = 1) -> int:
    """一帧 PCM 的字节数（24kHz/60ms/mono = 2880）。"""
    return _frame_samples(sample_rate) * 2 * channels
