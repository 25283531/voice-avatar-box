"""ASR 对比基准：把同一段音频喂给「配置里的每一家 ASR」，量响应时间与识别效果。

为什么需要这个脚本
------------------
``voice/asr_tts.asr()`` 是**按序兜底**的：只要第一条成功，后面的配置永远不会被
调用。所以「配置里有 5 家 ASR」不等于「5 家都能用」，更不等于「换一家会更快」。
管理后台的「真实测试」按钮对 ASR 只校验配置链路、**不发真实识别**
（见 ``routers/providers.py:test_voice`` 的注释），因此线上到底哪家快、哪家准，
除了这个脚本没有别的办法知道。

设计要点
--------
1. **绕开兜底链**：直接按 ``_asr_style()`` 分派到各家的底层实现，逐行独立计时。
   不修改数据库里的 priority/enabled —— 生产库不能为了测速被临时改脏。
2. **音频来源自洽**：用本项目自己的 TTS 合成普通话，再用 PyAV 重采样到
   16kHz/mono/S16LE（与设备上传的口径一致）。这样脚本零外部素材依赖，
   在任何一台部署机上都能跑。
   ⚠️ 合成语音比真人干净，**绝对识别率会偏高**；本脚本的价值在**相对比较**
   （同一段音频，谁快谁慢、谁错字），不能当作真实场景准确率。
3. **多长度**：短/中/长三句，观察耗时是否随音频长度线性增长 —— 这是区分
   「固定网络往返慢」与「推理慢」的关键。

用法（在容器内跑，才能同时访问内网 FunASR 与外网厂商）::

    docker cp tools/asr_bench.py elder-server:/tmp/asr_bench.py
    docker exec -w /app elder-server python /tmp/asr_bench.py
"""
from __future__ import annotations

import io
import time
from typing import Any

# 三档长度：短句（工具意图）、中句（日常闲聊）、长句（带条件的复杂请求）。
# 内容刻意包含老人高频词（吃药、喝水、天气、提醒）与数字，便于看错字落在哪。
SAMPLES: list[tuple[str, str]] = [
    ("短", "今天天气怎么样"),
    ("中", "小暖小暖，明天早上八点提醒我吃药"),
    ("长", "我这两天血压有点高，头也有点晕，你帮我看看今天适不适合出门散步，"
           "顺便提醒我晚上六点吃降压药"),
]


def _to_pcm16k(pcm24k: bytes) -> bytes:
    """24kHz/mono/S16LE -> 16kHz/mono/S16LE（PyAV 重采样）。

    ⚠️ 必须按 ``frame.samples * 2`` 截断 plane 再取字节。
    PyAV 的 ``AudioFrame`` 底层 buffer 是**按 32 字节对齐分配**的，容量通常大于
    实际采样数；直接 ``bytes(frame.planes[0])`` 会把对齐填充也当成采样点，
    于是音频被凭空拉长（实测 3.2s 的句子变成 6.4s），喂给 ASR 就是同一句话
    被念两遍 —— 表现为"每家 ASR 都重复识别"，看起来像 ASR 的锅，其实是
    测试脚本的锅。这个坑值得写在代码里：它会让整份对比数据全部作废。
    """
    import av

    frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm24k) // 2)
    frame.sample_rate = 24000
    frame.planes[0].update(pcm24k)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

    out = io.BytesIO()
    chunks = list(resampler.resample(frame)) + list(resampler.resample(None))
    for chunk in chunks:
        out.write(bytes(chunk.planes[0])[: chunk.samples * 2])
    return out.getvalue()


def _cer(ref: str, hyp: str) -> float:
    """字错率（编辑距离 / 参考长度）。去标点与空白后再算。"""
    import re

    strip = lambda s: re.sub(r"[\s，。！？、,.!?]", "", s or "")
    a, b = strip(ref), strip(hyp)
    if not a:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / len(a)


