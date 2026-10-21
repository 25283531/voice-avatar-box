"""MQTT + UDP 传输（F1）：小智协议栈的第二条设备通道。

架构总览
--------
一次「MQTT + UDP」会话由三块拼成，全部住在**本进程**（不引 Node、不装
mosquitto、不转发到任何第三方服务器）::

    设备 ──MQTT(TCP)──► MqttUdpServer._MqttConn ──► MqttUdpTransport ──► VoiceSession
                        （JSON 控制面）              （quack WS 接口）
    设备 ◀──MQTT(TCP)──  publish devices/p2p/{mac}
    设备 ──UDP────────► UdpAudioGateway ──► 同一个 VoiceSession
                        （AES-128-CTR 加密的 Opus）

为什么是「传输适配器」而不是「第二套会话」
--------------------------------------
:class:`~app.voice.session.VoiceSession` 的全部编排（鉴权 / VAD / 断句 /
打断 / 工具调用 / 埋点）与**传输无关**，它只依赖 ``ws`` 对象的五个方法：
``receive()`` / ``send_text()`` / ``send_bytes()`` / ``close(code=)`` /
``headers``。:class:`MqttUdpTransport` 把这五个方法桥到 MQTT+UDP，
VoiceSession **一行不改**（除 hello 的一个可选改写钩子）就跑在新通道上。
反过来说：任何「MQTT 版会话行为与 WS 版不一致」的 bug，都只可能在适配器里。

固件契约（逐条对应 ``firmware/xiaozhi/main/protocols/mqtt_protocol.cc``）
---------------------------------------------------------------------
1. **凭据**（OTA ``mqtt`` 段下发，固件存 NVS ``mqtt`` 命名空间）::

       client_id = "GID-elder@@@{mac}@@@{mac}"     # mac = 小写无冒号
       username  = base64('{"ip":"unknown"}')
       password  = base64(HMAC-SHA256(client_id + "|" + username, signature_key))

   ``signature_key`` 由 ``JWT_SECRET`` 确定性派生（见 :func:`signature_key`）——
   **不新增存储**，且轮换 JWT secret 时旧 MQTT 凭据自然失效（设备下一次
   OTA 轮询会拿到新的，固件只在值变化时才写 NVS）。
2. **端口语义**：8883 = TLS + cert bundle，**其它端口 = 明文 TCP**。
   Phase 1 只实现明文（见下「已知缺口」）。
3. **固件从不 Subscribe**：由服务端（broker）按 client_id 自动订阅
   ``devices/p2p/{mac}``；设备只往 ``device-server`` publish。本实现**必须**
   复刻这点，否则设备收不到任何下行。
4. **hello 交换**：设备 publish ``{"type":"hello","version":3,"transport":"udp",
   "audio_params":{...}}``；服务端必须回一个 ``transport=="udp"`` 且带
   ``udp:{server,port,key,nonce}``（key/nonce 均为 **hex**）的 hello，
   否则固件 ``ParseServerHello`` 报 "Unsupported transport" 直接返回。
   服务端 hello 由 ``VoiceSession.run()`` 发出，经
   :meth:`MqttUdpTransport.rewrite_hello` 改写；设备自己发来的那个 hello
   是冗余的（WS 路径从没有过），在适配器里拦下、记日志、不送进会话。
5. **UDP 音频包**（两个方向同一布局）::

       |nonce 16B|AES-128-CTR(key, nonce 为 IV) 加密的 Opus|
       nonce[2:4]  = payload_len（大端，固件 SendAudio 写死）
       nonce[8:12] = timestamp（大端；我们填 0 = 无时间戳，同 WS 路径口径）
       nonce[12:16]= sequence（大端，从 1 起逐包 +1）
       下行包首字节必须是 0x01（固件 OnMessage 硬校验）

⚠️ Phase 1 已知缺口（**不假装支持**）
------------------------------------
1. **明文 MQTT**：1883 上凭据可被网络攻击者截获并重放（口令是确定性的，
   截获即获得该设备的完整 impersonation 能力）。TLS（8883）需要一张服务端
   IP 的短期证书（固件 cert bundle 只认公开 CA），列为后续增量。
   ⇒ **开启前必须确认 1883/UDP 只暴露在可信网络**，见 config.MQTT_PORT 注释。
2. **MQTT 主题是精确匹配**，不支持 ``+`` / ``#`` 通配符；QoS 只实现 0
   （设备 publish/subscribe 都是 QoS 0，见 ``net/mqtt.h`` 的默认参数）。
3. **不校验上行包的序列号连续性**（固件下行侧才会因断号告警，我们只解密）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from starlette.concurrency import run_in_threadpool

from .. import config
from ..macs import norm_mac

log = logging.getLogger(__name__)

# ================================================================ 协议常量
#: 设备 → 服务端 的 publish 主题（固件 NVS 的 publish_topic，由 OTA 下发）。
DEVICE_PUBLISH_TOPIC = "device-server"
#: client_id 里的模型标签。只影响可读性，broker 按 "@@@" 切出 MAC。
MQTT_MODEL_TAG = "elder"
#: 下行包首字节（固件 ``mqtt_protocol.cc`` OnMessage 里硬校验 ``data[0] != 0x01``）。
DOWNLINK_PACKET_TYPE = 0x01
#: AES-128-CTR 的 IV / nonce 长度，也是每个 UDP 包的明文头长度。
NONCE_LEN = 16
#: Opus 单帧长度上限（Opus 规范最大值 1275B）。用于「未绑定 peer 的
#: 试探性绑定」长度 plausibility 过滤，见 :meth:`MqttUdpServer._bind_peer`。
MAX_OPUS_FRAME = 1275
#: 断线后 session 任务的收尾时间（秒）。给 VoiceSession.run() 机会走完
#: finally（取消轮次任务、停播报器），超时就地取消。
SESSION_DRAIN_TIMEOUT = 5.0

# 传输断开时塞进 inbox 的哨兵。receive() 见到它返回 WS 形状的 disconnect，
# VoiceSession.run() 的主循环据此退出（与 WS 断开的字典形状一致）。
_DISCONNECTED = object()


class _DevInfo:
    """设备行的最小投影。

    为什么不在 DB session 外直接用 ORM 对象：SQLAlchemy 的实例在
    ``SessionLocal()`` 关闭后进入 detached 态，访问未加载属性会抛
    ``DetachedInstanceError``。broker 的鉴权在 threadpool 里查库、
    会话在事件循环里用结果，中间隔着一次线程切换 —— 投影成平凡对象
    把这个坑从结构上消掉。
    """

    __slots__ = ("device_id", "pk", "token_epoch")

    def __init__(self, pk: int, device_id: str, token_epoch: int) -> None:
        self.pk = pk
        self.device_id = device_id
        self.token_epoch = token_epoch


# ================================================================ 凭据
def signature_key() -> bytes:
    """MQTT 口令签名密钥。**由 JWT_SECRET 确定性派生**，不落任何存储。

    为什么不单独存一份：多一个密钥就多一处「轮换时忘掉它」的隐患。派生自
    JWT secret 意味着：运维轮换 JWT secret（那本来就会让全部设备 token
    失效、要重新扫码绑定）时，MQTT 凭据**同时**失效 —— 语义一致，不是耦合
    bug。设备侧的下一次 OTA（15s 轮询）会拿到新凭据，无需人工干预。
    """
    from ..security import JWT_SECRET
    return hashlib.sha256(b"elder-mqtt-signature-v1|" +
                          JWT_SECRET.encode("utf-8")).digest()


def mqtt_client_id(mac: str) -> str:
    """``GID-elder@@@{mac}@@@{mac}``，mac 用归一化形式（小写无冒号）。"""
    m = norm_mac(mac)
    return f"GID-{MQTT_MODEL_TAG}@@@{m}@@@{m}"


def mqtt_username() -> str:
    """base64 的 JSON。固件只原样存储/上报，服务端不解析内容。"""
    return base64.b64encode(b'{"ip":"unknown"}').decode("ascii")


def mqtt_password(client_id: str, username: str) -> str:
    """base64(HMAC-SHA256(client_id + "|" + username, signature_key))。

    与上游 ``generate_password_signature`` 逐字节同构 —— 将来若要把设备
    迁到上游官方网关，凭据不用重签。
    """
    digest = hmac.new(signature_key(),
                      (client_id + "|" + username).encode("utf-8"),
                      hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_password(client_id: str, username: str, password: str) -> bool:
    """常量时间比较。**不解析 username 内容**（它只是 base64 的 JSON 串）。"""
    try:
        given = base64.b64decode(password.encode("ascii"), validate=True)
    except Exception:  # noqa: BLE001  畸形 base64 一律按拒
        return False
    expect = hmac.new(signature_key(),
                      (client_id + "|" + username).encode("utf-8"),
                      hashlib.sha256).digest()
    return hmac.compare_digest(given, expect)


def mac_from_client_id(client_id: str) -> str:
    """从 ``GID-elder@@@{mac}@@@{mac}`` 取回 MAC；取不到返回空串。"""
    parts = client_id.split("@@@")
    if len(parts) != 3 or not parts[1]:
        return ""
    return norm_mac(parts[1])


def subscribe_topic(mac: str) -> str:
    """服务端自动订阅、并向设备下发消息的主题。"""
    return f"devices/p2p/{norm_mac(mac)}"


def mqtt_section(mac: str, host: str) -> dict[str, Any]:
    """OTA 响应的 ``mqtt`` 段（键名与固件 NVS 命名空间一一对应）。

    ``host`` 是**设备可达的** broker 主机名/IP（由 OTA 请求头推导，与
    websocket 段同源，见 ``voice.__init__._mqtt_endpoint_host``）。
    固件 ``ota.cc`` 把整个对象的字符串/数字值原样写进 NVS，所以键名
    不能改；多给的键只是白占一点 NVS，不会让固件报错。
    """
    cid = mqtt_client_id(mac)
    user = mqtt_username()
    return {
        "endpoint": f"{host}:{config.MQTT_PORT}",
        "client_id": cid,
        "username": user,
        "password": mqtt_password(cid, user),
        "keepalive": int(config.MQTT_KEEPALIVE),
        "publish_topic": DEVICE_PUBLISH_TOPIC,
        "subscribe_topic": subscribe_topic(mac),
    }


# ================================================================ AES-128-CTR
def aes128_ctr(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """AES-128-CTR。``nonce`` 就是 16 字节初始计数块（IV）。

    与固件的 ``mbedtls_aes_crypt_ctr(ctx, len, &nc_off=0, nonce,
    stream_block=0, in, out)`` 逐位等价：mbedtls 的 CTR 把**整个 16 字节**
    当大端计数器递增，``cryptography`` 的 ``modes.CTR(16 字节 IV)`` 同理。
    """
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def new_crypto() -> tuple[bytes, bytes]:
    """生成 (key 16B, nonce 16B)。nonce[0] 预置为下行包类型字节。"""
    key = secrets.token_bytes(16)
    nonce = bytearray(secrets.token_bytes(NONCE_LEN))
    nonce[0] = DOWNLINK_PACKET_TYPE
    return key, bytes(nonce)


def build_downlink_packet(key: bytes, base_nonce: bytes, payload: bytes,
                          sequence: int) -> bytes:
    """按固件下行契约组包：nonce[2:4]=len、[8:12]=0、[12:16]=seq（大端）+ 密文。

    独立成模块级函数（而不是 server 的方法）是为了让冒烟能**不启动任何
    服务**直接断言字节布局 —— 它正是「与固件逐字节兼容」的那个点。
    """
    if not payload or len(payload) > 0xFFFF:
        raise ValueError(f"下行帧长异常：{len(payload)}B")
    nonce = bytearray(base_nonce)
    nonce[2:4] = len(payload).to_bytes(2, "big")
    nonce[8:12] = b"\x00\x00\x00\x00"          # 无时间戳，同 WS 路径口径
    nonce[12:16] = sequence.to_bytes(4, "big")
    return bytes(nonce) + aes128_ctr(key, bytes(nonce), payload)


def parse_uplink_nonce(data: bytes) -> tuple[int, int, int] | None:
    """解析上行包头的 (payload_len, timestamp, sequence)；过短返回 None。

    同样独立出来供冒烟直接断言固件 ``SendAudio`` 的写入布局。
    """
    if len(data) < NONCE_LEN:
        return None
    return (int.from_bytes(data[2:4], "big"),
            int.from_bytes(data[8:12], "big"),
            int.from_bytes(data[12:16], "big"))


# ================================================================ MQTT 报文编解码
def encode_remaining_length(n: int) -> bytes:
    """MQTT 变长剩余长度（1~4 字节）。"""
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            return bytes(out)


def encode_packet(ptype: int, body: bytes = b"") -> bytes:
    return bytes([ptype]) + encode_remaining_length(len(body)) + body


def connack(session_present: bool, code: int) -> bytes:
    return encode_packet(0x20, bytes([1 if session_present else 0, code]))


def publish_packet(topic: str, payload: bytes) -> bytes:
    """QoS 0 下行 PUBLISH（无 packet id）。设备只订阅 QoS 0。"""
    body = struct.pack(">H", len(topic)) + topic.encode("utf-8") + payload
    return encode_packet(0x30, body)


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """读一个完整 MQTT 报文。返回 None = 连接已断 / 报文畸形。"""
    try:
        head = (await reader.readexactly(1))[0]
        length = 0
        mult = 1
        for _ in range(4):
            byte = (await reader.readexactly(1))[0]
            length += (byte & 0x7F) * mult
            if not byte & 0x80:
                break
            mult *= 128
        else:
            return None                      # 变长编码超过 4 字节：畸形
        body = await reader.readexactly(length) if length else b""
        return head, body
    except (asyncio.IncompleteReadError, ConnectionError):
        return None


# CONNECT 标志位（MQTT 3.1.1 spec §3.1.2.2）
_FLAG_USERNAME = 0x80
_FLAG_PASSWORD = 0x40
_FLAG_WILL = 0x04
_FLAG_CLEAN = 0x02


def parse_connect(body: bytes) -> dict[str, Any] | None:
    """解析 CONNECT。返回 None = 协议名/级别不符（回 CONNACK 0x01）。"""
    if len(body) < 10:
        return None
    try:
        name_len = struct.unpack_from(">H", body, 0)[0]
        if body[2:2 + name_len] != b"MQTT":
            return None
        off = 2 + name_len
        if off + 4 > len(body):
            return None
        level = body[off]
        if level != 4:                       # MQTT 3.1.1
            return None
        flags = body[off + 1]
        keepalive = struct.unpack_from(">H", body, off + 2)[0]
        off += 4

        def read_field() -> bytes:
            nonlocal off
            if off + 2 > len(body):
                raise ValueError("truncated")
            ln = struct.unpack_from(">H", body, off)[0]
            off += 2
            if off + ln > len(body):
                raise ValueError("truncated")
            raw = body[off:off + ln]
            off += ln
            return raw

        client_id = read_field().decode("utf-8", "replace")
        if flags & _FLAG_WILL:               # will flag：丢弃 topic/message
            read_field()
            read_field()
        username = read_field().decode("utf-8", "replace") if flags & _FLAG_USERNAME else ""
        password = read_field().decode("utf-8", "replace") if flags & _FLAG_PASSWORD else ""
    except (ValueError, struct.error):
        return None
    if not client_id:
        return None
    return {"client_id": client_id, "username": username,
            "password": password, "keepalive": keepalive,
            "clean": bool(flags & _FLAG_CLEAN)}


# ================================================================ 传输适配器
class _HeaderMap:
    """大小写不敏感的 headers（VoiceSession 只用 ``.get``）。

    为什么要自己做：WS 那边是 starlette 的 ``Headers``，MQTT 这边没有请求
    对象；而 ``_authenticate`` 读的是 ``authorization`` / ``device-id`` 两个
    小写键。用普通 dict 也行（键本来就小写），但**封一层**是为了将来加键时
    不会因为大小写再次踩坑（本仓在 HTTP 头上已经踩过一次，见冒烟里的
    ``_lower_headers`` 教训）。
    """

    def __init__(self, items: dict[str, str]) -> None:
        self._items = {k.lower(): v for k, v in items.items()}

    def get(self, key: str, default: Any = None) -> Any:
        return self._items.get(key.lower(), default)


class MqttUdpTransport:
    """把 VoiceSession 的 WS 形状接口桥到 MQTT(JSON) + UDP(音频)。

    会话侧看到的与 WebSocket **逐方法同构**：

    ====================== ==========================================
    ``receive()``          阻塞取一条消息：``{"text": json}`` /
                           ``{"bytes": opus}`` / disconnect 哨兵
    ``send_text(s)``       publish 到 ``devices/p2p/{mac}``
    ``send_bytes(b)``      加密后走 UDP 发给设备
    ``close(code=...)``    publish goodbye + 唤醒 receive()
    ``headers``            现铸的 device token + MAC（鉴权与 WS 同路）
    ====================== ==========================================
    """

    #: VoiceSession.run() 用 ``getattr(self.ws, "rewrite_hello", None)`` 探测，
    #: 见 session.py 的 hello 段。WS 传输没有这个属性 ⇒ 行为零变化。
    transport_name = "udp"

    def __init__(self, server: MqttUdpServer, mac: str, token: str,
                 device_id: str) -> None:
        self._server = server
        self._mac = norm_mac(mac)
        self.headers = _HeaderMap({
            "authorization": f"Bearer {token}",
            # 与 OTA 下发的 MAC 同形（归一化后），_authenticate 的
            # norm_mac 双边比较因此恒过 —— 身份链在 MQTT CONNECT 的
            # HMAC 口令那里已经验过一次，这里只是让同一套代码复跑。
            "device-id": norm_mac(device_id),
        })
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._closed = False

    # ---- 会话 → 设备 ----
    async def receive(self) -> dict[str, Any]:
        item = await self._inbox.get()
        if item is _DISCONNECTED:
            return {"type": "websocket.disconnect"}
        return item

    async def send_text(self, text: str) -> None:
        if self._closed:
            return
        self._server.publish(subscribe_topic(self._mac), text.encode("utf-8"))

    async def send_bytes(self, payload: bytes) -> None:
        if self._closed:
            return
        self._server.send_udp_audio(self._mac, payload)

    async def close(self, code: int | None = None) -> None:
        """WS close 的 MQTT 等价物：发 goodbye 让设备关音频通道，然后唤醒
        receive() 使会话主循环退出。``code`` 在 MQTT 侧无对应物（没有
        应用层关闭码），刻意忽略而不是硬造一个。
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._server.publish(subscribe_topic(self._mac),
                                 json.dumps({"type": "goodbye"},
                                            ensure_ascii=False).encode("utf-8"))
        except Exception:  # noqa: BLE001  收尾路径不能反抛
            log.debug("[mqtt %s] goodbye 发送失败", self._mac, exc_info=True)
        self._inbox.put_nowait(_DISCONNECTED)

    # ---- 设备 → 会话（broker / UDP 网关调用）----
    def push_text(self, text: str) -> None:
        """设备 publish 来的 JSON。hello / goodbye 在此拦截。

        为什么拦 hello：服务端 hello 由 ``VoiceSession.run()`` 在鉴权后
        立刻发出（经 rewrite_hello 带上 udp 参数），设备再发一遍是协议
        冗余，原样转给会话只会被 ``_on_json`` 当未知类型丢掉。

        为什么拦 goodbye 而不是当断开：固件 ``CloseAudioChannel`` 先 publish
        goodbye 再关 **UDP**，MQTT 连接仍在（下一次交互会重新
        OpenAudioChannel）。把它当断开会杀死整个会话，表现为「第二轮起
        设备能连上但不说话」。
        """
        try:
            obj = json.loads(text)
        except Exception:  # noqa: BLE001  非 JSON 的设备 publish：透传给会话
            obj = None
        if isinstance(obj, dict):
            mtype = obj.get("type")
            if mtype == "hello":
                log.debug("[mqtt %s] 收到设备 hello（冗余，服务端 hello 已发）",
                          self._mac)
                return
            if mtype == "goodbye":
                log.info("[mqtt %s] 设备关闭音频通道（goodbye），会话保持",
                         self._mac)
                return
        self._inbox.put_nowait({"text": text})

    def push_bytes(self, data: bytes) -> None:
        """UDP 解密后的 Opus 裸包。**协议版本 1 = 无头**（固件 UDP 侧
        解密后直接把 payload 当 Opus，见 mqtt_protocol.cc OnMessage），
        所以 MQTT 会话的 VoiceSession 必须以 protocol_version=1 构造，
        否则 _send_audio / _strip_binary_header 会去加减不存在的头。
        """
        self._inbox.put_nowait({"bytes": data})

    # ---- hello 改写钩子 ----
    def rewrite_hello(self, hello: dict[str, Any]) -> dict[str, Any]:
        """把 WS 形状的服务端 hello 改写成 MQTT+UDP 契约要求的形状。

        固件 ``ParseServerHello`` 的两个硬要求：``transport`` 必须等于
        ``"udp"``；``udp`` 对象必须带 server/port/key/nonce（hex）。缺一个
        设备就停在「等待服务器 hello」，10s 后报 SERVER_TIMEOUT。
        """
        hello["transport"] = "udp"
        hello["udp"] = self._server.udp_hello_params(self._mac)
        return hello


