"""声纹识别提供者（F3 · 3D Speaker）—— 复刻上游小智的「外部声纹服务」契约。

## 它是什么、不是什么
**是**：一个外部声纹 HTTP 服务的客户端。本服务自己不加载声纹模型，
只把 VAD 切出来的那一段语音 POST 出去，拿回 ``{"speaker_id", "score"}``，
过阈值就认人，再把身份喂给 LLM 做个性化。

**不是**：模型服务本身。3D-Speaker 的**注册（enroll）与向量库都在外部服务上**
（社区实现 voiceprint-api 用 ModelScope 3D-Speaker 模型），本服务只认人、
不注册。这与上游 xiaozhi-esp32-server 的分工逐字一致 —— 上游文档原话：
"Speaker voice vectors are enrolled out-of-band by the service itself; the
server only sends audio and reads back identifications."

## 契约（逐条对齐上游 ``VoiceprintProvider``，改一处就读不动上游服务）
1. **配置**：``url`` 形如 ``http://host:port/voiceprint/health?key=XXXX``。
   ``base = scheme://netloc``，``key`` 从 query 解析；
   identify 地址 = ``base + "/voiceprint/identify"``；
   health 地址 = ``base + "/voiceprint/health?key=" + key``。
2. **健康检查**：GET，期望 200 + ``{"status": "healthy"}``，按 TTL 缓存
   （上游用全局缓存管理器，我们用带 TTL 的模块级字典，语义一致）。
3. **识别**：POST multipart/form-data：
   * ``speaker_ids`` 字段 = 逗号分隔的说话人 ID（调用方从库里查出这台设备
     主人登记的全部说话人）；
   * ``file`` 字段 = WAV 音频（字段名 ``file``、文件名 ``audio.wav``、
     ``Content-Type: audio/wav``）；
   * 请求头 ``Authorization: Bearer <key>``。
   期望 200 + ``{"speaker_id": "...", "score": 0.83}``。
4. **判定**：``score < similarity_threshold``（默认 0.4）⇒ 未认出；
   返回的 id 不在传入的 ``speaker_ids`` 里 ⇒ 未认出（防服务端乱报）。
5. **失败语义（本模块最重要的一条）**：超时 / 断连 / 非 200 / JSON 畸形
   ⇒ 一律返回 ``(None, 0.0)`` 并记日志，**绝不外抛**。声纹是增强项，
   它把一次陪聊搞坏是不可接受的 —— 老人多等一秒都比"整轮卡死"好。

## 与上游的有意分歧（都写在这里，别让下一个人以为是漏抄）
* **未认出时不给 LLM 塞字面量**。上游分数不够时返回字符串 ``"未知说话人"``
  并照样包进 ``{"speaker": "未知说话人", ...}``。我们不包：认不出人时
  **行为必须与没开声纹时逐字节一致**。理由是该分歧的失败方向不对称 ——
  把字面量喂给模型，模型可能对老人说"我不认识你"，而认不出的最常见原因是
  老人感冒、离得远、麦克风蒙灰，不是家里来了陌生人。
* **健康检查每次带 TTL 重探**。上游在建连时探一次，不健康就整个连接期间
  禁用。我们按 TTL 缓存正反两种结果：服务重启后能自己恢复，不需要老人
  重新唤醒或重连。

## 已知缺口（诚实记账）
* **模型不在本服务里**（见上）：要自建模型服务是独立重大决策
  （torch + 模型下载 + 向量库 + 注册流程），见 docs/DEPLOY.md §1.14。
* **明文 HTTP**：key 与家庭音频在链路上裸奔（与 F1 MQTT Phase 1 同类的
  已知缺口）。默认关闭；开启即显式运维决策，且声纹服务应只在可信网络可达。
* **不校验上行音频的完整性 / 不做防重放**：契约里没有，上游也没有。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Final
from urllib.parse import parse_qs, urlparse

from .. import config
from .asr_tts import wav_header_bytes

log = logging.getLogger("voice.voiceprint")

#: 音频送声纹服务时的采样率。**必须与 asr() 同口径**（16kHz/mono/S16LE）：
#: 上游声纹服务按 16k 训练，我们库里登记的向量也是按 16k 提的。
#: 改这个数而不改 asr 的口径 = 认人率静默下降，且没有任何报错。
VOICEPRINT_SAMPLE_RATE: Final[int] = 16000

#: 单条说话人登记在模块间的传递形状：``(speaker_key, name, description)``。
#: 用元组而不是模型对象：本模块**不 import models**（保持可独立单测，
#: 也让"认人"这条链路不依赖数据库形态）。
SpeakerRow = tuple[str, str, str]

# 绕过代理。声纹服务按设计就在局域网（192.168.x.x / 10.x / 容器网络），
# 而部署环境里可能有 http_proxy 环境变量 —— urllib 默认尊重它，会把请求
# 发去代理再失败。冒烟脚本早有同款纪律（ProxyHandler({})），这里同样办。
_OPENER: Final = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class VoiceprintProvider:
    """外部声纹服务的客户端。进程内单例，见 :func:`provider`。"""

    def __init__(self, url: str, threshold: float, timeout_s: float,
                 health_ttl_s: float, health_timeout_s: float) -> None:
        self.original_url = (url or "").strip()
        self.threshold = float(threshold)
        self.timeout_s = float(timeout_s)
        self._health_ttl_s = float(health_ttl_s)
        self._health_timeout_s = float(health_timeout_s)
        self._health_lock = threading.Lock()
        self._health_at: float = 0.0
        self._health_ok: bool = False

        parsed = urlparse(self.original_url)
        self.base_url = (f"{parsed.scheme}://{parsed.netloc}"
                         if parsed.scheme and parsed.netloc else "")
        # key 从 query 解析（与上游一致：key 不进单独的配置项，运维只维护一个 URL）
        self.api_key = (parse_qs(parsed.query).get("key") or [""])[0]
        self.identify_url = (f"{self.base_url}/voiceprint/identify"
                             if self.base_url else "")
        self.health_url = (f"{self.base_url}/voiceprint/health?key={self.api_key}"
                           if self.base_url else "")

    # ---------------------------------------------------------------- 启用判定
    @property
    def configured(self) -> bool:
        """URL / key / 阈值三者都像样才算配好。任一缺失 ⇒ 功能关闭。

        阈值必须在 (0, 1]：0 是"随便挑一个都算认出来"（那还认什么），
        >1 是"永远认不出"（配了却静默不工作，比没配更难排查）。
        """
        if not self.base_url or not self.api_key:
            return False
        return 0.0 < self.threshold <= 1.0

    # ---------------------------------------------------------------- 健康检查
    def health(self) -> bool:
        """GET /voiceprint/health?key=，期望 ``{"status": "healthy"}``。

        正反两种结果都按 TTL 缓存（与上游一致）：服务挂掉时不会每轮都去撞
        一次门；恢复后最多再等一个 TTL 就能自动重新启用。
        """
        if not self.configured:
            return False
        now = time.monotonic()
        with self._health_lock:
            if self._health_ok and now - self._health_at < self._health_ttl_s:
                return True
        ok = self._probe_health()
        with self._health_lock:
            self._health_at = time.monotonic()
            self._health_ok = ok
        return ok

    def _probe_health(self) -> bool:
        try:
            with _OPENER.open(urllib.request.Request(self.health_url),
                              timeout=self._health_timeout_s) as resp:
                if resp.status != 200:
                    log.info("声纹服务健康检查 HTTP %s", resp.status)
                    return False
                data = json.loads(resp.read().decode("utf-8", "replace"))
            ok = isinstance(data, dict) and data.get("status") == "healthy"
            if not ok:
                log.info("声纹服务健康检查响应不含 healthy：%r", data)
            return ok
        except Exception as exc:  # noqa: BLE001 —— 健康检查失败是常态，不是异常
            log.info("声纹服务健康检查失败：%s: %s", type(exc).__name__, exc)
            return False

    # ---------------------------------------------------------------- 识别
    def identify(self, pcm_16k: bytes,
                 speaker_ids: list[str]) -> tuple[str | None, float]:
        """POST /voiceprint/identify，返回 ``(speaker_id 或 None, score)``。

        任何失败都返回 ``(None, 0.0)``（见模块 docstring 第 5 条）。
        """
        if not self.configured or not speaker_ids:
            return None, 0.0

        wav = wav_header_bytes(len(pcm_16k), VOICEPRINT_SAMPLE_RATE) + pcm_16k
        body, content_type = _multipart_body(speaker_ids, wav)
        req = urllib.request.Request(self.identify_url, data=body,
                                     method="POST")
        req.add_header("Content-Type", content_type)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        try:
            with _OPENER.open(req, timeout=self.timeout_s) as resp:
                if resp.status != 200:
                    log.info("声纹识别 HTTP %s", resp.status)
                    return None, 0.0
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # 非 2xx：服务端把错误码放在异常里，读不到 body 也无所谓
            log.info("声纹识别 HTTP %s", exc.code)
            return None, 0.0
        except Exception as exc:  # noqa: BLE001 —— 见模块 docstring 第 5 条
            log.info("声纹识别失败：%s: %s", type(exc).__name__, exc)
            return None, 0.0

        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            speaker_id = data.get("speaker_id")
            score = float(data.get("score") or 0.0)
        except Exception as exc:  # noqa: BLE001 —— 畸形 JSON 同上
            log.info("声纹识别响应解析失败：%s: %s", type(exc).__name__, exc)
            return None, 0.0

        if score < self.threshold:
            log.info("声纹相似度 %.3f 低于阈值 %.3f，按未认出处理",
                     score, self.threshold)
            return None, score
        if not speaker_id or str(speaker_id) not in speaker_ids:
            # 服务端报了一个我们没问过的 ID：不认（上游同款守卫）。
            log.info("声纹返回了未登记的说话人 ID：%r", speaker_id)
            return None, score
        return str(speaker_id), score


# ================================================================ 模块级单例
_provider: VoiceprintProvider | None = None
_provider_lock = threading.Lock()


def provider() -> VoiceprintProvider | None:
    """进程内单例（懒构造）。**未配置 URL 时返回 None** = 功能关闭。

    故意不在首次返回 None 时把结果缓存住：测试与变异子进程会改配置后
    重新 import/调用，缓存 None 会让「改配置 → 再看一眼」失效。
    """
    global _provider
    if _provider is not None:
        return _provider
    with _provider_lock:
        if _provider is not None:
            return _provider
        if not config.VOICEPRINT_URL:
            return None
        p = VoiceprintProvider(config.VOICEPRINT_URL,
                               config.VOICEPRINT_THRESHOLD,
                               config.VOICEPRINT_TIMEOUT_SEC,
                               config.VOICEPRINT_HEALTH_TTL_SEC,
                               config.VOICEPRINT_HEALTH_TIMEOUT_SEC)
        if not p.configured:
            # 配了却不像样（缺 key / 阈值越界）：明确告诉运维，别静默关闭
            log.warning("声纹识别已配置但不可用（url=%r threshold=%r），功能关闭",
                        config.VOICEPRINT_URL, config.VOICEPRINT_THRESHOLD)
            return None
        _provider = p
        return p


def reset_provider() -> None:
    """清掉单例。**只给测试 / 变异子进程用**：生产进程配置不变，无需重置。"""
    global _provider
    with _provider_lock:
        _provider = None


def enabled() -> bool:
    """功能是否开着（URL 配好且阈值像样）。调用方用它决定要不要并行发起识别。"""
    return provider() is not None


def identify_speaker(
    pcm_16k: bytes, speakers: list[SpeakerRow],
) -> tuple[SpeakerRow | None, float]:
    """对一整段语音做声纹识别，返回 ``(命中行或 None, score)``。

    ``speakers`` 是这台设备主人登记的全部说话人
    ``[(speaker_key, name, description), ...]``。

    这个函数把三道闸门收在一处，调用方（session）只管喂音频和人：
      1. 没配 URL ⇒ 不识别；
      2. 一个说话人都没登记 ⇒ 不识别（上游同语义：speakers 为空则禁用）；
      3. 服务不健康 ⇒ 这一轮不识别（缓存带 TTL，不会每轮撞门）。
    """
    p = provider()
    if p is None or not speakers:
        return None, 0.0
    if not p.health():
        return None, 0.0
    sid, score = p.identify(pcm_16k, [s[0] for s in speakers])
    if sid is None:
        return None, score
    for row in speakers:
        if row[0] == sid:
            return row, score
    return None, score


# ================================================================ multipart
def _multipart_body(speaker_ids: list[str],
                    wav: bytes) -> tuple[bytes, str]:
    """手搓 multipart/form-data 请求体，返回 ``(body, Content-Type)``。

    为什么不引 aiohttp/requests：服务端 HTTP 一律 urllib（见 asr_tts），
    为一个可选功能拉进 requests 不划算；而 aiohttp 只是 edge-tts 的可选
    传递依赖，不能拿它当硬依赖用。

    boundary 用 uuid4：固定串有极小概率与音频内容相撞，撞上的表现是
    服务端多收一个空字段 —— 难复现且无报错，不值得省这几十字节。
    """
    boundary = "----elder-voiceprint-" + uuid.uuid4().hex
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="speaker_ids"\r\n\r\n')
    parts.append(",".join(speaker_ids).encode("utf-8") + b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="file"; '
                 b'filename="audio.wav"\r\n')
    parts.append(b"Content-Type: audio/wav\r\n\r\n")
    parts.append(wav + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
