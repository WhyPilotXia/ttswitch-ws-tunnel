#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import ssl
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from aiohttp import WSMsgType, web
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# ==================== 公网服务器配置 ====================
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 18443
TUNNEL_PATH = "/_tunnel"

# 必须与 intranet_agent.py 完全一致，建议至少 32 个随机字符。
TUNNEL_SECRET = "ttunnel-FhCiE5K1gUAokBybTYCl5_m1f1jgx6ayT27yxWeFDbA"

# 没有证书时保持为空，服务使用 HTTP/WS，但隧道载荷仍进行应用层加密。
# 后续有证书时同时填写，可升级为 HTTPS/WSS。
SSL_CERT_FILE = ""
SSL_KEY_FILE = ""

AUTH_TIMEOUT = 10
RESPONSE_START_TIMEOUT = 60
STREAM_IDLE_TIMEOUT = 600
WS_HEARTBEAT = 20
CHUNK_SIZE = 64 * 1024
MAX_WS_MESSAGE_SIZE = 2 * 1024 * 1024
MAX_PENDING_CHUNKS = 128
MAX_REQUEST_SIZE = 128 * 1024 * 1024

# 外网请求日志。只提取 JSON 中的模型名和提示词，不记录 Authorization。
REQUEST_LOG_ENABLED = True
REQUEST_LOG_BODY_LIMIT = 1024 * 1024
REQUEST_LOG_TEXT_LIMIT = 4000
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


class TunnelUnavailable(Exception):
    pass


class TunnelFailure(Exception):
    pass


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
class PendingRequest:
    response_start: asyncio.Future[dict[str, Any]]
    response_body: asyncio.Queue[bytes | None | Exception]


class RelayState:
    def __init__(self) -> None:
        self.agent: web.WebSocketResponse | None = None
        self.channel: SecureChannel | None = None
        self.send_lock = asyncio.Lock()
        self.registration_lock = asyncio.Lock()
        self.pending: dict[str, PendingRequest] = {}

    def connected(self) -> bool:
        return (
            self.agent is not None
            and not self.agent.closed
            and self.channel is not None
        )

    async def register(
        self, ws: web.WebSocketResponse, channel: SecureChannel
    ) -> bool:
        async with self.registration_lock:
            if self.connected():
                return False
            await ws.send_bytes(
                channel.encrypt(
                    pack_control({"type": "auth_ok", "protocol": PROTOCOL_VERSION})
                )
            )
            self.agent = ws
            self.channel = channel
            return True

    async def unregister(self, ws: web.WebSocketResponse) -> bool:
        async with self.registration_lock:
            if self.agent is not ws:
                return False
            self.agent = None
            self.channel = None
            return True

    async def send_control(
        self, ws: web.WebSocketResponse, payload: dict[str, Any]
    ) -> None:
        await self.send_packet(ws, pack_control(payload))

    async def send_stream(
        self, ws: web.WebSocketResponse, kind: int, request_id: str, payload: bytes
    ) -> None:
        await self.send_packet(ws, pack_stream(kind, request_id, payload))

    async def send_packet(self, ws: web.WebSocketResponse, packet: bytes) -> None:
        async with self.send_lock:
            if self.agent is not ws or ws.closed or self.channel is None:
                raise TunnelUnavailable("内网 Agent 已断开")
            await ws.send_bytes(self.channel.encrypt(packet))

    def decrypt_packet(self, ws: web.WebSocketResponse, frame: bytes) -> bytes:
        if self.agent is not ws or self.channel is None:
            raise TunnelUnavailable("内网 Agent 状态无效")
        return self.channel.decrypt(frame)

    async def fail_all(self, message: str) -> None:
        for pending in list(self.pending.values()):
            error = TunnelUnavailable(message)
            if not pending.response_start.done():
                pending.response_start.set_exception(error)
            else:
                await pending.response_body.put(error)


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
        tx_key=server_to_agent_key,
        rx_key=agent_to_server_key,
        tx_prefix=SERVER_NONCE_PREFIX,
        rx_prefix=AGENT_NONCE_PREFIX,
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
    headers: Iterable[tuple[str, str]], *, drop_host: bool = False
) -> list[list[str]]:
    source = list(headers)
    connection_tokens: set[str] = set()
    for name, value in source:
        if name.lower() == "connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )

    blocked = HOP_BY_HOP_HEADERS | connection_tokens
    if drop_host:
        blocked = blocked | {"host"}
    return [[name, value] for name, value in source if name.lower() not in blocked]


