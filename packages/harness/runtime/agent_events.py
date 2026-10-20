"""Semantic terminal events and the plain compatibility frontend."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import json
import sys
import time
from types import MappingProxyType
from typing import Any, Literal, Protocol, TextIO

from console_protocol import decode_console_message


HUB_EVENT_PREFIX = "__MAI_HUB_EVENT_V1__ "
HUB_CODE_EVENTS = frozenset({"code_start", "code_delta", "code_end", "code_reset"})


def model_status_event(event: Mapping[str, Any], step: int) -> dict[str, Any] | None:
    """Small, content-free lifecycle frame shared by both Demo Hub agents."""
    kind = event.get("type")
    retrying = bool(event.get("retrying"))
    if kind == "request_start":
        stage, text = "waiting", "正在等待 API 响应"
    elif kind == "response_started":
        stage, text = "generating", "API 已开始返回，正在生成"
    elif kind == "response_complete":
        stage, text = "validating", "已收到完整响应，正在解析"
    elif kind == "response_validated":
        stage, text = "ready", "模型输出已通过校验"
    elif kind == "response_rejected":
        stage, text = ("retrying" if retrying else "error"), "模型输出解析失败"
    elif kind == "request_aborted":
        reason = event.get("reason")
        if reason == "cancelled":
            stage, text = "cancelled", "API 请求已取消"
        else:
            stage = "retrying" if retrying else "error"
            text = {"timeout": "API 请求超时", "empty_model_content": "API 返回内容为空"}.get(reason, "API 请求失败")
    else:
        return None
    if stage in {"retrying", "error"}:
        text += "，将自动重试" if retrying else "，本次未执行，请查看错误或重新发送"
    if event.get("purpose") == "compaction":
        text = "上下文压缩：" + text
        if stage == "validating":
            stage, text = "ready", "上下文摘要已返回"
    return {"type": "model_status", "step": step, "text": text, "stage": stage,
            "at_ms": int(time.time() * 1000), "attempt": event.get("attempt"),
            "max_attempts": event.get("max_attempts"), "timeout_seconds": event.get("timeout_seconds"),
            "attempt_id": event.get("attempt_id"), "retrying": retrying}


def decode_hub_event(line: str) -> dict[str, Any] | None:
    """Decode the opt-in, one-line transport used by Demo Hub's stdout reader."""
    if not line.startswith(HUB_EVENT_PREFIX):
        return None
    try:
        event = json.loads(line[len(HUB_EVENT_PREFIX):])
    except json.JSONDecodeError as exc:
        raise ValueError("invalid hub event JSON") from exc
    if not isinstance(event, dict) or event.get("type") not in HUB_CODE_EVENTS | {"model_status"}:
        raise ValueError("invalid hub event type")
    if type(event.get("step")) is not int or event["step"] < 0:
        raise ValueError("invalid hub event step")
    if not isinstance(event.get("text"), str):
        raise ValueError("invalid hub event text")
    index = event.get("block_index")
    if index is not None and (type(index) is not int or index < 1):
        raise ValueError("invalid hub event block index")
    return event


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One immutable, presentation-neutral update from the agent loop."""

    kind: str
    text: str = ""
    step: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class AgentPrompt:
    """A request for terminal input."""

    kind: Literal["task", "question", "retry", "approval"]
    text: str = ""
    empty_exits: bool = False


@dataclass(frozen=True, slots=True)
class TerminalCommand:
    """A host-side command, never a model-facing user message."""

    kind: Literal["resume", "compaction"]
    target: str | None = None


ConsentChoice = Literal["allow", "reject", "cancel"]


@dataclass(frozen=True, slots=True)
class ForegroundApprovalPrompt:
    request_id: str
    text: str
    task_index: int
    app_id: str
    window_id: str


class AgentFrontend(Protocol):
    """The I/O boundary consumed by ``mac_agent.run_agent``."""

    interactive: bool

    def emit(self, event: AgentEvent) -> None: ...

    async def read(self, prompt: AgentPrompt) -> str | None: ...

    async def choose_approval(self, prompt: ForegroundApprovalPrompt) -> ConsentChoice: ...

    def close(self) -> None: ...


def emit_safely(
    frontend: AgentFrontend,
    event: AgentEvent,
    *,
    on_error: Callable[[Exception], None] | None = None,
) -> bool:
    """Deliver a UI event without letting a renderer retry agent work."""

    try:
        frontend.emit(event)
    except Exception as exc:  # noqa: BLE001 - UI failures cannot own agent lifecycle
        if on_error is not None:
            on_error(exc)
        return False
    return True


class PlainFrontend:
    """Current line-oriented terminal and pipe behavior."""

    def __init__(
        self,
        input_reader: Callable[[str], Awaitable[str]],
        *,
        interactive: bool,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        hub_events: bool = False,
    ) -> None:
        self.input_reader = input_reader
        self.interactive = interactive
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.hub_events = hub_events
        self._abort_callback: Callable[[bool], None] | None = None
        self._turn_active = False
        self._abort_requested = False
        self._input_interrupt: asyncio.Event | None = None
        self._input_kind: str | None = None

    def bind_abort(self, callback: Callable[[bool], None]) -> None:
        self._abort_callback = callback

    def handle_sigint(self) -> None:
        if self._input_interrupt is not None:
            if self._input_kind == "approval" and self._abort_callback is not None:
                self._abort_callback(False)
            self._input_interrupt.set()
            return
        if self._turn_active and self._abort_callback is not None:
            self._abort_callback(self._abort_requested)
            self._abort_requested = True

    def emit(self, event: AgentEvent) -> None:
        if event.kind == "phase":
            phase = str(event.payload.get("phase") or event.text)
            self._turn_active = phase in {"planning", "compaction", "execution", "postprocess", "recovery"}
            if phase == "planning":
                self._abort_requested = False
        elif event.kind in {"aborted", "answer", "finished"}:
            self._turn_active = False
        target = self.stderr if event.kind == "error" else self.stdout
        if event.kind == "model_status":
            if self.hub_events and event.step is not None:
                frame = {**event.payload, "type": event.kind, "step": event.step, "text": event.text}
                # Reasoning may still occupy an unterminated stdout line.
                print("\n" + HUB_EVENT_PREFIX + json.dumps(frame, ensure_ascii=False), flush=True, file=self.stdout)
            return
        if event.kind == "foreground_consent":
            return
        if event.kind in HUB_CODE_EVENTS:
            if self.hub_events and event.step is not None:
                frame = {"type": event.kind, "step": event.step,
                         "block_index": event.payload.get("block_index"), "text": event.text}
                print(HUB_EVENT_PREFIX + json.dumps(frame, ensure_ascii=False),
                      flush=True, file=self.stdout)
            return
        if event.kind == "reasoning_delta":
            if event.payload.get("start"):
                print(
                    f"[Step {event.step or 0:03d}] reasoning: ",
                    end="",
                    flush=True,
                    file=target,
                )
            print(event.text, end="", flush=True, file=target)
            return
        if event.kind == "intent_delta":
            if event.payload.get("start"):
                print(
                    f"[Step {event.step or 0:03d}] intent "
                    f"{event.payload.get('block_index', 1)}（预览）: ",
                    end="",
                    flush=True,
                    file=target,
                )
            print(event.text, end="", flush=True, file=target)
            return
        if event.kind in {"reasoning_end", "intent_end"}:
            print(flush=True, file=target)
            return
        if event.kind in {"execution", "question"}:
            return
        if event.kind == "phase":
            if event.payload.get("plain_text"):
                print(event.payload["plain_text"], flush=True, file=target)
            return
        if event.kind == "answer":
            print(f"\nAgent> {event.text}", file=target)
            return
        if event.kind == "finished":
            print(f"\nFinished: {event.text}", file=target)
            answer = str(event.payload.get("answer") or "")
            if answer and not event.payload.get("answer_displayed"):
                print(answer, file=target)
            path = str(event.payload.get("trajectory") or "")
            if path:
                print(f"Trajectory saved to: {path}", file=target)
            return
        if event.kind == "resume_hint":
            print(f"会话可恢复：{event.text}", file=target)
            return
        text = event.text
        if not text:
            text = event.kind.replace("_", " ")
        print(text, flush=bool(event.payload.get("flush", False)), file=target)

    async def read(self, prompt: AgentPrompt) -> str | None:
        self._turn_active = False
        self._abort_requested = False
        if prompt.kind in {"question", "approval"}:
            label = f"Agent> {prompt.text}\n你> " if self.interactive else "你> "
        elif prompt.kind == "retry":
            label = prompt.text
        else:
            label = prompt.text or "\n你> "
        while True:
            reader = asyncio.create_task(self.input_reader(label))
            interrupted = asyncio.Event()
            waiter = asyncio.create_task(interrupted.wait())
            self._input_interrupt = interrupted
            self._input_kind = prompt.kind
            try:
                done, _ = await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if waiter in done or interrupted.is_set():
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                    if prompt.kind == "approval":
                        return None
                    continue
                message = decode_console_message(await reader)
            except (EOFError, KeyboardInterrupt):
                return None
            finally:
                waiter.cancel()
                if not reader.done():
                    reader.cancel()
                self._input_interrupt = None
                self._input_kind = None
            command = message.strip().lower()
            if command in {"/exit", "/quit", "exit", "quit", "q"}:
                return None
            if command:
                return message
            if prompt.kind == "retry":
                return ""
            if prompt.empty_exits:
                return None

    async def choose_approval(self, prompt: ForegroundApprovalPrompt) -> ConsentChoice:
        if not self.interactive:
            return "cancel"
        answer = await self.read(AgentPrompt(kind="approval", text=prompt.text, empty_exits=True))
        if not isinstance(answer, str):
            return "cancel"
        value = answer.strip().casefold()
        if value in {"yes", "y", "同意", "允许"}:
            return "allow"
        if value in {"no", "n", "拒绝"}:
            return "reject"
        return "cancel"

    def close(self) -> None:
        return None


__all__ = [
    "AgentEvent",
    "AgentFrontend",
    "AgentPrompt",
    "ConsentChoice",
    "ForegroundApprovalPrompt",
    "PlainFrontend",
    "emit_safely",
    "decode_hub_event",
]
