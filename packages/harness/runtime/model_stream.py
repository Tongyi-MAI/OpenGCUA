from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable

from model_metrics import TokenEstimator, normalize_usage
from rollout_data import DEFAULT_MAX_BYTES, RolloutRecorder, recording_call, warn_recording


__all__ = [
    "AgentError", "ContextBudgetError", "ContextWindowError", "CodeParseError", "extract_reasoning", "CodeBlock",
    "CodeAction", "parse_code_action", "ModelClient",
]

_LOG = logging.getLogger(__name__)
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.I | re.S)
_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.I | re.S)
_INNER_FENCE_RE = re.compile(
    r"\s*```[ \t]*(?:python3?|py|code)?[ \t]*\r?\n?(.*?)\r?\n?```\s*", re.I | re.S
)
_ACTION_TAGS = ("<intent>", "</intent>", "<code>", "</code>")


class AgentError(RuntimeError):
    pass


class EmptyModelContent(ValueError):
    """A completed model response had no final/action text in ``content``.

    Reasoning-only output must never be interpreted as an executable action.
    Keep only its length for diagnostics; reasoning text is not safe telemetry.
    """

    def __init__(self, reasoning_chars: int = 0) -> None:
        self.reasoning_chars = reasoning_chars
        super().__init__("model returned empty content")


_TERMINAL_REASONING_CODE_RE = re.compile(
    r"(<intent>.*?</intent>\s*<code>.*?</code>)\s*\Z", re.S
)


def terminal_reasoning_code(reasoning: str) -> str | None:
    """Extract one complete final code action, never a speculative fragment.

    This narrow DeepSeek fallback is used only when the formal content channel
    is empty.  A reasoning trace with multiple candidates, DSML trailers, or
    malformed Python is not executable.
    """
    if any(reasoning.count(tag) != 1 for tag in (
        "<intent>", "</intent>", "<code>", "</code>",
    )) or "<｜｜DSML｜｜" in reasoning or "<tool_call" in reasoning:
        return None
    match = _TERMINAL_REASONING_CODE_RE.search(reasoning)
    if match is None:
        return None
    candidate = match.group(1)
    try:
        parse_code_action(candidate, require_intent=True)
    except CodeParseError:
        return None
    return candidate


class ModelOutputTruncated(AgentError):
    """finish_reason=length：输出超过 max_tokens 被截断。确定性失败，重试同参数必再截断，
    因此按 AgentError 快速失败（不计入传输重试），由上层加大 max_tokens 或提示分段。"""


class ContextBudgetError(AgentError):
    def __init__(self, budget: dict[str, Any]) -> None:
        self.budget = copy.deepcopy(budget)
        super().__init__(
            f"model context budget exceeded: {budget.get('total_tokens')} "
            f"> {budget.get('context_limit')}"
        )


class ContextWindowError(AgentError):
    def __init__(self, body: str, http_status: int = 400) -> None:
        self.body = body
        self.http_status = http_status
        super().__init__(f"model context window exceeded (not retrying): HTTP {http_status}: {body[:2000]}")


class CodeParseError(AgentError):
    pass


def extract_reasoning(content: str, reasoning_field: str) -> tuple[str, str]:
    """保留原优先级及缺失 think 开标签修复；所有完整内联推理均剥离。"""
    reasoning = (reasoning_field or "").strip()
    if "</think>" in content.lower() and "<think>" not in content.lower():
        content = "<think>" + content
    think_blocks = [block.strip() for block in _THINK_RE.findall(content) if block.strip()]
    content = _THINK_RE.sub("", content).strip()
    thinking_blocks = [block.strip() for block in _THINKING_RE.findall(content) if block.strip()]
    content = _THINKING_RE.sub("", content).strip()
    if not reasoning:
        reasoning = "\n".join(think_blocks or thinking_blocks)
    return content, reasoning


@dataclass(frozen=True)
class CodeBlock:
    intent: str
    code: str


@dataclass(frozen=True)
class CodeAction:
    intent: str
    code: str
    blocks: tuple[CodeBlock, ...] = ()

    @property
    def ordered_blocks(self) -> tuple[CodeBlock, ...]:
        return self.blocks or (CodeBlock(self.intent, self.code),)

    @property
    def execution_source(self) -> str | list[str]:
        blocks = self.ordered_blocks
        return blocks[0].code if len(blocks) == 1 else [block.code for block in blocks]

    def as_response(self) -> str:
        return "\n".join(
            (f"<intent>{block.intent}</intent>\n" if block.intent else "")
            + f"<code>\n{block.code}\n</code>"
            for block in self.ordered_blocks
        )


def _clean_code(code: str) -> str:
    code = code.replace("\r\n", "\n").strip()
    fence = _INNER_FENCE_RE.fullmatch(code)
    return fence.group(1).strip() if fence else code


class _TagScanner:
    """只保留可能的标签前缀；每个字符仅扫描一次，标签集合由状态控制。"""

    def __init__(self, tags: tuple[str, ...], on_text: Callable, on_tag: Callable):
        self.tags = tags
        self.on_text = on_text
        self.on_tag = on_tag
        self.pending = ""

    def feed(self, text: str) -> None:
        plain: list[str] = []
        for char in text:
            if not self.pending and char != "<":
                plain.append(char)
                continue
            self.pending += char
            while self.pending:
                lower = self.pending.lower()
                if lower in self.tags:
                    if plain:
                        self.on_text("".join(plain))
                        plain.clear()
                    token, self.pending = self.pending, ""
                    self.on_tag(token)
                    break
                if any(tag.startswith(lower) for tag in self.tags):
                    break
                plain.append(self.pending[0])
                self.pending = self.pending[1:]
        if plain:
            self.on_text("".join(plain))

    def finish(self) -> None:
        if self.pending:
            pending, self.pending = self.pending, ""
            self.on_text(pending)


class _ProtocolParser:
    def __init__(self, require_intent: bool, emit: Callable | None = None):
        self.require_intent = require_intent
        self.emit = emit
        self.state = "ready"
        self.blocks: list[CodeBlock] = []
        self.intent: list[str] = []
        self.code: list[str] = []
        self.scanner = _TagScanner(_ACTION_TAGS, self._text, self._tag)

    def _event(self, kind: str, **fields: Any) -> None:
        if self.emit is not None:
            self.emit(kind, block_index=len(self.blocks) + 1, preview=True, **fields)

    def _text(self, text: str) -> None:
        if self.state == "intent":
            self.intent.append(text)
            self._event("intent_delta", delta=text)
        elif self.state == "code":
            self.code.append(text)
            self._event("code_delta", delta=text)
        elif text.strip():
            # salvage：块外多余文字（含 <intent> 前的散文、intent 与 code 间的插话）直接忽略，
            # 不整段丢弃（否则几分钟的生成白费）。
            self._event("output_salvaged", reason="stray_text")

    def _tag(self, tag: str) -> None:
        tag = tag.lower()
        if self.state == "ready" and tag == "<intent>":
            self.intent, self.code = [], []
            self.state = "intent"
            self._event("intent_start")
        elif self.state == "intent" and tag == "</intent>":
            intent = "".join(self.intent).strip()
            if self.require_intent and not intent:
                # salvage：intent 为空也继续（用空 intent），不整段丢弃。
                self._event("output_salvaged", reason="empty_intent")
            self.state = "need_code"
            self._event("intent_end", intent=intent)
        elif tag == "<code>" and self.state in ("need_code", "ready"):
            if self.state == "ready":
                # salvage：模型漏写 <intent> 直接给代码时，用空 intent 收下代码块，
                # 而不是把整块当 stray 丢弃（那会退化成"看不到代码"的假象）。
                self.intent = []
                if self.require_intent:
                    self._event("output_salvaged", reason="code_without_intent")
            self.code = []
            self.state = "code"
            # Python 内的开标签、普通比较符均为正文；结束标签始终是协议边界。
            self.scanner.tags = ("</code>",)
            self._event("code_start")
        elif self.state == "code" and tag == "</code>":
            code = _clean_code("".join(self.code))
            if not code:
                # salvage：空代码块跳过，不整段丢弃。
                self.state = "ready"
                self.scanner.tags = _ACTION_TAGS
                self._event("output_salvaged", reason="empty_code")
                return
            block = CodeBlock("".join(self.intent).strip(), code)
            self._event("code_end", code=code)
            self.blocks.append(block)
            self.state = "ready"
            self.scanner.tags = _ACTION_TAGS
        else:
            # salvage：多余/错位的协议标签（如多吐的 </code>）忽略，不整段丢弃。
            self._event("output_salvaged", reason=f"stray_tag:{tag}")

    def feed(self, text: str) -> None:
        self.scanner.feed(text)

    def finish(self) -> tuple[CodeBlock, ...]:
        self.scanner.finish()
        if self.state == "code":
            # salvage：未闭合的 code 块按已有内容收尾，不整段丢弃。
            code = _clean_code("".join(self.code))
            if code:
                self.blocks.append(CodeBlock("".join(self.intent).strip(), code))
                self._event("output_salvaged", reason="unterminated_code")
            self.state = "ready"
        if self.state != "ready":
            raise CodeParseError(f"incomplete intent/code pair ({self.state})")
        if not self.blocks:
            raise CodeParseError("no <code>...</code> block found")
        return tuple(self.blocks)