def json_error(status: int, message: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": "tunnel_error"}},
        status=status,
        dumps=lambda value: json.dumps(value, ensure_ascii=False),
    )


def get_state(request: web.Request) -> RelayState:
    return request.app["relay_state"]


def client_ip(request: web.Request) -> str:
    return request.remote or "unknown"


def clipped_text(value: str) -> str:
    if len(value) <= REQUEST_LOG_TEXT_LIMIT:
        return value
    omitted = len(value) - REQUEST_LOG_TEXT_LIMIT
    return f"{value[:REQUEST_LOG_TEXT_LIMIT]}…（已省略 {omitted} 字符）"


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            else:
                parts.append(f"<{item.get('type', '非文本内容')}>")
        else:
            parts.append(str(item))
    return "".join(parts)


def log_request_body(body: bytes, truncated: bool) -> None:
    suffix = "（请求体过大，仅解析前半部分）" if truncated else ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(f"请求体：无法解析为完整 UTF-8 JSON{suffix}", flush=True)
        return

    if not isinstance(payload, dict):
        print(f"请求体：JSON 类型为 {type(payload).__name__}{suffix}", flush=True)
        return

    model = payload.get("model")
    if model is not None:
        print(f"模型：{model}", flush=True)

    messages = payload.get("messages")
    if isinstance(messages, list):
        found = False
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "unknown")
            if role not in {"user", "developer", "system"}:
                continue
            found = True
            text = clipped_text(content_text(message.get("content", "")))
            print(f"提示词[{role}]：{text}", flush=True)
        if not found:
            print("提示词：messages 中没有可显示的输入消息", flush=True)
        return

    prompt = payload.get("prompt", payload.get("input"))
    if prompt is not None:
        print(f"提示词：{clipped_text(content_text(prompt))}", flush=True)
    elif model is None:
        print(f"请求体：JSON 未包含 model/messages/prompt/input{suffix}", flush=True)


async def authenticate_agent(ws: web.WebSocketResponse) -> SecureChannel:
    secret = validate_secret()
    server_nonce = os.urandom(32)
    await ws.send_json(
        {
            "type": "challenge",
            "protocol": PROTOCOL_VERSION,
            "server_nonce": b64encode(server_nonce),
        }
    )

    message = await ws.receive(timeout=AUTH_TIMEOUT)
    if message.type != WSMsgType.TEXT:
        raise ProtocolError("认证响应必须是文本 JSON")
    try:
        payload = json.loads(message.data)
    except json.JSONDecodeError as exc:
        raise ProtocolError("认证响应不是有效 JSON") from exc
    if not isinstance(payload, dict) or payload.get("type") != "auth":
        raise ProtocolError("认证响应类型错误")
    if payload.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError("隧道协议版本不一致")

    client_nonce = b64decode(payload.get("client_nonce"), 32)
    supplied_proof = b64decode(payload.get("proof"), 32)
    expected_proof = hmac.new(
        secret,
        AUTH_CONTEXT + server_nonce + client_nonce,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied_proof, expected_proof):
        raise ProtocolError("隧道认证失败")
    return derive_channel(secret, server_nonce, client_nonce)


async def handle_agent_control(
    state: RelayState, message: dict[str, Any]
) -> None:
    message_type = message.get("type")
    if message_type == "hello":
        return

    request_id = message.get("id")
    if not isinstance(request_id, str):
        raise ProtocolError("控制消息缺少请求 ID")
    try:
        uuid.UUID(request_id)
    except ValueError as exc:
        raise ProtocolError("请求 ID 无效") from exc

    pending = state.pending.get(request_id)
    if pending is None:
        return

    if message_type == "response_start":
        status = message.get("status")
        headers = message.get("headers", [])
        reason = message.get("reason", "")
        if not isinstance(status, int) or not 100 <= status <= 599:
            raise ProtocolError("无效的 HTTP 状态码")
        if not isinstance(headers, list):
            raise ProtocolError("无效的响应头")
        metadata = {"status": status, "reason": str(reason), "headers": headers}
        if not pending.response_start.done():
            pending.response_start.set_result(metadata)
        return

    if message_type == "response_end":
        await pending.response_body.put(None)
        return

    if message_type == "response_error":
        error = TunnelFailure(str(message.get("message", "内网转发失败")))
        if not pending.response_start.done():
            pending.response_start.set_exception(error)
        else:
            await pending.response_body.put(error)
        return

    raise ProtocolError(f"未知控制消息类型：{message_type}")