# ================================================================ 会话
class _MqttSession:
    """一个设备在 MQTT+UDP 通道上的全部状态。"""

    def __init__(self, mac: str, dev: _DevInfo, transport: MqttUdpTransport,
                 key: bytes, nonce: bytes, udp_host: str) -> None:
        self.mac = norm_mac(mac)
        self.dev = dev
        self.transport = transport
        self.key = key
        self.nonce = nonce
        self.udp_host = udp_host
        self.peer: tuple[str, int] | None = None   # 设备 UDP 端点（首个包学到）
        self.up_seq = 0                            # 最近一个上行包的 sequence
        self.down_seq = 0                          # 下行序列号（首个包 = 1）
        self.task: asyncio.Task | None = None
        self.closed = False


# ================================================================ UDP 网关
class _UdpProtocol(asyncio.DatagramProtocol):
    """单 socket 服务所有会话的 UDP 面。

    **为什么不给每个会话开一个 socket**：设备连上来的 UDP 源端口事先不
    知道（hello 交换里没有这个信息，设备是先建 MQTT 再建 UDP），单 socket
    + 「首个包定 peer」是唯一可行解。peer 归属的判定见
    :meth:`MqttUdpServer._bind_peer`。
    """

    def __init__(self, server: MqttUdpServer) -> None:
        self._server = server

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._server.on_udp(data, addr)

    def error_received(self, exc: Exception) -> None:      # pragma: no cover
        log.debug("[mqtt udp] socket 错误：%s", exc)