_INTENT_TAG_RE = re.compile(r"</?intent>", re.I)
# 模型漂移成 markdown 围栏时的 salvage：优先闭合块，其次未闭合（截断）块。
_INTENT_BODY_RE = re.compile(r"<intent>(.*?)</intent>", re.I | re.S)
# 模型常用错配标签写意图（<think>意图</intent> 或 <think>意图 直接接 <code>）；
# 解析时回收为首行意图，避免 step 卡片显示空 intent。
_INTENT_MISMATCH_RE = re.compile(r"<think>(.*?)</intent>", re.I | re.S)
_INTENT_THINK_HEAD_RE = re.compile(r"<think>(.*?)<code>", re.I | re.S)
_FENCE_CLOSED_RE = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n(.*?)```", re.S)
_FENCE_OPEN_RE = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n(.+)\Z", re.S)
_FOREIGN_TRAILER_LINE_RE = re.compile(
    r"\s*</(?:parameter|function|tool_call|antml-code)>\s*", re.I
)


def _first_line(text: str, limit: int = 100) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _recover_intent(block: CodeBlock, raw: str) -> CodeBlock:
    """intent 缺失时从错配标签/think 首行/代码首行回收，保证展示与轨迹有意图文本。"""
    if block.intent.strip():
        return block
    text = ""
    for pattern in (_INTENT_MISMATCH_RE, _INTENT_THINK_HEAD_RE):
        m = pattern.search(raw)
        if m:
            text = _first_line(m.group(1))
            break
    if not text:
        text = _first_line(block.code, 80)
    if not text:
        return block
    return CodeBlock(text, block.code)


def _repair_foreign_code_trailer(code: str) -> str:
    """Drop a foreign tool-protocol trailer after otherwise valid Python.

    Some OpenAI-compatible endpoints occasionally append Anthropic-style closing
    tags after an unterminated ``<code>`` block.  The protocol parser correctly
    salvages the block to EOF, but those standalone XML lines make valid Python
    fail compilation.  Only accept the repair when the complete prefix compiles;
    never delete an arbitrary invalid suffix or reinterpret tool-call markup.
    """
    lines = code.splitlines()
    for index, line in enumerate(lines):
        if not _FOREIGN_TRAILER_LINE_RE.fullmatch(line):
            continue
        candidate = "\n".join(lines[:index]).strip()
        if not candidate:
            return code
        try:
            compile(candidate, "<code>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        except (SyntaxError, ValueError):
            return code
        return candidate
    return code


def parse_code_action(content: str, require_intent: bool = True) -> CodeAction:
    raw_in = str(content or "")
    if raw_in.count("<think>") > raw_in.count("</think>"):
        # 内联 <think> 未闭合（常见于生成被截断）：协议标签视为 think 的隐式结束，
        # 否则推理剥离器会把 <code> 块一起吞掉，导致"看不到代码"的假象。
        cut = -1
        for tag in ("<code>", "<intent>"):
            idx = raw_in.rfind(tag)
            if idx > cut:
                cut = idx
        if cut > 0:
            raw_in = raw_in[:cut] + "</think>" + raw_in[cut:]
    parser = _ProtocolParser(require_intent)
    parser.feed(raw_in)
    try:
        blocks = parser.finish()
    except CodeParseError as exc:
        # 兜底一：模型把代码写成 markdown 围栏（```python ... ```）而非 <code> 标签；
        # 兜底二：模型只回散文（想提问/想直接答）→ salvage 成 ask_user。
        # 两者都避免三轮重试后整轮硬失败（界面上只剩一个 attention 死局）。
        msg = str(exc)
        if "no <code>" not in msg and "need_code" not in msg:
            raise
        raw = str(content or "")
        bodies = [m.group(1) for m in _FENCE_CLOSED_RE.finditer(raw)]
        reason = "fenced_code_as_code"
        if not any(b.strip() for b in bodies):
            open_m = _FENCE_OPEN_RE.search(raw)
            if open_m and open_m.group(1).strip():
                bodies, reason = [open_m.group(1)], "fenced_code_unclosed_as_code"
        if any(b.strip() for b in bodies):
            intent_m = _INTENT_BODY_RE.search(raw)
            intent_text = intent_m.group(1).strip() if intent_m else ""
            if parser.emit is not None:
                parser.emit("output_salvaged", reason=reason, preview=True)
            blocks = tuple(
                CodeBlock(intent_text[:120], body.strip())
                for body in bodies if body.strip()
            )
        else:
            # 严格协议：<code> 必须存在（标签优先、围栏兜底）。纯散文或半截协议
            # 一律视为 parse 失败交给重试循环，不再 salvage 成 ask_user。
            raise
    repaired_blocks: list[CodeBlock] = []
    for index, block in enumerate(blocks, 1):
        code = block.code
        try:
            compile(code, "<code>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        except (SyntaxError, ValueError) as exc:
            code = _repair_foreign_code_trailer(code)
            if code == block.code:
                detail = f"line {exc.lineno}: {exc.msg}" if isinstance(exc, SyntaxError) else str(exc)
                raise CodeParseError(f"invalid Python in block {index}: {detail}") from exc
            if parser.emit is not None:
                parser.emit("output_salvaged", reason="foreign_protocol_trailer", preview=True)
        repaired_blocks.append(CodeBlock(block.intent, code))
    blocks = tuple(_recover_intent(b, raw_in) for b in repaired_blocks)
    if len(blocks) == 1:
        return CodeAction(blocks[0].intent, blocks[0].code)
    return CodeAction(
        "\n".join(block.intent for block in blocks),
        "\n".join(block.code for block in blocks),
        blocks,
    )


class _ReasoningStripper:
    """增量剥离一种推理标签；未闭合块不向预览输出。"""

    def __init__(self, name: str, on_text: Callable, on_open: Callable | None = None):
        self.open_tag, self.close_tag = f"<{name}>", f"</{name}>"
        self.on_text, self.on_open = on_text, on_open
        self.hidden = False
        self.scanner = _TagScanner((self.open_tag, self.close_tag), self._text, self._tag)

    def _text(self, text: str) -> None:
        if not self.hidden:
            self.on_text(text)

    def _tag(self, tag: str) -> None:
        if self.hidden:
            self.hidden = False
            self.scanner.tags = (self.open_tag, self.close_tag)
        elif tag.lower() == self.open_tag:
            self.hidden = True
            self.scanner.tags = (self.close_tag,)
            if self.on_open is not None:
                self.on_open()
        else:
            self.on_text(tag)

    def feed(self, text: str) -> None:
        self.scanner.feed(text)

    def finish(self) -> None:
        self.scanner.finish()


class _ContentPreview:
    """标准首标签即时预览；自由文本前缀延后，不编译或执行。

    预览是暂态：缺失 think 开标签的兼容修复可能撤销已展示的正文。
    此时 preview_reset 清空同 attempt 的所有 block 预览，再重放最终正文；
    正式解析与执行授权仍由调用方在完整响应校验后处理。
    """

    def __init__(self, emit: Callable):
        self.emit = emit
        self.response_started = False
        self.parser = _ProtocolParser(False, emit)
        self.confirmed = False
        self.disabled = False
        self.deferred = False
        self.prefix = ""
        self.think_opened = False
        self.orphan_think_close = False

        # 与最终抽取保持相同的两级顺序，交叉或嵌套标签也不会泄漏示例。
        self.thinking = _ReasoningStripper("thinking", self._forward)
        self.think = _ReasoningStripper("think", self._text, self._open_think)

    def feed_reasoning(self, text: str) -> None:
        """reasoning 增量只作展示事件转发，不进预览状态机、不影响解析与执行授权。"""
        if text:
            self._mark_response_started()
            self.emit("reasoning_delta", delta=text, preview=True)

    def _mark_response_started(self) -> None:
        if not self.response_started:
            self.response_started = True
            self.emit("response_started")

    def _forward(self, text: str) -> None:
        if self.disabled or self.deferred:
            return
        if not self.confirmed:
            # 仅缓存首标签的候选前缀，支持标签跨分片及任意前导空白。
            self.prefix = (self.prefix + text).lstrip()
            lower = self.prefix.lower()
            opening_tags = ("<intent>", "<code>")
            if any(lower.startswith(tag) for tag in opening_tags):
                self.confirmed = True
                text, self.prefix = self.prefix, ""
            else:
                if not any(tag.startswith(lower) for tag in opening_tags):
                    self.deferred = True
                    self.prefix = ""
                return
        try:
            self.parser.feed(text)
        except CodeParseError:
            # 协议错误交由最终解析处理，不转换为 HTTP 重试。
            self.disabled = True

    def _text(self, text: str) -> None:
        # 剥离器将游离闭标签作为独立文本回调，保留大小写及正文语义。
        if text.lower() == "</think>":
            self.orphan_think_close = True
        self.thinking.feed(text)

    def _open_think(self) -> None:
        self.think_opened = True

    def feed(self, text: str) -> None:
        if text:
            self._mark_response_started()
        self.think.feed(text)

    def finish(self, content: str) -> None:
        # 后续显式开标签会排除缺开标签修复，必须按整轮结果决定是否重放。
        repaired = self.orphan_think_close and not self.think_opened
        if repaired or not self.confirmed:
            if repaired and self.confirmed:
                self.emit("preview_reset", preview=True, reason="reasoning_repair")
            self.parser = _ProtocolParser(False, self.emit)
            self.confirmed = self.disabled = self.deferred = False
            self.prefix = ""
            for offset in range(0, len(content), 128):
                self._forward(content[offset:offset + 128])
        else:
            self.think.finish()
            self.thinking.finish()
        if not self.disabled:
            try:
                self.parser.finish()
            except CodeParseError:
                self.disabled = True


class _TextPreview:
    """纯文本请求不解析动作协议，也不发布任何预览；语义校验由调用方负责。"""

    def feed(self, text: str) -> None:
        pass

    def finish(self, content: str) -> None:
        pass


def _message_text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if content is None:
        return ""
    if not isinstance(content, str):
        raise ValueError("model content must be text")
    return content


def _usage(data: dict[str, Any]) -> dict[str, Any] | None:
    usage = data.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("invalid model usage")
    return usage


def _anthropic_content(content: Any) -> list[dict[str, Any]]:
    """Convert OpenAI-style message content to Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content or " "}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content or " ")}]
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue
        if item.get("type") == "text":
            blocks.append({"type": "text", "text": str(item.get("text") or " ")})
            continue
        if item.get("type") == "image_url":
            image = item.get("image_url") or {}
            url = str(image.get("url") or "") if isinstance(image, dict) else str(image)
            if url.startswith("data:") and ";base64," in url:
                header, data = url.split(",", 1)
                media_type = header[5:].split(";", 1)[0] or "image/png"
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                })
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            continue
        if item.get("type") == "image" and isinstance(item.get("source"), dict):
            blocks.append(copy.deepcopy(item))
            continue
        blocks.append({"type": "text", "text": json.dumps(item, ensure_ascii=False)})
    return blocks or [{"type": "text", "text": " "}]


def anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Lift system messages to the Anthropic top-level field and merge adjacent roles."""
    systems: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role == "system":
            systems.append(_message_text(message.get("content")))
            continue
        if role not in ("user", "assistant"):
            role = "user"
        if role == "assistant" and isinstance(message.get("anthropic_content"), list):
            blocks = copy.deepcopy(message["anthropic_content"])
        else:
            blocks = _anthropic_content(message.get("content"))
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})
    return "\n\n".join(part for part in systems if part), converted


def anthropic_thinking_metadata(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize opaque blocks for diagnostics without exposing their values."""
    signatures = [str(block.get("signature") or "") for block in blocks
                  if isinstance(block, dict) and block.get("type") == "thinking"
                  and block.get("signature")]
    redacted = [str(block.get("data") or "") for block in blocks
                if isinstance(block, dict) and block.get("type") == "redacted_thinking"
                and block.get("data")]
    return {
        "thinking_blocks": sum(isinstance(block, dict) and block.get("type") == "thinking"
                               for block in blocks),
        "redacted_thinking_blocks": sum(isinstance(block, dict) and block.get("type") == "redacted_thinking"
                                        for block in blocks),
        "signature_fingerprints": [hashlib.sha256(value.encode()).hexdigest()[:16] for value in signatures],
        "redacted_fingerprints": [hashlib.sha256(value.encode()).hexdigest()[:16] for value in redacted],
    }


def openai_thinking_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert shared chat history to stateless Responses input items.

    Prior Responses output is replayed as complete items, including opaque
    reasoning items. It must never be flattened into an assistant text message.
    """
    systems: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system":
            systems.append(_message_text(content))
            continue
        if role == "assistant" and isinstance(message.get("response_output"), list):
            items.extend(copy.deepcopy(message["response_output"]))
            continue
        if not isinstance(content, list):
            items.append({"role": role, "content": _message_text(content)})
            continue
        parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                parts.append({"type": "input_text", "text": str(part)})
            elif part.get("type") in ("text", "input_text", "output_text"):
                parts.append({"type": "input_text", "text": str(part.get("text") or "")})
            elif part.get("type") in ("image_url", "input_image"):
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                parts.append({"type": "input_image", "image_url": str(url or "")})
            else:
                raise ValueError(f"unsupported Responses input part: {part.get('type')!r}")
        items.append({"role": role, "content": parts})
    return "\n\n".join(part for part in systems if part), items


def vertex_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert the shared chat history to Gemini Vertex contents/parts."""
    systems: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system":
            systems.append(_message_text(content))
            continue
        parts: list[dict[str, Any]] = []
        items = content if isinstance(content, list) else [{"type": "text", "text": _message_text(content)}]
        for item in items:
            if not isinstance(item, dict):
                parts.append({"text": str(item)})
            elif item.get("type") in ("text", "input_text", "output_text"):
                parts.append({"text": str(item.get("text") or "")})
            elif item.get("type") in ("image_url", "input_image", "image"):
                image = item.get("image_url") or {}
                url = str(image.get("url") or "") if isinstance(image, dict) else str(image)
                if not url.startswith("data:") or ";base64," not in url:
                    raise ValueError("Vertex Gemini requires base64 data-URL images")
                header, data = url.split(",", 1)
                media_type = header[5:].split(";", 1)[0] or "image/png"
                parts.append({"inlineData": {"mimeType": media_type, "data": data}})
        if not parts:
            parts = [{"text": " "}]
        vertex_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == vertex_role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": vertex_role, "parts": parts})
    return "\n\n".join(part for part in systems if part), contents


def _anthropic_stop_reason(reason: Any) -> str:
    if reason == "max_tokens":
        return "length"
    if reason in (None, "end_turn", "stop_sequence"):
        return "stop"
    return str(reason)


def _anthropic_usage(usage: Any) -> dict[str, Any]:
    """Expose Anthropic token accounting in the shared OpenAI-shaped schema."""
    raw = usage if isinstance(usage, dict) else {}
    fresh = int(raw.get("input_tokens", 0) or 0)
    created = int(raw.get("cache_creation_input_tokens", 0) or 0)
    cached = int(raw.get("cache_read_input_tokens", 0) or 0)
    output = int(raw.get("output_tokens", 0) or 0)
    details = raw.get("output_tokens_details") or {}
    thinking = int(details.get("thinking_tokens", 0) or 0) if isinstance(details, dict) else 0
    return {
        "prompt_tokens": fresh + created + cached,
        "completion_tokens": output,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": thinking},
    }


class _AttemptStats:
    """随本次预览传递，不改变请求、静态解析器或既有事件的调用协议。"""

    def __init__(self, attempt_id: str, call_id: str, purpose: str, attempt: int,
                 budget: dict, max_tokens: int) -> None:
        self.started_at = 0.0
        self.has_usage = False
        self.dispatched = False
        self.data: dict[str, Any] = {
            "attempt_id": attempt_id, "call_id": call_id, "purpose": purpose,
            "attempt": attempt, "status": "error", "elapsed_seconds": 0.0,
            "time_to_first_token_seconds": None, "usage": {},
            "normalized_usage": normalize_usage({}),
            "input_estimate": copy.deepcopy(budget), "requested_max_tokens": max_tokens,
            "http_status": None, "finish_reason": None, "error": None,
        }

    def capture_usage(self, usage: dict | None) -> None:
        if usage is not None:
            self.has_usage = True
            self.data["usage"] = copy.deepcopy(usage)
            self.data["normalized_usage"] = normalize_usage(usage)

    def capture_choice(self, choice: dict, body_key: str) -> None:
        if choice.get("finish_reason") is not None:
            self.data["finish_reason"] = choice["finish_reason"]
        body = choice.get(body_key)
        if not isinstance(body, dict) or self.data["time_to_first_token_seconds"] is not None:
            return
        for name in ("content", "reasoning_content", "reasoning"):
            value = body.get(name)
            has_text = isinstance(value, str) and bool(value)
            if isinstance(value, list):
                has_text = any(
                    isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str) and bool(block["text"])
                    for block in value
                )
            if has_text:
                self.data["time_to_first_token_seconds"] = perf_counter() - self.started_at
                break

    def failed(self, exc: BaseException, *, cancelled: bool = False) -> None:
        self.data["status"] = "cancelled" if cancelled else "error"
        self.data["error"] = f"{type(exc).__name__}: {exc}"[:2000]


class ModelClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout: float,
        retries: int,
        on_event: Callable[[dict], None] | None = None,
        extra_body: dict[str, Any] | None = None,
        protocol: str = "openai",
        stream: bool = True,
        on_attempt: Callable[[dict], None] | None = None,
        context_limit: int = 200000,
        reasoning_effort: str | None = None,
        reasoning_code_salvage: bool = False,
    ) -> None:
        if reasoning_effort not in (None, "none", "low", "medium", "high", "xhigh", "max"):
            raise ValueError(
                f"unexpected reasoning_effort {reasoning_effort!r}; "
                "supported: none/low/medium/high/xhigh/max"
            )
        if protocol not in ("openai", "openai-thinking", "anthropic", "vertex"):
            raise ValueError("protocol must be 'openai', 'openai-thinking', 'anthropic', or 'vertex'")
        self.protocol = protocol
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.timeout = timeout
        self.retries = retries
        self.extra_body = dict(extra_body or {})
        self.reasoning_effort = reasoning_effort
        self.reasoning_code_salvage = reasoning_code_salvage
        self.on_event = on_event
        # Vertex generateContent is JSON-only; never parse it as OpenAI SSE.
        self.stream = stream and protocol != "vertex"
        self.on_attempt = on_attempt
        self.last_call_attempts: list[dict] = []
        self.context_limit = context_limit
        self.token_estimator = TokenEstimator(scope=f"{self.model_name}/{self.endpoint()}")
        self.client: Any = None
        self._seq = 0
        self._last_attempt_id: str | None = None
        self._last_purpose = "action"
        self._recorder: RolloutRecorder | None = None
        self._last_recording: Any = None
        self.last_response_output: list[dict[str, Any]] | None = None
        self._response_output_candidate: list[dict[str, Any]] | None = None
        self.last_anthropic_content: list[dict[str, Any]] | None = None
        self._anthropic_content_candidate: list[dict[str, Any]] | None = None

    def configure_recording(
        self, run_dir: Any = None, *, metadata: dict | None = None,
        max_record_bytes: int = DEFAULT_MAX_BYTES,
    ) -> bool:
        """显式启用精确请求记录；None 关闭。失败仅告警，不影响模型请求。"""
        self._recorder = None
        self._last_recording = None
        if run_dir is None:
            return True
        try:
            self._recorder = RolloutRecorder(
                run_dir, metadata=metadata, max_record_bytes=max_record_bytes, secrets=(self.api_key,),
            )
            return True
        except (Exception, asyncio.CancelledError) as exc:
            warn_recording("请求记录初始化失败，已禁用（%s）", type(exc).__name__)
            return False

    @property
    def last_attempt_id(self) -> str | None:
        return self._last_attempt_id

    async def __aenter__(self) -> ModelClient:
        try:
            import httpx
        except ImportError as exc:
            raise AgentError("httpx is not installed") from exc
        self.client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self.client is not None:
            await self.client.aclose()

    def endpoint(self) -> str:
        if self.protocol == "anthropic":
            return self.base_url if self.base_url.endswith("/messages") else self.base_url + "/messages"
        if self.protocol == "vertex":
            return f"{self.base_url}/models/{self.model_name}:generateContent"
        if self.protocol == "openai-thinking":
            base = self.base_url.removesuffix("/chat/completions")
            return base if base.endswith("/responses") else base + "/responses"
        return self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"

    def _emit_for(self, attempt_id: str, event_type: str, *, purpose: str, **fields: Any) -> None:
        self._seq += 1
        event = dict(fields)
        event.update(schema_version=1, seq=self._seq, type=event_type, attempt_id=attempt_id, purpose=purpose)
        if self.on_event is not None:
            try:
                self.on_event(copy.deepcopy(event))
            except (Exception, asyncio.CancelledError) as exc:
                _LOG.warning("模型事件回调异常，已隔离（%s）：%s", event_type, exc)

    def emit_event(self, type: str, **fields: Any) -> None:
        """默认沿用最近成功请求的用途；传输完成不代表动作或摘要已通过语义校验。"""
        if self._last_attempt_id is not None:
            fields.setdefault("purpose", self._last_purpose)
            if type in ("response_validated", "response_rejected"):
                recording_call(self._last_recording, "validate", type == "response_validated"
                               and fields.get("validated") is True)
            self._emit_for(self._last_attempt_id, type, **fields)

    def _finish_attempt(self, stats: _AttemptStats, messages: list[dict[str, Any]]) -> None:
        if stats.has_usage:
            try:
                self.token_estimator.observe(
                    copy.deepcopy(messages), copy.deepcopy(stats.data["usage"]),
                    purpose=stats.data["purpose"], attempt_id=stats.data["attempt_id"],
                )
            except (Exception, asyncio.CancelledError) as exc:
                _LOG.warning("模型用量校准异常，已隔离：%s", exc)
        self.last_call_attempts.append(copy.deepcopy(stats.data))
        if self.on_attempt is not None:
            try:
                self.on_attempt(copy.deepcopy(stats.data))
            except (Exception, asyncio.CancelledError) as exc:
                _LOG.warning("模型统计回调异常，已隔离：%s", exc)

    @staticmethod
    async def _check_http(response: Any) -> None:
        if response.status_code >= 400:
            await response.aread()
            full_body = str(response.text or "").strip()
            if response.status_code == 400 and re.search(
                r"context[_ -](?:length|window)[_ -]exceeded|maximum context length|"
                r"context (?:length|window).{0,80}(?:exceed|too (?:long|large))|"
                r"(?:prompt|input) (?:is )?too long",
                full_body, re.I | re.S,
            ):
                raise ContextWindowError(full_body, response.status_code)
            body = full_body[:2000]
            detail = f"HTTP {response.status_code}: {body or '(空响应体)'}"
            if response.status_code in (401, 403, 404):
                raise AgentError(f"model request failed (not retrying): {detail}")
            raise RuntimeError(detail)

    def _json_result(self, data: Any, preview: _ContentPreview | _TextPreview) -> tuple[str, str, dict, str]:
        if self.protocol == "anthropic":
            return self._anthropic_json_result(data, preview)
        if self.protocol == "vertex":
            return self._vertex_json_result(data, preview)
        if self.protocol == "openai-thinking":
            return self._responses_json_result(data, preview)
        stats = getattr(preview, "_attempt_stats", None)
        if stats is not None and isinstance(data, dict):
            stats.capture_usage(_usage(data))
        choice = data["choices"][0]
        recording_call(getattr(preview, "_rollout_capture", None), "capture", choice, "message")
        if stats is not None and isinstance(choice, dict):
            stats.capture_choice(choice, "message")
        finish = choice.get("finish_reason")
        # 旧非流式网关可省略 finish_reason；若提供则必须明确正常结束。
        if finish == "length":
            raise ModelOutputTruncated("model response finish_reason=length")
        if finish is not None and finish != "stop":
            raise ValueError(f"model response finish_reason={finish}")
        message = choice["message"]
        raw = _message_text(message.get("content"))
        reasoning_field = str(message.get("reasoning_content") or message.get("reasoning") or "")
        content, reasoning = extract_reasoning(raw, reasoning_field)
        if (not content.strip() and self.reasoning_code_salvage
                and self.model_name.lower() == "deepseek-flash"
                and isinstance(preview, _ContentPreview)):
            candidate = terminal_reasoning_code(reasoning)
            if candidate is not None:
                content = candidate
                if stats is not None:
                    stats.data["response_salvage"] = "terminal_reasoning_code"
                    stats.data["response_original_content_chars"] = len(raw)
        if stats is not None:
            stats.data["response_content_chars"] = len(content)
            stats.data["response_reasoning_chars"] = len(reasoning)
        if not content.strip():
            raise EmptyModelContent(len(reasoning))
        usage = _usage(data) or {}
        preview.feed(raw)
        preview.finish(content)
        return content, reasoning, usage, finish or "stop"

    def _responses_json_result(
        self, data: Any, preview: _ContentPreview | _TextPreview,
        *, streamed_text: str = "",
    ) -> tuple[str, str, dict, str]:
        if not isinstance(data, dict):
            raise ValueError("invalid Responses API body")
        status = data.get("status")
        if status == "incomplete" and (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
            raise ModelOutputTruncated("Responses API max_output_tokens exhausted")
        if status != "completed":
            raise ValueError(f"Responses API status={status!r}")
        output = data.get("output")
        if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
            raise ValueError("Responses API output must be a list of items")
        text_parts: list[str] = []
        summaries: list[str] = []
        for item in output:
            if item.get("type") == "reasoning":
                summaries.extend(str(part.get("text") or "") for part in (item.get("summary") or [])
                                 if isinstance(part, dict))
            elif item.get("type") == "message" and item.get("role") == "assistant":
                text_parts.extend(str(part.get("text") or "") for part in (item.get("content") or [])
                                  if isinstance(part, dict) and part.get("type") == "output_text")
        content = "".join(text_parts)
        reasoning = "".join(summaries)
        if not content.strip():
            raise EmptyModelContent(len(reasoning))
        if streamed_text and not content.startswith(streamed_text):
            raise ValueError("Responses stream text disagrees with completed output")
        preview.feed(content[len(streamed_text):])
        preview.finish(content)
        raw_usage = _usage(data) or {}
        usage = {
            "prompt_tokens": raw_usage.get("input_tokens", raw_usage.get("prompt_tokens", 0)),
            "completion_tokens": raw_usage.get("output_tokens", raw_usage.get("completion_tokens", 0)),
            "prompt_tokens_details": raw_usage.get("input_tokens_details") or {},
            "completion_tokens_details": raw_usage.get("output_tokens_details") or {},
        }
        stats = getattr(preview, "_attempt_stats", None)
        if stats is not None:
            stats.capture_usage(usage)
            stats.data["response_content_chars"] = len(content)
            stats.data["response_reasoning_chars"] = len(reasoning)
        recording_call(getattr(preview, "_rollout_capture", None), "capture",
                       {"output": output, "usage": raw_usage}, "message")
        self._response_output_candidate = copy.deepcopy(output)
        return content, reasoning, usage, "stop"

    async def _responses_sse_result(
        self, response: Any, preview: _ContentPreview | _TextPreview,
    ) -> tuple[str, str, dict, str]:
        streamed: list[str] = []
        completed: dict[str, Any] | None = None
        frame: list[str] = []

        def consume() -> None:
            nonlocal completed
            if not frame:
                return
            event = json.loads("\n".join(frame))
            frame.clear()
            if not isinstance(event, dict):
                raise ValueError("invalid Responses SSE event")
            kind = event.get("type")
            if kind == "response.output_text.delta":
                delta = str(event.get("delta") or "")
                streamed.append(delta)
                preview.feed(delta)
            elif kind == "response.reasoning_summary_text.delta":
                feed = getattr(preview, "feed_reasoning", None)
                if feed is not None:
                    feed(str(event.get("delta") or ""))
            elif kind == "response.completed":
                completed = event.get("response")
            elif kind in ("response.failed", "response.incomplete", "error"):
                raise ValueError(f"Responses stream {kind}: {str(event.get('error') or event.get('response'))[:1000]}")

        async for line in response.aiter_lines():
            if not line:
                consume()
            elif line.startswith("data:"):
                value = line[5:].lstrip()
                if value != "[DONE]":
                    frame.append(value)
        consume()
        if completed is None:
            raise ValueError("incomplete Responses stream: missing response.completed")
        return self._responses_json_result(completed, preview, streamed_text="".join(streamed))

    @staticmethod
    def _vertex_json_result(
        data: Any, preview: _ContentPreview | _TextPreview,
    ) -> tuple[str, str, dict, str]:
        candidates = data.get("candidates") or []
        if not candidates or not isinstance(candidates[0], dict):
            raise ValueError("Vertex Gemini returned no candidate")
        candidate = candidates[0]
        finish_reason = str(candidate.get("finishReason") or "")
        if finish_reason == "MAX_TOKENS":
            raise ModelOutputTruncated("Vertex Gemini finishReason=MAX_TOKENS")
        if finish_reason != "STOP":
            raise ValueError(f"Vertex Gemini finishReason={finish_reason!r}")
        parts = (candidate.get("content") or {}).get("parts") or []
        content = "".join(str(part.get("text") or "") for part in parts
                          if isinstance(part, dict) and not part.get("thought"))
        reasoning = "".join(str(part.get("text") or "") for part in parts
                            if isinstance(part, dict) and part.get("thought"))
        if not content.strip():
            raise EmptyModelContent(len(reasoning))
        metadata = data.get("usageMetadata") or {}
        reasoning_tokens = int(metadata.get("thoughtsTokenCount", 0) or 0)
        usage = {
            "prompt_tokens": int(metadata.get("promptTokenCount", 0) or 0),
            "completion_tokens": int(metadata.get("candidatesTokenCount", 0) or 0)
            + reasoning_tokens,
            "prompt_tokens_details": {
                "cached_tokens": int(metadata.get("cachedContentTokenCount", 0) or 0),
            },
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        }
        stats = getattr(preview, "_attempt_stats", None)
        if stats is not None:
            stats.capture_usage(usage)
            stats.capture_choice(
                {"finish_reason": "stop", "message": {"content": content}}, "message",
            )
        recording_call(
            getattr(preview, "_rollout_capture", None), "capture", candidate, "message",
        )
        feed_reasoning = getattr(preview, "feed_reasoning", None)
        if feed_reasoning is not None and reasoning:
            feed_reasoning(reasoning)
        preview.feed(content)
        preview.finish(content)
        return content, reasoning, usage, "stop"

    async def _sse_result(self, response: Any, preview: _ContentPreview | _TextPreview) -> tuple[str, str, dict, str]:
        if self.protocol == "anthropic":
            return await self._anthropic_sse_result(response, preview)
        if self.protocol == "openai-thinking":
            return await self._responses_sse_result(response, preview)
        stats = getattr(preview, "_attempt_stats", None)
        parts: list[str] = []
        native: list[str] = []
        fallback: list[str] = []
        usage: dict[str, Any] = {}
        finish: str | None = None
        rejection: str | None = None
        done = False
        frame: list[str] = []
        first_line = True
        async for line in response.aiter_lines():
            if first_line:
                line = line.removeprefix("\ufeff")
                first_line = False
            if line != "":
                if line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if field == "data":
                    frame.append(value.removeprefix(" ") if separator else "")
                continue
            if not frame:
                continue
            payload, frame = "\n".join(frame), []
            if payload.strip() == "[DONE]":
                done = True
                break
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("invalid SSE JSON object")
            if stats is not None:
                stats.capture_usage(_usage(data))
            if "error" in data:
                raise ValueError(f"model stream error: {str(data['error'])[:2000]}")
            frame_usage = _usage(data)
            if frame_usage is not None:
                usage = frame_usage
            choices = data.get("choices")
            if choices == []:
                # Gateways (routify / Azure-style) emit a leading content-filter frame
                # with empty choices and no usage; it carries no delta, so skip it.
                # Previously only the usage-bearing empty frame was tolerated, which
                # made every gpt-6-astra stream die with "invalid SSE choices".
                continue
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("invalid SSE choices")
            choice = choices[0]
            recording_call(getattr(preview, "_rollout_capture", None), "capture", choice, "delta")
            if stats is not None and isinstance(choice, dict):
                stats.capture_choice(choice, "delta")
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ValueError("unexpected SSE choice index")
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise ValueError("invalid SSE delta")
            text = _message_text(delta.get("content"))
            reasoning_text = _message_text(delta.get("reasoning_content"))
            fallback_text = _message_text(delta.get("reasoning"))
            if delta.get("tool_calls") or delta.get("function_call") or delta.get("refusal"):
                # 已确定不可执行，停止预览，但仍接收尾部计量。
                rejection = "model returned a non-code completion"
            if finish is not None and (text or reasoning_text or fallback_text or choice.get("finish_reason")):
                raise ValueError("model sent delta after finish_reason")
            parts.append(text)
            native.append(reasoning_text)
            fallback.append(fallback_text)
            if rejection is None:
                feed_reasoning = getattr(preview, "feed_reasoning", None)
                if feed_reasoning is not None and reasoning_text:
                    feed_reasoning(reasoning_text)
                preview.feed(text)
            reason = choice.get("finish_reason")
            if reason is not None:
                # 非正常结束仍读取 usage 尾帧；拒绝执行留到流结束后，不能漏记已消耗用量。
                finish = reason
        # EOF 不隐式派发残帧；即使完整代码已预览，也不能将 EOF 当作 DONE。
        if rejection is not None:
            raise ValueError(rejection)
        if not done:
            raise ValueError("incomplete model stream: missing [DONE]")
        if finish == "length":
            raise ModelOutputTruncated("model response finish_reason=length")
        if finish is not None and finish != "stop":
            raise ValueError(f"model response finish_reason={finish}")
        if finish != "stop":
            raise ValueError("incomplete model stream: missing finish_reason=stop")
        content, reasoning = extract_reasoning("".join(parts), "".join(native) or "".join(fallback))
        if (not content.strip() and self.reasoning_code_salvage
                and self.model_name.lower() == "deepseek-flash"
                and isinstance(preview, _ContentPreview)):
            candidate = terminal_reasoning_code(reasoning)
            if candidate is not None:
                content = candidate
                if stats is not None:
                    stats.data["response_salvage"] = "terminal_reasoning_code"
                    stats.data["response_original_content_chars"] = 0
        if stats is not None:
            stats.data["response_content_chars"] = len(content)
            stats.data["response_reasoning_chars"] = len(reasoning)
        if not content.strip():
            raise EmptyModelContent(len(reasoning))
        preview.finish(content)
        return content, reasoning, usage, finish

    def _anthropic_json_result(
        self, data: Any, preview: _ContentPreview | _TextPreview,
    ) -> tuple[str, str, dict, str]:
        if not isinstance(data, dict):
            raise ValueError("invalid Anthropic JSON response")
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ValueError("Anthropic response missing content blocks")
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block.get("type") == "thinking":
                thinking_parts.append(str(block.get("thinking") or ""))
        content = "".join(text_parts)
        reasoning = "".join(thinking_parts)
        if not content.strip():
            raise ValueError("Anthropic model returned empty text content")
        usage = _anthropic_usage(data.get("usage"))
        stats = getattr(preview, "_attempt_stats", None)
        if stats is not None:
            stats.capture_usage(usage)
            stats.capture_choice({"finish_reason": _anthropic_stop_reason(data.get("stop_reason")),
                                  "message": {"content": content}}, "message")
        recording_call(
            getattr(preview, "_rollout_capture", None), "capture",
            {"message": {"content": blocks}, "finish_reason": data.get("stop_reason")},
            "message",
        )
        finish = _anthropic_stop_reason(data.get("stop_reason"))
        if finish == "length":
            raise ModelOutputTruncated("Anthropic response stop_reason=max_tokens")
        if finish != "stop":
            raise ValueError(f"Anthropic response stop_reason={data.get('stop_reason')!r}")
        self._anthropic_content_candidate = copy.deepcopy(blocks)
        feed_reasoning = getattr(preview, "feed_reasoning", None)
        if feed_reasoning is not None and reasoning:
            feed_reasoning(reasoning)
        preview.feed(content)
        preview.finish(content)
        return content, reasoning, usage, finish

    async def _anthropic_sse_result(
        self, response: Any, preview: _ContentPreview | _TextPreview,
    ) -> tuple[str, str, dict, str]:
        stats = getattr(preview, "_attempt_stats", None)
        capture = getattr(preview, "_rollout_capture", None)
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        stop_reason: Any = None
        stopped = False
        event_name = ""
        frame: list[str] = []

        async def consume() -> None:
            nonlocal usage, stop_reason, stopped
            if not frame:
                return
            data = json.loads("\n".join(frame))
            frame.clear()
            if not isinstance(data, dict):
                raise ValueError("invalid Anthropic SSE JSON object")
            kind = str(data.get("type") or event_name)
            if kind == "error":
                raise ValueError(f"Anthropic stream error: {str(data.get('error'))[:2000]}")
            if kind == "message_start":
                message = data.get("message") or {}
                if isinstance(message.get("usage"), dict):
                    usage.update(message["usage"])
            elif kind == "content_block_start":
                block = data.get("content_block") or {}
                index = data.get("index")
                if not isinstance(index, int):
                    index = len(blocks)
                blocks[index] = copy.deepcopy(block)
                if block.get("type") == "text" and block.get("text"):
                    value = str(block["text"])
                    text_parts.append(value)
                    preview.feed(value)
                    recording_call(capture, "capture", {"delta": {"content": value}}, "delta")
                elif block.get("type") == "thinking" and block.get("thinking"):
                    value = str(block["thinking"])
                    thinking_parts.append(value)
                    recording_call(capture, "capture", {"delta": {"reasoning_content": value}}, "delta")
                    feed = getattr(preview, "feed_reasoning", None)
                    if feed is not None:
                        feed(value)
            elif kind == "content_block_delta":
                delta = data.get("delta") or {}
                delta_type = delta.get("type")
                index = data.get("index")
                if not isinstance(index, int):
                    index = max(blocks, default=-1)
                target = blocks.get(index)
                if delta_type == "text_delta":
                    value = str(delta.get("text") or "")
                    if target is not None:
                        target["text"] = str(target.get("text") or "") + value
                    text_parts.append(value)
                    preview.feed(value)
                    recording_call(capture, "capture", {"delta": {"content": value}}, "delta")
                elif delta_type == "thinking_delta":
                    value = str(delta.get("thinking") or "")
                    if target is not None:
                        target["thinking"] = str(target.get("thinking") or "") + value
                    thinking_parts.append(value)
                    recording_call(capture, "capture", {"delta": {"reasoning_content": value}}, "delta")
                    feed = getattr(preview, "feed_reasoning", None)
                    if feed is not None:
                        feed(value)
                elif delta_type == "signature_delta" and target is not None:
                    target["signature"] = str(target.get("signature") or "") + str(delta.get("signature") or "")
            elif kind == "message_delta":
                delta = data.get("delta") or {}
                stop_reason = delta.get("stop_reason", stop_reason)
                if stop_reason is not None:
                    recording_call(capture, "capture", {
                        "finish_reason": _anthropic_stop_reason(stop_reason), "delta": {},
                    }, "delta")
                if isinstance(data.get("usage"), dict):
                    usage.update(data["usage"])
            elif kind == "message_stop":
                stopped = True

        first_line = True
        async for line in response.aiter_lines():
            if first_line:
                line = line.removeprefix("\ufeff")
                first_line = False
            if line == "":
                await consume()
                event_name = ""
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            value = value.removeprefix(" ") if separator else ""
            if field == "event":
                event_name = value
            elif field == "data":
                frame.append(value)
        await consume()
        if not stopped:
            raise ValueError("incomplete Anthropic stream: missing message_stop")
        content = "".join(text_parts)
        reasoning = "".join(thinking_parts)
        if not content.strip():
            raise ValueError("Anthropic model returned empty text content")
        finish = _anthropic_stop_reason(stop_reason)
        if finish == "length":
            raise ModelOutputTruncated("Anthropic response stop_reason=max_tokens")
        if finish != "stop":
            raise ValueError(f"Anthropic response stop_reason={stop_reason!r}")
        usage = _anthropic_usage(usage)
        if stats is not None:
            stats.capture_usage(usage)
            stats.capture_choice({"finish_reason": finish,
                                  "delta": {"content": content}}, "delta")
        self._anthropic_content_candidate = [blocks[index] for index in sorted(blocks)]
        preview.finish(content)
        return content, reasoning, usage, finish

    async def _request(self, payload: dict, headers: dict, preview: _ContentPreview | _TextPreview) -> tuple[str, str, dict, str]:
        stats = getattr(preview, "_attempt_stats", None)
        if stats is not None:
            # 仅表示已进入真实 HTTP 传输入口，不声称服务器已接收，更不代表执行成功。
            stats.dispatched = True
        if not self.stream:
            response = await self.client.post(self.endpoint(), json=payload, headers=headers)
            if stats is not None:
                stats.data["http_status"] = response.status_code
            await self._check_http(response)
            return self._json_result(response.json(), preview)
        async with self.client.stream("POST", self.endpoint(), json=payload, headers=headers) as response:
            if stats is not None:
                stats.data["http_status"] = response.status_code
            await self._check_http(response)
            if "application/json" in response.headers.get("content-type", "").lower():
                # 兼容忽略 stream 参数、仍返回完整 JSON 的既有网关。
                await response.aread()
                return self._json_result(response.json(), preview)
            return await self._sse_result(response, preview)

    async def complete(
        self, messages: list[dict[str, Any]], *, max_tokens: int, purpose: str = "action",
        metadata: dict | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        self.last_response_output = None
        self.last_anthropic_content = None
        call_id = uuid.uuid4().hex
        if self._recorder is not None and self.api_key and self.api_key not in self._recorder.secrets:
            # 同一客户端换钥后，旧钥与本次实际发送的钥都必须拒绝落盘。
            self._recorder.secrets = (*self._recorder.secrets, self.api_key)
        record = recording_call(self._recorder, "begin", call_id, purpose, metadata)
        self._last_recording = None
        try:
            result = await self._complete(
                messages, max_tokens=max_tokens, purpose=purpose, call_id=call_id, record=record,
            )
        except BaseException as exc:
            recording_call(record, "finish", "cancelled" if isinstance(exc, asyncio.CancelledError)
                           else "error", type(exc).__name__)
            raise
        else:
            recording_call(record, "finish", "success")
            self._last_recording = record
            return result

    async def _complete(
        self, messages: list[dict[str, Any]], *, max_tokens: int, purpose: str,
        call_id: str, record: Any,
    ) -> tuple[str, str, dict[str, Any]]:
        self.last_call_attempts = []
        if purpose not in ("action", "compaction") or self.client is None:
            # 前置拒绝仍是一轮 complete 调用；记录输入，但不虚构 HTTP attempt。
            recording_call(record, "save_request", {
                "protocol": self.protocol, "model": self.model_name,
                "messages": messages, "max_tokens": max_tokens,
                "stream": self.stream, **self.extra_body,
            })
            if purpose not in ("action", "compaction"):
                raise ValueError("purpose must be 'action' or 'compaction'")
            raise AgentError("model client is not open")
        request_messages = copy.deepcopy(messages)
        if self.protocol == "anthropic":
            system, converted = anthropic_messages(request_messages)
            payload = {
                "model": self.model_name,
                "messages": converted,
                "max_tokens": max_tokens,
                "stream": self.stream,
            }
            if system:
                payload["system"] = system
            for key, value in self.extra_body.items():
                if key not in {
                    "vl_high_resolution_images", "stream_options", "reasoning_effort",
                    "temperature", "top_p",
                }:
                    payload[key] = value
            headers = {
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            if self.api_key:
                headers["x-api-key"] = self.api_key
        elif self.protocol == "vertex":
            system, converted = vertex_messages(request_messages)
            payload = {
                "contents": converted,
                "generationConfig": {
                    "maxOutputTokens": max_tokens,
                    "thinkingConfig": {"thinkingBudget": 0, "includeThoughts": False},
                },
            }
            if system:
                payload["systemInstruction"] = {"parts": [{"text": system}]}
            if "routify" in self.base_url.lower():
                payload["routifyConfig"] = {"enableContentSafetyIntercept": False}
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.protocol == "openai-thinking":
            instructions, converted = openai_thinking_messages(request_messages)
            effort = self.reasoning_effort
            payload = {
                "model": self.model_name, "input": converted,
                "max_output_tokens": max_tokens, "store": False,
                "include": ["reasoning.encrypted_content"],
                "stream": self.stream, **self.extra_body,
            }
            if instructions:
                payload["instructions"] = instructions
            if effort:
                payload["reasoning"] = {"effort": effort}
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            # GPT/o 系只接受 max_completion_tokens；routify 的 effort 枚举不含 xhigh。
            token_field = (
                "max_completion_tokens"
                if self.model_name.lower().startswith(("gpt-", "o1", "o3", "o4"))
                else "max_tokens"
            )
            effort = self.reasoning_effort
            if effort == "xhigh" and "routify" in self.base_url:
                effort = "high"
            payload = {
                "model": self.model_name, "messages": request_messages,
                token_field: max_tokens, "stream": self.stream, **self.extra_body,
            }
            if effort:
                payload["reasoning_effort"] = effort
            if self.stream:
                payload["stream_options"] = {"include_usage": True}
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
        recording_call(record, "save_request", payload)
        last_error: Exception | None = None
        empty_content_reprompted = False
        has_encrypted_replay = self.protocol == "openai-thinking" and any(
            isinstance(item, dict) and item.get("type") == "reasoning"
            and item.get("encrypted_content") for item in payload.get("input", [])
        )
        has_anthropic_replay = self.protocol == "anthropic" and any(
            message.get("role") == "assistant" and any(
                block.get("type") in ("thinking", "redacted_thinking")
                for block in message.get("content", []) if isinstance(block, dict)
            )
            for message in payload.get("messages", [])
        )
        max_attempts = self.retries + 1 + int(has_encrypted_replay or has_anthropic_replay)
        ordinary_retries_used = 0
        encrypted_fallback_used = False
        anthropic_fallback_used = False
        for attempt in range(max_attempts):
            self._response_output_candidate = None
            self._anthropic_content_candidate = None
            budget = self.token_estimator.estimate(
                request_messages, purpose=purpose, reserve_tokens=max_tokens,
                context_limit=self.context_limit,
            )
            if budget["over_limit"]:
                raise ContextBudgetError(budget)
            attempt_id = uuid.uuid4().hex

            def emit(event_type: str, **fields: Any) -> None:
                self._emit_for(attempt_id, event_type, purpose=purpose, **fields)

            preview = _ContentPreview(emit) if purpose == "action" else _TextPreview()
            stats = _AttemptStats(attempt_id, call_id, purpose, attempt + 1, budget, max_tokens)
            if self.protocol == "anthropic":
                sent_blocks = [block for message in payload["messages"]
                               if message.get("role") == "assistant"
                               for block in message.get("content", [])]
                stats.data["anthropic_thinking_sent"] = anthropic_thinking_metadata(sent_blocks)
            preview._attempt_stats = stats
            preview._rollout_capture = recording_call(record, "new_capture")
            emit("request_start", attempt=attempt + 1, max_attempts=max_attempts,
                 timeout_seconds=self.timeout)
            stats.started_at = perf_counter()
            recovery: str | None = None
            try:
                try:
                    # 墙钟上限：SSE 心跳/注释行会不断重置 per-read 超时，
                    # 仅靠 read timeout 无法终止“连接活着但不出 token”的挂死流。
                    content, reasoning, usage, finish = await asyncio.wait_for(
                        self._request(payload, headers, preview), timeout=self.timeout
                    )
                finally:
                    stats.data["elapsed_seconds"] = perf_counter() - stats.started_at
            except asyncio.CancelledError as exc:
                stats.failed(exc, cancelled=True)
                emit("request_aborted", reason="cancelled", retrying=False)
                raise
            except AgentError as exc:
                stats.failed(exc)
                emit("request_aborted", reason="request_failed", error=str(exc), retrying=False)
                raise
            except EmptyModelContent as exc:
                stats.failed(exc)
                last_error = exc
                retrying = ordinary_retries_used < self.retries
                if retrying:
                    ordinary_retries_used += 1
                if retrying and self.protocol == "openai" and purpose == "action":
                    if (self.model_name.lower() == "deepseek-flash"
                            and payload.get("reasoning_effort") != "none"):
                        recovery = "reasoning_effort_none"
                    elif not empty_content_reprompted:
                        recovery = "content_format_reminder"
                if recovery:
                    stats.data["retry_strategy"] = recovery
                emit("request_aborted", reason="empty_model_content", error=str(exc),
                     retrying=retrying, recovery=recovery,
                     reasoning_chars=exc.reasoning_chars)
                if not retrying:
                    break
            except Exception as exc:
                from httpx import TimeoutException
                stats.failed(exc)
                last_error = exc
                error_text = str(exc).lower()
                invalid_encrypted = (
                    has_encrypted_replay and not encrypted_fallback_used
                    and "http 400" in error_text and "encrypted content" in error_text
                    and any(marker in error_text for marker in (
                        "could not be decrypted", "could not be verified", "is invalid",
                    ))
                )
                invalid_anthropic = (
                    has_anthropic_replay and not anthropic_fallback_used
                    and "http 400" in error_text and "thinking" in error_text
                    and ("invalid `signature`" in error_text
                         or "blocks in the latest assistant message cannot be modified" in error_text)
                )
                retrying = invalid_encrypted or invalid_anthropic or ordinary_retries_used < self.retries
                if invalid_encrypted:
                    recovery = "drop_invalid_encrypted_replay"
                    stats.data["retry_strategy"] = recovery
                elif invalid_anthropic:
                    recovery = "drop_invalid_anthropic_thinking"
                    stats.data["retry_strategy"] = recovery
                elif retrying:
                    ordinary_retries_used += 1
                emit("request_aborted", reason="timeout" if isinstance(exc, (TimeoutError, TimeoutException)) else "request_failed",
                     error=str(exc) or type(exc).__name__, retrying=retrying,
                     recovery=recovery)
                if not retrying:
                    break
            else:
                # 保持旧 mock 的三参数请求签名及原始 usage 返回值。
                if usage or stats.has_usage:
                    stats.capture_usage(usage)
                if self.protocol == "anthropic":
                    stats.data["anthropic_thinking_received"] = anthropic_thinking_metadata(
                        self._anthropic_content_candidate or []
                    )
                stats.data.update(status="success", finish_reason=finish)
                self._last_attempt_id, self._last_purpose = attempt_id, purpose
                if self.protocol == "openai-thinking":
                    self.last_response_output = copy.deepcopy(self._response_output_candidate)
                elif self.protocol == "anthropic":
                    self.last_anthropic_content = copy.deepcopy(self._anthropic_content_candidate)
                emit("response_complete", usage=usage, finish_reason=finish, validated=False, preview=purpose == "action")
                return content, reasoning, usage
            finally:
                recording_call(record, "save_attempt", stats.data, preview._rollout_capture,
                               dispatched=stats.dispatched)
                self._finish_attempt(stats, request_messages)
            if recovery == "reasoning_effort_none":
                # Routify's deepseek-flash sometimes puts the whole action in
                # reasoning_content and ends with empty content. A live replay of
                # the same GUI request returned a proper action with thinking off.
                payload["reasoning_effort"] = "none"
            elif recovery == "content_format_reminder":
                request_messages = request_messages + [{
                    "role": "user",
                    "content": (
                        "The previous response had empty final content. Reply in the normal "
                        "content field, following the system message's required action format. "
                        "Do not place actions only in reasoning_content."
                    ),
                }]
                payload["messages"] = request_messages
                empty_content_reprompted = True
            elif recovery == "drop_invalid_encrypted_replay":
                payload["input"] = [item for item in payload["input"]
                                    if not (isinstance(item, dict) and item.get("type") == "reasoning")]
                encrypted_fallback_used = True
                continue
            elif recovery == "drop_invalid_anthropic_thinking":
                for message in payload["messages"]:
                    if message.get("role") == "assistant":
                        message["content"] = [block for block in message["content"]
                                              if block.get("type") not in ("thinking", "redacted_thinking")]
                for message in request_messages:
                    if message.get("role") == "assistant" and isinstance(message.get("anthropic_content"), list):
                        message["anthropic_content"] = [block for block in message["anthropic_content"]
                                                        if block.get("type") not in ("thinking", "redacted_thinking")]
                anthropic_fallback_used = True
                _LOG.warning("Anthropic thinking signature rejected; retrying once with text-only history")
                continue
            # 当前尝试已经 aborted；退避期间取消不生成虚构的新 HTTP 尝试。
            await asyncio.sleep(min(2**attempt, 8))
        error = AgentError(
            f"model request failed after {len(self.last_call_attempts)} attempts: {last_error}"
        )
        error.attempts = copy.deepcopy(self.last_call_attempts)
        raise error from last_error