async def handle_agent_frame(
    state: RelayState, ws: web.WebSocketResponse, frame: bytes
) -> None:
    packet = state.decrypt_packet(ws, frame)
    kind, request_id, payload = unpack_packet(packet)
    if kind == PACKET_CONTROL:
        await handle_agent_control(state, payload)
        return
    if kind != PACKET_RESPONSE_BODY or request_id is None:
        raise ProtocolError("Agent 发来了方向错误的数据包")
    pending = state.pending.get(request_id)
    if pending is not None:
        await pending.response_body.put(payload)


async def tunnel_handler(request: web.Request) -> web.StreamResponse:
    if request.method != "GET":
        return json_error(405, "隧道端点只接受 WebSocket GET 请求")

    ws = web.WebSocketResponse(
        heartbeat=WS_HEARTBEAT,
        max_msg_size=MAX_WS_MESSAGE_SIZE,
        autoping=True,
    )
    await ws.prepare(request)
    peer = request.remote or "unknown"
    state = get_state(request)
    registered = False

    try:
        channel = await authenticate_agent(ws)
        registered = await state.register(ws, channel)
        if not registered:
            await ws.send_bytes(
                channel.encrypt(
                    pack_control({"type": "auth_error", "message": "已有 Agent 在线"})
                )
            )
            await ws.close(code=1013, message=b"agent already connected")
            return ws

        print(f"[tunnel] 加密 Agent 已连接：{peer}", flush=True)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                await handle_agent_frame(state, ws, message.data)
            elif message.type == WSMsgType.TEXT:
                raise ProtocolError("认证后不接受明文数据")
            elif message.type == WSMsgType.ERROR:
                raise ws.exception() or TunnelUnavailable("WebSocket 异常")
    except asyncio.TimeoutError:
        print(f"[tunnel] Agent 认证超时：{peer}", flush=True)
        await ws.close(code=1008, message=b"authentication timeout")
    except ProtocolError as exc:
        print(f"[tunnel] 协议或认证错误：{peer}：{exc}", flush=True)
        await ws.close(code=1008, message=b"protocol or authentication error")
    except Exception as exc:
        print(f"[tunnel] Agent 连接异常：{peer}：{exc}", flush=True)
    finally:
        if registered and await state.unregister(ws):
            await state.fail_all("内网 Agent 已断开")
            print(f"[tunnel] 加密 Agent 已离线：{peer}", flush=True)

    return ws


