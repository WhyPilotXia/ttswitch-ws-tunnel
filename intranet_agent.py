#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import re
import ssl
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable

import aiohttp
from aiohttp import ClientSession, WSMsgType
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

def _load_env_txt() -> dict:
    """读取脚本同目录 .env.txt，解析 KEY=VALUE 行（# 开头为注释）。"""
    env = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


# ==================== 内网服务器配置 ====================
# 本地 TT Switch（本机 Mac）的内网 IP：
# 优先读取同目录 .env.txt 中的 IP=xxx（推荐；DHCP 变化后只改 .env.txt）。
# 没有该文件或未配置 IP 时，才使用下面的内置默认值。
_ENV = _load_env_txt()
LOCAL_TTSWITCH_IP = _ENV.get("IP", "10.34.105.48")
LOCAL_TTSWITCH_PORT = 15721

# 公网服务器没有 HTTPS 证书，所以先使用 ws://；隧道载荷会在应用层加密。
PUBLIC_TUNNEL_URL = "ws://118.31.105.6:18443/_tunnel"

# 必须与 public_relay.py 完全一致，建议至少 32 个随机字符。
TUNNEL_SECRET = "ttunnel-FhCiE5K1gUAokBybTYCl5_m1f1jgx6ayT27yxWeFDbA"

# 后续改用 wss:// 且证书可信时保持 True；自签名测试可临时改为 False。
VERIFY_PUBLIC_TLS = True

AUTH_TIMEOUT = 10
CONNECT_TIMEOUT = 10
REQUEST_BODY_IDLE_TIMEOUT = 300
WS_HEARTBEAT = 20
CHUNK_SIZE = 64 * 1024
MAX_WS_MESSAGE_SIZE = 2 * 1024 * 1024
MAX_PENDING_CHUNKS = 128
MAX_CONCURRENT_REQUESTS = 64
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 30
# ========================================================

PROTOCOL_VERSION = 2
PACKET_CONTROL = 0
PACKET_REQUEST_BODY = 1
PACKET_RESPONSE_BODY = 2
STREAM_PACKET_HEADER_SIZE = 17
AUTH_CONTEXT = b"ttswitch-ws-tunnel-auth-v2\x00"
KDF_INFO = b"ttswitch-ws-tunnel-keys-v2"
AAD_PREFIX = b"ttswitch-ws-tunnel-frame-v2|"
AGENT_NONCE_PREFIX = b"AGT2"
SERVER_NONCE_PREFIX = b"SRV2"

HTTP_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class ProtocolError(Exception):
    pass


class SecureChannel:
    def __init__(
        self,
        tx_key: bytes,
        rx_key: bytes,
        tx_prefix: bytes,
        rx_prefix: bytes,
    ) -> None:
        self.tx_cipher = ChaCha20Poly1305(tx_key)
        self.rx_cipher = ChaCha20Poly1305(rx_key)
        self.tx_prefix = tx_prefix
        self.rx_prefix = rx_prefix
        self.tx_sequence = 0
        self.rx_sequence = 0

    def encrypt(self, plaintext: bytes) -> bytes:
        if self.tx_sequence >= 2**64:
            raise ProtocolError("发送序列号已耗尽")
        sequence = self.tx_sequence.to_bytes(8, "big")
        nonce = self.tx_prefix + sequence
        aad = AAD_PREFIX + self.tx_prefix + sequence
        ciphertext = self.tx_cipher.encrypt(nonce, plaintext, aad)
        self.tx_sequence += 1
        return sequence + ciphertext

    def decrypt(self, frame: bytes) -> bytes:
        if len(frame) < 24:
            raise ProtocolError("加密帧过短")
        sequence = int.from_bytes(frame[:8], "big")
        if sequence != self.rx_sequence:
            raise ProtocolError(
                f"加密帧序号异常：期望 {self.rx_sequence}，收到 {sequence}"
            )
        sequence_bytes = frame[:8]
        nonce = self.rx_prefix + sequence_bytes
        aad = AAD_PREFIX + self.rx_prefix + sequence_bytes
        try:
            plaintext = self.rx_cipher.decrypt(nonce, frame[8:], aad)
        except InvalidTag as exc:
            raise ProtocolError("加密帧认证失败") from exc
        self.rx_sequence += 1
        return plaintext


@dataclass
class RequestContext:
    body: asyncio.Queue[bytes | None]
    task: asyncio.Task[None] | None = None


