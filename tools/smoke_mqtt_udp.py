"""MQTT + UDP 传输冒烟（F1）：broker 协议 + AES-128-CTR 音频面 + 端到端。

覆盖（与 ``server/app/voice/mqtt_udp.py`` 的契约一一对应）
---------------------------------------------------------
1. **线上字节布局**（不连任何服务，纯函数级）：
   - 下行组包 ``build_downlink_packet``：首字节 0x01（固件硬校验）、
     nonce[2:4]=payload_len、[8:12]=0、[12:16]=sequence（大端）；
   - 上行解包 ``parse_uplink_nonce`` + AES round-trip：按固件
     ``SendAudio`` 的写法构造的包，解回来必须**逐字节等于**原始 Opus；
   - CTR 是对合运算（加密两次 = 原文）—— 固件两个方向共用同一个
     ``mbedtls_aes_crypt_ctr`` 上下文，这个性质不成立就说明模式选错；
   - MQTT 变长剩余长度的 7 个边界值（0/127/128/16383/16384/2097151/2097152）
     过一遍真实 ``read_packet``；
   - ``parse_connect`` 正常 / 畸形 / 协议级别不符三分支；
   - 凭据派生：client_id 形态、HMAC 口令验签、改一个字符必拒、畸形 base64 必拒。
2. **会话内直连 UDP 网关**（不起服务、不打桩 socket）：
   ``MqttUdpServer.on_udp`` 收到固件形态的上行包 ⇒ 试探绑定 peer ⇒
   解密 ⇒ 原始 Opus 进 ``transport`` 的 inbox；长度字段说谎的包被丢弃
   且**不**换绑 peer。
3. **OTA 门控**：启用时已绑定设备拿到 mqtt 段（字段逐个校验 + 口令能过
   ``verify_password``）；**未绑定设备拿不到**；关掉开关时 ``_mqtt_section``
   返回 None（代码级判据，不是环境判据）。
4. **broker 协议**（真起服务，假设备是真 MQTT 客户端）：
   - 坏口令 ⇒ CONNACK 0x04（不是 0x00，也不是连接挂着不动）；
   - 好口令 ⇒ CONNACK 0x00 + 自动订阅 ⇒ 收到服务端 hello
     （transport=="udp" + udp{server,port,key,nonce} 四字段齐全）；
   - PINGREQ ⇒ PINGRESP；
   - 设备 goodbye ⇒ **会话不中断**（紧接着还能再跑一轮）；
   - TCP 断开 ⇒ 会话结束（服务端日志出现"会话结束"）。
5. **端到端一轮**（MQTT 控制面 + UDP 音频面，全真跑）：
   listen start → 加密 Opus 上行 → listen stop → stt/llm/tts JSON 下行 →
   加密 Opus 下行（逐包校验 sequence 从 1 递增、解回来等于假 TTS 帧）；
   随后 goodbye + 第二轮证明会话存活；再喂一个**长度字段说谎**的 UDP 包
   必须被丢弃（不影响第三轮）。

⚠️ 编解码替身（与 smoke_voice 同一纪律）
--------------------------------------
本机（Windows）没有 libopus 时，第 5 组的编解码用替身：只换
``opus_codec.decode_opus`` / ``encode_opus`` 两个**叶子函数**，封帧、
AES、协议、VAD、断句、播报时序全都真跑。替身脚本用**恒定 RMS**（不是按帧
脚本），这样多轮复用同一个补丁也不会因为计数器走到脚本末尾而退化成静音。
判定门限是 ``failed == 0``，不是 ``passed == total``。

反向变异（防假绿，本脚本自己跑）
--------------------------------
正常跑完之后，父进程对**每个变异**各起一个子进程（import hook 在内存里
改写 ``mqtt_udp.py``，不落盘、不动源文件），要求指定断言**变红**：

=====  =====================================  ==================================
M1     ``new_crypto`` 不预置 nonce[0]=0x01    下行包首字节 0x01（两组都查）
M2     下行 sequence 不递增                  下行 sequence 逐包递增且首包为 1
M3     broker 不自动订阅下行主题              收到服务端 hello
M4     上行用会话 nonce 而非包头 nonce 解密    on_udp 解密上行得到原始 Opus
=====  =====================================  ==================================

变异跑**不红** ⇒ 对应断言没有鉴别力 ⇒ 本脚本 exit 1。跳过变异：
``--no-mutation``。每个变异子进程用自己的 MQTT/UDP 口（父口 + 序号），
不会与父进程正在跑的服务抢端口。

用法::

    python tools/smoke_mqtt_udp.py [--port 8132] [--no-mutation]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import (  # 见 smoke_ports.py 模块 docstring
    guard_or_die,
    guard_port,
    release_port,
    resolve_port,
)

# 复用 smoke_voice 的假上游 / 播种 / 编解码替身 / 断言账本。
# 只 import 不跑它的 main（模块级无副作用，除了删自己那份 smoke DB）。
sys.path.insert(0, str(ROOT / "tools"))
import smoke_voice as SV

# MQTT broker / UDP 音频口。**刻意不在上游默认段**（1883/8884）上：
# 开发机可能真装着 mosquitto，撞了会得到"连到别人的 broker"这种假绿。
# 可用环境变量覆盖 —— 反向变异的子进程靠它拿到自己的口（父进程此时还占着）。
MQTT_PORT = int(os.environ.get("SMOKE_MQTT_PORT", "18183"))
UDP_PORT = int(os.environ.get("SMOKE_MQTT_UDP_PORT", "18884"))

MAC_BOUND = SV.MAC_BOUND          # aabbccddee01 归一化后
MAC_UNBOUND = SV.MAC_UNBOUND

#: 本进程起的 HTTP 服务端口（main() 里赋值）。**不能图省事用 SV.PORT** ——
#: 那是 smoke_voice 的端口，本脚本起的是自己的服务，用错了会连到
#: 「根本没起的服务」上，症状是 ConnectionRefused 而不是断言失败。
HTTP_PORT: int = 0

# ================================================================ 变异定义
#: 每条变异：(锚点原文, 替换成, 要求变红的断言名列表)。
#: 锚点必须**语义不同**（不是"关掉功能"）：M1 改的是固件硬校验的那个字节，
#: M2 改的是断号判据，M3 改的是上游文档明说的"网关按 client_id 自动订阅"，
#: M4 改的是 IV 来源（用会话 nonce 代替包头 nonce —— 两者只在首包相同）。
MUTATIONS: dict[str, tuple[str, str, list[str]]] = {
    "M1": (
        "    nonce[0] = DOWNLINK_PACKET_TYPE\n",
        "    nonce[0] = 0x00\n",
        ["第 1 条 ★ 下行包首字节为 0x01（固件 OnMessage 硬校验）",
         "第 4 条 ★ 下行包首字节为 0x01（固件 OnMessage 硬校验）"],
    ),
    "M2": (
        "        sess.down_seq += 1\n",
        "        sess.down_seq += 0\n",
        ["第 4 条 ★ 下行 sequence 逐包递增且首包为 1"],
    ),
    "M3": (
        "        conn.subs.add(subscribe_topic(mac))\n",
        "        pass  # 变异：不自动订阅\n",
        ["第 3 条 ★ 设备收到服务端 hello（transport=udp + udp 段）"],
    ),
    "M4": (
        "            return aes128_ctr(sess.key, data[:NONCE_LEN], data[NONCE_LEN:])\n",
        "            return aes128_ctr(sess.key, sess.nonce, data[NONCE_LEN:])\n",
        ["第 2 条 ★ on_udp 解密上行包得到原始 Opus"],
    ),
}


def _install_mutation_hook(mid: str) -> None:
    """在内存里改写 ``app/voice/mqtt_udp.py``（**不落盘**）。

    用 meta path finder 拦截该模块的 import：子进程在 import 任何 app.*
    之前装好钩子，之后无论谁 import 拿到的都是变异版。父进程**不装**。
    """
    import importlib.abc
    import importlib.util

    src = SERVER / "app" / "voice" / "mqtt_udp.py"
    original = src.read_text(encoding="utf-8")
    old, new, _required = MUTATIONS[mid]
    if original.count(old) != 1:
        raise SystemExit(
            f"变异 {mid} 锚点没找到或不唯一（{original.count(old)} 处）：{old!r}\n"
            f"护栏可能已被改名/重构，变异自检无从谈起 —— 请更新 MUTATIONS。")
    mutated = original.replace(old, new, 1)

    class _Loader(importlib.abc.Loader):
        def create_module(self, spec):  # noqa: D102
            return None

        def exec_module(self, module) -> None:  # noqa: D102
            exec(compile(mutated, str(src), "exec"), module.__dict__)

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):  # noqa: D102
            if fullname != "app.voice.mqtt_udp":
                return None
            return importlib.util.spec_from_loader(fullname, _Loader())

    sys.meta_path.insert(0, _Finder())
    print(f"[mutate] {mid} 已注入（仅本进程内存）：{old.strip()!r} -> {new.strip()!r}")


# ================================================================ 起服务
def start_server(port: int) -> object:
    """后台线程里跑 app。环境变量必须在 import app.* **之前**设好。"""
    db = SV.DB_DIR / f"elder_smoke_mqtt_{os.getpid()}.db"
    os.environ["ELDER_DB"] = f"sqlite:///{db.as_posix()}"
    os.environ["ELDER_JWT_SECRET"] = "smoke-mqtt-secret-0123456789abcdef"
    os.environ["ELDER_SCHEDULER_INTERVAL"] = "3600"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 假上游就在本机，确保绕过代理
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.pop(k, None)
    # ---- F1 开关：本脚本的全部意义就在于**开着**它 ----
    os.environ["ELDER_MQTT_PORT"] = str(MQTT_PORT)
    os.environ["ELDER_MQTT_UDP_PORT"] = str(UDP_PORT)
    # 显式清掉，保证「未配置 firmware 段」的默认行为可预期
    os.environ.pop("ELDER_FIRMWARE_VERSION", None)
    os.environ.pop("ELDER_FIRMWARE_URL", None)

    SV.SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler(SV.SERVER_LOG, encoding="utf-8")],
    )

    sys.path.insert(0, str(SERVER))
    import threading

    import uvicorn
    from app.main import app
    from app.voice import attach

    attach(app)   # 幂等；main 的 import 期已经 attach 过

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server


def wait_ready(port: int, timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with SV._LOCAL_OPENER.open(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/health"), timeout=2):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def udp_port_free(port: int) -> bool:
    """UDP 口空闲检查。**不能复用 is_port_free**：那个建的是 SOCK_STREAM，
    TCP 能绑不等于 UDP 能绑（两种 socket 的端口空间在同一主机上共享，
    但占用方只注册了自己那一类）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# ================================================================ 假设备
