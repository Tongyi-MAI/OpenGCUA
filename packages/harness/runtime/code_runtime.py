#!/usr/bin/env python3
"""Persistent Python worker backed directly by mai-ui SDK's macOS Runtime.

The worker owns one SDK Runtime/Session for its lifetime; model variables and SDK
handles survive across cells but never survive worker reconstruction.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import base64
import io
import json
import os
import re
import shlex
import signal
import selectors
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
from collections import OrderedDict, namedtuple
from contextvars import ContextVar
from itertools import islice
from pathlib import Path
from typing import Any


CTRL_PREFIX = "__MAC_AGENT_CTRL__ "
MAX_STDOUT_BYTES = 256 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_RPC_BYTES = 64 * 1024 * 1024

_REAL_STDOUT = sys.stdout
_REAL_STDERR = sys.stderr
_CONFIG: dict[str, Any] = {}
_TODO_ITEMS: list[dict[str, Any]] = []
_BASH_CALL_COUNT = 0
_TOOL_CALL_COUNT = 0
_CONTROL_SOCKET: socket.socket | None = None
_CONTROL_LOCK = threading.Lock()
_REQUEST_CONTEXT = threading.local()
_ACTIVE_REQUEST: ContextVar[dict | None] = ContextVar("active_code_request", default=None)
_MAI_SESSION = None
_SDK_RUNTIME = None
_SDK_DEVICE = None
_SDK_PHONE = None
_SDK_PHONE_RESOURCE = None
_SDK_WEB = None
_SDK_NATIVE_WEB_FAILURE: str | None = None
# 专用浏览器的登录态快照（storage_state）：首次人工登录后持久化，后续会话自动载入。
_WEB_STATE = Path(__file__).resolve().parents[1] / "configs" / "web_storage_state.json"
_WEB_CREATE_TIMEOUT_MS = 30000  # The SDK host rejects browser_create budgets above 120000 ms.
# 专用浏览器的持久 profile（codex 同款逻辑）：登录一次跨会话保留，绝不碰用户 Chrome。
_WEB_PROFILE_DIR = Path.home() / ".cache" / "mai-ui-web-profile"
_SDK_RESOURCE = None
_SDK_REVIVE_PENDING = False
_SDK_GUI_OK = False           # 本会话是否有过成功的 GUI 调用（自愈前置条件）
_SDK_LAST_GUI_SUCCESS_AT: float | None = None
_SDK_LAST_KEEPALIVE_ERROR: str | None = None
_SDK_HEAL_INPROGRESS = False  # 自愈重试进行中，防止 traced 递归自愈
_SDK_KEEPALIVE: Any = None     # 会话保活心跳任务（防驱动 5 分钟空闲回收）
_EXEC_NAMESPACE: dict[str, Any] | None = None  # worker 持久 cell 命名空间（重置后需刷新句柄）
_NATIVE_CUA = None  # Legacy helpers remain importable but are not bootstrapped.
_EXECUTION_TASKS: set[asyncio.Task] = set()
MAX_FRAME_BYTES = 8 * 1024 * 1024
SOURCE_PREFIX = "<mac-agent-code-"
MAX_SOURCE_CHARS = 1000
MAX_LABEL_CHARS = 256


class _SourceCache:
    """仅保留有界源码和调用位置索引；不使用可被回填的全局 linecache。"""

    def __init__(
        self, *, max_entries: int = 64, max_bytes: int = 2 * 1024 * 1024,
        max_source_bytes: int = 256 * 1024, max_nodes: int = 20_000,
    ) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_source_bytes = max_source_bytes
        self.max_nodes = max_nodes
        self.entries: OrderedDict[str, tuple] = OrderedDict()
        self.size = 0

    def remember(self, filename: str, source: str) -> None:
        # 超限只失去展示来源，不能改变代码执行语义。
        if len(source) > self.max_source_bytes or self.max_entries <= 0:
            return
        raw = source.encode("utf-8")
        if len(raw) > self.max_source_bytes:
            return
        tree = ast.parse(source, filename=filename, mode="exec")
        calls: set[tuple[int, int, int, int]] = set()
        ambiguous: set[tuple[int, int, int, int]] = set()
        for count, node in enumerate(ast.walk(tree), 1):
            if count > self.max_nodes:
                return
            if isinstance(node, ast.Call) or (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)):
                position = (node.lineno, node.end_lineno, node.col_offset, node.end_col_offset)
                if position in calls:
                    ambiguous.add(position)
                calls.add(position)
        calls.difference_update(ambiguous)
        # Python 将 CRLF/CR 规范化为换行，列号仍是 UTF-8 字节偏移。
        raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = raw.split(b"\n")
        charge = len(raw) + len(lines) * 40 + len(calls) * 192 + 256
        if charge > self.max_bytes:
            return
        while self.entries and (
            len(self.entries) >= self.max_entries or self.size + charge > self.max_bytes
        ):
            _, (_, _, removed) = self.entries.popitem(last=False)
            self.size -= removed
        self.entries[filename] = (lines, calls, charge)
        self.size += charge

    def locate(self, frame: Any) -> tuple[str | None, str]:
        filename = frame.f_code.co_filename
        location = _excerpt(f"{filename}:{frame.f_lineno}", 512, "...[location truncated]")
        entry = self.entries.get(filename)
        positions = getattr(frame.f_code, "co_positions", None)
        if entry is None or positions is None or sys.implementation.name != "cpython":
            return None, location
        if frame.f_lasti < 0:
            return None, location
        # CPython 的位置表与二字节 code unit（含 CACHE）一一对应。
        position = next(islice(positions(), frame.f_lasti // 2, frame.f_lasti // 2 + 1), None)
        if position is None or any(value is None for value in position):
            return None, location
        start, end, column, end_column = position
        location = f"{filename}:{start}:{column}-{end}:{end_column} (UTF-8 byte columns)"
        lines, calls, _ = entry
        if position not in calls:
            return None, location
        if not (1 <= start <= end <= len(lines)):
            return None, location
        if start == end:
            raw = lines[start - 1][column:end_column]
        else:
            raw = b"\n".join([
                lines[start - 1][column:], *lines[start:end - 1], lines[end - 1][:end_column]
            ])
        self.entries.move_to_end(filename)
        return _excerpt(raw.decode("utf-8"), MAX_SOURCE_CHARS, "...[source excerpt; truncated]"), location


_SOURCE_CACHE = _SourceCache()


def _excerpt(text: str, limit: int, marker: str) -> str:
    return text if len(text) <= limit else text[:max(0, limit - len(marker))] + marker[:limit]


def _output_metadata(value: Any, frame: Any) -> dict[str, Any]:
    output_id = getattr(_REQUEST_CONTEXT, "output_id", 0) + 1
    _REQUEST_CONTEXT.output_id = output_id
    try:
        source, location = _SOURCE_CACHE.locate(frame)
    except Exception:
        # 不读取磁盘、不猜测相邻行，也不重新求值任何表达式。
        source, location = None, "unavailable"
    return {
        "output_id": output_id, "source": source, "source_location": location,
        "block_index": getattr(_REQUEST_CONTEXT, "block_index", None),
        "root_type": _excerpt(type(value).__name__, 128, "...[type truncated]"),
    }


Point = namedtuple("Point", "x y")
Size = namedtuple("Size", "width height")


class _ControlExit(SystemExit):
    pass


class _LimitedStream(io.TextIOBase):
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, target: Any, limit: int) -> None:
        self.target = target
        self.limit = limit
        self.written = 0
        self.truncated = False
        self.at_line_start = True

    def writable(self) -> bool:
        return True

    def write(self, text: Any) -> int:
        text = text if isinstance(text, str) else str(text)
        if not text:
            return 0
        if self.truncated:
            return len(text)
        encoded = text.encode("utf-8", "replace")
        if self.written + len(encoded) > self.limit:
            keep = max(0, self.limit - self.written)
            self.target.write(encoded[:keep].decode("utf-8", "ignore"))
            self.target.write("\n[mac-agent] output limit reached; remaining output dropped.\n")
            self.target.flush()
            self.written = self.limit
            self.truncated = True
            self.at_line_start = True
            return len(text)
        self.target.write(text)
        self.target.flush()
        self.written += len(encoded)
        self.at_line_start = text.endswith("\n")
        return len(text)

    def flush(self) -> None:
        self.target.flush()

    def fileno(self) -> int:
        return self.target.fileno()

    @property
    def buffer(self) -> Any:
        return self.target.buffer


def _send_frame(frame: dict[str, Any]) -> None:
    assert _CONTROL_SOCKET is not None
    encoded = json.dumps(frame, ensure_ascii=True).encode("utf-8") + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError("worker 控制帧超过额度")
    with _CONTROL_LOCK:
        _CONTROL_SOCKET.sendall(encoded)


def _emit(event: dict[str, Any]) -> None:
    if _CONTROL_SOCKET is not None:
        request_id = getattr(_REQUEST_CONTEXT, "request_id", None)
        if request_id is not None:
            _send_frame({"kind": "event", "request_id": request_id, "event": event})
        return
    output = sys.stdout
    try:
        output.flush()
        if isinstance(output, _LimitedStream) and not output.at_line_start:
            _REAL_STDOUT.write("\n")
    except Exception:
        pass
    encoded = base64.b64encode(
        json.dumps(event, ensure_ascii=True).encode("utf-8")
    ).decode("ascii")
    _REAL_STDOUT.write(CTRL_PREFIX + encoded + "\n")
    _REAL_STDOUT.flush()
    if isinstance(output, _LimitedStream):
        output.at_line_start = True


def _recv_host_ack(kind: str, id_key: str, request_id: str, timeout: float | None) -> dict[str, Any] | None:
    """Read one fenced host reply. Callers serialize reads for each SDK session."""
    if _CONTROL_SOCKET is None:
        return None
    data = bytearray()
    deadline = time.monotonic() + timeout if timeout is not None else None
    while (deadline is None or time.monotonic() < deadline) and len(data) < 4096:
        try:
            part = _CONTROL_SOCKET.recv(1)
        except socket.timeout:
            continue
        if not part:
            return None
        data.extend(part)
        if part == b"\n":
            try:
                reply = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                return None
            return reply if isinstance(reply, dict) and reply.get("kind") == kind and reply.get(id_key) == request_id else None
    return None


async def _request_gui_approval(
    app_id: str, window_id: str, *, needs_vision: bool, needs_foreground: bool,
) -> bool:
    if not _CONFIG.get("gui_approval", True) or _CONTROL_SOCKET is None:
        return False
    request_id = uuid.uuid4().hex
    _emit({
        "type": "gui_approval_request", "request_id": request_id,
        "app_id": app_id, "window_id": window_id,
        "needs_vision": needs_vision, "needs_foreground": needs_foreground,
    })
    reply = await asyncio.to_thread(
        _recv_host_ack, "gui_approval_ack", "request_id", request_id, None,
    )
    return reply is not None and reply.get("approved") is True


def gui_action_scope(device: Any, message: dict[str, Any]) -> tuple[str, str, bool, bool] | None:
    """Classify macOS SDK calls requiring task/window GUI consent."""
    if getattr(device, "platform", None) != "macos" or message.get("op") != "action":
        return None
    action = message.get("action")
    needs_vision = action == "point_click"
    needs_foreground = action == "activate" or message.get("delivery") == "foreground"
    if not needs_vision and not needs_foreground:
        return None
    app_id = message.get("app_id") or getattr(device, "_app_id", None) or ""
    window_id = message.get("window_id") or getattr(device, "_window_id", None) or ""
    return str(app_id), str(window_id), needs_vision, needs_foreground


def _check_active_request() -> None:
    """共享可撤销 token，避免继承 ContextVar 的任务在下轮复活。"""
    if _CONFIG.get("ui_api", "legacy-cua") != "mai-ui":
        return
    token = _ACTIVE_REQUEST.get()
    if (threading.current_thread() is not threading.main_thread() or token is None
            or not token["active"]
            or token["request_id"] != getattr(_REQUEST_CONTEXT, "request_id", None)):
        raise RuntimeError("操作仅允许当前活动回复；旧任务或后台线程已被拒绝")


def _rpc(method: str, /, **params: Any) -> Any:
    _check_active_request()
    if _CONTROL_SOCKET is not None:
        request_id = getattr(_REQUEST_CONTEXT, "request_id", None)
        generation = getattr(_REQUEST_CONTEXT, "generation", None)
        if (
            threading.current_thread() is not threading.main_thread()
            or request_id is None or generation is None
        ):
            raise RuntimeError("GUI RPC 仅允许当前请求的主线程调用；后台线程或缺少请求上下文的调用已拒绝")
    else:
        generation = _CONFIG.get("rpc_generation", 0)
    socket_path = str(_CONFIG.get("rpc_socket") or "")
    if not socket_path:
        raise RuntimeError("Cua RPC socket is not configured")
    request = json.dumps(
        {"method": method, "params": params, "generation": generation},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    timeout = float(_CONFIG.get("rpc_timeout", 60.0))
    chunks: list[bytes] = []
    size = 0
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall(request)
        while True:
            chunk = client.recv(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RPC_BYTES:
                raise RuntimeError("Cua RPC response exceeded size limit")
            if b"\n" in chunk:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    response = json.loads(raw.decode("utf-8"))
    if not response.get("ok"):
        if _CONFIG.get("ui_api", "mai-ui") == "mai-ui":
            from mai_ui import MuError
            raise MuError(response.get("error_code") or (
                "ACTION_OUTCOME_UNKNOWN" if response.get("outcome_unknown") else "BACKEND_ERROR"
            ), response.get("error") or "Cua RPC failed", receipt=response.get("receipt"))
        suffix = " (action outcome may be unknown)" if response.get("outcome_unknown") else ""
        raise RuntimeError(str(response.get("error") or "Cua RPC failed") + suffix)
    return response.get("result")


def _public_json(value: Any, *, include_tree: bool = True) -> Any:
    """Remove transport-only image/token payloads before model feedback."""
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"data_base64", "element_token", "images", "_note"}:
                continue
            if key == "tree_markdown" and not include_tree:
                continue
            if key in {"launch", "activation"}:
                continue
            output[str(key)] = _public_json(item, include_tree=include_tree)
        return output
    if isinstance(value, (list, tuple)):
        return [_public_json(item, include_tree=include_tree) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


AUDIT_SLIM_CHARS = 8192


def _collect_counts(value: Any, prefix: str, out: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    if depth > 6 or len(out) > 200:
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, list):
                out[path] = len(item)
            elif isinstance(item, dict):
                _collect_counts(item, path, out, depth + 1)
            elif isinstance(item, str) and len(item) > 512:
                out[path] = f"str:{len(item)}"
    return out


def _audit_slim(value: Any, original_chars: int) -> dict[str, Any]:
    """确定性审计摘要：只保留键、点分路径列表计数与截断标记，不保留元素正文。"""
    if isinstance(value, list):
        return {"audit_truncated": True, "original_chars": original_chars,
                "top_keys": None, "counts": {"items": len(value)}}
    return {"audit_truncated": True, "original_chars": original_chars,
            "top_keys": sorted(value.keys()) if isinstance(value, dict) else None,
            "counts": _collect_counts(value, "", {}) }


def _slim_large_audit(value: Any, depth: int = 0):
    """只截断过大的 dict/list 子树；显式记录的 AX 文本等字符串保持全量。"""
    if isinstance(value, (dict, list)) and depth < 4:
        compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(compact) > AUDIT_SLIM_CHARS:
            if isinstance(value, list):
                return _audit_slim(value, len(compact))
            return {key: _slim_large_audit(item, depth + 1) for key, item in value.items()}
        return value
    return value


def _record_tool(
    name: str,
    arguments: dict[str, Any],
    result: Any = None,
    *,
    error: str | None = None,
    include_tree: bool = True,
    error_code: str | None = None,
    receipt: Any = None,
    outcome_unknown: bool = False,
) -> None:
    global _TOOL_CALL_COUNT
    _TOOL_CALL_COUNT += 1
    event: dict[str, Any] = {
        "type": "tool_call",
        "call_id": f"runtime_call_{_TOOL_CALL_COUNT:03d}",
        "tool": name,
        "arguments": _public_json(
            {key: value for key, value in arguments.items() if value is not None},
            include_tree=False,
        ),
        "ok": error is None,
    }
    if error is None:
        recorded = _public_json(result, include_tree=include_tree)
        # 成功且过大的观察类结果（整棵 AX 树等）在审计里只留摘要：
        # 模型可见内容已在 presentation，失败/小结果仍保留全量。
        event["result"] = _slim_large_audit(recorded)
    else:
        # 按结构化错误码分类；未知结果保留回执，不能当作已拒绝或已失败。
        event["error"] = error
        if isinstance(error_code, str) and error_code:
            event["error_code"] = error_code
        if receipt is not None:
            event["receipt"] = _public_json(receipt, include_tree=False)
        event["effect"] = "outcome_unknown" if outcome_unknown else "refused"
    _emit(event)


def _managed_rpc(
    name: str,
    method: str,
    arguments: dict[str, Any],
    *,
    include_tree: bool = True,
) -> Any:
    try:
        result = _rpc(method, **arguments)
    except Exception as exc:
        code = getattr(exc, "code", None)
        code = code if isinstance(code, str) and code else None
        _record_tool(
            name, arguments, error=f"{type(exc).__name__}: {exc}", error_code=code,
            receipt=getattr(exc, "receipt", None),
            outcome_unknown=code == "ACTION_OUTCOME_UNKNOWN"
            or "outcome may be unknown" in str(exc),
        )
        raise
    recorded = (
        _result_summary(result)
        if isinstance(result, dict)
        and "structured" in result
        and ("text" in result or "is_error" in result)
        else result
    )
    _record_tool(name, arguments, recorded, include_tree=include_tree)
    return result


def _sleep_after_call() -> None:
    pause = max(0.0, float(getattr(pyautogui, "PAUSE", 0.0)))
    if pause:
        time.sleep(pause)


class _PyAutoGUIProxy(types.ModuleType):
    """Supported pyautogui surface backed entirely by Cua Driver."""

    def __init__(self) -> None:
        super().__init__("pyautogui")
        self.PAUSE = float(_CONFIG.get("pyautogui_pause", 0.5))
        self.FAILSAFE = False
        self.Point = Point
        self.Size = Size

    @staticmethod
    def _xy(x: Any, y: Any) -> tuple[Any, Any]:
        if isinstance(x, (tuple, list)) and len(x) == 2 and y is None:
            return x[0], x[1]
        return x, y

    def click(
        self,
        x: Any = None,
        y: Any = None,
        clicks: int = 1,
        interval: float = 0.0,
        button: str = "left",
        **_kwargs: Any,
    ) -> None:
        x, y = self._xy(x, y)
        for index in range(max(1, int(clicks))):
            _rpc("click", x=x, y=y, clicks=1, button=button)
            if interval and index + 1 < int(clicks):
                time.sleep(float(interval))
        _sleep_after_call()

    def leftClick(self, x: Any = None, y: Any = None, **kwargs: Any) -> None:
        self.click(x, y, button="left", **kwargs)

    def rightClick(self, x: Any = None, y: Any = None, **kwargs: Any) -> None:
        self.click(x, y, button="right", **kwargs)

    def middleClick(self, x: Any = None, y: Any = None, **kwargs: Any) -> None:
        self.click(x, y, button="middle", **kwargs)

    def doubleClick(self, x: Any = None, y: Any = None, **kwargs: Any) -> None:
        x, y = self._xy(x, y)
        _rpc("click", x=x, y=y, clicks=2, button=kwargs.pop("button", "left"))
        _sleep_after_call()

    def tripleClick(self, x: Any = None, y: Any = None, **kwargs: Any) -> None:
        x, y = self._xy(x, y)
        _rpc("click", x=x, y=y, clicks=3, button=kwargs.pop("button", "left"))
        _sleep_after_call()

    def moveTo(
        self,
        x: Any,
        y: Any = None,
        duration: float = 0.0,
        **_kwargs: Any,
    ) -> None:
        x, y = self._xy(x, y)
        _rpc("move_to", x=x, y=y, duration=duration)
        _sleep_after_call()

    def moveRel(self, xOffset: float = 0, yOffset: float = 0, **kwargs: Any) -> None:
        position = self.position()
        self.moveTo(position.x + xOffset, position.y + yOffset, **kwargs)

    move = moveRel

    def dragTo(
        self,
        x: Any,
        y: Any = None,
        duration: float = 0.5,
        button: str = "left",
        **_kwargs: Any,
    ) -> None:
        x, y = self._xy(x, y)
        _rpc("drag_to", x=x, y=y, duration=duration, button=button)
        _sleep_after_call()

    def dragRel(
        self,
        xOffset: float = 0,
        yOffset: float = 0,
        duration: float = 0.5,
        button: str = "left",
        **kwargs: Any,
    ) -> None:
        position = self.position()
        self.dragTo(
            position.x + xOffset,
            position.y + yOffset,
            duration=duration,
            button=button,
            **kwargs,
        )

    drag = dragRel

    def scroll(self, clicks: int, x: Any = None, y: Any = None, **_kwargs: Any) -> None:
        x, y = self._xy(x, y)
        _rpc("scroll", clicks=int(clicks), x=x, y=y, horizontal=False)
        _sleep_after_call()

    def vscroll(self, clicks: int, x: Any = None, y: Any = None, **kwargs: Any) -> None:
        self.scroll(clicks, x, y, **kwargs)

    def hscroll(self, clicks: int, x: Any = None, y: Any = None, **_kwargs: Any) -> None:
        x, y = self._xy(x, y)
        _rpc("scroll", clicks=int(clicks), x=x, y=y, horizontal=True)
        _sleep_after_call()

    def write(self, message: Any, interval: float = 0.0, **_kwargs: Any) -> None:
        text = str(message)
        if interval > 0:
            for character in text:
                _rpc("type_text", text=character)
                time.sleep(float(interval))
        else:
            _rpc("type_text", text=text)
        _sleep_after_call()

    typewrite = write

    def press(
        self,
        keys: Any,
        presses: int = 1,
        interval: float = 0.0,
        **_kwargs: Any,
    ) -> None:
        sequence = list(keys) if isinstance(keys, (list, tuple)) else [keys]
        for repeat in range(max(1, int(presses))):
            for key in sequence:
                _rpc("press", key=str(key))
                if interval:
                    time.sleep(float(interval))
        _sleep_after_call()

    def hotkey(self, *keys: Any, interval: float = 0.0, **_kwargs: Any) -> None:
        if interval:
            # Cua owns chord ordering atomically; interval is intentionally ignored.
            pass
        _rpc("hotkey", keys=[str(key) for key in keys])
        _sleep_after_call()

    def position(self) -> Point:
        result = _rpc("position")
        return Point(*result["position"])

    def size(self) -> Size:
        scale = int(_CONFIG.get("coordinate_range", 1000))
        return Size(scale, scale)

    def onScreen(self, x: Any, y: Any = None) -> bool:
        x, y = self._xy(x, y)
        scale = float(_CONFIG.get("coordinate_range", 1000))
        return all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= scale
            for value in (x, y)
        )

    def screenshot(self, imageFilename: str | None = None, region: Any = None):
        result = _managed_rpc("pyautogui.screenshot", "screenshot", {})
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow is required for screenshot()") from exc
        image = Image.open(io.BytesIO(base64.b64decode(result["data_base64"])))
        image.load()
        if region is not None:
            left, top, width, height = region
            scale = float(_CONFIG.get("coordinate_range", 1000))
            raw_width, raw_height = image.size
            box = (
                round(float(left) / scale * raw_width),
                round(float(top) / scale * raw_height),
                round(float(left + width) / scale * raw_width),
                round(float(top + height) / scale * raw_height),
            )
            image = image.crop(box)
        image.metadata = _image_metadata(result)
        if region is not None:
            image.metadata["region"] = list(region)
        if imageFilename:
            image.save(imageFilename)
        return image

    def pixel(self, x: float, y: float):
        image = self.screenshot()
        scale = float(_CONFIG.get("coordinate_range", 1000))
        px = min(max(round(x / scale * (image.width - 1)), 0), image.width - 1)
        py = min(max(round(y / scale * (image.height - 1)), 0), image.height - 1)
        return image.convert("RGB").getpixel((px, py))

    def pixelMatchesColor(
        self,
        x: float,
        y: float,
        expectedRGBColor: Any,
        tolerance: int = 0,
    ) -> bool:
        actual = self.pixel(x, y)
        return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(actual, expectedRGBColor))

    @staticmethod
    def mouseDown(*_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError("Cua backend does not expose standalone mouseDown; use dragTo")

    @staticmethod
    def mouseUp(*_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError("Cua backend does not expose standalone mouseUp; use dragTo")

    @staticmethod
    def keyDown(*_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError("Cua backend does not expose standalone keyDown; use hotkey")

    @staticmethod
    def keyUp(*_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError("Cua backend does not expose standalone keyUp; use hotkey")


def _wrapped(value: Any) -> Any:
    if isinstance(value, dict) and not isinstance(value, _Record):
        return _Record(value)
    if isinstance(value, list):
        return [_wrapped(item) for item in value]
    return value


class _Record(dict):
    """Small dict with attribute access for concise model-authored code."""

    def __init__(self, value: dict[str, Any] | None = None, **kwargs: Any) -> None:
        merged = dict(value or {})
        merged.update(kwargs)
        super().__init__((key, _wrapped(item)) for key, item in merged.items())

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class CuaResult(_Record):
    """Raw Cua result plus convenient access to structured output fields."""

    def __getattr__(self, name: str) -> Any:
        try:
            return super().__getattr__(name)
        except AttributeError:
            structured = self.get("structured")
            if isinstance(structured, dict) and name in structured:
                return structured[name]
            raise

    @property
    def image(self):
        images = self.get("images") or []
        if not images:
            return None
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow is required to decode a Cua image") from exc
        encoded = images[0].get("data_base64")
        image = Image.open(io.BytesIO(base64.b64decode(encoded)))
        image.load()
        image.metadata = _image_metadata(self.get("structured") or self)
        return image


def _role_key(value: Any) -> str:
    normalized = str(value or "").strip().casefold().replace("_", "").replace(" ", "")
    return normalized[2:] if normalized.startswith("ax") else normalized


class AxElement(_Record):
    """One snapshot-bound accessibility element returned by Cua Driver."""

    def __init__(self, value: dict[str, Any], *, pid: int, window_id: int) -> None:
        super().__init__(value)
        object.__setattr__(self, "_pid", int(pid))
        object.__setattr__(self, "_window_id", int(window_id))

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def window_id(self) -> int:
        return self._window_id

    def _action(self, tool: str, **kwargs: Any) -> CuaResult:
        element_ref = self.get("element_ref")
        token = self.get("element_token")
        if not element_ref and not token:
            raise RuntimeError(
                "This element has no element reference; take a fresh actionable "
                "get_window_state() snapshot before acting"
            )
        addressing = (
            {"element_ref": element_ref}
            if element_ref
            else {"element_token": token}
        )
        return CuaResult(
            _rpc(
                "ax_action",
                tool=tool,
                pid=self._pid,
                **addressing,
                **kwargs,
            )
        )

    def click(self, action: str | None = None, button: str | None = None) -> CuaResult:
        return self._action("click", action=action, button=button)

    def double_click(self, delivery_mode: str | None = None) -> CuaResult:
        return self._action("double_click", delivery_mode=delivery_mode)

    def right_click(self, delivery_mode: str | None = None) -> CuaResult:
        return self._action("right_click", delivery_mode=delivery_mode)

    def type_text(
        self,
        text: Any,
        *,
        delay_ms: int | None = None,
        delivery_mode: str | None = None,
    ) -> CuaResult:
        return self._action(
            "type_text",
            text=str(text),
            delay_ms=delay_ms,
            delivery_mode=delivery_mode,
        )

    def set_value(self, value: Any) -> CuaResult:
        return self._action("set_value", value=str(value))

    def press(
        self,
        key: Any,
        *,
        modifiers: list[str] | tuple[str, ...] | None = None,
        delivery_mode: str | None = None,
    ) -> CuaResult:
        return self._action(
            "press_key",
            key=str(key),
            modifiers=list(modifiers or []),
            delivery_mode=delivery_mode,
        )

    def scroll(
        self,
        direction: str,
        amount: int = 3,
        *,
        by: str = "line",
        delivery_mode: str | None = None,
    ) -> CuaResult:
        return self._action(
            "scroll",
            direction=str(direction),
            amount=int(amount),
            by=str(by),
            delivery_mode=delivery_mode,
        )


class WindowState(_Record):
    """Searchable structured AX snapshot with optional window screenshot."""

    _SELECTORS = {
        "role",
        "label",
        "name",
        "label_contains",
        "value",
        "value_contains",
        "text_contains",
        "enabled",
        "selected",
        "action",
        "element_index",
        "index",
    }

    def __init__(self, result: dict[str, Any]) -> None:
        raw = CuaResult(result)
        structured = dict(raw.get("structured") or {})
        pid = int(structured.get("pid") or 0)
        window_id = int(structured.get("window_id") or 0)
        elements = [
            AxElement(dict(item), pid=pid, window_id=window_id)
            for item in structured.get("elements") or []
        ]
        structured["elements"] = elements
        structured.setdefault("degraded", bool(raw.get("degraded", False)))
        super().__init__(structured)
        object.__setattr__(self, "_raw", raw)

    @property
    def raw(self) -> CuaResult:
        return self._raw

    @property
    def image(self):
        return self._raw.image

    @staticmethod
    def _matches(element: AxElement, selectors: dict[str, Any]) -> bool:
        for key, expected in selectors.items():
            if expected is None:
                continue
            if key == "role":
                if _role_key(element.get("role")) != _role_key(expected):
                    return False
            elif key in {"label", "name"}:
                if str(element.get("label") or "").casefold() != str(expected).casefold():
                    return False
            elif key == "label_contains":
                if str(expected).casefold() not in str(element.get("label") or "").casefold():
                    return False
            elif key == "value":
                if str(element.get("value") or "").casefold() != str(expected).casefold():
                    return False
            elif key == "value_contains":
                if str(expected).casefold() not in str(element.get("value") or "").casefold():
                    return False
            elif key == "text_contains":
                haystack = " ".join(
                    str(element.get(field) or "") for field in ("label", "value", "value_description")
                ).casefold()
                if str(expected).casefold() not in haystack:
                    return False
            elif key in {"enabled", "selected"}:
                if element.get(key) != bool(expected):
                    return False
            elif key == "action":
                actions = [_role_key(item) for item in element.get("actions") or []]
                if _role_key(expected) not in actions:
                    return False
            elif key in {"element_index", "index"}:
                if element.get("element_index") != int(expected):
                    return False
        return True

    def find_all(self, predicate: Any = None, **selectors: Any) -> list[AxElement]:
        unknown = sorted(set(selectors) - self._SELECTORS)
        if unknown:
            raise TypeError("unsupported AX selector(s): " + ", ".join(unknown))
        if predicate is not None and not callable(predicate):
            raise TypeError("predicate must be callable")
        return [
            element
            for element in self.get("elements", [])
            if (predicate is None or bool(predicate(element)))
            and self._matches(element, selectors)
        ]

    def find(self, predicate: Any = None, **selectors: Any) -> AxElement | None:
        matches = self.find_all(predicate, **selectors)
        return matches[0] if matches else None

    def require(self, predicate: Any = None, **selectors: Any) -> AxElement:
        element = self.find(predicate, **selectors)
        if element is not None:
            return element
        raise LookupError(f"No AX element matched {selectors!r}")

    def require_unique(self, predicate: Any = None, **selectors: Any) -> AxElement:
        matches = self.find_all(predicate, **selectors)
        if len(matches) != 1:
            raise LookupError(
                f"Expected exactly one AX element for {selectors!r}; found {len(matches)}"
            )
        return matches[0]


class _CuaProxy:
    """Semantic/window Cua API; pyautogui remains the visual-coordinate API."""

    @staticmethod
    def open_app(name: str) -> _Record:
        result = _Record(_rpc("open_app", name=str(name)))
        _sleep_after_call()
        return result

    @staticmethod
    def list_windows(
        pid: int | None = None, *, on_screen_only: bool = False
    ) -> list[_Record]:
        result = CuaResult(
            _rpc("list_windows", pid=pid, on_screen_only=on_screen_only)
        )
        return [_Record(dict(item)) for item in result.structured.get("windows", [])]

    @staticmethod
    def get_window_state(
        pid: int,
        window_id: int,
        *,
        query: str | None = None,
        include_screenshot: bool = False,
        include_accessibility_tree: bool = True,
        max_elements: int | None = None,
        max_depth: int | None = None,
        max_dimension: int | None = None,
    ) -> WindowState:
        return WindowState(
            _managed_rpc("legacy_cua.get_window_state", "get_window_state", {
                "pid": pid, "window_id": window_id, "query": query,
                "include_screenshot": include_screenshot,
                "include_accessibility_tree": include_accessibility_tree,
                "max_elements": max_elements, "max_depth": max_depth,
                "max_dimension": max_dimension,
            })
        )

    @staticmethod
    def bring_to_front(pid: int, window_id: int) -> CuaResult:
        result = CuaResult(_rpc("bring_to_front", pid=pid, window_id=window_id))
        _sleep_after_call()
        return result

    @staticmethod
    def verify_state(
        pid: int,
        window_id: int,
        expect: list[dict[str, Any]],
        *,
        timeout_ms: int = 5000,
        stable_samples: int = 2,
        include_screenshot: bool = False,
    ) -> CuaResult:
        return CuaResult(
            _managed_rpc("legacy_cua.verify_state", "verify_state", {
                "pid": pid, "window_id": window_id, "expect": expect,
                "timeout_ms": timeout_ms, "stable_samples": stable_samples,
                "include_screenshot": include_screenshot,
            })
        )


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    structured = dict(result.get("structured") or {})
    return {
        "text": str(result.get("text") or ""),
        "is_error": bool(result.get("is_error", False)),
        "degraded": bool(result.get("degraded", False)),
        **structured,
    }


def _state_summary(state: WindowState, *, include_tree: bool) -> dict[str, Any]:
    keys = (
        "pid",
        "window_id",
        "app_name",
        "window_title",
        "snapshot_id",
        "element_count",
        "returned_element_count",
        "filtered_element_count",
        "elements_complete",
        "degraded",
        "degraded_reason",
        "escalation",
        "visual_ref",
        "coordinate_space",
        "screenshot_width",
        "screenshot_height",
        "window_bounds",
        "image_attached",
    )
    result = {key: state.get(key) for key in keys if key in state}
    result["elements"] = [
        _public_json(dict(element), include_tree=False)
        for element in state.get("elements", [])
    ]
    if include_tree:
        result["tree_markdown"] = state.get("tree_markdown", "")
    return result


class VisualObservation(_Record):
    @property
    def image(self):
        encoded = self.get("data_base64")
        if not encoded:
            return None
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow is required to decode a screenshot") from exc
        image = Image.open(io.BytesIO(base64.b64decode(encoded)))
        image.load()
        image.metadata = _image_metadata(self.get("structured") or self)
        return image


class _Mouse:
    def __init__(self, *, kind: str, pid: int | None = None, window_id: int | None = None):
        self.kind = kind
        self.pid = pid
        self.window_id = window_id

    def _call(self, action: str, **arguments: Any) -> CuaResult:
        params = {
            "kind": self.kind,
            "pid": self.pid,
            "window_id": self.window_id,
            "action": action,
            **arguments,
        }
        name = f"mac.{self.kind}.mouse.{action}"
        raw = _managed_rpc(name, "pixel_action", params)
        return CuaResult(raw)

    def move_to(self, x: float, y: float, *, based_on: str) -> CuaResult:
        return self._call("move", x=x, y=y, based_on=based_on)

    def click(
        self,
        x: float,
        y: float,
        *,
        based_on: str,
        button: str = "left",
        count: int = 1,
        delivery: str = "background",
    ) -> CuaResult:
        return self._call(
            "click",
            x=x,
            y=y,
            based_on=based_on,
            button=button,
            count=count,
            delivery=delivery,
        )

    def double_click(
        self, x: float, y: float, *, based_on: str, delivery: str = "background"
    ) -> CuaResult:
        return self._call(
            "double_click", x=x, y=y, based_on=based_on, delivery=delivery
        )

    def right_click(
        self, x: float, y: float, *, based_on: str, delivery: str = "background"
    ) -> CuaResult:
        return self._call(
            "right_click", x=x, y=y, based_on=based_on, delivery=delivery
        )

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        *,
        based_on: str,
        duration: float = 0.5,
        button: str = "left",
        delivery: str = "background",
    ) -> CuaResult:
        return self._call(
            "drag",
            from_x=from_x,
            from_y=from_y,
            to_x=to_x,
            to_y=to_y,
            based_on=based_on,
            duration=duration,
            button=button,
            delivery=delivery,
        )

    def scroll(
        self,
        direction: str,
        amount: int = 3,
        *,
        based_on: str,
        x: float = 500,
        y: float = 500,
        by: str = "line",
        delivery: str = "background",
    ) -> CuaResult:
        return self._call(
            "scroll",
            direction=direction,
            amount=amount,
            x=x,
            y=y,
            based_on=based_on,
            by=by,
            delivery=delivery,
        )


class _Keyboard:
    def __init__(self, *, kind: str, pid: int | None = None, window_id: int | None = None):
        self.kind = kind
        self.pid = pid
        self.window_id = window_id

    def _call(self, action: str, **arguments: Any) -> CuaResult:
        params = {
            "kind": self.kind,
            "pid": self.pid,
            "window_id": self.window_id,
            "action": action,
            **arguments,
        }
        raw = _managed_rpc(
            f"mac.{self.kind}.keyboard.{action}", "keyboard_action", params
        )
        return CuaResult(raw)

    def type_text(self, text: Any, *, delivery: str = "background") -> CuaResult:
        return self._call("type_text", text=str(text), delivery=delivery)

    def press(
        self,
        key: Any,
        *,
        modifiers: list[str] | tuple[str, ...] | None = None,
        delivery: str = "background",
    ) -> CuaResult:
        return self._call(
            "press",
            key=str(key),
            modifiers=list(modifiers or []),
            delivery=delivery,
        )

    def hotkey(self, *keys: Any, delivery: str = "background") -> CuaResult:
        return self._call(
            "hotkey", keys=[str(key) for key in keys], delivery=delivery
        )


class _WindowAx:
    def __init__(self, pid: int, window_id: int) -> None:
        self.pid = int(pid)
        self.window_id = int(window_id)

    def snapshot(
        self,
        *,
        query: str | None = None,
        include_screenshot: bool = False,
        include_tree_markdown: bool = False,
        max_elements: int | None = None,
        max_depth: int | None = None,
    ) -> WindowState:
        arguments = {
            "pid": self.pid,
            "window_id": self.window_id,
            "query": query,
            "include_screenshot": include_screenshot,
            "include_accessibility_tree": True,
            "max_elements": max_elements,
            "max_depth": max_depth,
            "max_dimension": None,
        }
        try:
            raw = _rpc("get_window_state", **arguments)
            state = WindowState(raw)
        except Exception as exc:
            _record_tool(
                "mac.window.ax.snapshot",
                arguments,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        _record_tool(
            "mac.window.ax.snapshot",
            arguments,
            _state_summary(state, include_tree=include_tree_markdown),
            include_tree=include_tree_markdown,
        )
        return state

    def _action(self, tool: str, element_ref: str, **arguments: Any) -> CuaResult:
        params = {
            "tool": tool,
            "pid": self.pid,
            "window_id": self.window_id,
            "element_ref": str(element_ref),
            **arguments,
        }
        raw = _managed_rpc(f"mac.window.ax.{tool}", "ax_action", params)
        return CuaResult(raw)

    def click(self, element_ref: str, *, action: str | None = None) -> CuaResult:
        return self._action("click", element_ref, action=action)

    def double_click(
        self, element_ref: str, *, delivery: str = "background"
    ) -> CuaResult:
        return self._action("double_click", element_ref, delivery_mode=delivery)

    def right_click(
        self, element_ref: str, *, delivery: str = "background"
    ) -> CuaResult:
        return self._action("right_click", element_ref, delivery_mode=delivery)

    def type_text(
        self,
        element_ref: str,
        text: Any,
        *,
        delay_ms: int | None = None,
        delivery: str = "background",
    ) -> CuaResult:
        return self._action(
            "type_text",
            element_ref,
            text=str(text),
            delay_ms=delay_ms,
            delivery_mode=delivery,
        )

    def set_value(self, element_ref: str, value: Any) -> CuaResult:
        return self._action("set_value", element_ref, value=str(value))

    def press(
        self,
        element_ref: str,
        key: Any,
        *,
        modifiers: list[str] | tuple[str, ...] | None = None,
        delivery: str = "background",
    ) -> CuaResult:
        return self._action(
            "press_key",
            element_ref,
            key=str(key),
            modifiers=list(modifiers or []),
            delivery_mode=delivery,
        )

    def scroll(
        self,
        element_ref: str,
        direction: str,
        amount: int = 3,
        *,
        by: str = "line",
        delivery: str = "background",
    ) -> CuaResult:
        return self._action(
            "scroll",
            element_ref,
            direction=str(direction),
            amount=int(amount),
            by=str(by),
            delivery_mode=delivery,
        )


class WindowTarget:
    def __init__(self, pid: int, window_id: int) -> None:
        self.pid = int(pid)
        self.window_id = int(window_id)
        self.mouse = _Mouse(kind="window", pid=self.pid, window_id=self.window_id)
        self.keyboard = _Keyboard(kind="window", pid=self.pid, window_id=self.window_id)
        self.ax = _WindowAx(self.pid, self.window_id)

    def activate(self) -> CuaResult:
        arguments = {"pid": self.pid, "window_id": self.window_id}
        return CuaResult(
            _managed_rpc("mac.window.activate", "bring_to_front", arguments)
        )

    def screenshot(self, *, max_dimension: int | None = None) -> WindowState:
        arguments = {
            "pid": self.pid,
            "window_id": self.window_id,
            "include_screenshot": True,
            "include_accessibility_tree": False,
            "max_dimension": max_dimension,
            "query": None,
            "max_elements": None,
            "max_depth": None,
        }
        try:
            raw = _rpc("get_window_state", **arguments)
            state = WindowState(raw)
        except Exception as exc:
            _record_tool(
                "mac.window.screenshot",
                arguments,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        summary = _state_summary(state, include_tree=False)
        _record_tool("mac.window.screenshot", arguments, summary, include_tree=False)
        return state

    def verify(
        self,
        expect: list[dict[str, Any]],
        *,
        timeout_ms: int = 5000,
        stable_samples: int = 2,
    ) -> CuaResult:
        arguments = {
            "pid": self.pid,
            "window_id": self.window_id,
            "expect": expect,
            "timeout_ms": timeout_ms,
            "stable_samples": stable_samples,
            "include_screenshot": False,
        }
        return CuaResult(
            _managed_rpc("mac.window.verify", "verify_state", arguments)
        )


class _MacApps:
    @staticmethod
    def list(
        *, query: str | None = None, running: bool | None = None
    ) -> list[_Record]:
        arguments = {"query": query, "running": running}
        try:
            raw = _rpc("list_apps")
            apps = list((raw.get("structured") or {}).get("apps") or [])
            if query:
                needle = query.casefold()
                apps = [
                    app
                    for app in apps
                    if needle
                    in " ".join(
                        str(app.get(key) or "") for key in ("name", "bundle_id", "launch_path")
                    ).casefold()
                ]
            if running is not None:
                apps = [app for app in apps if bool(app.get("running")) is bool(running)]
        except Exception as exc:
            _record_tool(
                "mac.apps.list", arguments, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        public = {"count": len(apps), "apps": apps}
        _record_tool("mac.apps.list", arguments, public, include_tree=False)
        return [_Record(app) for app in apps]

    @staticmethod
    def open(
        *,
        name: str | None = None,
        bundle_id: str | None = None,
        activate: bool = False,
        new_instance: bool = False,
    ) -> _Record:
        arguments = {
            "name": name,
            "bundle_id": bundle_id,
            "activate": activate,
            "new_instance": new_instance,
        }
        raw = _managed_rpc("mac.apps.open", "open_app", arguments)
        return _Record(raw)


class _MacWindows:
    @staticmethod
    def list(
        *,
        pid: int | None = None,
        app_name: str | None = None,
        on_screen_only: bool = False,
    ) -> list[_Record]:
        arguments = {
            "pid": pid,
            "app_name": app_name,
            "on_screen_only": on_screen_only,
        }
        try:
            raw = _rpc(
                "list_windows", pid=pid, on_screen_only=on_screen_only
            )
            windows = list((raw.get("structured") or {}).get("windows") or [])
            if app_name:
                needle = app_name.casefold()
                windows = [
                    window
                    for window in windows
                    if needle in str(window.get("app_name") or "").casefold()
                ]
        except Exception as exc:
            _record_tool(
                "mac.windows.list", arguments, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        public = {"count": len(windows), "windows": windows}
        _record_tool("mac.windows.list", arguments, public, include_tree=False)
        return [_Record(window) for window in windows]

    @staticmethod
    def get(*, pid: int, window_id: int) -> WindowTarget:
        return WindowTarget(pid, window_id)


class _MacDesktop:
    def __init__(self) -> None:
        self.mouse = _Mouse(kind="desktop")
        self.keyboard = _Keyboard(kind="desktop")

    @staticmethod
    def screenshot() -> VisualObservation:
        raw = _managed_rpc("mac.desktop.screenshot", "screenshot", {})
        return VisualObservation(raw)


class _Mac:
    def __init__(self) -> None:
        self.apps = _MacApps()
        self.windows = _MacWindows()
        self.desktop = _MacDesktop()


class BashResult(str):
    """字符串本体即 stdout；同时支持属性与 dict 风格读取，容忍 .get()/["stdout"] 习惯。"""

    _FIELDS = ("stdout", "stderr", "returncode", "timed_out", "ok")

    def __new__(
        cls,
        stdout: str,
        stderr: str,
        returncode: int,
        timed_out: bool,
    ):
        value = str.__new__(cls, stdout)
        value.stdout = stdout
        value.stderr = stderr
        value.returncode = returncode
        value.timed_out = timed_out
        value.ok = returncode == 0 and not timed_out
        return value

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._FIELDS:
            return getattr(self, key)
        return default

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            if key in self._FIELDS:
                return getattr(self, key)
            raise KeyError(key)
        # 整数/切片下标保持字符串语义。
        return super().__getitem__(key)


def _image_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """仅复制当前图片的来源，不读取最近观察或更新动作依据。"""
    source = dict(value.get("provenance") or {})
    source.update(value)
    target = source.get("target")
    if target is None and source.get("window_id") is not None:
        target = {"kind": "window", "pid": source.get("pid"), "window_id": source["window_id"]}
    return {
        "visual_ref": source.get("visual_ref"),
        "coordinate_space": source.get("coordinate_space"),
        "target": target,
    }


def _emit_image(value: Any, label: str | None, publish: Any) -> None:
    from PIL import Image

    metadata = _image_metadata(getattr(value, "metadata", {}) or {})
    if isinstance(value, bytes):
        try:
            # 校验和解码当前 bytes；绝不能用缓存中的最近截图替代。
            with Image.open(io.BytesIO(value)) as checked:
                checked.verify()
            with Image.open(io.BytesIO(value)) as decoded:
                decoded.load()
                image = decoded.copy()
        except (OSError, ValueError, SyntaxError) as exc:
            raise ValueError("emit(bytes) 需要可解码的图片，收到无效或非图片 bytes") from exc
    else:
        value.load()
        image = value.copy()
    if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}:
        image = image.convert("RGBA")
    from image_store import save_image
    image_dir = Path(str(_CONFIG.get("image_dir") or _CONFIG.get("work_dir") or "/tmp"))
    path = save_image(image, image_dir)
    event = {
        "type": "screenshot", "path": str(path),
        "width": image.width, "height": image.height, **metadata,
    }
    if label is not None:
        event["label"] = label
    publish(event)


def emit(value: Any) -> None:
    """Legacy synchronous helper; SDK-backed Agent code must use ``await mu.emit``."""
    _check_active_request()
    if _MAI_SESSION is not None:
        raise RuntimeError("SDK mode requires: await mu.emit(value)")
    _display_value(value)


def _capability_views(original: Any, converted: Any, path: tuple = ()) -> list[dict]:
    """由真实对象生成展示标记，绝不以四字段 JSON 的形状猜测能力类型。"""
    if _MAI_SESSION is None:
        return []
    from mai_ui_values import Capability

    if isinstance(original, Capability) and isinstance(converted, dict):
        return [{"path": list(path), "kind": "detail", "operation": getattr(original, "_operation", None)}]
    if isinstance(original, dict) and isinstance(converted, dict):
        # JSON 会将数字键转成字符串；键碰撞后无法证明值仍来自同一原对象。
        if len(original) != len(converted):
            return []
        if (original and all(type(key) is str and isinstance(child, Capability)
                             for key, child in original.items()) and original.keys() == converted.keys()):
            return [{"path": list(path), "kind": "overview", "operations": {
                key: getattr(child, "_operation", None) or key for key, child in original.items()
            }}]
        return [view for key, child in original.items() if type(key) is str and key in converted
                for view in _capability_views(child, converted[key], path + (key,))]
    if isinstance(original, (list, tuple)) and isinstance(converted, (list, tuple)) and len(original) == len(converted):
        return [view for index, (child, value) in enumerate(zip(original, converted))
                for view in _capability_views(child, value, path + (index,))]
    return []


def _inventory_view(original: Any, top_level: bool) -> dict | None:
    """仅当顶层 original 是真实 AppInventory/WindowInventory 实例时返回展示注解。

    注解只认真实类型，绝不以 apps/windows+complete 的形状猜测；嵌套在 dict/list
    中的清单不加注解。event value 保持原转换数据不变，注解只服务展示层紧凑渲染。
    """
    if not top_level:
        return None
    try:
        from mai_ui_values import AppInventory, WindowInventory
    except Exception:
        return None
    if isinstance(original, AppInventory):
        return {"kind": "apps"}
    if isinstance(original, WindowInventory):
        return {"kind": "windows"}
    return None


def _display_value(value: Any) -> None:
    """显式展示返回值；结构中的 AX 与图片按键路径单独发布，返回 None。"""
    _check_active_request()
    if _CONTROL_SOCKET is not None and (
        threading.current_thread() is not threading.main_thread()
        or getattr(_REQUEST_CONTEXT, "request_id", None) is None
    ):
        raise RuntimeError("emit() 仅允许活动请求的主线程调用，未发布任何输出")
    frame = sys._getframe(1)
    internal_sources = {__file__, str(Path(__file__).with_name("mai_ui_api.py")),
                        str(Path(__file__).with_name("mai_ui_values.py"))}
    # 只跳过宿主包装帧，动态代码的来源不可归到调用它的外层模型代码。
    while frame is not None and frame.f_code.co_filename in internal_sources:
        frame = frame.f_back
    original = getattr(_MAI_SESSION, "_displaying_value", value)
    metadata = _output_metadata(original, frame)
    del frame
    from cua_api import ActionReceipt, AXFieldFragment, AXState, AXView
    try:
        from PIL import Image
        image_type = Image.Image
    except ImportError:
        image_type = ()

    ax_types = (AXState, AXView, AXFieldFragment)
    rich_types = (BashResult, ActionReceipt, *ax_types, bytes, WindowState, CuaResult, VisualObservation)

    def has_rich(item: Any, seen: set[int]) -> bool:
        if isinstance(item, rich_types) or isinstance(item, image_type):
            return True
        if isinstance(item, (dict, list, tuple)):
            if id(item) in seen:
                raise ValueError("emit() 不支持循环引用的结构")
            seen.add(id(item))
            try:
                children = item.values() if isinstance(item, dict) else item
                # 检查所有子项，避免短路遗漏循环引用。
                return any([has_rich(child, seen) for child in children])
            finally:
                seen.remove(id(item))
        return False

    def publish(item: Any, label: str | None = None, original_item: Any = None,
                top_level: bool = False) -> None:
        def send(event: dict[str, Any]) -> None:
            _emit({
                **event, **metadata,
                "value_type": _excerpt(type(item).__name__, 128, "...[type truncated]"),
                **({"label": label} if label is not None else {}),
            })

        if isinstance(item, ax_types):
            # AX 对象负责预算展示和发布前后的版本校验；普通 str/print 不经过此协议。
            item._emit_to(send)
        elif isinstance(item, BashResult):
            send({"type": "display", "kind": "bash", "value": {
                field: getattr(item, field) for field in item._FIELDS
            }})
        elif isinstance(item, ActionReceipt):
            send({"type": "display", "kind": "json", "value": {
                field: getattr(item, field) for field in item._FIELDS
            }})
        elif isinstance(item, str):
            send({"type": "display", "kind": "text", "text": item})
        elif isinstance(item, bytes) or isinstance(item, image_type):
            _emit_image(item, label, send)
        elif isinstance(item, (WindowState, CuaResult, VisualObservation)):
            data = _public_json(item)
            image = item.image
            if image is not None:
                data["screenshot"] = image
            publish(data, label)
        elif isinstance(item, (dict, list, tuple)) and has_rich(item, set()):
            children = item.items() if isinstance(item, dict) else enumerate(item)
            for key, child in children:
                if isinstance(item, dict):
                    suffix = f".{key}" if isinstance(key, str) and key.isidentifier() else (
                        "[" + json.dumps(key, ensure_ascii=False, default=str) + "]"
                    )
                else:
                    suffix = f"[{key}]"
                child_label = _excerpt((label or "$") + suffix, MAX_LABEL_CHARS, "...[path truncated]")
                original_child = (original_item.get(key) if isinstance(original_item, dict) else
                                  original_item[key] if isinstance(original_item, (list, tuple))
                                  and type(key) is int and 0 <= key < len(original_item) else None)
                publish(child, child_label, original_child)
        else:
            # 普通结构保留紧凑列表渲染；富值已递归拆分，绝不字符串化图片。
            data = json.loads(json.dumps(item, ensure_ascii=False, default=str))
            views = _capability_views(original_item, data)
            inventory = _inventory_view(original_item, top_level)
            send({"type": "display", "kind": "json", "value": data,
                  **({"capability_views": views} if views else {}),
                  **({"inventory_view": inventory} if inventory else {})})

    has_rich(value, set())
    publish(value, original_item=original, top_level=True)


def _read_limited(path: Path, limit: int = 256 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= limit:
                data = handle.read()
            else:
                half = limit // 2
                head = handle.read(half)
                handle.seek(max(0, size - half))
                tail = handle.read(half)
                data = head + b"\n...[output truncated]...\n" + tail
        return data.decode("utf-8", "replace")
    except Exception:
        return ""


def bash(
    cmd: str,
    timeout: float | None = 120,
    echo: bool = False,
    cwd: str | None = None,
) -> BashResult:
    global _BASH_CALL_COUNT
    _check_active_request()
    if not _CONFIG.get("enable_bash", True):
        raise PermissionError("bash() is disabled for this run")
    if not isinstance(cmd, str):
        raise TypeError("bash() expects a command string")
    work_dir = Path(str(_CONFIG.get("work_dir") or "/tmp"))
    work_dir.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}_{time.time_ns()}"
    out_path = work_dir / f"bash_{token}.out"
    err_path = work_dir / f"bash_{token}.err"
    timed_out = False
    with out_path.open("wb", buffering=0) as stdout_file, err_path.open(
        "wb", buffering=0
    ) as stderr_file:
        process = subprocess.Popen(
            ["/bin/bash", "-lc", cmd],
            stdout=stdout_file,
            stderr=stderr_file,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            start_new_session=True,
        )
        if _CONTROL_SOCKET is not None:
            _emit({"type": "_child_group", "pid": process.pid, "active": True})
        try:
            returncode = process.wait(timeout=None if timeout is None else float(timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = 124
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except Exception:
                    pass
                try:
                    process.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if _CONTROL_SOCKET is not None:
                # Bash 使用独立进程组，向父执行器登记，并在本次调用结束时回收后代。
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                _emit({"type": "_child_group", "pid": process.pid, "active": False})
    stdout = _read_limited(out_path)
    stderr = _read_limited(err_path)
    out_path.unlink(missing_ok=True)
    err_path.unlink(missing_ok=True)
    if timed_out:
        stderr = stderr.rstrip() + f"\n[bash timed out after {timeout}s; process group killed]\n"
    result = BashResult(stdout, stderr, returncode, timed_out)
    _record_tool(
        "bash",
        {"cmd": cmd, "timeout": timeout, "cwd": cwd},
        {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
            "ok": result.ok,
            "timed_out": timed_out,
        },
        include_tree=False,
    )
    if echo:
        _BASH_CALL_COUNT += 1
        preview = " ".join(cmd.strip().split())
        if len(preview) > 240:
            preview = preview[:240] + "..."
        print(f"[Bash result #{_BASH_CALL_COUNT}]")
        print(f"command: {preview}")
        print(
            f"returncode: {returncode}; ok: {str(result.ok).lower()}; "
            f"timed_out: {str(timed_out).lower()}"
        )
        print("stdout:\n" + (stdout.rstrip() if stdout else "(empty)"))
        print("stderr:\n" + (stderr.rstrip() if stderr else "(empty)"))
        print(f"[/Bash result #{_BASH_CALL_COUNT}]")
    return result


def screenshot():
    return pyautogui.screenshot()


def open_app(name: str) -> Any:
    return legacy_cua.open_app(name)


def terminate(status: str = "success", answer: Any = None) -> None:
    _check_active_request()
    normalized = str(status).strip().lower()
    aliases = {"done": "success", "succeeded": "success", "ok": "success", "fail": "failure", "failed": "failure"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"success", "failure"}:
        raise ValueError('terminate status must be "success" or "failure"')
    event: dict[str, Any] = {
        "type": "terminate", "status": normalized,
        "block_index": getattr(_REQUEST_CONTEXT, "block_index", None),
    }
    if answer is not None:
        event["answer"] = str(answer)
    _emit(event)
    _REQUEST_CONTEXT.stop_reason = "terminate"
    raise _ControlExit(0)


def ask_user(question: Any) -> None:
    _check_active_request()
    value = str(question).strip()
    if not value:
        raise ValueError("ask_user question must not be empty")
    _emit({
        "type": "ask_user", "question": value,
        "block_index": getattr(_REQUEST_CONTEXT, "block_index", None),
    })
    _REQUEST_CONTEXT.stop_reason = "ask_user"
    raise _ControlExit(0)


class _Todo:
    def update(
        self,
        id: Any = None,
        kind: Any = None,
        content: Any = None,
        status: Any = None,
        **_extra: Any,
    ) -> None:
        _check_active_request()
        if id is None or not str(id).strip():
            raise ValueError("todo.update requires id")
        arguments = {"id": str(id)}
        for key, value in (("kind", kind), ("content", content), ("status", status)):
            if value is not None:
                arguments[key] = str(value)
        _emit({"type": "todo", "op": "update", "arguments": arguments})
        existing = next((item for item in _TODO_ITEMS if item.get("id") == str(id)), None)
        if str(status).lower() == "delete":
            if existing is not None:
                _TODO_ITEMS.remove(existing)
            return
        if existing is None:
            existing = {"id": str(id), "kind": "task", "content": "", "status": "pending"}
            _TODO_ITEMS.append(existing)
        existing.update(arguments)

    def list(self) -> list[dict[str, Any]]:
        _check_active_request()
        _emit({"type": "todo", "op": "list"})
        return [dict(item) for item in _TODO_ITEMS]


todo = _Todo()
pyautogui: _PyAutoGUIProxy
legacy_cua = _CuaProxy()
cua: Any = legacy_cua
mac = _Mac()


def _format_exception(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    kept = [frame for frame in frames if frame.filename == "<code>"
            or frame.filename.startswith(SOURCE_PREFIX)] or frames[-1:]
    rendered = "".join(
        ["Traceback (most recent call last):\n", *traceback.format_list(kept), *traceback.format_exception_only(type(exc), exc)]
    )
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        child_stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr)
        if len(child_stderr) > 2000:
            child_stderr = "[truncated] " + child_stderr[-2000:]
        rendered += f"child stderr (diagnostic data): {child_stderr!r}\n"
    return rendered


class _RequestOutput:
    """按轮次捕获文件描述符，覆盖 print、二进制写入和子进程输出。"""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.selector = selectors.DefaultSelector()
        self.stop = threading.Event()
        self.saved: list[tuple[int, int]] = []
        self.targets: list[Any] = []
        self.streams = (sys.stdout, sys.stderr)
        self.counts = {"stdout": 0, "stderr": 0}
        self.truncated: set[str] = set()
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> _RequestOutput:
        try:
            for fd, name, default in ((1, "stdout", MAX_STDOUT_BYTES), (2, "stderr", MAX_STDERR_BYTES)):
                saved = os.dup(fd)
                self.saved.append((fd, saved))
                read_fd, write_fd = os.pipe()
                os.set_blocking(read_fd, False)
                limit = int(_CONFIG.get(f"max_{name}_bytes", default))
                self.selector.register(read_fd, selectors.EVENT_READ, (name, limit))
                try:
                    os.dup2(write_fd, fd)
                    target = os.fdopen(os.dup(write_fd), "w", encoding="utf-8", errors="replace", buffering=1)
                finally:
                    os.close(write_fd)
                self.targets.append(target)
                setattr(sys, name, _LimitedStream(target, limit))
            self.thread = threading.Thread(target=self._drain, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _chunk(self, name: str, data: bytes, limit: int) -> None:
        room = max(0, limit - self.counts[name])
        kept = data[:room]
        self.counts[name] += len(kept)
        if len(data) > room and name not in self.truncated:
            self.truncated.add(name)
            kept += "\n[mac-agent] 本轮输出超过额度，后续输出已丢弃。\n".encode("utf-8")
        if kept:
            _send_frame({
                "kind": "output", "request_id": self.request_id,
                "stream": name, "data": base64.b64encode(kept).decode("ascii"),
            })

    def _read(self, key: Any) -> bool:
        try:
            chunk = os.read(key.fd, 16384)
        except BlockingIOError:
            return False
        if not chunk:
            self.selector.unregister(key.fd)
            os.close(key.fd)
            return False
        name, limit = key.data
        self._chunk(name, chunk, limit)
        return True

    def _drain(self) -> None:
        try:
            while not self.stop.is_set():
                for key, _ in self.selector.select(0.02):
                    self._read(key)
            # 不等待继承输出描述符的后台进程退出，也不让持续写入阻塞 done。
            for key in list(self.selector.get_map().values()):
                for _ in range(64):
                    if not self._read(key):
                        break
        except BaseException as exc:
            self.error = exc

    def __exit__(self, *_exc: Any) -> None:
        for target in self.targets:
            try:
                target.flush()
            except (OSError, ValueError):
                pass
        for fd, saved in self.saved:
            os.dup2(saved, fd)
            os.close(saved)
        self.saved.clear()
        for target in self.targets:
            target.close()
        sys.stdout, sys.stderr = self.streams
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        for key in list(self.selector.get_map().values()):
            self.selector.unregister(key.fd)
            os.close(key.fd)
        self.selector.close()
        if self.error is not None:
            raise self.error


def _sdk_output(output: dict[str, Any]) -> None:
    """Translate mai-ui's output sink events into the existing worker protocol."""
    _check_active_request()
    if output.get("type") == "image":
        data = output.get("data")
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError("mai-ui image output did not contain bytes")
        from PIL import Image
        try:
            with Image.open(io.BytesIO(data)) as frame:
                frame.load()
                if "A" in frame.getbands() and frame.getchannel("A").getbbox() is None:
                    raise RuntimeError(
                        "mai-ui returned a fully transparent capture. The window image is "
                        "unusable; reacquire the window or observe device.ui.vision instead."
                    )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"mai-ui returned an invalid image: {exc}") from exc
        from image_store import save_image
        image_dir = Path(str(_CONFIG.get("image_dir") or _CONFIG.get("work_dir") or "/tmp"))
        path = save_image(data, image_dir)
        _emit({
            "type": "screenshot",
            "path": str(path),
            "width": output.get("width"),
            "height": output.get("height"),
            "visual_ref": output.get("image_id"),
            "coordinate_space": output.get("coordinate_space", "normalized_1000"),
            "label": output.get("caption"),
            "sdk_output": True,
        })
        return
    text_value = output.get("text")
    if not isinstance(text_value, str):
        text_value = json.dumps(output, ensure_ascii=False, default=str)
    _emit({"type": "display", "kind": "text", "text": text_value,
           "value_type": "mai_ui_sdk", "sdk_output": True})


