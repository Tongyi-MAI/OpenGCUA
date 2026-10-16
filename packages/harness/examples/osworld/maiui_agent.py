"""Standalone MAIUI screenshot-to-action agent.

This module intentionally has no dependency on mai-gym or OSWorld. It owns only
model input assembly, history management, feature-flag prompt composition, an
OpenAI-compatible model call, and response parsing. The caller remains
responsible for executing returned actions and supplying the next screenshot and
execution result.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import inspect
import json
import mimetypes
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence, Union

IMAGE_COLLAPSED = "(screenshot omitted by the image window)"
TOOL_RESULT_COMPACTED = "(old observation compacted)"

_TOOL_CALL_RE = re.compile(r"<tool_call[^>]*>(.*?)</tool_call>", re.I | re.S)
_ACTION_BLOCK_RE = re.compile(
    r"<tool_call(?P<attrs>[^>]*)>(?P<tool>.*?)</tool_call>"
    r"|<bash>(?P<bash>.*?)</bash>",
    re.I | re.S,
)
_INTENT_RE = re.compile(r"<intent>(.*?)</intent>", re.I | re.S)
_THINK_RE = re.compile(r"<(?:think|thinking)>(.*?)</(?:think|thinking)>", re.I | re.S)

ModelResult = Union[Mapping[str, Any], str]
ModelCallable = Callable[
    [list[dict[str, Any]], dict[str, Any]],
    Union[ModelResult, Awaitable[ModelResult]],
]
SummaryCallable = Callable[
    [str, list[dict[str, Any]]], Union[str, Awaitable[str]]
]
VerifierResult = Union[bool, Mapping[str, Any]]
VerifierCallable = Callable[
    [str, Sequence["Turn"], dict[str, Any]],
    Union[VerifierResult, Awaitable[VerifierResult]],
]
ImageTransform = Callable[[Any, Sequence[dict[str, Any]]], Any]


@dataclass
class FeatureConfig:
    """All optional MAIUI behaviors. Every capability is opt-in."""

    enable_bash: bool = False
    enable_batch_actions: bool = False
    enable_ask_user: bool = False
    enable_screenshot_action: bool = False
    enable_intent: bool = False
    micro_compaction: bool = False
    llm_compaction: bool = False
    enable_todo: bool = False
    todo_diff_snapshot: bool = False
    enable_step_hint: bool = False
    enable_action_overlay: bool = False
    enable_claude_thinking_pattern: bool = False
    enable_notice: bool = False
    enable_verifier: bool = False
    prompt_v10: bool = False
    img_keep_n: int = 0
    img_interval: int = 0
    micro_compaction_trigger_steps: int = 60
    micro_keep_turns: int = 25
    micro_interval: int = 35
    compaction_trigger_steps: int = 120
    compaction_trigger_tokens: int = 100_000
    image_token_estimate: int = 1600

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "FeatureConfig":
        values = values or {}
        known = cls.__dataclass_fields__
        return cls(**{key: values[key] for key in known if key in values})

    @classmethod
    def from_flags(cls, flags: str | Sequence[str]) -> "FeatureConfig":
        """Parse compact flags such as ``+intent +keep_n(n=5,interval=10)``."""
        text = flags if isinstance(flags, str) else " ".join(flags)
        aliases = {
            "bash": "enable_bash",
            "batch": "enable_batch_actions",
            "ask_user": "enable_ask_user",
            "screenshot": "enable_screenshot_action",
            "intent": "enable_intent",
            "micro_compact": "micro_compaction",
            "micro compact": "micro_compaction",
            "micro compaction": "micro_compaction",
            "llm_compact": "llm_compaction",
            "llm compact": "llm_compaction",
            "llm compaction": "llm_compaction",
            "todo": "enable_todo",
            "step_hint": "enable_step_hint",
            "action_overlay": "enable_action_overlay",
            "claude_pattern": "enable_claude_thinking_pattern",
            "notice": "enable_notice",
            "verifier": "enable_verifier",
            "prompt_v10": "prompt_v10",
        }
        values: dict[str, Any] = {}
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        for alias, field_name in aliases.items():
            if re.search(rf"(?:^|\s)\+{re.escape(alias)}(?:\s|$)", normalized):
                values[field_name] = True
        keep = re.search(
            r"\+keep_n\s*\(\s*n\s*=\s*(\d+)\s*,\s*interval\s*=\s*(\d+)\s*\)",
            normalized,
        )
        if keep:
            values["img_keep_n"] = int(keep.group(1))
            values["img_interval"] = int(keep.group(2))
        if values.get("enable_todo"):
            values["todo_diff_snapshot"] = True
        return cls(**values)


@dataclass
class TodoItem:
    id: str
    content: str
    kind: str = "task"
    status: str = "pending"
    updated_step: int = 0


@dataclass
class Turn:
    step: int
    observation: str
    image_url: str | None
    response: str
    reasoning: str
    action: dict[str, Any]
    execution_result: str = ""
    intent: str = ""


@dataclass
class AgentOutput:
    action: dict[str, Any]
    raw_response: str
    reasoning: str
    intent: str
    messages: list[dict[str, Any]]
    model_response: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _display_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _format_prompt(template: str, *, password: str, range_scale: float) -> str:
    return template.format(
        password=password,
        range_scale=_display_number(range_scale),
        range_half=_display_number(range_scale / 2),
    )


def image_to_data_url(image: Any, *, image_format: str = "jpeg", jpeg_quality: int = 85) -> str:
    """Normalize a path, bytes, data URL, or PIL-compatible image to a data URL."""
    if image is None:
        raise ValueError("image must not be None")
    if isinstance(image, str):
        if image.startswith("data:image/"):
            return image
        path = Path(image).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        payload = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
    if isinstance(image, Path):
        return image_to_data_url(str(image), image_format=image_format, jpeg_quality=jpeg_quality)
    if isinstance(image, (bytes, bytearray, memoryview)):
        payload = bytes(image)
        return f"data:image/{image_format};base64,{base64.b64encode(payload).decode('ascii')}"
    if hasattr(image, "save"):
        import io

        buffer = io.BytesIO()
        fmt = "JPEG" if image_format.lower() in {"jpg", "jpeg"} else "PNG"
        prepared = image.convert("RGB") if fmt == "JPEG" and hasattr(image, "convert") else image
        save_kwargs = {"quality": jpeg_quality} if fmt == "JPEG" else {}
        prepared.save(buffer, format=fmt, **save_kwargs)
        return f"data:image/{fmt.lower()};base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"
    raise TypeError("image must be a path, bytes, data URL, or PIL-compatible image")


def _claude_pattern_prompt() -> str:
    return (
        "## Deliberate task discipline\n"
        "Derive a concrete task contract before acting. Distinguish observed facts from "
        "hypotheses, revise a failed approach instead of repeating it, and verify the final "
        "state with external evidence before reporting success."
    )


def _notice_prompt() -> str:
    return (
        "## Notices\n"
        "Honor the application named by the user. When editing an existing artifact, preserve "
        "unrequested content. Record important values in reasoning before their screenshot leaves "
        "the visible history window."
    )


def build_simple_prompt(
    features: FeatureConfig,
    *,
    password: str = "password",
    range_scale: float = 1000,
) -> str:
    """Compose the tool-call prompt from feature switches."""
    screenshot_mode = (
        "A screenshot is attached only when requested in the previous response."
        if features.enable_screenshot_action
        else "A current screenshot is attached automatically at every step."
    )
    sections = [
        "You are a computer-use agent operating a desktop. Each step contains the immutable "
        "task instruction, the current observation, prior action results, and optional history. "
        f"{screenshot_mode} The computer password is {{password}}."
    ]
    if features.enable_claude_thinking_pattern:
        sections.append(_claude_pattern_prompt())
    if features.enable_notice:
        sections.append(_notice_prompt())

    sections.append(
        "## GUI actions\n"
        "Use pyautogui calls such as click, rightClick, doubleClick, moveTo, dragTo, "
        "typewrite, press, hotkey, and scroll. Coordinates are NORMALIZED integers in "
        "[0, {range_scale}] on both axes; ({range_half}, {range_half}) is screen center. "
        "Never copy raw pixel coordinates without converting them."
    )
    if features.enable_bash:
        sections.append(
            "## Bash actions\n"
            "Put one complete raw script in a bare <bash>...</bash> block. Never nest it "
            "inside <tool_call>. Its stdout and stderr arrive in the next observation."
        )

    controls = [
        '- computer.wait() waits for loading.',
        '- computer.terminate(status="success"|"failure", answer=None) ends the task.',
    ]
    if features.enable_screenshot_action:
        controls.insert(0, "- computer.screenshot() requests the image delivered next step.")
    if features.enable_ask_user:
        controls.append("- computer.ask_user(question=...) asks one essential concise question.")
    sections.append("## Flow control\n" + "\n".join(controls))

    if features.enable_todo:
        snapshot = (
            "The full ledger is shown only after changes; otherwise a compact status is shown."
            if features.todo_diff_snapshot
            else "The current ledger is shown with every observation."
        )
        sections.append(
            "## TODO ledger\n"
            "Use todo.update(id=..., kind=..., content=..., status=...) and todo.list(). "
            "Statuses are pending, attempted, changed_unverified, claimed_verified, blocked, "
            f"or delete. {snapshot}"
        )

    if features.enable_batch_actions:
        response = (
            "Emit one or more action blocks in execution order, at most 20 executable actions. "
            "Use one <tool_call>ONE CALL</tool_call> per GUI/flow action"
            + (" and one bare <bash>RAW SCRIPT</bash> per Bash action." if features.enable_bash else ".")
        )
    else:
        response = "Emit exactly one action in one <tool_call>ONE CALL</tool_call> block."
        if features.enable_bash:
            response += " A Bash step instead uses exactly one bare <bash>RAW SCRIPT</bash> block."
    if features.enable_intent:
        response = (
            "After reasoning, emit one short <intent>...</intent> naming the immediate purpose. "
            + response
        )
    sections.append("## Response format\n" + response + " Do not add prose outside these blocks.")
    sections.append(
        "## Guidelines\n"
        "Read exact values rather than guessing. Observe before acting. Batch only independent "
        "actions, stop before an action that depends on unseen results, change approach after a "
        "failed retry, commit edits, and verify the requested final state before success."
    )
    return _format_prompt("\n\n".join(sections), password=password, range_scale=range_scale)


def _literal_call(code: str) -> tuple[str, list[Any], dict[str, Any]] | None:
    try:
        tree = ast.parse(code.strip(), mode="exec")
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        return None
    call = tree.body[0].value
    if not isinstance(call, ast.Call):
        return None
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        name = f"{call.func.value.id}.{call.func.attr}"
    elif isinstance(call.func, ast.Name):
        name = call.func.id
    else:
        return None
    try:
        args = [ast.literal_eval(arg) for arg in call.args]
        kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}
    except (ValueError, TypeError):
        args, kwargs = [], {}
    return name, args, kwargs


def _normalize_status(value: Any) -> str:
    value = str(value or "success").strip().lower()
    if value in {"success", "done", "succeeded", "ok"}:
        return "success"
    if value in {"failure", "fail", "failed", "infeasible"}:
        return "failure"
    raise ValueError(f"invalid termination status: {value!r}")


def parse_simple_response(content: str, features: FeatureConfig) -> tuple[dict[str, Any], str, str]:
    """Parse model text into an executor-neutral action dictionary."""
    intent_match = _INTENT_RE.search(content)
    intent = intent_match.group(1).strip() if intent_match and features.enable_intent else ""
    think_parts = [part.strip() for part in _THINK_RE.findall(content) if part.strip()]
    actions: list[dict[str, Any]] = []
    for match in _ACTION_BLOCK_RE.finditer(content):
        bash_body = match.group("bash")
        if bash_body is not None:
            if features.enable_bash:
                actions.append({"action_type": "execute_bash", "command": bash_body.strip("\r\n")})
            continue
        body = (match.group("tool") or "").strip()
        parsed = _literal_call(body)
        if parsed is None:
            actions.append({"action_type": "pyautogui_code", "code": body})
            continue
        name, args, kwargs = parsed
        if name == "computer.screenshot":
            if features.enable_screenshot_action:
                actions.append({"action_type": "screenshot"})
        elif name == "computer.wait":
            actions.append({"action_type": "wait", "seconds": kwargs.get("seconds", args[0] if args else 20)})
        elif name == "computer.ask_user":
            if features.enable_ask_user:
                question = kwargs.get("question", args[0] if args else "")
                actions.append({"action_type": "ask_user", "question": str(question)})
        elif name == "computer.terminate":
            status = kwargs.get("status", args[0] if args else "success")
            answer = kwargs.get("answer", args[1] if len(args) > 1 else None)
            action = {"action_type": "finished", "status": _normalize_status(status)}
            if answer is not None:
                action["answer"] = str(answer)
            actions.append(action)
        elif name.startswith("todo."):
            actions.append({"action_type": "todo", "name": name, "args": args, "kwargs": kwargs})
        else:
            actions.append({"action_type": "pyautogui_code", "code": body})

    executable = [a for a in actions if a["action_type"] != "todo"]
    if not features.enable_batch_actions and len(executable) > 1:
        first = executable[0]
        actions = [a for a in actions if a["action_type"] == "todo"] + [first]
    if not actions:
        action: dict[str, Any] = {"action_type": "FAIL", "error": "no enabled action block found"}
    elif len(actions) == 1:
        action = actions[0]
    else:
        action = {"action_type": "action_sequence", "actions": actions[:20]}
    return action, "\n".join(think_parts), intent


class MaiUIAgent:
    """Stateful standalone screenshot-to-tool-call agent."""

    def __init__(
        self,
        model_name: str,
        *,
        llm_base_url: str | None = None,
        api_key: str | None = None,
        model: ModelCallable | None = None,
        features: FeatureConfig | Mapping[str, Any] | None = None,
        history_n: int = 3,
        max_steps: int = 100,
        temperature: float = 1.0,
        top_p: float = 0.95,
        max_tokens: int = 4096,
        thinking: bool = True,
        password: str = "password",
        range_scale: float = 1000,
        image_format: str = "jpeg",
        jpeg_quality: int = 85,
        max_retries: int = 3,
        retry_sleep: float = 2.0,
        request_timeout: float = 180.0,
        summary_model: SummaryCallable | None = None,
        verifier: VerifierCallable | None = None,
        image_transform: ImageTransform | None = None,
        **feature_overrides: Any,
    ) -> None:
        self.model_name = model_name
        self.llm_base_url = (llm_base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        base_features = (
            asdict(features)
            if isinstance(features, FeatureConfig)
            else dict(features or {})
        )
        unknown = set(feature_overrides) - set(FeatureConfig.__dataclass_fields__)
        if unknown:
            raise TypeError(f"unknown feature option(s): {', '.join(sorted(unknown))}")
        base_features.update(feature_overrides)
        self.features = FeatureConfig.from_mapping(base_features)
        self.history_n = max(0, int(history_n))
        self.max_steps = max(1, int(max_steps))
        self.temperature = float(temperature)
        self.top_p = float(top_p)
        self.max_tokens = max(1, int(max_tokens))
        self.thinking = bool(thinking)
        self.password = password
        self.range_scale = float(range_scale)
        self.image_format = image_format
        self.jpeg_quality = int(jpeg_quality)
        self.max_retries = max(1, int(max_retries))
        self.retry_sleep = max(0.0, float(retry_sleep))
        self.request_timeout = float(request_timeout)
        self.summary_model = summary_model
        self.verifier = verifier
        self.image_transform = image_transform
        self.instruction = ""
        self.turns: list[Turn] = []
        self.previous_summary = ""
        self.todos: dict[str, TodoItem] = {}
        self._todo_changed = False
        self._micro_cutoff = 0
        self.system_prompt = self._build_system_prompt()

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | Any) -> "MaiUIAgent":
        if isinstance(config, Mapping):
            agent = config.get("agent", config)
            runtime = dict(agent.get("runtime_conf") or config.get("runtime_conf") or {})
            get_agent = agent.get
            get_root = config.get
        else:
            agent = config
            runtime = dict(getattr(agent, "runtime_conf", None) or {})
            get_agent = lambda key, default=None: getattr(agent, key, default)
            get_root = get_agent
        features = FeatureConfig.from_mapping(runtime)
        return cls(
            model_name=str(get_agent("model_name") or get_root("model_name") or ""),
            llm_base_url=get_agent("llm_base_url") or get_root("llm_base_url"),
            api_key=get_agent("api_key") or get_root("api_key"),
            features=features,
            history_n=runtime.get("history_n", 3),
            max_steps=get_agent("max_steps", get_root("max_steps", 100)),
            temperature=runtime.get("temperature", 1.0),
            top_p=runtime.get("top_p", 0.95),
            max_tokens=runtime.get("max_tokens", 4096),
            thinking=runtime.get("thinking", True),
            password=runtime.get("password", "password"),
            range_scale=runtime.get("range_scale", runtime.get("coordinate_range", 1000)),
            image_format=runtime.get("image_format", "jpeg"),
            jpeg_quality=runtime.get("jpeg_quality", 85),
            max_retries=runtime.get("max_retries", 3),
            retry_sleep=runtime.get("retry_sleep", 2.0),
        )

    from_agent_config = from_config

    def _build_system_prompt(self) -> str:
        return build_simple_prompt(self.features, password=self.password, range_scale=self.range_scale)

    def initialize(self, instruction: str) -> None:
        self.instruction = str(instruction).strip()
        if not self.instruction:
            raise ValueError("instruction must not be empty")
        self.turns.clear()
        self.previous_summary = ""
        self.todos.clear()
        self._todo_changed = False
        self._micro_cutoff = 0

    reset = initialize

    def _image_window_start(self, current_has_image: bool) -> int:
        image_indices = [i for i, turn in enumerate(self.turns) if turn.image_url]
        total = len(image_indices) + int(current_has_image)
        if self.features.img_keep_n > 0 and self.features.img_interval > 0:
            upper = self.features.img_keep_n + self.features.img_interval
            keep = total if total <= upper else self.features.img_keep_n
        else:
            keep = max(5, self.history_n)
        historical_keep = max(0, keep - int(current_has_image))
        if historical_keep >= len(image_indices):
            return 0
        if historical_keep > 0:
            return image_indices[-historical_keep]
        return len(self.turns)

    def _todo_snapshot(self) -> str:
        if not self.features.enable_todo:
            return ""
        if self.features.todo_diff_snapshot and not self._todo_changed:
            pending = sum(item.status != "claimed_verified" for item in self.todos.values())
            return f"# TODO Ledger: {len(self.todos)} items, {pending} open"
        items = sorted(self.todos.values(), key=lambda item: (item.updated_step, item.id))
        lines = ["# TODO Ledger"]
        for item in items[-64:]:
            lines.append(f"- [{item.status}] {item.id} ({item.kind}): {item.content}")
        self._todo_changed = False
        return "\n".join(lines)

    def _observation_text(self, observation: str, execution_result: str) -> str:
        step = len(self.turns) + 1
        parts = [f"# Observation Step {step}"]
        if self.features.enable_step_hint and step >= max(1, int(self.max_steps * 0.75)):
            parts.append(f"[Step {step}/{self.max_steps}]")
        if observation:
            parts.extend(["# Observation", observation])
        if execution_result:
            parts.extend(["# Previous Action Result", execution_result])
        todo = self._todo_snapshot()
        if todo:
            parts.append(todo)
        parts.append("Generate the next action from the current evidence.")
        return "\n\n".join(parts)

    def _micro_compact(self) -> None:
        f = self.features
        if not f.micro_compaction or len(self.turns) <= f.micro_compaction_trigger_steps:
            return
        full_after_cutoff = len(self.turns) - self._micro_cutoff
        if full_after_cutoff <= f.micro_keep_turns + f.micro_interval:
            return
        cutoff = max(self._micro_cutoff, len(self.turns) - f.micro_keep_turns)
        for turn in self.turns[self._micro_cutoff:cutoff]:
            turn.observation = TOOL_RESULT_COMPACTED
            turn.execution_result = TOOL_RESULT_COMPACTED
        self._micro_cutoff = cutoff

    def _estimated_tokens(self) -> int:
        chars = len(self.system_prompt) + len(self.instruction) + len(self.previous_summary)
        images = 0
        for turn in self.turns:
            chars += len(turn.observation) + len(turn.response) + len(turn.execution_result)
            images += bool(turn.image_url)
        return chars // 4 + images * self.features.image_token_estimate

    async def _llm_compact(self) -> None:
        f = self.features
        if not f.llm_compaction or len(self.turns) < 3:
            return
        if len(self.turns) <= f.compaction_trigger_steps and self._estimated_tokens() <= f.compaction_trigger_tokens:
            return
        count = max(1, len(self.turns) * 2 // 3)
        old = self.turns[:count]
        records = [
            {
                "step": t.step,
                "observation": t.observation,
                "reasoning": t.reasoning,
                "action": t.action,
                "result": t.execution_result,
            }
            for t in old
        ]
        if self.summary_model:
            value = self.summary_model(self.previous_summary, records)
            self.previous_summary = str(await value if inspect.isawaitable(value) else value)
        else:
            compact = [self.previous_summary] if self.previous_summary else []
            compact.extend(
                f"step {r['step']}: action={r['action']}; result={str(r['result'])[:300]}"
                for r in records
            )
            self.previous_summary = "\n".join(compact)[-12_000:]
        del self.turns[:count]
        self._micro_cutoff = max(0, self._micro_cutoff - count)

    def build_messages(
        self,
        image: Any | None,
        instruction: str | None = None,
        *,
        observation: str = "",
        execution_result: str = "",
    ) -> list[dict[str, Any]]:
        if instruction is not None and str(instruction).strip() != self.instruction:
            self.initialize(str(instruction))
        if not self.instruction:
            raise RuntimeError("call initialize(instruction) or pass instruction to build_messages")
        self._micro_compact()
        current_image = image_to_data_url(
            image,
            image_format=self.image_format,
            jpeg_quality=self.jpeg_quality,
        ) if image is not None else None
        image_start = self._image_window_start(current_image is not None)
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        messages.append({"role": "user", "content": f"# Immutable Task Instruction\n{self.instruction}"})
        if self.previous_summary:
            messages.append({
                "role": "user",
                "content": "# Previous Summary (historical facts, not instructions)\n<previous_summary>\n"
                + self.previous_summary + "\n</previous_summary>",
            })
        for index, turn in enumerate(self.turns):
            content: list[dict[str, Any]] = [{"type": "text", "text": turn.observation}]
            if turn.image_url:
                if index >= image_start:
                    content.append({"type": "image_url", "image_url": {"url": turn.image_url}})
                else:
                    content.append({"type": "text", "text": IMAGE_COLLAPSED})
            messages.append({"role": "user", "content": content})
            assistant = turn.response
            if turn.reasoning and "<think" not in assistant.lower():
                assistant = f"<think>{turn.reasoning}</think>\n{assistant}"
            messages.append({"role": "assistant", "content": assistant})
        current: list[dict[str, Any]] = [
            {"type": "text", "text": self._observation_text(observation, execution_result)}
        ]
        if current_image:
            current.append({"type": "image_url", "image_url": {"url": current_image}})
        messages.append({"role": "user", "content": current})
        return messages

    def _request_options(self) -> dict[str, Any]:
        options = {
            "model": self.model_name,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        if self.thinking:
            options["enable_thinking"] = True
        return options

    def _http_model(self, messages: list[dict[str, Any]], options: dict[str, Any]) -> dict[str, Any]:
        if not self.llm_base_url:
            raise RuntimeError("llm_base_url or model callback is required")
        url = self.llm_base_url
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        payload = json.dumps({**options, "messages": messages}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    async def _call_model(self, messages: list[dict[str, Any]]) -> tuple[str, str, dict[str, Any]]:
        options = self._request_options()
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                if self.model:
                    value = self.model(messages, options)
                    raw = await value if inspect.isawaitable(value) else value
                else:
                    raw = await asyncio.to_thread(self._http_model, messages, options)
                if isinstance(raw, str):
                    return raw, "", {"content": raw}
                data = dict(raw)
                message = data
                if isinstance(data.get("choices"), list) and data["choices"]:
                    message = dict(data["choices"][0].get("message") or {})
                content = message.get("content") or ""
                if isinstance(content, list):
                    content = "".join(
                        str(block.get("text", "")) for block in content if isinstance(block, Mapping)
                    )
                reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
                return str(content), reasoning, data
            except Exception as exc:  # model gateways vary widely
                last_error = exc
                if attempt + 1 < self.max_retries:
                    await asyncio.sleep(self.retry_sleep * (2 ** attempt))
        raise RuntimeError(f"model request failed after {self.max_retries} attempts") from last_error

    def _update_todos(self, action: dict[str, Any]) -> None:
        actions = action.get("actions", []) if action.get("action_type") == "action_sequence" else [action]
        for item in actions:
            if item.get("action_type") != "todo" or item.get("name") != "todo.update":
                continue
            kwargs = item.get("kwargs") or {}
            item_id = str(kwargs.get("id") or "").strip()
            if not item_id:
                continue
            status = str(kwargs.get("status") or "pending")
            if status == "delete":
                self.todos.pop(item_id, None)
            else:
                self.todos[item_id] = TodoItem(
                    id=item_id,
                    content=str(kwargs.get("content") or self.todos.get(item_id, TodoItem(item_id, "")).content),
                    kind=str(kwargs.get("kind") or "task"),
                    status=status,
                    updated_step=len(self.turns) + 1,
                )
            self._todo_changed = True

    def _parse_response(self, content: str) -> tuple[dict[str, Any], str, str]:
        return parse_simple_response(content, self.features)

    async def _verify(self, action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if action.get("action_type") != "finished" or not self.features.enable_verifier:
            return action, None
        if self.verifier is None:
            return action, {"skipped": True, "reason": "no verifier callback configured"}
        result = self.verifier(self.instruction, tuple(self.turns), action)
        result = await result if inspect.isawaitable(result) else result
        audit = dict(result) if isinstance(result, Mapping) else {"judge": bool(result)}
        if not audit.get("judge"):
            return {
                "action_type": "wait",
                "verifier_feedback": str(audit.get("analyze") or "termination was not verified"),
            }, audit
        return action, audit

    async def step(
        self,
        image: Any | None,
        instruction: str | None = None,
        *,
        observation: str = "",
        execution_result: str = "",
    ) -> dict[str, Any]:
        if instruction is not None and str(instruction).strip() != self.instruction:
            self.initialize(str(instruction))
        await self._llm_compact()
        messages = self.build_messages(
            image,
            observation=observation,
            execution_result=execution_result,
        )
        raw_response, native_reasoning, model_response = await self._call_model(messages)
        action, inline_reasoning, intent = self._parse_response(raw_response)
        reasoning = native_reasoning or inline_reasoning
        self._update_todos(action)
        action, verifier_result = await self._verify(action)
        stored_image = image
        if (
            image is not None
            and self.features.enable_action_overlay
            and self.image_transform is not None
        ):
            stored_image = self.image_transform(image, [action])
        image_url = image_to_data_url(
            stored_image,
            image_format=self.image_format,
            jpeg_quality=self.jpeg_quality,
        ) if stored_image is not None else None
        self.turns.append(Turn(
            step=len(self.turns) + 1,
            observation=self._observation_text(observation, execution_result),
            image_url=image_url,
            response=raw_response,
            reasoning=reasoning,
            action=action,
            execution_result=execution_result,
            intent=intent,
        ))
        return AgentOutput(
            action=action,
            raw_response=raw_response,
            reasoning=reasoning,
            intent=intent,
            messages=messages,
            model_response=model_response,
            verifier=verifier_result,
        ).as_dict()

    async def select_action(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        """Compatibility adapter for harnesses that pass one observation mapping."""
        image = None
        for key in ("image", "screenshot", "screenshot_b64", "image_url"):
            if key in observation and observation[key] is not None:
                image = observation[key]
                break
        output = await self.step(
            image,
            observation.get("instruction"),
            observation=str(observation.get("text") or observation.get("observation") or ""),
            execution_result=str(
                observation.get("execution_result")
                or observation.get("tool_result")
                or observation.get("bash_output")
                or ""
            ),
        )
        return output["action"]


MAIUIAgent = MaiUIAgent
MAIUISimpleAgentPolicy = MaiUIAgent

__all__ = [
    "AgentOutput",
    "FeatureConfig",
    "MAIUIAgent",
    "MAIUISimpleAgentPolicy",
    "MaiUIAgent",
    "TodoItem",
    "Turn",
    "build_simple_prompt",
    "image_to_data_url",
    "parse_simple_response",
]