class _UdpSink(asyncio.DatagramProtocol):
    """假设备的 UDP 收包端：把收到的包塞进队列供断言消费。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    def datagram_received(self, data: bytes, addr) -> None:
        self.queue.put_nowait(data)


class FakeDevice:
    """最小 MQTT 3.1.1 客户端 + UDP 音频 peer。**只实现设备固件用的那部分**。

    复用服务端的 ``read_packet`` / ``publish_packet`` / ``encode_packet``：
    客户端与服务端的报文编解码是同一套字节格式，复用正是对它的校验。
    UDP socket **connected**（与固件 ``udp_->Connect`` 同形态）：只收
    broker 那个口来的包。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = MQTT_PORT) -> None:
        self.host = host
        self.port = port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.udp: asyncio.DatagramTransport | None = None

    async def connect(self, client_id: str, username: str,
                      password: str) -> int:
        from app.voice import mqtt_udp as M
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port)
        body = (struct.pack(">H", 4) + b"MQTT" + bytes([4])
                + bytes([0x02 | 0x80 | 0x40])        # clean + username + password
                + struct.pack(">H", 240)
                + struct.pack(">H", len(client_id)) + client_id.encode()
                + struct.pack(">H", len(username)) + username.encode()
                + struct.pack(">H", len(password)) + password.encode())
        self.writer.write(M.encode_packet(0x10, body))
        await self.writer.drain()
        head, payload = await M.read_packet(self.reader)
        if head is None or (head & 0xF0) != 0x20 or len(payload) < 2:
            return -1
        return payload[1]

    async def publish(self, topic: str, payload: bytes) -> None:
        from app.voice import mqtt_udp as M
        self.writer.write(M.publish_packet(topic, payload))
        await self.writer.drain()

    async def pingreq(self) -> bool:
        from app.voice import mqtt_udp as M
        self.writer.write(M.encode_packet(0xC0))
        await self.writer.drain()
        head, _ = await M.read_packet(self.reader)
        return head == 0xD0

    async def recv_publish(self, timeout: float = 10.0) -> tuple[str, bytes]:
        """收一个下行 PUBLISH，返回 (topic, payload)。超时抛 TimeoutError。"""
        from app.voice import mqtt_udp as M
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            head, body = await asyncio.wait_for(
                M.read_packet(self.reader),
                timeout=max(0.1, deadline - time.monotonic()))
            if head is None:
                raise ConnectionError("broker 关闭了连接")
            if (head & 0xF0) != 0x30:
                continue
            tlen = struct.unpack_from(">H", body, 0)[0]
            return (body[2:2 + tlen].decode("utf-8"), body[2 + tlen:])
        raise asyncio.TimeoutError()

    async def open_udp(self) -> None:
        loop = asyncio.get_running_loop()
        self.udp, _ = await loop.create_datagram_endpoint(
            lambda: _UdpSink(), remote_addr=("127.0.0.1", UDP_PORT))

    async def send_udp(self, data: bytes) -> None:
        assert self.udp is not None
        self.udp.sendto(data)

    async def recv_udp(self, timeout: float = 10.0) -> bytes:
        sink = self.udp.get_protocol()
        return await asyncio.wait_for(sink.queue.get(), timeout=timeout)

    async def close(self) -> None:
        from app.voice import mqtt_udp as M
        try:
            if self.writer is not None:
                self.writer.write(M.encode_packet(0xE0))
                await self.writer.drain()
        except Exception:  # noqa: BLE001
            pass
        for obj in (self.udp, self.writer):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass


def firmware_uplink_packet(key: bytes, base_nonce: bytes, opus: bytes,
                           seq: int, timestamp: int = 0) -> bytes:
    """**逐字节复刻**固件 ``MqttProtocol::SendAudio`` 的组包。

    复刻而不是调用被测代码：这一组断言的全部意义就是"服务端能解开固件
    真发出来的包"。复刻漏一个字段，这里就该红。
    """
    nonce = bytearray(base_nonce)
    struct.pack_into(">H", nonce, 2, len(opus))
    struct.pack_into(">I", nonce, 8, timestamp)
    struct.pack_into(">I", nonce, 12, seq)
    from app.voice import mqtt_udp as M
    return bytes(nonce) + M.aes128_ctr(key, bytes(nonce), opus)


# ================================================================ 断言组
def t_wire_codec() -> None:
    """第 1/2 组：字节布局 + AES round-trip + 会话内 UDP 网关直连。"""
    print("── 1. 线上字节布局（与固件 mqtt_protocol.cc 逐字段对齐）──")
    from app.voice import mqtt_udp as M

    key, nonce = M.new_crypto()
    opus = b"\xaa" * 60
    pkt = M.build_downlink_packet(key, nonce, opus, 7)
    SV.check("第 1 条 ★ 下行包首字节为 0x01（固件 OnMessage 硬校验）",
             pkt[0] == 0x01, hex(pkt[0]))
    SV.check("第 1 条 · 下行 nonce[2:4] = payload_len（大端）",
             int.from_bytes(pkt[2:4], "big") == len(opus),
             str(int.from_bytes(pkt[2:4], "big")))
    SV.check("第 1 条 · 下行 nonce[8:12] = 0（无时间戳，同 WS 路径口径）",
             pkt[8:12] == b"\x00\x00\x00\x00", pkt[8:12].hex())
    SV.check("第 1 条 ★ 下行 nonce[12:16] = sequence（大端）",
             int.from_bytes(pkt[12:16], "big") == 7,
             str(int.from_bytes(pkt[12:16], "big")))
    SV.check("第 1 条 ★ 下行包体用包头 nonce 当 IV 解密后逐字节等于原始 Opus",
             M.aes128_ctr(key, pkt[:16], pkt[16:]) == opus, "")
    SV.check("第 1 条 · 下行总长 = 16 + payload",
             len(pkt) == 16 + len(opus), str(len(pkt)))
    SV.check("第 1 条 · key/nonce 都是 16 字节（AES-128）",
             len(key) == 16 and len(nonce) == 16, f"{len(key)}/{len(nonce)}")
    SV.check("第 1 条 · hello 的 udp 段 key/nonce 是 hex 且长度正确",
             len(key.hex()) == 32 and len(nonce.hex()) == 32, "")
    # CTR 对合：固件两个方向共用同一个 mbedtls 上下文，这个性质不成立
    # 说明模式/IV 用法与固件不一致（表现为"服务端发的设备解不开"）。
    twice = M.aes128_ctr(key, nonce, M.aes128_ctr(key, nonce, opus))
    SV.check("第 1 条 ★ AES-128-CTR 是对合运算（加密两次 = 原文）",
             twice == opus, "")

    # 上行：按固件写法组包 → 服务端解包
    up = firmware_uplink_packet(key, nonce, opus, seq=3, timestamp=0x11223344)
    fields = M.parse_uplink_nonce(up)
    SV.check("第 1 条 ★ 上行解包读出 (len, timestamp, sequence)",
             fields == (len(opus), 0x11223344, 3), str(fields))
    SV.check("第 1 条 ★ 上行包用包头 nonce 解密后逐字节等于原始 Opus",
             M.aes128_ctr(key, up[:16], up[16:]) == opus, "")
    SV.check("第 1 条 · 不足 16 字节的上行包解包返回 None（不抛）",
             M.parse_uplink_nonce(b"\x00" * 15) is None, "")

    # ---- MQTT 变长剩余长度边界 ----
    async def _varint_roundtrip(n: int) -> bool:
        reader = asyncio.StreamReader()
        reader.feed_data(M.encode_packet(0x30, b"\x00" * n))
        reader.feed_eof()
        got = await M.read_packet(reader)
        return got is not None and got[0] == 0x30 and len(got[1]) == n

    async def _varint_all() -> list[bool]:
        return [await _varint_roundtrip(n)
                for n in (0, 127, 128, 16383, 16384, 2097151, 2097152)]

    ok = asyncio.run(_varint_all())
    SV.check("第 1 条 ★ MQTT 变长剩余长度 7 个边界值全部 round-trip"
             "（0/127/128/16383/16384/2097151/2097152）",
             all(ok), str(ok))

    # ---- CONNECT 解析 ----
    def _connect_body(cid: str, user: str, pw: str, level: int = 4) -> bytes:
        return (struct.pack(">H", 4) + b"MQTT" + bytes([level])
                + bytes([0x02 | 0x80 | 0x40]) + struct.pack(">H", 240)
                + struct.pack(">H", len(cid)) + cid.encode()
                + struct.pack(">H", len(user)) + user.encode()
                + struct.pack(">H", len(pw)) + pw.encode())

    parsed = M.parse_connect(_connect_body("cid", "user", "pw"))
    SV.check("第 1 条 ★ parse_connect 取回 client_id/username/password/keepalive",
             parsed is not None and parsed["client_id"] == "cid"
             and parsed["username"] == "user" and parsed["password"] == "pw"
             and parsed["keepalive"] == 240 and parsed["clean"] is True,
             str(parsed))
    SV.check("第 1 条 · 协议级别不是 4 时 parse_connect 返回 None"
             "（服务端据此回 CONNACK 0x01）",
             M.parse_connect(_connect_body("cid", "u", "p", level=3)) is None, "")
    SV.check("第 1 条 · 截断的 CONNECT 返回 None 而不是抛",
             M.parse_connect(b"\x00\x04MQ") is None, "")

    # ---- 凭据派生 ----
    from app.macs import norm_mac
    mac = norm_mac(MAC_BOUND)
    cid = M.mqtt_client_id(MAC_BOUND)
    SV.check("第 1 条 ★ client_id 形如 GID-elder@@@{mac}@@@{mac}（MAC 归一化）",
             cid == f"GID-elder@@@{mac}@@@{mac}", cid)
    SV.check("第 1 条 ★ 口令是 base64(HMAC-SHA256(client_id+'|'+username))，"
             "且 verify_password 常量时间比对通过",
             M.verify_password(cid, M.mqtt_username(),
                               M.mqtt_password(cid, M.mqtt_username())), "")
    SV.check("第 1 条 ★ 改一个字符的口令必须被拒（不是\"看着像就行\"）",
             not M.verify_password(
                 cid, M.mqtt_username(),
                 M.mqtt_password(cid, M.mqtt_username())[:-1] + "X"), "")
    SV.check("第 1 条 · 畸形 base64 口令被拒而不是抛",
             not M.verify_password(cid, M.mqtt_username(), "!!!"), "")
    SV.check("第 1 条 ★ 从 client_id 里取回 MAC（归一化）",
             M.mac_from_client_id(cid) == mac, M.mac_from_client_id(cid))
    SV.check("第 1 条 · 不合形的 client_id 取不到 MAC（返回空串）",
             M.mac_from_client_id("GID-elder@@@only-one") == "", "")
    SV.check("第 1 条 · subscribe_topic = devices/p2p/{mac}",
             M.subscribe_topic(MAC_BOUND) == f"devices/p2p/{mac}", "")

    # ---- 第 2 组：on_udp 直连（不起服务）----
    print("── 2. UDP 网关直连：绑定 peer + 解密 + 进 inbox ──")

    async def _on_udp_case() -> tuple[list[bytes], object]:
        srv = M.MqttUdpServer()
        key2, nonce2 = M.new_crypto()
        transport = M.MqttUdpTransport(srv, MAC_BOUND, "tok", MAC_BOUND)
        sess = M._MqttSession(MAC_BOUND, M._DevInfo(1, MAC_BOUND, 0),
                              transport, key2, nonce2, "127.0.0.1")
        srv._sessions[sess.mac] = sess      # 键必须是归一化 MAC（与实现一致）
        got: list[bytes] = []
        transport.push_bytes = got.append          # 记录器替身 inbox
        up2 = firmware_uplink_packet(key2, nonce2, opus, seq=1)
        srv.on_udp(up2, ("127.0.0.1", 5555))
        # 长度字段说谎的第二个包：必须被丢弃、且**不**换绑 peer
        liar = bytearray(up2)
        struct.pack_into(">H", liar, 2, 999)
        srv.on_udp(bytes(liar), ("127.0.0.1", 6666))
        return got, sess

    got, sess = asyncio.run(_on_udp_case())
    SV.check("第 2 条 ★ on_udp 解密上行包得到原始 Opus",
             got == [opus], f"{len(got)} 帧")
    SV.check("第 2 条 ★ on_udp 把 UDP 源地址绑到会话（peer 学到了）",
             sess.peer == ("127.0.0.1", 5555), str(sess.peer))
    SV.check("第 2 条 ★ 长度字段说谎的包被丢弃且不换绑 peer"
             "（nonce[2:4] != 实际密文长）",
             len(got) == 1 and sess.peer == ("127.0.0.1", 5555),
             f"got={len(got)} peer={sess.peer}")


