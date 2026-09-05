#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import aiohttp


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


# ==================== 外网客户端配置 ====================
# 修改 SERVER_URL 即可切换公网服务器；TT_TOKEN 不写入代码文件，
# 优先读环境变量 TT_TOKEN，其次同目录 .env.txt（模板见 .env.txt.example）。
SERVER_URL = "http://118.31.105.6:18443"
TT_TOKEN = os.getenv("TT_TOKEN") or _load_env_txt().get("TT_TOKEN")

DEFAULT_MODEL = "glm-5.3-ioa"
CONNECT_TIMEOUT = 10.0
STREAM_IDLE_TIMEOUT = 600.0
MAX_HISTORY_MESSAGES = 20
# ========================================================

# 环境变量可临时覆盖顶部配置，无需修改文件。
PUBLIC_ORIGIN = os.getenv("TT_SWITCH_PUBLIC_ORIGIN", SERVER_URL).rstrip("/")
BASE_URL = os.getenv("TT_SWITCH_PUBLIC_BASE_URL", f"{PUBLIC_ORIGIN}/tencent/v1").rstrip("/")
API_KEY = os.getenv("TT_SWITCH_API_KEY") or os.getenv("TENCENT_MODEL_API_KEY") or TT_TOKEN
SELECTED_DEFAULT_MODEL = os.getenv("TT_SWITCH_MODEL", DEFAULT_MODEL)

ANSI_DIM_BLUE = "\033[2;94m"
ANSI_BOLD_GREEN = "\033[1;92m"
ANSI_YELLOW = "\033[93m"
ANSI_RED = "\033[91m"
ANSI_RESET = "\033[0m"

REASONING_LEVELS: dict[str, str | None] = {
    "none": None,
    "off": None,
    "no": None,
    "false": None,
    "0": None,
    "无": None,
    "关闭": None,
    "不推理": None,
    "minimal": "minimal",
    "低": "low",
    "low": "low",
    "中": "medium",
    "默认": "medium",
    "medium": "medium",
    "高": "high",
    "强": "high",
    "high": "high",
}


@dataclass
class ConsoleState:
    models: list[dict[str, Any]] = field(default_factory=list)
    selected_model: str | None = None
    reasoning_effort: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)


def enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        stdout_handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(stdout_handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(stdout_handle, mode.value | 0x0004)
    except Exception:
        pass


def auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def model_id(model: dict[str, Any], fallback_index: int) -> str:
    return str(model.get("id") or model.get("name") or f"model-{fallback_index}")


def reasoning_name(value: str | None) -> str:
    return value or "未设置"


def parse_reasoning_effort(text: str) -> str | None:
    value = text.strip()
    for prefix in ("reasoning_effort=", "reasoning=", "effort=", "推理等级=", "推理=", "等级="):
        if value.lower().startswith(prefix):
            value = value[len(prefix):].strip()
            break
    key = value.lower()
    if key not in REASONING_LEVELS and value not in REASONING_LEVELS:
        raise ValueError("推理等级仅支持 none、minimal、low、medium、high。")
    return REASONING_LEVELS.get(key, REASONING_LEVELS.get(value))


def stream_value_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(stream_value_to_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "summary", "reasoning_content", "reasoning"):
            text = stream_value_to_text(value.get(key))
            if text:
                return text
    return ""


def extract_stream_text(data: dict[str, Any]) -> tuple[str, str]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", ""

    first = choices[0]
    if not isinstance(first, dict):
        return "", ""

    delta = first.get("delta") or {}
    message = first.get("message") or {}
    if not isinstance(delta, dict):
        delta = {}
    if not isinstance(message, dict):
        message = {}

    content = stream_value_to_text(delta.get("content") or message.get("content"))
    reasoning = ""
    for source in (delta, message):
        for key in ("reasoning_content", "reasoning", "reasoning_details", "thinking"):
            reasoning = stream_value_to_text(source.get(key))
            if reasoning:
                break
        if reasoning:
            break
    return content, reasoning


def error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error)
        return str(error)
    return str(payload)