async def health_handler(request: web.Request) -> web.Response:
    state = get_state(request)
    return web.json_response(
        {
            "status": "ok" if state.connected() else "waiting_for_agent",
            "agent_connected": state.connected(),
            "active_requests": len(state.pending),
            "listen_port": LISTEN_PORT,
            "tunnel_encryption": "ChaCha20-Poly1305",
        }
    )


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    request_id = str(uuid.uuid4())
    peer = client_ip(request)
    started_at = time.monotonic()
    if REQUEST_LOG_ENABLED:
        print(
            f"外网请求：IP={peer} {request.method} {request.raw_path} "
            f"请求ID={request_id}",
            flush=True,
        )

    state = get_state(request)
    agent = state.agent
    if agent is None or agent.closed:
        if REQUEST_LOG_ENABLED:
            print(
                f"请求结束：IP={peer} 请求ID={request_id} 状态=503 "
                f"耗时={time.monotonic() - started_at:.3f}秒，内网 Agent 未连接",
                flush=True,
            )
        return json_error(503, "内网 Agent 未连接")

    loop = asyncio.get_running_loop()
    pending = PendingRequest(
        response_start=loop.create_future(),
        response_body=asyncio.Queue(maxsize=MAX_PENDING_CHUNKS),
    )
    state.pending[request_id] = pending
    response: web.StreamResponse | None = None
    completed = False
    response_status: int | str = "-"
    response_bytes = 0
    result_detail = "未完成"

    try:
        await state.send_control(
            agent,
            {
                "type": "request_start",
                "id": request_id,
                "method": request.method,
                "path_qs": request.raw_path,
                "headers": filtered_headers(request.headers.items(), drop_host=True),
                "has_body": request.can_read_body,
            },
        )

        body_preview = bytearray()
        body_truncated = False
        if request.can_read_body:
            async for chunk in request.content.iter_chunked(CHUNK_SIZE):
                if chunk:
                    if REQUEST_LOG_ENABLED:
                        remaining = REQUEST_LOG_BODY_LIMIT - len(body_preview)
                        if remaining > 0:
                            body_preview.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            body_truncated = True
                    await state.send_stream(
                        agent, PACKET_REQUEST_BODY, request_id, chunk
                    )
        if REQUEST_LOG_ENABLED and body_preview:
            log_request_body(bytes(body_preview), body_truncated)
        await state.send_control(agent, {"type": "request_end", "id": request_id})

        metadata = await asyncio.wait_for(
            asyncio.shield(pending.response_start), timeout=RESPONSE_START_TIMEOUT
        )
        response_status = metadata["status"]
        response = web.StreamResponse(
            status=response_status,
            reason=metadata["reason"].replace("\r", " ").replace("\n", " "),
        )
        for header in metadata["headers"]:
            if (
                isinstance(header, list)
                and len(header) == 2
                and isinstance(header[0], str)
                and isinstance(header[1], str)
                and header[0].lower() not in HOP_BY_HOP_HEADERS
                and "\r" not in header[0]
                and "\n" not in header[0]
                and "\r" not in header[1]
                and "\n" not in header[1]
            ):
                response.headers.add(header[0], header[1])

        await response.prepare(request)
        while True:
            item = await asyncio.wait_for(
                pending.response_body.get(), timeout=STREAM_IDLE_TIMEOUT
            )
            if item is None:
                completed = True
                break
            if isinstance(item, Exception):
                raise item
            response_bytes += len(item)
            await response.write(item)

        await response.write_eof()
        result_detail = "完成"
        return response

    except asyncio.TimeoutError:
        result_detail = "等待内网服务响应超时"
        if response is None:
            response_status = 504
            return json_error(504, result_detail)
        raise
    except TunnelUnavailable as exc:
        result_detail = str(exc)
        if response is None:
            response_status = 503
            return json_error(503, result_detail)
        raise
    except TunnelFailure as exc:
        result_detail = str(exc)
        if response is None:
            response_status = 502
            return json_error(502, result_detail)
        raise
    except ConnectionResetError:
        result_detail = "外网客户端断开连接"
        raise
    except asyncio.CancelledError:
        result_detail = "请求被取消"
        raise
    except Exception as exc:
        result_detail = f"转发失败：{type(exc).__name__}: {exc}"
        print(
            f"转发失败：IP={peer} 请求ID={request_id} {request.method} "
            f"{request.raw_path}，{exc}",
            flush=True,
        )
        if response is None:
            response_status = 502
            return json_error(502, "隧道转发失败")
        raise
    finally:
        state.pending.pop(request_id, None)
        if not completed and state.agent is agent and not agent.closed:
            try:
                await state.send_control(
                    agent, {"type": "request_cancel", "id": request_id}
                )
            except Exception:
                pass
        if pending.response_start.done() and not pending.response_start.cancelled():
            try:
                pending.response_start.exception()
            except Exception:
                pass
        elif not pending.response_start.done():
            pending.response_start.cancel()
        if REQUEST_LOG_ENABLED:
            print(
                f"请求结束：IP={peer} 请求ID={request_id} 状态={response_status} "
                f"响应={response_bytes}字节 耗时={time.monotonic() - started_at:.3f}秒 "
                f"结果={result_detail}",
                flush=True,
            )


async def shutdown_handler(app: web.Application) -> None:
    state: RelayState = app["relay_state"]
    if state.agent is not None and not state.agent.closed:
        await state.agent.close(code=1001, message=b"server shutdown")
    await state.fail_all("公网中继正在关闭")


def build_ssl_context() -> ssl.SSLContext | None:
    if not SSL_CERT_FILE and not SSL_KEY_FILE:
        return None
    if not SSL_CERT_FILE or not SSL_KEY_FILE:
        raise RuntimeError("SSL_CERT_FILE 与 SSL_KEY_FILE 必须同时填写")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
    return context


def create_app() -> web.Application:
    validate_secret()
    app = web.Application(client_max_size=MAX_REQUEST_SIZE)
    app["relay_state"] = RelayState()
    app.router.add_route("*", TUNNEL_PATH, tunnel_handler)
    app.router.add_get("/__tunnel_health", health_handler)
    app.router.add_route("*", "/{path_info:.*}", proxy_handler)
    app.on_shutdown.append(shutdown_handler)
    return app


if __name__ == "__main__":
    ssl_context = build_ssl_context()
    scheme = "https" if ssl_context else "http"
    ws_scheme = "wss" if ssl_context else "ws"
    print(f"[server] 外网 API：{scheme}://118.31.105.6:{LISTEN_PORT}/", flush=True)
    print(
        f"[server] 加密 Agent 入口："
        f"{ws_scheme}://118.31.105.6:{LISTEN_PORT}{TUNNEL_PATH}",
        flush=True,
    )
    web.run_app(
        create_app(),
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        ssl_context=ssl_context,
        access_log=None,
    )