def t_ota_gate() -> None:
    """第 3 组：OTA mqtt 段门控（启用给、关掉不给、未绑定不给）。"""
    print("── 3. OTA mqtt 段门控 ──")
    from app import config
    from app.voice import _mqtt_section
    from app.voice import mqtt_udp as M

    class _Req:
        headers = {"host": f"127.0.0.1:{HTTP_PORT}"}

    # ---- 代码级判据：开关关着时必须返回 None ----
    # 用 monkeypatch 而不是重启服务：重启只能证明"环境变了"，这里要证明的
    # 是"if config.MQTT_PORT <= 0: return None" 这一行真的在起作用。
    saved = config.MQTT_PORT
    try:
        config.MQTT_PORT = 0
        SV.check("第 3 条 ★ ELDER_MQTT_PORT<=0 时 _mqtt_section 返回 None"
                 "（删掉这行 = 全部设备被静默切到 MQTT）",
                 _mqtt_section(MAC_BOUND, _Req()) is None, "")
        config.MQTT_PORT = saved
        sec = _mqtt_section(MAC_BOUND, _Req())
        SV.check("第 3 条 · 启用时 _mqtt_section 返回七字段",
                 isinstance(sec, dict) and set(sec) == {
                     "endpoint", "client_id", "username", "password",
                     "keepalive", "publish_topic", "subscribe_topic"},
                 str(sorted(sec)) if isinstance(sec, dict) else str(sec))
        SV.check("第 3 条 ★ endpoint 主机来自请求 Host（设备可达），端口是 MQTT 口",
                 sec["endpoint"] == f"127.0.0.1:{MQTT_PORT}", str(sec["endpoint"]))
        SV.check("第 3 条 · 口令能过 verify_password（OTA 签发的与 broker 验的同源）",
                 M.verify_password(sec["client_id"], sec["username"],
                                   sec["password"]), "")
        SV.check("第 3 条 · keepalive 是数字（固件按数字写 NVS）",
                 isinstance(sec["keepalive"], int)
                 and sec["keepalive"] == config.MQTT_KEEPALIVE,
                 str(sec["keepalive"]))
        SV.check("第 3 条 · publish/subscribe 主题与 broker 用的一致",
                 sec["publish_topic"] == M.DEVICE_PUBLISH_TOPIC
                 and sec["subscribe_topic"] == M.subscribe_topic(MAC_BOUND), "")
    finally:
        config.MQTT_PORT = saved

    # ---- 端到端：OTA 响应本身 ----
    def _ota(mac: str) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{HTTP_PORT}/api/voice/ota", data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "Device-Id": mac,
                     "Client-Id": "smoke-mqtt", "Activation-Version": "1"})
        with SV._LOCAL_OPENER.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    bound = _ota(MAC_BOUND)
    SV.check("第 3 条 ★ 已绑定设备的 OTA 响应带 mqtt 段（设备将改走 MQTT+UDP）",
             isinstance(bound.get("mqtt"), dict), str(list(bound.keys())))
    SV.check("第 3 条 · 同一响应仍带 websocket 段（回退路径还在）",
             isinstance(bound.get("websocket"), dict), "")

    unbound = _ota(MAC_UNBOUND)
    SV.check("第 3 条 ★ 未绑定设备的 OTA 响应**不带** mqtt 段"
             "（无主连接 = 能调 LLM、能读主人记忆的匿名通道）",
             "mqtt" not in unbound, str(list(unbound.keys())))