def _sdk_audit_result(operation: str, result: Any, elapsed: float) -> Any:
    """Keep trajectories useful without duplicating full AX trees or image payloads."""
    if not isinstance(result, dict):
        return {"elapsed_seconds": elapsed, "value_type": type(result).__name__}
    summary: dict[str, Any] = {"elapsed_seconds": elapsed}
    if operation == "list_apps":
        summary.update(app_count=len(result.get("apps") or ()), complete=result.get("complete"))
    elif operation == "list_windows":
        summary.update(window_count=len(result.get("windows") or ()), complete=result.get("complete"))
    elif operation == "observe":
        summary.update(node_count=len(result.get("nodes") or ()),
                       image_count=int(bool(result.get("png"))) + len(result.get("images") or ()),
                       truncated=result.get("truncated"), source=result.get("source"))
    else:
        for key in ("execution", "effect", "action_id", "mechanism", "status", "complete"):
            if key in result:
                summary[key] = result[key]
        if len(summary) == 1:
            summary["keys"] = sorted(result)[:20]
    return summary


def _repair_transparent_observation(result: Any) -> Any:
    """Replace a broken SDK window frame with an exact macOS window capture.

    Older CuaDriver releases can return an all-transparent PNG for a valid Chrome window.
    The SDK window ID embeds the native CGWindowID, so ``screencapture -l`` can
    capture the *same* window without changing the observation's target or token.
    Keep the original pixel dimensions because the SDK's coordinate basis was
    established before this function runs.
    """
    if not isinstance(result, dict) or not isinstance(result.get("images"), list):
        return result
    from PIL import Image

    repaired = None
    for index, item in enumerate(result["images"]):
        if not isinstance(item, dict):
            continue
        field = "png" if isinstance(item.get("png"), str) else "data_base64"
        encoded = item.get(field)
        if not isinstance(encoded, str):
            continue
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as frame:
            frame.load()
            if "A" not in frame.getbands() or frame.getchannel("A").getbbox() is not None:
                continue
            original_size = frame.size

        target = item.get("target") or {}
        window_ref = target.get("window_id") if isinstance(target, dict) else None
        match = re.fullmatch(r"window:(\d+):\d+", str(window_ref or ""))
        if not match:
            raise RuntimeError(
                "mai-ui returned a fully transparent frame without a native window ID"
            )
        window_id = match.group(1)
        fd, filename = tempfile.mkstemp(prefix="sdk_window_repair_", suffix=".png")
        os.close(fd)
        try:
            capture = subprocess.run(
                ["/usr/sbin/screencapture", "-x", "-o", "-l", window_id,
                 "-t", "png", filename],
                capture_output=True, text=True, timeout=20, check=False,
            )
            if capture.returncode != 0:
                raise RuntimeError(
                    f"window {window_id} fallback capture failed: "
                    f"{capture.stderr[-300:]}"
                )
            with Image.open(filename) as fallback:
                fallback.load()
                if ("A" in fallback.getbands()
                        and fallback.getchannel("A").getbbox() is None):
                    raise RuntimeError(
                        f"window {window_id} fallback was also fully transparent"
                    )
                ratio = (fallback.width / original_size[0],
                         fallback.height / original_size[1])
                # screencapture may return backing pixels while the SDK reports
                # logical pixels (e.g. 1920x955 for a 1568x780 Chrome window).
                # Uniform scaling preserves the window's coordinate basis.
                if (any(not 0.5 <= value <= 4.0 for value in ratio)
                        or abs(ratio[0] - ratio[1]) / max(ratio) > 0.03):
                    raise RuntimeError(
                        f"window {window_id} fallback geometry differs: "
                        f"{fallback.size} vs {original_size}"
                    )
                if fallback.size != original_size:
                    fallback = fallback.resize(original_size, Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                fallback.save(buffer, format="PNG")
                replacement = base64.b64encode(buffer.getvalue()).decode("ascii")
        finally:
            Path(filename).unlink(missing_ok=True)
        if repaired is None:
            repaired = {**result, "images": list(result["images"])}
        repaired["images"][index] = {**item, field: replacement}
        print(f"[sdk] repaired transparent capture for window {window_id}", flush=True)
    return repaired if repaired is not None else result


def _instrument_sdk_session(session: Any) -> None:
    """Record SDK provider requests in the existing per-step tool-call audit."""
    original = session._request

    async def traced_call(device, message):
        operation = str(message.get("op") or "request")
        action = str(message.get("action") or "")
        name = "mai_ui." + operation + (("." + action) if action else "")
        audit_message = {**message, "resource_id": getattr(device, "resource_id", None),
                         "platform": getattr(device, "platform", "macos")}
        started = time.monotonic()
        global _SDK_REVIVE_PENDING, _SDK_GUI_OK, _SDK_HEAL_INPROGRESS
        global _SDK_LAST_GUI_SUCCESS_AT
        endpoint = getattr(device, "_endpoint", None)
        try:
            offline_before = endpoint.offline if endpoint is not None else None
        except Exception:
            offline_before = None
        try:
            result = await original(device, message)
            if operation == "observe":
                result = await asyncio.to_thread(_repair_transparent_observation, result)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if (
                code == "PERMISSION_DENIED"
                and _SDK_GUI_OK
                and not _SDK_HEAL_INPROGRESS
                and getattr(device, "platform", "macos") == "macos"
            ):
                # 驱动空闲回收/复活会丢 TCC 授权上下文：此前同类调用成功过说明授权
                # 本身没被撤销，重建 SDK 会话（重新走已授权解释器）并重试一次。
                print(
                    "[System] GUI 调用被 PERMISSION_DENIED 拒绝；判定为驱动会话空闲回收"
                    "导致授权上下文丢失，正在自动重建 GUI 会话并重试该调用…",
                    flush=True,
                )
                try:
                    _SDK_HEAL_INPROGRESS = True
                    session2, device2 = await _reset_sdk_session()
                    result = await session2._request(device2, message)
                    if operation == "observe":
                        result = await asyncio.to_thread(
                            _repair_transparent_observation, result
                        )
                except Exception as retry_exc:  # noqa: BLE001 - 自愈失败如实上报
                    _SDK_HEAL_INPROGRESS = False
                    _record_tool(
                        name, audit_message,
                        error=f"{exc} | auto-heal retry failed: {retry_exc}",
                        error_code=code,
                    )
                    print(
                        "[System] 已自动重建 GUI 会话并重试，仍被 PERMISSION_DENIED 拒绝："
                        "授权上下文未恢复。请 ask_user 请求用户检查「系统设置 → 隐私与安全性」"
                        "中屏幕录制/辅助功能对当前解释器的授权，或本步改走非 GUI 路径；"
                        "不要重复重试同一 GUI 调用。",
                        flush=True,
                    )
                    raise
                _SDK_HEAL_INPROGRESS = False
                elapsed = round(time.monotonic() - started, 3)
                _record_tool(
                    name, audit_message,
                    _sdk_audit_result(operation, result, elapsed),
                    include_tree=False,
                )
                notice = (
                    "GUI 会话已自动重建（驱动空闲回收致旧会话失效）且新会话验证可用；"
                    "但本步代码持有的 window/ui 句柄与 typed 对象已失效，本步中止。"
                    "下一步请重新 list_windows/observe 绑定目标后重做该操作。"
                )
                print(f"[System] {notice}", flush=True)
                from mai_ui import MuError as _MuError
                raise _MuError("SESSION_RESET", notice)
            audit_error = str(exc)
            if code == "DEVICE_OFFLINE":
                try:
                    offline_after = endpoint.offline if endpoint is not None else None
                except Exception:
                    offline_after = None
                age = (round(time.monotonic() - _SDK_LAST_GUI_SUCCESS_AT, 3)
                       if _SDK_LAST_GUI_SUCCESS_AT is not None else None)
                audit_error += (
                    f" [diagnostic: endpoint_offline_before={offline_before}, "
                    f"endpoint_offline_after={offline_after}, "
                    f"last_gui_success_age_seconds={age}, "
                    f"keepalive_error={_SDK_LAST_KEEPALIVE_ERROR!r}]"
                )
            _record_tool(
                name, audit_message,
                error=audit_error, error_code=code,
                receipt=getattr(exc, "receipt", None),
                outcome_unknown=code == "ACTION_OUTCOME_UNKNOWN",
            )
            if code == "DEVICE_OFFLINE":
                # endpoint 被 fence 后同 Session 永不恢复；标记下一轮由宿主重建会话。
                _SDK_REVIVE_PENDING = True
            raise
        elapsed = round(time.monotonic() - started, 3)
        if getattr(device, "platform", "macos") == "macos":
            _SDK_GUI_OK = True
            _SDK_LAST_GUI_SUCCESS_AT = time.monotonic()
        _record_tool(name, audit_message, _sdk_audit_result(operation, result, elapsed),
                     include_tree=False)
        return result

    control_ack_lock = asyncio.Lock()
    async def traced(device, message):
        from overlay_guard import impact
        scope = gui_action_scope(device, message)
        if scope is not None:
            from mai_ui import MuError
            app_id, window_id, needs_vision, needs_foreground = scope
            attached_app = getattr(device, "_app_id", None)
            attached_window = getattr(device, "_window_id", None)
            if (not app_id or not window_id
                    or (attached_app and app_id != attached_app)
                    or (attached_window and window_id != attached_window)):
                raise MuError("INPUT_REFUSED", "GUI approval requires an exact bound app/window target")
            async with control_ack_lock:
                approved = await _request_gui_approval(
                    app_id, window_id,
                    needs_vision=needs_vision, needs_foreground=needs_foreground,
                )
            if not approved:
                raise MuError("PERMISSION_DENIED", "GUI approval was denied or unavailable; action not dispatched")
        footprint = impact(device, message) if _CONFIG.get("overlay_guard") else None
        if footprint is None:
            return await traced_call(device, message)
        async with control_ack_lock:
            guard_id = uuid.uuid4().hex
            _emit({"type": "overlay_guard", "phase": "begin", "guard_id": guard_id, **footprint})
            try:
                reply = await asyncio.to_thread(
                    _recv_host_ack, "overlay_ack", "guard_id", guard_id, 8,
                )
                if reply is None or reply.get("ready") is not True:
                    raise RuntimeError("Overlay preparation was not acknowledged; UI operation was not dispatched")
                return await traced_call(device, message)
            finally:
                _emit({"type": "overlay_guard", "phase": "end", "guard_id": guard_id})

    session._request = traced
    # mu.help 升级为按需手册（从生成契约 api.pyi + playwright contract 渲染），
    # 取代 Rust core 里 topic 无效的静态一句话。
    try:
        from rich_help import build_help
        session.help = lambda topic="core": build_help(topic)
    except Exception:  # noqa: BLE001 - help upgrade is best-effort
        pass


def _prefer_local_browser_worker() -> None:
    """Use the rebuilt worker in an editable checkout when its npm deps exist."""
    if "MAI_UI_PLAYWRIGHT_WORKER" in os.environ:
        return
    root = Path(__file__).resolve().parents[3]
    adapter = root / "packages/sdk/adapters/browser-playwright"
    worker = adapter / "dist/worker.js"
    dependency = adapter / "node_modules/playwright/package.json"
    if worker.is_file() and dependency.is_file():
        os.environ["MAI_UI_PLAYWRIGHT_WORKER"] = str(worker)


async def _add_web_factory(runtime: Any, *, visible: bool = False) -> None:
    """Register the browser factory, preferring the user's installed Google Chrome.

    channel="chrome" launches the real Google Chrome app as a separate temp-profile
    instance (no separate "Chrome for Testing" binary; the human can Cmd+Q it like any
    Chrome). Verified working for normal sites; note some corporate-blocked hosts
    (e.g. example.com) fail with ERR_CERT_COMMON_NAME_INVALID in ANY browser here.
    Falls back to Playwright's downloaded chromium if the channel is unavailable.
    """
    try:
        profile = os.environ.get("MAC_AGENT_WEB_PROFILE", "isolated")
        if profile == "attach":
            # 并发共用：接入用户正在跑的 Chrome（需以 --remote-debugging-port 启动）。
            await runtime.add_browser_factory(
                "web", engine="chromium", headless=False, channel="chrome",
                connect_url=os.environ.get("MAC_AGENT_WEB_CDP", "http://127.0.0.1:9222"))
        elif profile == "borrow":
            # 独占借用：以用户真实 profile 启动（用户需先退出自己的 Chrome）。
            await runtime.add_browser_factory(
                "web", engine="chromium", headless=False, channel="chrome",
                user_data_dir=os.environ.get(
                    "MAC_AGENT_WEB_PROFILE_DIR",
                    str(Path.home() / "Library/Application Support/Google/Chrome")))
        else:
            # isolated：有界面任务专属 profile；DOM 操作无需前台焦点。
            await runtime.add_browser_factory(
                "web", engine="chromium", headless=False,
                channel="chrome",
                background_launch=(os.environ.get("MAC_AGENT_BACKGROUND") == "1"
                                   and not visible))
    except Exception:
        await runtime.add_browser_factory(
            "web", engine="chromium", headless=False,
            background_launch=(os.environ.get("MAC_AGENT_BACKGROUND") == "1"
                               and not visible))


async def _create_web_resource(runtime: Any, session: Any, visible: bool = False) -> Any:
    """Create the `web` browser resource, falling back to a local Playwright shim.

    The SDK route needs the Rust core's browser endpoint. Older deployed SDK wheels
    default browser_create to 300000 ms, exceeding the host's 120000 ms limit, so
    always pass a valid explicit timeout. The shim remains a fallback for genuine
    browser endpoint failures.
    """
    global _SDK_NATIVE_WEB_FAILURE
    state = str(_WEB_STATE) if _WEB_STATE.is_file() else None
    require_native = os.environ.get("MAC_AGENT_REQUIRE_NATIVE_WEB") == "1"
    if require_native or _SDK_NATIVE_WEB_FAILURE is None:
        try:
            _prefer_local_browser_worker()
            await _add_web_factory(runtime, visible=visible)
            web = await session.browsers.create(
                name="web", provider="web", storage_state=state,
                timeout_ms=_WEB_CREATE_TIMEOUT_MS,
            )
            _SDK_NATIVE_WEB_FAILURE = None
            print("[sdk] web: SDK browser endpoint")
            return web
        except Exception as exc:  # noqa: BLE001 - browser route is optional unless required
            if require_native:
                raise RuntimeError(f"native SDK browser unavailable: {exc}") from exc
            _SDK_NATIVE_WEB_FAILURE = str(exc)
            print(f"[sdk] SDK browser endpoint unavailable ({exc}); trying local playwright shim")
    try:
        try:
            import web_shim
        except ImportError:  # the runtime dir is not always on sys.path
            import importlib.util
            _sp = importlib.util.spec_from_file_location(
                "web_shim", Path(__file__).resolve().with_name("web_shim.py"))
            web_shim = importlib.util.module_from_spec(_sp)
            _sp.loader.exec_module(web_shim)
        web = await web_shim.create_web(storage_state=state, visible=visible)
        print("[sdk] web: local playwright shim active (interface-compatible)")
        return web
    except Exception as exc:  # noqa: BLE001
        print(f"[sdk] web shim failed too, web=None: {exc}")
        return None


async def _open_sdk_session() -> tuple[Any, Any, Any, Any]:
    from mai_ui import Runtime

    global _SDK_PHONE, _SDK_PHONE_RESOURCE, _SDK_WEB
    _SDK_PHONE = _SDK_PHONE_RESOURCE = None
    runtime = Runtime()
    await runtime.__aenter__()
    session = None
    try:
        options = {}
        # Set only by an embedding host. SDK still verifies the pinned companion
        # signature/version and requires an explicit private socket.
        if os.environ.get("GENERALIST_DRIVER_APP"):
            options = {"app_path": os.environ["GENERALIST_DRIVER_APP"],
                       "socket": os.environ["GENERALIST_DRIVER_SOCKET"]}
        resource = await runtime.add_macos(name="local-mac", **options)
        session = runtime.session(output=_sdk_output)
        await session.__aenter__()
        session.auto_reconnect = True
        device = await session.register(resource, name="mac")
        session.coordinate_space = "normalized_1000"
        serial = os.environ.get("GENERALIST_ANDROID_SERIAL")
        if serial:
            # A selected phone is required: never silently fall back to PC actions.
            _SDK_PHONE_RESOURCE = await runtime.add_android(serial=serial, name="demo-phone")
            _SDK_PHONE = await session.register(_SDK_PHONE_RESOURCE, name="phone")
        # Warm native inventory with a budget allowing first driver startup.
        await device.apps.list(timeout_ms=30000)
        _instrument_sdk_session(session)
        # A headed browser may show a window. Do not launch it for non-web tasks.
        _SDK_WEB = (None if os.environ.get("MAC_AGENT_BACKGROUND") == "1"
                    else await _create_web_resource(runtime, session))
        return runtime, resource, session, device
    except BaseException:
        # Release every acquired endpoint, including Android's writer lease,
        # even if a later Mac warmup or browser startup failed.
        _SDK_PHONE = _SDK_PHONE_RESOURCE = None
        if session is not None:
            try:
                await session.close(timeout_ms=5000)
            except Exception:
                pass
        try:
            await runtime.close(timeout_ms=5000)
        except Exception:
            pass
        raise


async def _save_web_state() -> None:
    """Best-effort export of the web browser's cookies (incl. session-only ones).

    Sites like ke.com keep login state in session cookies that die with the browser;
    snapshotting at teardown and re-injecting at next start is what makes a login
    survive across sessions/restarts.
    """
    if _SDK_WEB is None:
        return
    try:
        state = await _SDK_WEB.browser.storage_state(timeout_ms=5000)
        _WEB_STATE.parent.mkdir(parents=True, exist_ok=True)
        _WEB_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 - snapshot is best-effort
        pass


def _start_keepalive(loop: asyncio.AbstractEventLoop | None = None, *, interval: float = 90) -> None:
    """Keep the native lease alive without a visible inventory/cursor action."""
    global _SDK_KEEPALIVE
    if _SDK_KEEPALIVE is not None:
        _SDK_KEEPALIVE.cancel()

    async def _beat() -> None:
        global _SDK_LAST_KEEPALIVE_ERROR
        while True:
            await asyncio.sleep(interval)
            session, device = _MAI_SESSION, _SDK_DEVICE
            if session is None or device is None:
                continue
            try:
                await asyncio.wait_for(device._backend._request(
                    {"op": "keepalive", "timeout_ms": 5000}), timeout=5)
                _SDK_LAST_KEEPALIVE_ERROR = None
            except Exception as exc:  # noqa: BLE001 - keepalive failure is diagnostic
                _SDK_LAST_KEEPALIVE_ERROR = str(exc)

    try:
        owner_loop = loop or asyncio.get_running_loop()
        _SDK_KEEPALIVE = owner_loop.create_task(_beat())
    except RuntimeError:
        _SDK_KEEPALIVE = None


async def _close_sdk_session(*, close_runtime: bool) -> None:
    global _MAI_SESSION, _SDK_RUNTIME, _SDK_DEVICE, _SDK_RESOURCE, _SDK_WEB, _SDK_KEEPALIVE
    global _SDK_PHONE, _SDK_PHONE_RESOURCE
    if _SDK_KEEPALIVE is not None:
        _SDK_KEEPALIVE.cancel()
        _SDK_KEEPALIVE = None
    session, runtime = _MAI_SESSION, _SDK_RUNTIME
    if session is not None:
        try:
            await _save_web_state()  # 先快照（含会话级 cookie），再拆会话
        except Exception:
            pass
    _MAI_SESSION = _SDK_DEVICE = _SDK_RESOURCE = _SDK_WEB = None
    _SDK_PHONE = _SDK_PHONE_RESOURCE = None
    if session is not None:
        try:
            await session.close(timeout_ms=5000)
        except Exception:
            pass
    if close_runtime and runtime is not None:
        _SDK_RUNTIME = None
        try:
            await runtime.close(timeout_ms=5000)
        except Exception:
            pass


async def _reset_sdk_session() -> tuple[Any, Any]:
    """Rebuild the macOS endpoint and create a fresh Session on it.

    A DEVICE_OFFLINE fence is latched on the Rust endpoint (offline/fenced never
    reset), so a new Session over the SAME endpoint stays offline. The endpoint
    itself must be reconnected (remove + re-add) before re-registering.
    """
    global _MAI_SESSION, _SDK_DEVICE, _SDK_RESOURCE, _SDK_WEB
    global _SDK_PHONE, _SDK_PHONE_RESOURCE
    if _SDK_RUNTIME is None or _SDK_RESOURCE is None:
        raise RuntimeError("mai-ui SDK runtime is not initialized")
    if _MAI_SESSION is not None:
        try:
            await _save_web_state()
        except Exception:
            pass
        try:
            await _MAI_SESSION.close(timeout_ms=5000)
        except Exception:
            pass
        _MAI_SESSION = None
    try:
        _SDK_RESOURCE = await _SDK_RUNTIME.reconnect(_SDK_RESOURCE.id, force=True)
    except Exception:
        pass  # if the endpoint cannot be rebuilt, the register below still attempts a session
    session = _SDK_RUNTIME.session(output=_sdk_output)
    await session.__aenter__()
    session.auto_reconnect = True
    device = await session.register(_SDK_RESOURCE, name="mac")
    if _SDK_PHONE_RESOURCE is not None:
        try:
            _SDK_PHONE_RESOURCE = await _SDK_RUNTIME.reconnect(_SDK_PHONE_RESOURCE.id, force=True)
            _SDK_PHONE = await session.register(_SDK_PHONE_RESOURCE, name="phone")
        except BaseException:
            _SDK_PHONE = None
            if _EXEC_NAMESPACE is not None:
                _EXEC_NAMESPACE["phone"] = None
            await session.close(timeout_ms=5000)
            raise
    session.coordinate_space = "normalized_1000"
    await device.apps.list(timeout_ms=30000)
    _instrument_sdk_session(session)
    # The local shim owns its browser independently of the Mac SDK session.
    # Reusing it avoids an orphan browser and a second launch after GUI reconnect.
    if _SDK_WEB is not None and getattr(_SDK_WEB, "resource_id", None) != "browser:web-shim":
        _SDK_WEB = await _create_web_resource(_SDK_RUNTIME, session)
    _MAI_SESSION, _SDK_DEVICE = session, device
    if _EXEC_NAMESPACE is not None:
        # cell 命名空间里的 mu/device/ui 若仍指向旧会话，后续每次调用都会
        # SESSION_CLOSED；重建后必须就地刷新，模型的 rebind 才有活对象可用。
        _EXEC_NAMESPACE.update(mu=session, device=device, ui=device.ui, phone=_SDK_PHONE)
        _EXEC_NAMESPACE["web"] = _SDK_WEB
    _start_keepalive()
    return session, device


async def rebind_window(
    native_id: Any, *, app: str | None = None, timeout_ms: int = 15000,
) -> Any:
    """Re-acquire a Window handle by its stable CGWindowID after a session rebuild.

    The SDK window id is ``window:<cwid>:<seq>``; ``<seq>`` is a first-seen counter
    that changes when the endpoint is rebuilt (e.g. after DEVICE_OFFLINE recovery),
    but ``<cwid>`` is the WindowServer CGWindowID and persists for the window's
    lifetime. Matching on ``<cwid>`` rebinds the SAME physical window deterministically
    even among many identical-title windows, unlike title/index matching. Without
    an explicit app, search the device inventory: the window may belong to any
    native application, not just Chrome.
    """
    device = _SDK_DEVICE
    if device is None:
        raise RuntimeError("rebind_window requires an active SDK device")
    provider_id = getattr(native_id, "window_id", native_id)
    if isinstance(provider_id, int) and not isinstance(provider_id, bool):
        cwid = str(provider_id)
    elif isinstance(provider_id, str):
        match_id = re.fullmatch(r"window:(\d+):\d+", provider_id)
        if match_id:
            cwid = match_id.group(1)
        elif provider_id.isdigit():
            cwid = provider_id
        else:
            raise ValueError(
                "rebind_window requires an old Window, its window_id, or CGWindowID; "
                "Window.id is a session attachment ID, not a native window ID"
            )
    else:
        raise ValueError("rebind_window requires an old Window, its window_id, or CGWindowID")
    owner = await device.apps.get(app, timeout_ms=timeout_ms) if app else device
    inventory = await owner.windows.list(timeout_ms=timeout_ms)
    wins = inventory.windows
    match = [w for w in wins if w.id.split(":")[1:2] == [cwid]]
    if len(match) != 1:
        raise RuntimeError(
            f"rebind_window: {len(match)} matches for CGWindowID {cwid} in the current "
            f"{app or 'device'} inventory (complete={inventory.complete}); no window was bound")
    return await owner.windows.get(match[0].id, timeout_ms=timeout_ms)


async def reset_web(visible: bool = False) -> Any:
    """Recreate the dedicated automation browser (e.g. after a human quit it).

    The isolated browser is headed in all modes. ``visible=True`` additionally
    requests the explicitly visible recovery path for human intervention.
    """
    global _SDK_WEB, _SDK_NATIVE_WEB_FAILURE
    if _SDK_RUNTIME is None or _MAI_SESSION is None:
        raise RuntimeError("reset_web requires an active SDK session")
    old_web = _SDK_WEB
    if old_web is not None:
        try:
            await _save_web_state()
        except Exception:
            pass
        try:
            await old_web.close(timeout_ms=5000)
        except Exception as exc:  # noqa: BLE001 - reset must still attempt recovery
            print(f"[sdk] old web resource did not close cleanly: {exc}")
        _SDK_WEB = None
    saved = os.environ.get("MAC_AGENT_BACKGROUND")
    if visible:
        os.environ.pop("MAC_AGENT_BACKGROUND", None)
    try:
        _SDK_NATIVE_WEB_FAILURE = None
        _SDK_WEB = await _create_web_resource(_SDK_RUNTIME, _MAI_SESSION, visible=visible)
    finally:
        if visible and saved is not None:
            os.environ["MAC_AGENT_BACKGROUND"] = saved
    if _EXEC_NAMESPACE is not None:
        _EXEC_NAMESPACE["web"] = _SDK_WEB
    return _SDK_WEB


async def save_web_session() -> str:
    """Persist the dedicated browser's login state (cookies/origins) for reuse.

    Call after a human finishes logging in inside the web browser window; later
    sessions load it automatically so the agent's browser carries the user's logins.
    """
    if _SDK_WEB is None:
        raise RuntimeError("save_web_session requires an active web browser")
    state = await _SDK_WEB.browser.storage_state()
    _WEB_STATE.parent.mkdir(parents=True, exist_ok=True)
    _WEB_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(_WEB_STATE)


def _bootstrap(loop: asyncio.AbstractEventLoop) -> dict[str, Any]:
    global _MAI_SESSION, _SDK_RUNTIME, _SDK_DEVICE, _SDK_RESOURCE
    _SDK_RUNTIME, _SDK_RESOURCE, _MAI_SESSION, _SDK_DEVICE = loop.run_until_complete(
        _open_sdk_session()
    )
    # The worker's event loop persists for its whole process lifetime. Start the
    # keepalive for the initial session too (not only after reconnect), otherwise a
    # long model/web/Python-only interval lets CuaDriver's 300s lease expire.
    _start_keepalive(loop)
    namespace: dict[str, Any] = {
        "__name__": "__main__",
        "__builtins__": __builtins__,
        "time": time,
        "os": os,
        "sys": sys,
        "re": re,
        "json": json,
        "subprocess": subprocess,
        "shlex": shlex,
        "Path": Path,
        "bash": bash,
        "rebind_window": rebind_window,
        "reset_web": reset_web,
        "save_web_session": save_web_session,
        "terminate": terminate,
        "ask_user": ask_user,
        "todo": todo,
        "log": print,
        "mu": _MAI_SESSION,
        "device": _SDK_DEVICE,
        "phone": _SDK_PHONE,
        "ui": _SDK_DEVICE.ui,
        "web": _SDK_WEB,
        "asyncio": asyncio,
    }
    global _EXEC_NAMESPACE
    _EXEC_NAMESPACE = namespace
    try:
        from PIL import Image
        namespace["Image"] = Image
    except ImportError:
        pass
    return namespace


def _reset_round(todo_items: list[dict[str, Any]]) -> None:
    global _BASH_CALL_COUNT, _TOOL_CALL_COUNT
    _TODO_ITEMS[:] = [dict(item) for item in todo_items]
    _BASH_CALL_COUNT = 0
    _TOOL_CALL_COUNT = 0


def _task_factory(loop, coroutine, context=None):
    task = asyncio.Task(coroutine, loop=loop, context=context)
    _EXECUTION_TASKS.add(task)
    return task


async def _eval_block(compiled, namespace):
    value = eval(compiled, namespace)
    if inspect.isawaitable(value):
        await value


async def _drain_code_tasks() -> bool:
    """清理本回复遗留任务，清理过程中新建的任务也不能跨回复存活。"""
    current = asyncio.current_task()
    deadline = asyncio.get_running_loop().time() + 0.5
    while True:
        # 直接 asyncio.Task(...) 不经过 factory，也必须在当前 loop 中排空。
        persistent = {_SDK_KEEPALIVE} if _SDK_KEEPALIVE is not None else set()
        tasks = (set(_EXECUTION_TASKS) | asyncio.all_tasks()) - {current} - persistent
        for task in tasks:
            if task.done():
                if not task.cancelled():
                    task.exception()
                _EXECUTION_TASKS.discard(task)
        pending = {task for task in tasks if not task.done()}
        if not pending:
            return True
        for task in pending:
            task.cancel()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.wait(pending, timeout=remaining)


def _execute(source: str | list[str], namespace: dict[str, Any], *, loop=None) -> dict[str, Any]:
    started = time.monotonic()
    exit_code = 0
    ok = True
    error = ""
    error_stream = sys.stderr
    _REQUEST_CONTEXT.output_id = 0
    _REQUEST_CONTEXT.block_index = None
    _REQUEST_CONTEXT.stop_reason = None
    total_blocks = len(source) if isinstance(source, list) else int(isinstance(source, str))
    multi_block = total_blocks > 1
    started_blocks = completed_blocks = 0
    stopped_block = active_block = None
    stop_reason = None
    phase = "validation_error"
    owned_loop = loop is None
    loop = loop or asyncio.new_event_loop()
    loop.set_task_factory(_task_factory)
    permit = {"active": True, "request_id": getattr(_REQUEST_CONTEXT, "request_id", None)}
    token = _ACTIVE_REQUEST.set(permit)
    turn_started = False
    fatal = False
    try:
        if isinstance(source, str):
            sources = [source]
        elif isinstance(source, list):
            if not source:
                raise ValueError("code 列表不能为空")
            sources = list(source)
            for index, block in enumerate(sources, 1):
                if not isinstance(block, str):
                    stopped_block = index
                    raise TypeError(f"code 第 {index} 块必须是 str")
        else:
            raise TypeError("code 必须是 str 或非空 list[str]")
        phase = "compile_error"
        compiled_blocks = []
        for index, block in enumerate(sources, 1):
            stopped_block = index
            filename = f"{SOURCE_PREFIX}{uuid.uuid4().hex}>"
            flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT if _MAI_SESSION is not None else 0
            compiled_blocks.append(compile(block, filename, "exec", flags=flags))
        if _MAI_SESSION is not None:
            _MAI_SESSION.begin_turn()
            turn_started = True
        stopped_block = None
        phase = "runtime_error"
        for index, (block, compiled) in enumerate(zip(sources, compiled_blocks), 1):
            _REQUEST_CONTEXT.block_index = active_block = index
            started_blocks = index
            if multi_block:
                _emit({"type": "code_block_start", "block_index": index,
                       "filename": compiled.co_filename, "total_blocks": total_blocks})
            try:
                _SOURCE_CACHE.remember(compiled.co_filename, block)
            except Exception:
                # 来源索引失败不影响已成功编译的代码。
                pass
            if _MAI_SESSION is not None:
                loop.run_until_complete(_eval_block(compiled, namespace))
            else:
                exec(compiled, namespace)
            if _REQUEST_CONTEXT.stop_reason is not None:
                # 即使用户代码捕获了控制异常，也不能开始后续块。
                raise _ControlExit(0)
            completed_blocks += 1
            if multi_block:
                _emit({"type": "code_block_end", "block_index": index, "status": "completed"})
            active_block = None
    except _ControlExit:
        stop_reason = _REQUEST_CONTEXT.stop_reason or "control_exit"
        stopped_block = active_block
    except BaseException as exc:
        ok = False
        error = type(exc).__name__
        stop_reason = phase
        stopped_block = active_block or stopped_block
        exit_code = int(exc.code) if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
        error_stream.write(_format_exception(exc))
    finally:
        permit["active"] = False
        try:
            if turn_started:
                _MAI_SESSION.end_turn()
            if not loop.run_until_complete(_drain_code_tasks()):
                raise RuntimeError("后台任务未在截止时间内结束，必须重置 worker")
        except BaseException as exc:
            fatal, ok, exit_code = True, False, 1
            error, stop_reason = type(exc).__name__, "session_fault"
            error_stream.write(_format_exception(exc))
        finally:
            _ACTIVE_REQUEST.reset(token)
            _EXECUTION_TASKS.difference_update(task for task in tuple(_EXECUTION_TASKS) if task.done())
            if owned_loop:
                loop.close()
    if multi_block and active_block is not None and stop_reason is not None:
        _emit({"type": "code_block_end", "block_index": active_block,
               "status": "aborted" if ok else "failed", "stop_reason": stop_reason,
               "error": error})
    _REQUEST_CONTEXT.block_index = None
    return {
        "type": "end", "ok": ok, "error": error, "exit_code": exit_code, "fatal": fatal,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "total_blocks": total_blocks, "started_blocks": started_blocks,
        "completed_blocks": completed_blocks, "stopped_block": stopped_block,
        "stop_reason": stop_reason,
    }


def _worker(control_fd: int) -> int:
    global _CONTROL_SOCKET, _CONFIG
    _CONTROL_SOCKET = socket.socket(fileno=control_fd)
    _CONTROL_SOCKET.set_inheritable(False)
    namespace: dict[str, Any] | None = None
    loop = asyncio.new_event_loop()
    try:
        return _worker_requests(loop, namespace)
    finally:
        loop.run_until_complete(_close_sdk_session(close_runtime=True))
        loop.close()


def _drive_worker_loop_once(loop: asyncio.AbstractEventLoop) -> None:
    """Run one non-blocking event-loop iteration without creating a tracked Task."""
    loop.call_soon(loop.stop)
    loop.run_forever()


def _worker_readline(loop: asyncio.AbstractEventLoop, buffer: bytearray) -> bytes | None:
    """Read one control frame while continuing to drive persistent SDK tasks.

    The worker previously blocked forever in ``makefile().readline()`` between code
    cells. That also stopped the asyncio loop, so the 90s CuaDriver keepalive task
    never ran while the model was thinking. A timed recv lets due timers execute.
    """
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            raw = bytes(buffer[:newline + 1])
            del buffer[:newline + 1]
            return raw
        if len(buffer) > MAX_FRAME_BYTES:
            return bytes(buffer)
        try:
            chunk = _CONTROL_SOCKET.recv(min(65536, MAX_FRAME_BYTES + 1 - len(buffer)))
        except socket.timeout:
            _drive_worker_loop_once(loop)
            continue
        if not chunk:
            return None
        buffer.extend(chunk)


def _worker_requests(loop, namespace) -> int:
    global _CONFIG, _SDK_REVIVE_PENDING
    buffer = bytearray()
    _CONTROL_SOCKET.settimeout(1.0)
    with _CONTROL_SOCKET:
        while True:
            raw = _worker_readline(loop, buffer)
            if not raw:
                return 0
            if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                return 2
            request = json.loads(raw)
            request_id = request["request_id"]
            _REQUEST_CONTEXT.request_id = request_id
            _REQUEST_CONTEXT.generation = request["config"].get("rpc_generation", 0)
            fatal = False
            if namespace is None:
                _CONFIG = request["config"]
            with _RequestOutput(request_id):
                try:
                    _reset_round(request.get("todo_items") or [])
                    if namespace is None:
                        namespace = _bootstrap(loop)
                    # 每轮更新账本内容，始终使用同一个 todo 对象。
                    namespace["todo"] = todo
                    if request.get("reset_task"):
                        session, device = loop.run_until_complete(_reset_sdk_session())
                        namespace.update(mu=session, device=device, ui=device.ui)
                    if request.get("reset_output_context") and _MAI_SESSION is not None:
                        _MAI_SESSION.reset_output_context()
                    if _SDK_REVIVE_PENDING:
                        _SDK_REVIVE_PENDING = False
                        session, device = loop.run_until_complete(_reset_sdk_session())
                        namespace.update(mu=session, device=device, ui=device.ui)
                        _emit({
                            "type": "display", "kind": "text",
                            "text": "SDK endpoint 曾在上一轮被隔离（DEVICE_OFFLINE）；宿主已重建会话。"
                                    "此前所有 window/ui 句柄已失效：重新 list/bind/observe 后再操作，"
                                    "不要复用旧句柄或盲目重放上一轮动作。",
                            "value_type": "session_revive", "sdk_output": True,
                        })
                except BaseException as exc:
                    sys.stderr.write(_format_exception(exc))
                    end = {"type": "end", "ok": False, "error": type(exc).__name__, "exit_code": 1}
                    fatal = True
                else:
                    end = _execute(request["code"], namespace, loop=loop)
                    fatal = end.pop("fatal", False)
            _send_frame({"kind": "done", "request_id": request_id, "event": end, "fatal": fatal})
            del _REQUEST_CONTEXT.request_id
            del _REQUEST_CONTEXT.generation
            if fatal:
                return 1


def main(argv: list[str]) -> int:
    global _CONFIG
    if len(argv) == 3 and argv[1] == "--worker":
        return _worker(int(argv[2]))
    if len(argv) != 3:
        print("用法：code_runtime.py CODE_FILE CONFIG_FILE 或 --worker CONTROL_FD", file=sys.stderr)
        return 2
    code_path, config_path = Path(argv[1]), Path(argv[2])
    _CONFIG = json.loads(config_path.read_text(encoding="utf-8"))
    source = code_path.read_text(encoding="utf-8")
    _reset_round(_CONFIG.get("todo_items") or [])
    sys.stdout = _LimitedStream(_REAL_STDOUT, int(_CONFIG.get("max_stdout_bytes", MAX_STDOUT_BYTES)))
    sys.stderr = _LimitedStream(_REAL_STDERR, int(_CONFIG.get("max_stderr_bytes", MAX_STDERR_BYTES)))
    loop = asyncio.new_event_loop()
    try:
        namespace = _bootstrap(loop)
        end = _execute(source, namespace, loop=loop)
    finally:
        loop.run_until_complete(_close_sdk_session(close_runtime=True))
        loop.close()
    _emit(end)
    return end["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