def validate_secret() -> bytes:
    secret = TUNNEL_SECRET.encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("TUNNEL_SECRET 至少需要 32 个 UTF-8 字节")
    return secret


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(value: Any, expected_size: int) -> bytes:
    if not isinstance(value, str):
        raise ProtocolError("认证字段格式错误")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ProtocolError("认证字段不是有效 Base64") from exc
    if len(decoded) != expected_size:
        raise ProtocolError("认证字段长度错误")
    return decoded


def derive_channel(
    secret: bytes, server_nonce: bytes, client_nonce: bytes
) -> SecureChannel:
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=server_nonce + client_nonce,
        info=KDF_INFO,
    ).derive(secret)
    agent_to_server_key = key_material[:32]
    server_to_agent_key = key_material[32:]
    return SecureChannel(
        tx_key=agent_to_server_key,
        rx_key=server_to_agent_key,
        tx_prefix=AGENT_NONCE_PREFIX,
        rx_prefix=SERVER_NONCE_PREFIX,
    )


def pack_control(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return bytes((PACKET_CONTROL,)) + encoded


def pack_stream(kind: int, request_id: str, payload: bytes) -> bytes:
    if kind not in {PACKET_REQUEST_BODY, PACKET_RESPONSE_BODY}:
        raise ValueError("无效的数据帧类型")
    return bytes((kind,)) + uuid.UUID(request_id).bytes + payload


def unpack_packet(packet: bytes) -> tuple[int, str | None, Any]:
    if not packet:
        raise ProtocolError("收到空数据包")
    kind = packet[0]
    if kind == PACKET_CONTROL:
        try:
            payload = json.loads(packet[1:].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("控制包不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ProtocolError("控制包必须是 JSON 对象")
        return kind, None, payload

    if kind not in {PACKET_REQUEST_BODY, PACKET_RESPONSE_BODY}:
        raise ProtocolError(f"未知数据包类型：{kind}")
    if len(packet) < STREAM_PACKET_HEADER_SIZE:
        raise ProtocolError("流数据包过短")
    request_id = str(uuid.UUID(bytes=packet[1:17]))
    return kind, request_id, packet[17:]


def filtered_headers(
    headers: Iterable[tuple[str, str]], *, request_headers: bool
) -> list[tuple[str, str]]:
    source = list(headers)
    connection_tokens: set[str] = set()
    for name, value in source:
        if name.lower() == "connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )

    blocked = HOP_BY_HOP_HEADERS | connection_tokens
    if request_headers:
        blocked = blocked | {"host", "expect"}
    return [(name, value) for name, value in source if name.lower() not in blocked]


def validate_headers(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        raise ProtocolError("headers 必须是列表")
    result: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            or not HTTP_TOKEN_RE.fullmatch(item[0])
            or "\r" in item[1]
            or "\n" in item[1]
        ):
            raise ProtocolError("收到无效 HTTP 请求头")
        result.append((item[0], item[1]))
    return result


async def authenticate_server(
    ws: aiohttp.ClientWebSocketResponse,
) -> SecureChannel:
    secret = validate_secret()
    message = await ws.receive(timeout=AUTH_TIMEOUT)
    if message.type != WSMsgType.TEXT:
        raise ProtocolError("未收到公网端认证挑战")
    try:
        challenge = json.loads(message.data)
    except json.JSONDecodeError as exc:
        raise ProtocolError("公网端认证挑战不是有效 JSON") from exc
    if not isinstance(challenge, dict) or challenge.get("type") != "challenge":
        raise ProtocolError("公网端认证挑战类型错误")
    if challenge.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError("隧道协议版本不一致")

    server_nonce = b64decode(challenge.get("server_nonce"), 32)
    client_nonce = os.urandom(32)
    proof = hmac.new(
        secret,
        AUTH_CONTEXT + server_nonce + client_nonce,
        hashlib.sha256,
    ).digest()
    await ws.send_json(
        {
            "type": "auth",
            "protocol": PROTOCOL_VERSION,
            "client_nonce": b64encode(client_nonce),
            "proof": b64encode(proof),
        }
    )

    channel = derive_channel(secret, server_nonce, client_nonce)
    response = await ws.receive(timeout=AUTH_TIMEOUT)
    if response.type != WSMsgType.BINARY:
        raise ProtocolError("公网端未返回加密认证结果")
    packet = channel.decrypt(response.data)
    kind, _, payload = unpack_packet(packet)
    if kind != PACKET_CONTROL:
        raise ProtocolError("公网端认证结果格式错误")
    if payload.get("type") == "auth_error":
        raise ProtocolError(str(payload.get("message", "公网端拒绝连接")))
    if (
        payload.get("type") != "auth_ok"
        or payload.get("protocol") != PROTOCOL_VERSION
    ):
        raise ProtocolError("公网端认证结果无效")
    return channel


class AgentTunnel:
    def __init__(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        session: ClientSession,
        channel: SecureChannel,
    ) -> None:
        self.ws = ws
        self.session = session
        self.channel = channel
        self.send_lock = asyncio.Lock()
        self.requests: dict[str, RequestContext] = {}
        self.request_slots = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def send_packet(self, packet: bytes) -> None:
        async with self.send_lock:
            if self.ws.closed:
                raise ConnectionError("公网 WebSocket 已关闭")
            await self.ws.send_bytes(self.channel.encrypt(packet))

    async def send_control(self, payload: dict[str, Any]) -> None:
        await self.send_packet(pack_control(payload))

    async def send_stream(
        self, kind: int, request_id: str, payload: bytes
    ) -> None:
        await self.send_packet(pack_stream(kind, request_id, payload))

    async def safe_error(self, request_id: str, message: str) -> None:
        try:
            await self.send_control(
                {"type": "response_error", "id": request_id, "message": message}
            )
        except Exception:
            pass

    async def request_body_stream(
        self, context: RequestContext
    ) -> AsyncIterator[bytes]:
        while True:
            item = await asyncio.wait_for(
                context.body.get(), timeout=REQUEST_BODY_IDLE_TIMEOUT
            )
            if item is None:
                return
            yield item

    async def forward_to_ttswitch(
        self,
        request_id: str,
        method: str,
        path_qs: str,
        headers: list[tuple[str, str]],
        has_body: bool,
    ) -> None:
        context = self.requests[request_id]
        url = f"http://{LOCAL_TTSWITCH_IP}:{LOCAL_TTSWITCH_PORT}{path_qs}"

        try:
            async with self.request_slots:
                body = self.request_body_stream(context) if has_body else None
                async with self.session.request(
                    method,
                    url,
                    headers=filtered_headers(headers, request_headers=True),
                    data=body,
                    allow_redirects=False,
                ) as response:
                    response_headers = filtered_headers(
                        response.headers.items(), request_headers=False
                    )
                    await self.send_control(
                        {
                            "type": "response_start",
                            "id": request_id,
                            "status": response.status,
                            "reason": response.reason or "",
                            "headers": [list(item) for item in response_headers],
                        }
                    )
                    async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                        if chunk:
                            await self.send_stream(
                                PACKET_RESPONSE_BODY, request_id, chunk
                            )
                    await self.send_control(
                        {"type": "response_end", "id": request_id}
                    )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            await self.safe_error(request_id, "读取请求体超时")
        except aiohttp.ClientConnectorError:
            await self.safe_error(
                request_id,
                f"无法连接 TT Switch：{LOCAL_TTSWITCH_IP}:{LOCAL_TTSWITCH_PORT}",
            )
        except Exception as exc:
            print(f"[agent] 请求 {request_id} 转发失败：{exc}", flush=True)
            await self.safe_error(request_id, f"内网请求失败：{type(exc).__name__}")
        finally:
            current = self.requests.get(request_id)
            if current is context:
                self.requests.pop(request_id, None)

    async def handle_request_start(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        path_qs = message.get("path_qs")
        has_body = message.get("has_body", False)

        if not isinstance(request_id, str):
            raise ProtocolError("请求缺少 ID")
        try:
            uuid.UUID(request_id)
        except ValueError as exc:
            raise ProtocolError("请求 ID 无效") from exc
        if request_id in self.requests:
            raise ProtocolError("收到重复请求 ID")
        if not isinstance(method, str) or not HTTP_TOKEN_RE.fullmatch(method):
            raise ProtocolError("HTTP 方法无效")
        if (
            not isinstance(path_qs, str)
            or not path_qs.startswith("/")
            or path_qs.startswith("//")
            or "\r" in path_qs
            or "\n" in path_qs
        ):
            raise ProtocolError("请求路径无效")

        headers = validate_headers(message.get("headers", []))
        context = RequestContext(body=asyncio.Queue(maxsize=MAX_PENDING_CHUNKS))
        self.requests[request_id] = context
        context.task = asyncio.create_task(
            self.forward_to_ttswitch(
                request_id,
                method.upper(),
                path_qs,
                headers,
                bool(has_body),
            ),
            name=f"forward-{request_id}",
        )

    async def handle_control(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "request_start":
            await self.handle_request_start(message)
            return

        request_id = message.get("id")
        if not isinstance(request_id, str):
            raise ProtocolError("控制消息缺少请求 ID")
        try:
            uuid.UUID(request_id)
        except ValueError as exc:
            raise ProtocolError("请求 ID 无效") from exc
        context = self.requests.get(request_id)
        if context is None:
            return

        if message_type == "request_end":
            await context.body.put(None)
            return
        if message_type == "request_cancel":
            self.requests.pop(request_id, None)
            if context.task is not None:
                context.task.cancel()
            return
        raise ProtocolError(f"未知控制消息类型：{message_type}")

    async def handle_frame(self, frame: bytes) -> None:
        packet = self.channel.decrypt(frame)
        kind, request_id, payload = unpack_packet(packet)
        if kind == PACKET_CONTROL:
            await self.handle_control(payload)
            return
        if kind != PACKET_REQUEST_BODY or request_id is None:
            raise ProtocolError("公网端发来了方向错误的数据包")
        context = self.requests.get(request_id)
        if context is not None:
            await context.body.put(payload)

    async def run(self) -> None:
        await self.send_control(
            {
                "type": "hello",
                "target": f"{LOCAL_TTSWITCH_IP}:{LOCAL_TTSWITCH_PORT}",
                "protocol": PROTOCOL_VERSION,
            }
        )
        try:
            async for message in self.ws:
                if message.type == WSMsgType.BINARY:
                    await self.handle_frame(message.data)
                elif message.type == WSMsgType.TEXT:
                    raise ProtocolError("认证后不接受明文数据")
                elif message.type == WSMsgType.ERROR:
                    raise self.ws.exception() or ConnectionError("WebSocket 异常")
        finally:
            tasks = [
                context.task
                for context in self.requests.values()
                if context.task is not None
            ]
            self.requests.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


async def check_local_ttswitch() -> None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(LOCAL_TTSWITCH_IP, LOCAL_TTSWITCH_PORT),
            timeout=CONNECT_TIMEOUT,
        )
        del reader
        writer.close()
        await writer.wait_closed()
        print(
            f"[agent] TT Switch 可连接：{LOCAL_TTSWITCH_IP}:{LOCAL_TTSWITCH_PORT}",
            flush=True,
        )
    except Exception as exc:
        print(
            f"[agent] 警告：暂时无法连接 TT Switch "
            f"{LOCAL_TTSWITCH_IP}:{LOCAL_TTSWITCH_PORT}（{exc}）",
            flush=True,
        )


def websocket_ssl_option() -> ssl.SSLContext | bool | None:
    if not PUBLIC_TUNNEL_URL.lower().startswith("wss://"):
        return None
    if VERIFY_PUBLIC_TLS:
        return ssl.create_default_context()
    return False


async def main() -> None:
    validate_secret()
    if "IP" in _ENV:
        print(f"[agent] 已从同目录 .env.txt 读取 IP={LOCAL_TTSWITCH_IP}", flush=True)
    else:
        print(f"[agent] 未配置 .env.txt 的 IP，使用内置默认 IP={LOCAL_TTSWITCH_IP}", flush=True)
    await check_local_ttswitch()
    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=CONNECT_TIMEOUT,
        sock_read=None,
    )
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS + 4)
    delay = RECONNECT_MIN_DELAY

    async with ClientSession(
        timeout=timeout,
        connector=connector,
        auto_decompress=False,
        skip_auto_headers={"User-Agent", "Accept-Encoding"},
    ) as session:
        while True:
            try:
                print(f"[agent] 正在连接 {PUBLIC_TUNNEL_URL}", flush=True)
                async with session.ws_connect(
                    PUBLIC_TUNNEL_URL,
                    heartbeat=WS_HEARTBEAT,
                    max_msg_size=MAX_WS_MESSAGE_SIZE,
                    ssl=websocket_ssl_option(),
                ) as ws:
                    channel = await authenticate_server(ws)
                    print(
                        "[agent] 反向隧道已建立，载荷已启用 ChaCha20-Poly1305 加密",
                        flush=True,
                    )
                    delay = RECONNECT_MIN_DELAY
                    await AgentTunnel(ws, session, channel).run()
                    print("[agent] 公网中继已断开", flush=True)
            except asyncio.CancelledError:
                raise
            except KeyboardInterrupt:
                return
            except Exception as exc:
                print(f"[agent] 连接失败：{exc}", flush=True)

            sleep_seconds = min(
                RECONNECT_MAX_DELAY,
                delay + random.uniform(0, max(0.2, delay * 0.2)),
            )
            print(f"[agent] {sleep_seconds:.1f} 秒后重连", flush=True)
            await asyncio.sleep(sleep_seconds)
            delay = min(RECONNECT_MAX_DELAY, delay * 2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[agent] 已停止", flush=True)