async def t_broker_and_e2e(ids: dict) -> None:
    """第 4/5 组：broker 协议 + 端到端一轮（跑在同一个事件循环里）。"""
    from app import config
    from app.voice import mqtt_udp as M

    print("── 4. broker 协议：坏口令 / hello 交换 / ping / goodbye / 断开 ──")

    # ---- 坏口令 ----
    bad = FakeDevice()
    code = await bad.connect(M.mqtt_client_id(MAC_BOUND), M.mqtt_username(),
                             "AAAA")
    SV.check("第 4 条 ★ 坏口令收到 CONNACK 0x04（不是 0x00，也不是挂着不动）",
             code == 0x04, f"code={code}")
    await bad.close()

    # ---- 好口令：hello 交换 ----
    dev = FakeDevice()
    cid = M.mqtt_client_id(MAC_BOUND)
    code = await dev.connect(cid, M.mqtt_username(), M.mqtt_password(
        cid, M.mqtt_username()))
    SV.check("第 4 条 ★ 好口令收到 CONNACK 0x00", code == 0x00, f"code={code}")

    # 设备先发自己的 hello（固件 OpenAudioChannel 的第一步）
    await dev.publish(M.DEVICE_PUBLISH_TOPIC, json.dumps({
        "type": "hello", "version": 3, "transport": "udp",
        "audio_params": {"format": "opus", "sample_rate": 16000,
                         "channels": 1, "frame_duration": 60},
    }).encode())
    try:
        topic, payload = await dev.recv_publish(timeout=15)
    except (asyncio.TimeoutError, ConnectionError) as exc:
        # 变异 M3（不自动订阅）就走这里：必须落成一条**红断言**而不是
        # 未捕获异常 —— 后者只会让子进程崩掉，父进程看不到"指定断言变红"。
        SV.check("第 3 条 ★ 设备收到服务端 hello（transport=udp + udp 段）",
                 False, f"{type(exc).__name__}: {exc}")
        await dev.close()
        return
    hello = json.loads(payload.decode("utf-8"))
    SV.check("第 4 条 · 服务端 hello 发在自动订阅的主题上"
             f"（{M.subscribe_topic(MAC_BOUND)}）",
             topic == M.subscribe_topic(MAC_BOUND), topic)
    SV.check("第 3 条 ★ 设备收到服务端 hello（transport=udp + udp 段）",
             hello.get("transport") == "udp", str(hello.get("transport")))
    udp = hello.get("udp") or {}
    SV.check("第 3 条 ★ hello 的 udp 段四字段齐全（server/port/key/nonce）",
             set(udp) >= {"server", "port", "key", "nonce"}
             and len(str(udp.get("key", ""))) == 32
             and len(str(udp.get("nonce", ""))) == 32,
             str({k: ("<hex>" if k in ("key", "nonce") else v)
                  for k, v in udp.items()}))
    SV.check("第 3 条 ★ udp.port 与我们监听的 UDP 口一致",
             udp.get("port") == UDP_PORT, str(udp.get("port")))
    SV.check("第 3 条 · hello 的 audio_params 与服务端配置一致"
             "（24kHz/60ms，固件按我们声明的解）",
             (hello.get("audio_params") or {}).get("sample_rate")
             == config.VOICE_OUTPUT_SAMPLE_RATE,
             str(hello.get("audio_params")))
    key = bytes.fromhex(str(udp.get("key", "")))
    base_nonce = bytes.fromhex(str(udp.get("nonce", "")))

    SV.check("第 4 条 ★ PINGREQ 收到 PINGRESP", await dev.pingreq(), "")

    await dev.open_udp()

    # ---- 第 5 组：端到端一轮 ----
    print("── 5. 端到端一轮：MQTT 控制面 + UDP 加密音频面 ──")
    # 恒定 RMS（不是按帧脚本）：多轮复用同一个补丁，计数器走到末尾也不会
    # 退化成静音 —— 按帧脚本会让第二轮起没有语音、ASR 不被调用，
    # 表现是"会话莫名死了"，查半天才知道是测试自己的计数器。
    backup = SV._patch_opus(rms_script=0.5)
    try:
        async def run_turn() -> dict:
            await dev.publish(M.DEVICE_PUBLISH_TOPIC, json.dumps({
                "session_id": hello.get("session_id", ""), "type": "listen",
                "state": "start", "mode": "auto"}).encode())
            await asyncio.sleep(0.25)
            # 8 帧 = 480ms > VOICE_MIN_SPEECH_MS(300)。帧内容无所谓
            # （decode 是替身），但**封包必须按固件写法**。
            for i in range(1, 9):
                await dev.send_udp(firmware_uplink_packet(
                    key, base_nonce, SV.FAKE_OPUS_PACKET, seq=i))
                await asyncio.sleep(0.02)
            await dev.publish(M.DEVICE_PUBLISH_TOPIC, json.dumps({
                "session_id": hello.get("session_id", ""), "type": "listen",
                "state": "stop"}).encode())
            msgs: list[dict] = []
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    _topic, payload2 = await dev.recv_publish(timeout=5)
                except asyncio.TimeoutError:
                    break
                obj = json.loads(payload2.decode("utf-8"))
                msgs.append(obj)
                if obj.get("type") == "tts" and obj.get("state") == "stop":
                    break
            # tts stop 之后节流器可能还有几帧在飞：最多再收 3s
            audio: list[bytes] = []
            drain_end = time.monotonic() + 3
            while time.monotonic() < drain_end:
                try:
                    audio.append(await dev.recv_udp(timeout=1.0))
                except asyncio.TimeoutError:
                    break
            return {"msgs": msgs, "audio": audio}

        turn1 = await run_turn()
        types1 = [o.get("type") for o in turn1["msgs"]]
        SV.check("第 5 条 ★ 端到端收到 stt（假 ASR 的文本，音频面真的通了）",
                 any(o.get("type") == "stt" for o in turn1["msgs"]), str(types1))
        stt = next((o for o in turn1["msgs"] if o.get("type") == "stt"), {})
        SV.check("第 5 条 · stt 文本 = 假 ASR 返回值",
                 stt.get("text") == SV.FAKE_ASR_TEXT, str(stt.get("text")))
        SV.check("第 5 条 ★ 端到端收到 tts start/stop 配对",
                 any(o.get("type") == "tts" and o.get("state") == "start"
                     for o in turn1["msgs"])
                 and any(o.get("type") == "tts" and o.get("state") == "stop"
                         for o in turn1["msgs"]),
                 str(types1))
        SV.check("第 5 条 · 收到 llm 情绪帧（F4 接线在新通道上同样生效）",
                 any(o.get("type") == "llm" for o in turn1["msgs"]), str(types1))

        audio = turn1["audio"]
        SV.check("第 5 条 ★ 下行 UDP 至少一帧（加密音频面通）",
                 len(audio) >= 1, f"{len(audio)} 帧")
        SV.check("第 4 条 ★ 下行包首字节为 0x01（固件 OnMessage 硬校验）",
                 all(p[0] == 0x01 for p in audio),
                 str([hex(p[0]) for p in audio[:3]]))
        seqs = [int.from_bytes(p[12:16], "big") for p in audio]
        SV.check("第 4 条 ★ 下行 sequence 逐包递增且首包为 1",
                 seqs == list(range(1, len(audio) + 1)), str(seqs[:8]))
        decoded = [M.aes128_ctr(key, p[:16], p[16:]) for p in audio]
        SV.check("第 4 条 ★ 下行密文解回来逐字节等于假 TTS 帧（AES 参数正确）",
                 bool(decoded) and all(d == SV.FAKE_OPUS_PACKET for d in decoded),
                 f"{len(decoded)} 帧")
        SV.check("第 5 条 · 下行 nonce[2:4] 与 payload 长一致",
                 all(int.from_bytes(p[2:4], "big") == len(p) - 16 for p in audio),
                 "")

        # ---- goodbye 不杀会话：第二轮还必须能跑 ----
        await dev.publish(M.DEVICE_PUBLISH_TOPIC, json.dumps({
            "type": "goodbye"}).encode())
        await asyncio.sleep(0.5)
        turn2 = await run_turn()
        SV.check("第 4 条 ★ 设备 goodbye 之后会话仍存活（第二轮照常 stt）",
                 any(o.get("type") == "stt" for o in turn2["msgs"]),
                 str([o.get("type") for o in turn2["msgs"]]))

        # ---- 噪声包不搞坏会话：第三轮 ----
        liar = bytearray(firmware_uplink_packet(key, base_nonce,
                                                SV.FAKE_OPUS_PACKET, seq=99))
        struct.pack_into(">H", liar, 2, 999)
        await dev.send_udp(bytes(liar))
        turn3 = await run_turn()
        SV.check("第 5 条 ★ 长度字段说谎的 UDP 包被丢弃，第三轮照常",
                 any(o.get("type") == "stt" for o in turn3["msgs"]),
                 str([o.get("type") for o in turn3["msgs"]]))
    finally:
        SV._restore_opus(backup)

    # ---- 断开 ⇒ 会话结束 ----
    await dev.close()
    await asyncio.sleep(1.5)
    tail = (SV.SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-40000:]
            if SV.SERVER_LOG.exists() else "")
    SV.check("第 4 条 ★ TCP 断开后会话结束（日志出现\"会话结束\"）",
             "会话结束" in tail, "")