# ================================================================ 连接
class _MqttConn:
    """一条设备 MQTT TCP 连接的生命周期。"""

    def __init__(self, server: MqttUdpServer, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.session: _MqttSession | None = None
        self.subs: set[str] = set()
        self.closed = False

    # ---------------------------------------------------------------- 主循环
    async def run(self) -> None:
        try:
            packet = await read_packet(self.reader)
            if packet is None:
                return
            head, body = packet
            if (head & 0xF0) != 0x10:
                # 第一包不是 CONNECT：直接关。回 CONNACK 都没有意义
                # （没有 client_id 可关联）。
                return
            info = parse_connect(body)
            if info is None:
                self.writer.write(connack(False, 0x01))   # 协议名/级别不符
                await self.writer.drain()
                return
            ok, code, reason, dev = await self.server.authenticate(info)
            if not ok:
                log.info("[mqtt] CONNECT 被拒：client_id=%r 原因=%s",
                         info["client_id"], reason)
                self.writer.write(connack(False, code))
                await self.writer.drain()
                return
            self.session = await self.server.open_session(self, info, dev)
            if self.session is None:
                self.writer.write(connack(False, 0x05))   # not authorized
                await self.writer.drain()
                return
            self.writer.write(connack(False, 0x00))
            await self.writer.drain()
            log.info("[mqtt] 设备 %s 已连接（client_id=%s）",
                     self.session.mac, info["client_id"])
            await self._pump(info["keepalive"])
        except Exception as exc:  # noqa: BLE001  单条连接的任何异常都不许拖垮进程
            log.warning("[mqtt] 连接处理异常：%s: %s", type(exc).__name__, exc)
        finally:
            await self._teardown()

    async def _pump(self, keepalive: int) -> None:
        """收包循环：PUBLISH / PINGREQ / DISCONNECT / SUBSCRIBE。"""
        # 1.5×keepalive 是 MQTT 3.1.1 规范给服务端的超时口径。
        timeout = max(30.0, float(keepalive) * 1.5)
        while not self.closed:
            try:
                packet = await asyncio.wait_for(read_packet(self.reader),
                                                timeout=timeout)
            except asyncio.TimeoutError:
                log.info("[mqtt] keepalive 超时（%ss），关闭连接", int(timeout))
                return
            if packet is None:
                return
            head, body = packet
            ptype = head & 0xF0
            if ptype == 0x30:                  # PUBLISH
                await self._on_publish(head, body)
            elif ptype == 0xC0:                # PINGREQ
                self.writer.write(encode_packet(0xD0))
                await self.writer.drain()
            elif ptype == 0xE0:                # DISCONNECT
                log.info("[mqtt] 设备主动断开")
                return
            elif ptype == 0x82:                # SUBSCRIBE（设备固件不发，兜底）
                self._on_subscribe(body)
            elif ptype == 0xA0:                # UNSUBSCRIBE
                self._on_unsubscribe(body)
            elif ptype == 0x60:                # PUBACK（QoS 1 回执）
                pass
            # 其余报文类型一律忽略：最小 broker 不实现的就当不存在。

    async def _on_publish(self, head: int, body: bytes) -> None:
        qos = (head >> 1) & 0x03
        if len(body) < 2:
            return
        topic_len = struct.unpack_from(">H", body, 0)[0]
        if 2 + topic_len > len(body):
            return
        topic = body[2:2 + topic_len].decode("utf-8", "replace")
        off = 2 + topic_len
        packet_id = None
        if qos > 0:
            if off + 2 > len(body):
                return
            packet_id = struct.unpack_from(">H", body, off)[0]
            off += 2
        payload = body[off:]
        if qos == 1 and packet_id is not None:
            self.writer.write(encode_packet(0x40, struct.pack(">H", packet_id)))
            await self.writer.drain()
        self.server.route_publish(self, topic, payload)

    def _on_subscribe(self, body: bytes) -> None:
        if len(body) < 2:
            return
        packet_id = struct.unpack_from(">H", body, 0)[0]
        off = 2
        granted: list[int] = []
        while off + 2 <= len(body):
            tlen = struct.unpack_from(">H", body, off)[0]
            off += 2
            if off + tlen > len(body):
                break
            topic = body[off:off + tlen].decode("utf-8", "replace")
            off += tlen
            if off < len(body):
                off += 1                     # requested qos 字节
            # 只支持精确匹配：通配符订阅按字面存，永远匹配不上任何主题
            # （宁可不匹配，也不让 '+' 悄悄变成「订阅全部」）。
            self.subs.add(topic)
            granted.append(0)
        if granted:
            self.writer.write(encode_packet(
                0x90, struct.pack(">H", packet_id) + bytes(granted)))
            self.writer.drain()

    def _on_unsubscribe(self, body: bytes) -> None:
        if len(body) < 2:
            return
        packet_id = struct.unpack_from(">H", body, 0)[0]
        off = 2
        while off + 2 <= len(body):
            tlen = struct.unpack_from(">H", body, off)[0]
            off += 2
            if off + tlen > len(body):
                break
            self.subs.discard(body[off:off + tlen].decode("utf-8", "replace"))
            off += tlen
        self.writer.write(encode_packet(
            0xB0, struct.pack(">H", packet_id)))
        self.writer.drain()

    def send_packet(self, data: bytes) -> None:
        if self.closed:
            return
        try:
            self.writer.write(data)
        except Exception:  # noqa: BLE001  对端已断：写失败由读循环收尸
            self.closed = True

    async def _teardown(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.server.forget(self)
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass


# ================================================================ 服务端
class MqttUdpServer:
    """MQTT broker + UDP 网关 + 会话管理的门面。

    生命周期由 ``main.py`` 的 lifespan 起停（``config.MQTT_PORT > 0`` 才起）。
    **不在 import 期起**：import 时没有事件循环，且冒烟脚本要能显式控制。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _MqttSession] = {}
        self._conns: set[_MqttConn] = set()
        self._peers: dict[tuple[str, int], str] = {}     # udp addr -> mac
        self._tcp: asyncio.AbstractServer | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._udp_proto: _UdpProtocol | None = None

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self._tcp = await asyncio.start_server(
            self._handle_conn, config.MQTT_BIND_HOST, config.MQTT_PORT)
        loop = asyncio.get_running_loop()
        self._udp, self._udp_proto = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self),
            local_addr=(config.MQTT_BIND_HOST, config.MQTT_UDP_PORT))
        log.info("MQTT+UDP 传输已启动：broker %s:%d（**明文**，仅限可信网络），"
                 "UDP %s:%d", config.MQTT_BIND_HOST, config.MQTT_PORT,
                 config.MQTT_BIND_HOST, config.MQTT_UDP_PORT)

    async def stop(self) -> None:
        for conn in list(self._conns):
            conn.session = None
            conn.send_packet(encode_packet(0xE0))       # DISCONNECT
            try:
                conn.writer.close()
            except Exception:  # noqa: BLE001
                pass
        for mac in list(self._sessions):
            await self._close_session(mac)
        if self._udp is not None:
            self._udp.close()
        if self._tcp is not None:
            self._tcp.close()
            await self._tcp.wait_closed()
        log.info("MQTT+UDP 传输已停止")

    async def _handle_conn(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        if len(self._conns) >= config.MQTT_MAX_CONNS:
            log.warning("[mqtt] 连接数达上限 %d，拒绝新连接",
                        config.MQTT_MAX_CONNS)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        conn = _MqttConn(self, reader, writer)
        self._conns.add(conn)
        try:
            await conn.run()
        finally:
            self._conns.discard(conn)

    # ---------------------------------------------------------------- 鉴权
    async def authenticate(
            self, info: dict[str, Any]) -> tuple[bool, int, str, _DevInfo | None]:
        """校验 CONNECT。返回 (是否接受, CONNACK 码, 原因, 设备投影)。

        口令错误与设备不存在**故意分成两步、两个码**（4 = 坏口令，
        5 = 未授权）：排障时「设备没绑定」和「口令过期」是两件事，
        混成一个码会让人先去查算法。两者都不回显细节给对端。
        """
        client_id = info["client_id"]
        if not verify_password(client_id, info["username"], info["password"]):
            return False, 0x04, "口令签名不符", None
        mac = mac_from_client_id(client_id)
        if not mac:
            return False, 0x05, "client_id 里取不到 MAC", None

        def _load() -> _DevInfo | None:
            from ..db import SessionLocal
            from . import _find_device
            with SessionLocal() as db:
                dev = _find_device(db, mac)
                if dev is None or getattr(dev, "owner_id", None) is None:
                    return None
                return _DevInfo(int(dev.id),
                                str(getattr(dev, "device_id", "") or mac),
                                int(getattr(dev, "token_epoch", 0) or 0))

        try:
            dev = await run_in_threadpool(_load)
        except Exception as exc:  # noqa: BLE001  DB 抖动不能拒掉合法设备两次
            log.warning("[mqtt] 查询设备失败：%s", exc)
            return False, 0x03, "服务端暂不可用", None
        if dev is None:
            # 未绑定设备**不该拿到 mqtt 段**（OTA 侧已保证）；走到这里说明
            # 是手工构造的 client_id —— 拒。理由与 OTA 只发已绑定设备相同：
            # 无主连接 = 一个能调 LLM、能读主人记忆的匿名通道。
            return False, 0x05, "设备不存在或未绑定", None
        return True, 0x00, "ok", dev

    # ---------------------------------------------------------------- 会话
    async def open_session(self, conn: _MqttConn, info: dict[str, Any],
                           dev: _DevInfo) -> _MqttSession | None:
        mac = mac_from_client_id(info["client_id"])
        if not mac:
            return None
        # 同一 MAC 重连：旧的先退位（取消会话任务、释放 UDP peer 绑定）。
        # 不退位会出现两个会话共用一对 AES 参数、互抢下行序列号。
        old = self._sessions.get(mac)
        if old is not None:
            log.info("[mqtt] 设备 %s 重连，顶掉旧会话", mac)
            await self._close_session(mac)
        from ..security import issue_device_token
        token = issue_device_token(dev.pk, dev.device_id, dev.token_epoch)
        key, nonce = new_crypto()
        udp_host = config.MQTT_UDP_HOST or self._local_ip(conn)
        transport = MqttUdpTransport(self, mac, token, dev.device_id)
        from .session import VoiceSession
        # protocol_version=1：MQTT+UDP 的音频面是**裸 Opus 无头**
        # （固件 UDP 侧解密后直接当 Opus 用），与 WS v1 逐字节同构。
        session = VoiceSession(transport, protocol_version=1)
        sess = _MqttSession(mac, dev, transport, key, nonce, udp_host)
        sess.task = asyncio.create_task(session.run())
        sess.task.add_done_callback(
            lambda t, s=sess: self._on_session_done(s, t))
        self._sessions[mac] = sess
        conn.session = sess
        # 自动订阅下行主题 —— 复刻上游网关行为（固件从不自己 Subscribe）。
        conn.subs.add(subscribe_topic(mac))
        return sess

    def _on_session_done(self, sess: _MqttSession, task: asyncio.Task) -> None:
        """会话任务结束（正常 / 异常 / 被取消）后的统一收尾。

        ⚠️ 只清理**仍然登记在册**的这个会话：重连时新会话会顶掉旧的，
        旧任务的 done 回调晚一步触发，若无条件 pop 会把**新**会话从表里
        删掉（症状：设备连着、音频包全被丢，且没有任何报错）。
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("[mqtt %s] 会话任务异常退出：%s: %s",
                        sess.mac, type(exc).__name__, exc)
        else:
            log.info("[mqtt %s] 会话结束", sess.mac)
        if self._sessions.get(sess.mac) is sess:
            self._sessions.pop(sess.mac, None)
            for addr, m in list(self._peers.items()):
                if m == sess.mac:
                    self._peers.pop(addr, None)
        # 会话自己死了（鉴权失败 / 内部异常）也要把 TCP 连接收掉：
        # 否则设备会挂在一个没有会话的连接上，直到 keepalive 超时。
        for conn in list(self._conns):
            if conn.session is sess:
                conn.session = None
                asyncio.get_running_loop().create_task(conn._teardown())
                break

    @staticmethod
    def _local_ip(conn: _MqttConn) -> str:
        """设备这条 MQTT 连接到达的**服务端本地地址**。

        它能连上 broker 说明这个地址对它可达，直暴露场景（公网 IP:端口）
        天然正确。反代 / 隧道后面 sockname 是代理容器内网地址 ⇒ 必须用
        ``ELDER_MQTT_UDP_HOST`` 显式覆盖（见 config 注释）。
        """
        try:
            sock = conn.writer.get_extra_info("sockname")
            if sock:
                return str(sock[0])
        except Exception:  # noqa: BLE001
            pass
        return "127.0.0.1"

    async def _close_session(self, mac: str) -> None:
        sess = self._sessions.pop(mac, None)
        if sess is None:
            return
        sess.closed = True
        for addr, m in list(self._peers.items()):
            if m == mac:
                self._peers.pop(addr, None)
        task = sess.task
        if task is not None and not task.done():
            # 先唤醒 receive() 让主循环自己退（走 finally 清理播报器），
            # 超时再硬取消。shield 防止 wait_for 超时时把任务一起带走后
            # 又没人管它。
            sess.transport._inbox.put_nowait(_DISCONNECTED)
            try:
                await asyncio.wait_for(asyncio.shield(task),
                                       timeout=SESSION_DRAIN_TIMEOUT)
            except asyncio.TimeoutError:
                task.cancel()
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception:  # noqa: BLE001
                task.cancel()

    def forget(self, conn: _MqttConn) -> None:
        """连接断开：关掉它承载的会话（若有）。"""
        sess = conn.session
        if sess is None:
            return
        conn.session = None
        mac = sess.mac
        if self._sessions.get(mac) is sess:
            # 异步收尸：run() 的 finally 里不做长 await，避免卡住读循环。
            asyncio.get_running_loop().create_task(self._close_session(mac))

    # ---------------------------------------------------------------- 主题路由
    def publish(self, topic: str, payload: bytes) -> None:
        """服务端 → 设备：给所有订阅了该主题的连接发 PUBLISH。"""
        packet = publish_packet(topic, payload)
        for conn in list(self._conns):
            if topic in conn.subs:
                conn.send_packet(packet)

    def route_publish(self, conn: _MqttConn, topic: str, payload: bytes) -> None:
        """设备 → 服务端。

        设备只往 ``device-server`` publish 协议 JSON，且从不订阅 ⇒ 每条
        设备 publish 都送进它自己那个会话。主题仍然参与匹配（别的客户端
        订阅了 device-server 也能收到），但**不会**因此多送一份进会话。
        """
        for other in list(self._conns):
            if other is conn:
                continue
            if topic in other.subs:
                other.send_packet(publish_packet(topic, payload))
        sess = conn.session
        if sess is None:
            return
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("[mqtt %s] 丢弃非 UTF-8 publish（%dB）",
                        sess.mac, len(payload))
            return
        sess.transport.push_text(text)

    # ---------------------------------------------------------------- UDP 面
    def udp_hello_params(self, mac: str) -> dict[str, Any]:
        """hello 的 ``udp`` 段。key/nonce 是 hex（固件 DecodeHexString）。"""
        sess = self._sessions.get(norm_mac(mac))
        if sess is None:
            # 理论不可达（rewrite_hello 只在会话内被调用）；真到了就现铸一对，
            # 至少让设备能建 UDP —— 那一对解不开下行，但不会静默挂死。
            key, nonce = new_crypto()
            host = config.MQTT_UDP_HOST or "127.0.0.1"
        else:
            key, nonce = sess.key, sess.nonce
            host = sess.udp_host
        return {
            "server": host,
            "port": int(config.MQTT_UDP_PORT),
            "key": key.hex(),
            "nonce": nonce.hex(),
        }

    def send_udp_audio(self, mac: str, payload: bytes) -> None:
        """下行一帧 Opus。字节布局与固件 ``SendAudio`` 对称。"""
        sess = self._sessions.get(norm_mac(mac))
        if sess is None or sess.peer is None or self._udp is None:
            # 设备还没来过第一个 UDP 包（peer 未学到）⇒ 无处可发。
            # 静默丢弃：这种丢帧只可能发生在会话刚建立、设备还没开始推流时。
            return
        sess.down_seq += 1
        try:
            packet = build_downlink_packet(sess.key, sess.nonce, payload,
                                           sess.down_seq)
        except ValueError as exc:
            sess.down_seq -= 1
            log.warning("[mqtt %s] %s", mac, exc)
            return
        self._udp.sendto(packet, sess.peer)

    def on_udp(self, data: bytes, addr: tuple[str, int]) -> None:
        """收到一个 UDP 包。**热点路径**：先 peer 命中，再试探性绑定。"""
        mac = self._peers.get(addr)
        if mac is None:
            mac = self._bind_peer(data, addr)
            if mac is None:
                return
        sess = self._sessions.get(mac)
        if sess is None or sess.closed:
            return
        payload = self._decrypt(sess, data)
        if payload is None:
            return
        parsed = parse_uplink_nonce(data)
        if parsed is not None:
            sess.up_seq = parsed[2]
        sess.transport.push_bytes(payload)

    def _decrypt(self, sess: _MqttSession, data: bytes) -> bytes | None:
        if len(data) <= NONCE_LEN:
            log.debug("[mqtt %s] UDP 包过短（%dB），丢弃", sess.mac, len(data))
            return None
        try:
            return aes128_ctr(sess.key, data[:NONCE_LEN], data[NONCE_LEN:])
        except Exception:  # noqa: BLE001  解密失败只丢这一帧
            log.debug("[mqtt %s] UDP 解密失败", sess.mac, exc_info=True)
            return None

    def _bind_peer(self, data: bytes, addr: tuple[str, int]) -> str | None:
        """把新出现的 UDP 源地址绑到某个会话上。

        **为什么需要试探**：hello 交换里没有设备的 UDP 源端口（设备先建
        MQTT、后建 UDP），单 socket 服务多设备时无从直接对应。判据（按强度）:

        1. ``nonce[2:4]``（大端）必须等于密文长度 —— 固件 ``SendAudio``
           写死 ``htons(payload.size())``。它把长度对不上的噪声包挡掉；
        2. ``sequence`` 必须正好是该会话的下一个期望值（首包 = 1）。
           序列号在**明文头**里，所以这不是密码学判据 —— 它防的是随机
           噪声与乱序，不防有意构造的攻击者（防攻击者是 HMAC 口令的活，
           那在 TCP 层已经做了）；
        3. 只有一个候选时按 1+2 绑定；多个候选都声称同一个 sequence 时
           拒绝并告警（等下一个包）。

        NAT 重绑（设备源端口变了、会话还活着）也走这里：只要 sequence 对得上
        就换 peer，否则设备换网后音频会静默死亡。
        """
        if len(data) <= NONCE_LEN:
            return None
        declared = int.from_bytes(data[2:4], "big")
        cipher_len = len(data) - NONCE_LEN
        if declared != cipher_len or cipher_len > MAX_OPUS_FRAME:
            return None
        seq = int.from_bytes(data[12:16], "big")
        exact = [s for s in self._sessions.values() if s.up_seq + 1 == seq]
        if len(exact) == 1:
            chosen = exact[0]
        else:
            if exact:
                log.warning("[mqtt udp] %d 个会话都期望 sequence=%d，拒绝绑定 %s",
                            len(exact), seq, addr)
                return None
            # 没有序列号对得上的（首包乱序 / 计数被重置）：只有当一个会话
            # 还没绑过 peer 时才按长度判据兜底，否则宁丢不绑。
            fresh = [s for s in self._sessions.values() if s.peer is None]
            if len(fresh) != 1:
                return None
            chosen = fresh[0]
        if chosen.peer is not None and chosen.peer != addr:
            log.info("[mqtt %s] UDP peer 变基 %s:%d -> %s:%d（NAT 重绑）",
                     chosen.mac, chosen.peer[0], chosen.peer[1],
                     addr[0], addr[1])
            for old_addr, m in list(self._peers.items()):
                if m == chosen.mac and old_addr != addr:
                    self._peers.pop(old_addr, None)
        chosen.peer = addr
        self._peers[addr] = chosen.mac
        log.info("[mqtt %s] UDP peer 绑定 %s:%d（seq=%d）",
                 chosen.mac, addr[0], addr[1], seq)
        return chosen.mac