def _call_one(db: Any, row: Any, pcm16k: bytes, asr_tts: Any) -> str:
    """按这一行的风格调一次真实识别，返回文本（异常原样抛出）。"""
    from app import config
    from app.model_catalog import API_STYLE_LOCAL_ASR
    from app.voice import asr_tencent

    style = asr_tts._asr_style(row)
    timeout = asr_tts._row_timeout(row, asr_tts.ASR_TIMEOUT_SEC)
    wav = asr_tts.wav_header_bytes(len(pcm16k), 16000) + pcm16k

    if style == "tencent":
        sid, skey = asr_tts._secret_pair(row)
        engine = (getattr(row, "model", "") or "").strip() or asr_tencent.DEFAULT_ENGINE
        return asr_tencent.transcribe(
            pcm16k, secret_id=sid, secret_key=skey, engine=engine,
            endpoint=asr_tts._row_endpoint(row), timeout=timeout).strip()

    if style == API_STYLE_LOCAL_ASR:
        payload = asr_tts._post_multipart(row, "/asr", {}, "audio",
                                          "utterance.wav", wav, timeout)
        return str(payload.get("text", "") or "").strip()

    model = (getattr(row, "model", "") or "").strip() or "whisper-1"
    payload = asr_tts._post_multipart(
        row, "/audio/transcriptions",
        {"model": model, "language": config.VOICE_ASR_LANGUAGE,
         "response_format": "json"},
        "file", "utterance.wav", wav, timeout)
    return str(payload.get("text", "") or "").strip()


def main() -> int:
    from app.db import SessionLocal
    from app.models import AIProvider
    from app.voice import asr_tts

    db = SessionLocal()
    try:
        rows = (db.query(AIProvider)
                  .filter(AIProvider.kind == "asr",
                          AIProvider.enabled.is_(True),
                          AIProvider.deleted_at.is_(None))
                  .order_by(AIProvider.priority, AIProvider.id)
                  .all())
        if not rows:
            print("没有启用的 ASR 配置")
            return 1

        print("=" * 78)
        print("合成测试音频（用本部署自己的 TTS，24kHz -> 重采样 16kHz）")
        print("=" * 78)
        audios: list[tuple[str, str, bytes, int]] = []
        for tag, text in SAMPLES:
            t0 = time.monotonic()
            pcm24 = asr_tts.tts(db, text)
            pcm16 = _to_pcm16k(pcm24)
            ms = int((time.monotonic() - t0) * 1000)
            audios.append((tag, text, pcm16, ms))
            print(f"  [{tag}] {len(pcm16) / 2 / 16000:.1f}s 音频 "
                  f"（{len(pcm16)} 字节，合成 {ms}ms）：{text[:30]}…")

        print()
        print("=" * 78)
        print(f"逐家 ASR 实测（{len(rows)} 条启用配置）")
        print("=" * 78)
        summary: list[dict[str, Any]] = []
        for row in rows:
            style = asr_tts._asr_style(row)
            label = f"{row.name}({row.model or '-'})"
            print(f"\n--- priority={row.priority} id={row.id} "
                  f"style={style} timeout={row.timeout_sec}s")
            print(f"    {label}  endpoint={row.endpoint or '(预置默认)'}")
            rec: dict[str, Any] = {"label": label, "style": style,
                                   "priority": row.priority, "runs": []}
            for tag, text, pcm16, _ in audios:
                t0 = time.monotonic()
                try:
                    got = _call_one(db, row, pcm16, asr_tts)
                    ms = int((time.monotonic() - t0) * 1000)
                    err = _cer(text, got)
                    rec["runs"].append({"tag": tag, "ms": ms, "cer": err,
                                        "text": got, "ok": True})
                    print(f"    [{tag}] {ms:6d}ms  CER={err:5.1%}  {got!r}")
                except Exception as exc:  # noqa: BLE001
                    ms = int((time.monotonic() - t0) * 1000)
                    rec["runs"].append({"tag": tag, "ms": ms, "cer": None,
                                        "text": str(exc)[:160], "ok": False})
                    print(f"    [{tag}] {ms:6d}ms  失败：{type(exc).__name__}: "
                          f"{str(exc)[:120]}")
            summary.append(rec)

        print()
        print("=" * 78)
        print("汇总（只统计成功的档位；失败不计入均值）")
        print("=" * 78)
        print(f"{'配置':38s} {'风格':12s} {'短':>7s} {'中':>7s} {'长':>7s} "
              f"{'均CER':>7s}")
        for rec in summary:
            cells = []
            for tag in ("短", "中", "长"):
                hit = next((r for r in rec["runs"] if r["tag"] == tag), None)
                cells.append(f"{hit['ms']}ms" if hit and hit["ok"] else "失败")
            cers = [r["cer"] for r in rec["runs"] if r["ok"]]
            mean_cer = f"{sum(cers) / len(cers):.1%}" if cers else "-"
            print(f"{rec['label'][:37]:38s} {rec['style'][:11]:12s} "
                  f"{cells[0]:>7s} {cells[1]:>7s} {cells[2]:>7s} {mean_cer:>7s}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