# ================================================================ main
def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="MQTT+UDP 传输冒烟")
    # 位置参数端口是**仓库约定**，不是可选糖：smoke_ports.py 的模块文档写明
    # 「命令行仍然可以覆盖默认值（位置参数或 --port N），CI 就是靠这个显式
    # 指定端口的」，server-ci.yml 里每一步也都是 `... smoke_xxx.py <port>`。
    # 本脚本早期只声明了 --port，CI 一步会直接 argparse rc=2 全红 ——
    # 所以位置参数必须收，且与 --port 等价。
    ap.add_argument("port", type=int, nargs="?", default=None,
                    help="HTTP 端口（与 --port 等价；不给则查 smoke_ports 表）")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-mutation", action="store_true",
                    help="跳过反向变异自检")
    ap.add_argument("--mutate", choices=sorted(MUTATIONS), default=None,
                    help=argparse.SUPPRESS)   # 内部用：变异子进程
    ap.add_argument("--out", default=None, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def _run_groups(ids: dict) -> None:
    t_wire_codec()
    t_ota_gate()
    asyncio.run(t_broker_and_e2e(ids))


def _dump_results(path: str) -> None:
    payload = {"passed": [n for n, ok, _d in SV.results if ok],
               "failed": [n for n, ok, _d in SV.results if not ok]}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False),
                          encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    global HTTP_PORT
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    mutate = args.mutate

    if mutate:
        # 变异子进程：装钩子 → 起服务 → 跑全部组 → 落盘结果。
        _install_mutation_hook(mutate)
        port = args.port or (resolve_port("smoke_mqtt_udp") + 1)
        HTTP_PORT = port
        upstream, up_port = SV.start_upstream()
        server = start_server(port)
        if not wait_ready(port):
            print("[fatal] 变异子进程服务未就绪")
            return 1
        ids = SV.seed(up_port)
        _run_groups(ids)
        try:
            server.should_exit = True
        except Exception:  # noqa: BLE001
            pass
        upstream.shutdown()
        if args.out:
            _dump_results(args.out)
        failed = [n for n, ok, _d in SV.results if not ok]
        print(f"\n[mutate {mutate}] 失败 {len(failed)} 项：")
        for n in failed:
            print(f"  - {n}")
        return 1 if failed else 0

    port = args.port or resolve_port("smoke_mqtt_udp")
    HTTP_PORT = port
    print(f"[boot] MQTT+UDP 传输冒烟，HTTP {port}，broker {MQTT_PORT}，UDP {UDP_PORT}")
    guard_or_die(port, "smoke_mqtt_udp.py")
    guard_or_die(MQTT_PORT, "smoke_mqtt_udp.py")
    if not udp_port_free(UDP_PORT):
        print(f"[boot] ✗ UDP 口 {UDP_PORT} 已被占用。\n"
              f"  换口重跑：SMOKE_MQTT_PORT=<tcp口> SMOKE_MQTT_UDP_PORT=<udp口> "
              f"python tools/smoke_mqtt_udp.py")
        return 1
    hold, hold_hint = guard_port(port)
    if hold is None:
        print(hold_hint)
        return 1

    upstream, up_port = SV.start_upstream()
    print(f"[boot] 假上游 :{up_port}")
    release_port(hold, port)

    server = start_server(port)
    if not wait_ready(port):
        print("[fatal] 服务未就绪。日志尾部：")
        print(SV._read_log_tail())
        return 1
    print("[boot] ready\n")

    try:
        ids = SV.seed(up_port)
        print(f"[boot] 播种完成：device_pk={ids['device_pk']}\n")
        _run_groups(ids)
    finally:
        try:
            server.should_exit = True
        except Exception:  # noqa: BLE001
            pass
        upstream.shutdown()
        release_port(hold, port)

    passed = sum(1 for _n, ok, _d in SV.results if ok)
    failed = [n for n, ok, _d in SV.results if not ok]
    print("\n" + "=" * 64)
    print(f"通过 {passed} 项，失败 {len(failed)} 项，跳过 {len(SV.skipped)} 项")
    if failed:
        print("\n失败的断言：")
        for name in failed:
            print(f"  - {name}")
        print("\n结论：**红**")
        return 1

    # ---- 反向变异自检 ----
    if args.no_mutation:
        print("\n结论：**绿**（--no-mutation：未做变异自检）")
        return 0

    print("\n── 反向变异自检（每条都必须让指定断言变红）──")
    mutation_failures: list[str] = []
    for i, (mid, (_old, _new, required)) in enumerate(MUTATIONS.items()):
        mport = port + 1 + i
        out = SV.DB_DIR / f"elder_smoke_mqtt_mut_{mid}_{os.getpid()}.json"
        child_env = dict(os.environ)
        # 每个子进程拿自己的 MQTT/UDP 口：父进程此刻还占着 18183/18884，
        # 共用会让子进程连到**父进程的 broker** 上 —— 那正是"连到别人的
        # 服务却全绿"的假绿形态（见 smoke_ports.py 的教训）。
        child_env["SMOKE_MQTT_PORT"] = str(MQTT_PORT + 1 + i)
        child_env["SMOKE_MQTT_UDP_PORT"] = str(UDP_PORT + 1 + i)
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 "--mutate", mid, "--port", str(mport), "--out", str(out)],
                capture_output=True, text=True, timeout=900,
                cwd=str(ROOT), env=child_env)
        except subprocess.TimeoutExpired:
            mutation_failures.append(f"{mid}: 变异子进程超时（900s）")
            continue
        try:
            summary = json.loads(out.read_text(encoding="utf-8"))
            child_failed = summary.get("failed", [])
        except Exception:  # noqa: BLE001
            child_failed = []
        missing = [r for r in required if r not in child_failed]
        if proc.returncode != 1:
            mutation_failures.append(
                f"{mid}: 变异子进程退出码 {proc.returncode}（期望 1 = 有断言变红）")
        elif missing:
            mutation_failures.append(
                f"{mid}: 这些断言本该变红却没有 —— {missing}")
        else:
            print(f"  {mid} ✓ 指定断言变红：{required[0][:44]}")
        if proc.returncode != 1 or missing:
            # 子进程输出只在这时候打印：绿的时候不打，免得刷屏。
            tail = (proc.stdout or "")[-1500:] + "\n" + (proc.stderr or "")[-1500:]
            print(f"    [子进程输出尾部]\n{tail}")
    if mutation_failures:
        print("\n变异自检失败（对应断言没有鉴别力 = 假绿）：")
        for line in mutation_failures:
            print(f"  - {line}")
        print("\n结论：**红**")
        return 1
    print(f"\n结论：**绿**（含 {len(MUTATIONS)} 条反向变异自检）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