async def read_error_response(response: aiohttp.ClientResponse) -> str:
    text = await response.text()
    try:
        return error_message(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        return text.strip() or response.reason or "未知错误"


async def check_tunnel(session: aiohttp.ClientSession) -> None:
    try:
        async with session.get(f"{PUBLIC_ORIGIN}/__tunnel_health") as response:
            if response.status != 200:
                print(f"{ANSI_YELLOW}隧道状态检查返回 HTTP {response.status}，继续尝试连接模型接口。{ANSI_RESET}")
                return
            data = await response.json(content_type=None)
            if data.get("agent_connected"):
                print(f"隧道状态：内网 Agent 已连接，加密方式 {data.get('tunnel_encryption', '未知')}")
            else:
                print(f"{ANSI_YELLOW}隧道状态：内网 Agent 尚未连接。{ANSI_RESET}")
    except Exception as exc:
        print(f"{ANSI_YELLOW}无法读取隧道状态：{exc}，继续尝试连接模型接口。{ANSI_RESET}")


async def fetch_models(
    session: aiohttp.ClientSession,
    state: ConsoleState,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    if state.models and not force:
        return state.models

    async with session.get(f"{BASE_URL}/models", headers=auth_headers()) as response:
        if response.status >= 400:
            detail = await read_error_response(response)
            raise RuntimeError(f"获取模型列表失败：HTTP {response.status}，{detail}")
        data = await response.json(content_type=None)

    models = data.get("data", []) if isinstance(data, dict) else []
    if not isinstance(models, list):
        models = []
    state.models = [item for item in models if isinstance(item, dict)]
    return state.models


def print_models(state: ConsoleState) -> None:
    if not state.models:
        print("未获取到模型。")
        return

    print("\n可用模型：")
    for index, item in enumerate(state.models, start=1):
        current = " *" if model_id(item, index) == state.selected_model else ""
        owner = str(item.get("owned_by") or item.get("owner") or "-")
        print(f"  {index:>2}. {model_id(item, index)}  [{owner}]{current}")
    print("\n用法：/model <编号或模型ID> [none|minimal|low|medium|high]")


def select_default_model(state: ConsoleState) -> None:
    ids = [model_id(item, index) for index, item in enumerate(state.models, start=1)]
    if SELECTED_DEFAULT_MODEL in ids:
        state.selected_model = SELECTED_DEFAULT_MODEL
    elif ids:
        state.selected_model = ids[0]


async def handle_model_command(
    line: str,
    session: aiohttp.ClientSession,
    state: ConsoleState,
) -> None:
    argument = line[len("/model"):].strip()
    parts = argument.split()
    target = parts[0] if parts else "list"
    effort_text = parts[1] if len(parts) > 1 else None

    if target.lower() in {"help", "帮助"}:
        print("/model                         显示模型列表")
        print("/model <编号或模型ID> [等级]   选择模型和推理等级")
        print("/model current                 显示当前选择")
        print("/model reload                  刷新模型列表")
        print("/model 0                       取消模型选择")
        return

    if target.lower() in {"current", "当前", "status", "状态"}:
        print(f"当前模型：{state.selected_model or '未选择'}")
        print(f"推理等级：{reasoning_name(state.reasoning_effort)}")
        print(f"上下文消息：{len(state.history)} 条")
        return

    if target in {"0", "停止", "关闭", "取消", "stop", "off"}:
        state.selected_model = None
        state.reasoning_effort = None
        state.history.clear()
        print("已取消模型选择并清空聊天上下文。")
        return

    force = target.lower() in {"list", "列表", "刷新", "reload"}
    models = await fetch_models(session, state, force=force)
    if force or not argument:
        print_models(state)
        return

    selected: str | None = None
    if target.isdigit():
        index = int(target)
        if 1 <= index <= len(models):
            selected = model_id(models[index - 1], index)
    else:
        for index, item in enumerate(models, start=1):
            candidate = model_id(item, index)
            if candidate == target:
                selected = candidate
                break

    if selected is None:
        raise ValueError(f"找不到模型“{target}”，请输入 /model 查看列表。")

    effort = parse_reasoning_effort(effort_text) if effort_text else None
    changed = selected != state.selected_model
    state.selected_model = selected
    state.reasoning_effort = effort
    if changed:
        state.history.clear()
    print(f"已选择模型：{selected}")
    print(f"推理等级：{reasoning_name(effort)}")
    if changed:
        print("模型已切换，聊天上下文已清空。")


async def chat_stream(
    prompt: str,
    session: aiohttp.ClientSession,
    state: ConsoleState,
) -> None:
    if not state.selected_model:
        print(f"{ANSI_YELLOW}尚未选择模型，请先输入 /model 查看并选择。{ANSI_RESET}")
        return

    request_messages = [*state.history, {"role": "user", "content": prompt}]
    payload: dict[str, Any] = {
        "model": state.selected_model,
        "messages": request_messages,
        "stream": True,
    }
    if state.reasoning_effort:
        payload["reasoning_effort"] = state.reasoning_effort

    answer_chunks: list[str] = []
    reasoning_chunks: list[str] = []
    active_section: str | None = None

    async with session.post(
        f"{BASE_URL}/chat/completions",
        headers=auth_headers(),
        json=payload,
    ) as response:
        if response.status >= 400:
            detail = await read_error_response(response)
            raise RuntimeError(f"模型调用失败：HTTP {response.status}，{detail}")

        async for raw_line in response.content:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if "error" in data:
                raise RuntimeError(f"模型流返回错误：{error_message(data)}")

            content, reasoning = extract_stream_text(data)
            if reasoning:
                if active_section != "reasoning":
                    if active_section is not None:
                        print()
                    print(f"{ANSI_DIM_BLUE}[推理]", flush=True)
                    active_section = "reasoning"
                reasoning_chunks.append(reasoning)
                print(f"{ANSI_DIM_BLUE}{reasoning}{ANSI_RESET}", end="", flush=True)
            if content:
                if active_section != "answer":
                    if active_section is not None:
                        print()
                    print(f"{ANSI_BOLD_GREEN}[回答]{ANSI_RESET}", flush=True)
                    active_section = "answer"
                answer_chunks.append(content)
                print(content, end="", flush=True)

    if active_section is not None:
        print()
    answer = "".join(answer_chunks).strip()
    reasoning = "".join(reasoning_chunks).strip()
    if not answer and not reasoning:
        print(f"{ANSI_YELLOW}模型没有返回可显示的文本内容。{ANSI_RESET}")
        return

    assistant_content = answer or "[模型仅返回了推理内容]"
    state.history.extend(
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": assistant_content},
        ]
    )
    if MAX_HISTORY_MESSAGES > 0 and len(state.history) > MAX_HISTORY_MESSAGES:
        state.history = state.history[-MAX_HISTORY_MESSAGES:]


async def async_input(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def print_help() -> None:
    print("\n命令：")
    print("  /model                         查看模型列表")
    print("  /model <编号或模型ID> [等级]   选择模型，例如 /model 1 high")
    print("  /model current                 查看当前模型")
    print("  /model reload                  刷新模型列表")
    print("  /clear                         清空聊天上下文")
    print("  /help                          显示帮助")
    print("  /exit                          退出")
    print("直接输入普通文本即可聊天。\n")


async def main() -> None:
    enable_windows_ansi()
    state = ConsoleState()
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=CONNECT_TIMEOUT,
        sock_connect=CONNECT_TIMEOUT,
        sock_read=STREAM_IDLE_TIMEOUT,
    )

    print("TT Switch 腾讯模型公网访问 Demo")
    print(f"API 地址：{BASE_URL}")


    async with aiohttp.ClientSession(timeout=timeout) as session:
        await check_tunnel(session)
        try:
            await fetch_models(session, state, force=True)
            select_default_model(state)
            print(f"已连接，共获取 {len(state.models)} 个模型。")
            if state.selected_model:
                print(f"默认模型：{state.selected_model}")
        except Exception as exc:
            print(f"{ANSI_RED}{exc}{ANSI_RESET}")

        print_help()
        while True:
            try:
                line = (await async_input("你> ")).strip()
            except EOFError:
                print("\n输入结束，程序退出。")
                break
            except KeyboardInterrupt:
                print("\n程序退出。")
                break

            if not line:
                continue
            lowered = line.lower()
            if lowered in {"/exit", "/quit", "退出", "quit", "exit"}:
                print("程序退出。")
                break
            if lowered in {"/help", "帮助"}:
                print_help()
                continue
            if lowered in {"/clear", "/reset"}:
                state.history.clear()
                print("聊天上下文已清空。")
                continue

            try:
                if lowered == "/model" or lowered.startswith("/model "):
                    await handle_model_command(line, session, state)
                else:
                    await chat_stream(line, session, state)
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
                print(f"{ANSI_RED}{exc}{ANSI_RESET}")
            except KeyboardInterrupt:
                print(f"\n{ANSI_YELLOW}本次请求已中断。{ANSI_RESET}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序退出。")
        sys.exit(130)
