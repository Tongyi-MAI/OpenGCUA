"""Standalone MAIUI code-action agent.

The class in this module emits an executor-neutral ``code_exec`` dictionary. It
does not execute Python, shell commands, GUI actions, screenshots, user prompts,
or Web searches; a host can implement those helpers however it chooses.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import asdict
from typing import Any, Mapping

try:
    from .maiui_agent import (
        FeatureConfig,
        MaiUIAgent,
        TodoItem,
        _claude_pattern_prompt,
        _format_prompt,
        _notice_prompt,
    )
except ImportError:  # Support direct loading from this directory.
    from maiui_agent import (  # type: ignore
        FeatureConfig,
        MaiUIAgent,
        TodoItem,
        _claude_pattern_prompt,
        _format_prompt,
        _notice_prompt,
    )

_CODE_BLOCK_RE = re.compile(r"<code>(.*?)</code>", re.I | re.S)
_CODE_OPEN_RE = re.compile(r"<code>", re.I)
_FENCE_RE = re.compile(r"```(?:python3?|py|code)?\s*(.*?)```", re.I | re.S)
_INTENT_RE = re.compile(r"<intent>(.*?)</intent>", re.I | re.S)
_THINK_RE = re.compile(r"<(?:think|thinking)>(.*?)</(?:think|thinking)>", re.I | re.S)
_LEGACY_ACTION_RE = re.compile(r"<(?:tool_call\b[^>]*|bash)>", re.I)

DEFAULT_CODE_EXEC_TIMEOUT_S = 300.0
DEFAULT_PYAUTOGUI_PAUSE = 0.5


def build_code_prompt(
    features: FeatureConfig,
    *,
    password: str = "password",
    range_scale: float = 1000,
    code_exec_timeout_s: float = DEFAULT_CODE_EXEC_TIMEOUT_S,
    pyautogui_pause: float = DEFAULT_PYAUTOGUI_PAUSE,
    enable_web_search: bool = False,
) -> str:
    """Compose the code-action prompt without environment-specific imports."""
    screenshot_mode = (
        "A screenshot is attached only when screenshot() was requested in the previous script."
        if features.enable_screenshot_action
        else "A current screenshot is attached automatically at every step."
    )
    sections = [
        "You are a computer-use agent operating a desktop. Each step contains the immutable "
        "task instruction, the current observation, prior script output, and optional history. "
        f"{screenshot_mode} Respond with Python code until the task is complete. "
        "The computer password is {password}."
    ]
    if features.enable_claude_thinking_pattern:
        sections.append(_claude_pattern_prompt())
    if features.enable_notice:
        sections.append(_notice_prompt())

    helpers = [
        "- pyautogui: coordinate-aware GUI module.",
        "- terminate(status='success'|'failure', answer=None): end the task.",
    ]
    if features.enable_bash:
        helpers.append(
            "- bash(cmd, timeout=120, echo=True): execute one shell command and return an object "
            "with stdout, stderr, returncode, ok, and timed_out."
        )
    if features.enable_screenshot_action:
        helpers.append("- screenshot(): request the screenshot delivered with the next observation.")
    else:
        helpers.append("- screenshot(): return a PIL image for in-script inspection; attachment is automatic.")
    if features.enable_ask_user:
        helpers.append("- ask_user(question): ask one essential concise question and stop this script.")
    if features.enable_todo:
        helpers.append("- todo.update(...) / todo.list(): maintain the persistent work ledger.")
    if enable_web_search:
        helpers.append(
            "- web_search(query, num=5): search the public Web; query must be a literal string."
        )
    sections.append(
        "## Code execution\n"
        "The <code> script runs top-to-bottom in a fresh Python process. Variables, imports, "
        "working-directory changes, and handles do not survive the step; files, application "
        "state, and background processes do. Printed output and tracebacks arrive in the next "
        f"observation. The host may stop the script after {int(code_exec_timeout_s)} seconds.\n"
        "Available names are pyautogui, time, os, sys, re, json, subprocess, Image, and only "
        "the enabled helpers below:\n" + "\n".join(helpers)
    )
    sections.append(
        "## GUI coordinates\n"
        "Every x/y, region, offset, and returned position is normalized to [0, {range_scale}] "
        "on each axis; ({range_half}, {range_half}) is screen center. Never emit raw pixel "
        "coordinates without converting them. pyautogui pauses "
        f"{pyautogui_pause:g} seconds after each call."
    )

    rules = [
        "- Inspect before acting; do not guess controls or values you have not observed.",
        "- Keep blind GUI chains short and stop before a decision that depends on unseen output.",
        "- Verify final state before terminate('success'); change approach after a failed retry.",
    ]
    if features.enable_batch_actions:
        rules.insert(
            1,
            "- Batch independent keystrokes, form filling, loops, and checks in one script when "
            "no intermediate observation is needed.",
        )
    else:
        rules.insert(1, "- Emit one executable top-level operation per script.")
    if features.enable_bash:
        rules.append(
            "- Prefer bash() when it can do the job reliably; never type shell commands into a GUI terminal."
        )
    if features.enable_screenshot_action:
        rules.append(
            "- When the next decision needs the visible result, call screenshot() once as the final statement."
        )
    sections.append("## Writing a step\n" + "\n".join(rules))

    if features.enable_todo:
        snapshot = (
            "A full snapshot follows ledger changes; unchanged steps receive a compact status."
            if features.todo_diff_snapshot
            else "The ledger snapshot is included with every observation."
        )
        sections.append(
            "## TODO ledger\n"
            "Use todo.update(id=..., kind=..., content=..., status=...) or todo.list(). "
            "Statuses: pending, attempted, changed_unverified, claimed_verified, blocked, delete. "
            + snapshot
        )
    if enable_web_search:
        sections.append(
            "## Web search\nUse web_search only for public information absent from the machine. "
            "Treat snippets as leads and verify important facts at their source."
        )

    response = "Decide fully in reasoning first. Then "
    if features.enable_intent:
        response += "emit one short <intent>...</intent> naming the immediate purpose, followed by "
    else:
        response += "emit "
    response += (
        "exactly one <code>...</code> block containing plain Python, no Markdown fence, and "
        "nothing after </code>."
    )
    sections.append("## Response format\n" + response)
    return _format_prompt("\n\n".join(sections), password=password, range_scale=range_scale)


def _clean_code(code: str) -> str:
    return textwrap.dedent(code.replace("\r\n", "\n")).strip("\n")


def _extract_code(content: str) -> tuple[str, str, str]:
    """Return ``(code, source, error)``; the last complete block wins."""
    matches = list(_CODE_BLOCK_RE.finditer(content))
    source = "code"
    if matches and _CODE_OPEN_RE.search(content, matches[-1].end()):
        return "", "", "the final <code> block is not closed"
    if not matches:
        if _CODE_OPEN_RE.search(content):
            return "", "", "an opening <code> tag has no closing </code> tag"
        matches = list(_FENCE_RE.finditer(content))
        source = "fence"
    if not matches:
        if _LEGACY_ACTION_RE.search(content):
            return "", "", "tool_call/bash blocks are invalid in code mode"
        return "", "", "no <code> block found"
    code = _clean_code(matches[-1].group(1))
    if not code:
        return "", source, "the code block is empty"
    try:
        compile(code, "<code>", "exec")
    except (SyntaxError, ValueError) as exc:
        return "", source, f"invalid Python: {exc}"
    return code, source, ""


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            names.add(f"{node.func.value.id}.{node.func.attr}")
    return names


def _termination_request(code: str) -> dict[str, Any] | None:
    """Convert a standalone literal terminate(...) call to a finished action."""
    tree = ast.parse(code, mode="exec")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        return None
    node = tree.body[0].value
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "terminate":
        return None
    try:
        args = [ast.literal_eval(value) for value in node.args]
        kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}
    except (ValueError, TypeError):
        return None
    status = str(kwargs.get("status", args[0] if args else "success")).lower()
    status = "success" if status in {"success", "done", "succeeded", "ok"} else "failure"
    answer = kwargs.get("answer", args[1] if len(args) > 1 else None)
    action: dict[str, Any] = {"action_type": "finished", "status": status}
    if answer is not None:
        action["answer"] = str(answer)
    return action


def parse_code_response(
    content: str,
    features: FeatureConfig,
    *,
    code_exec_timeout_s: float = DEFAULT_CODE_EXEC_TIMEOUT_S,
    pyautogui_pause: float = DEFAULT_PYAUTOGUI_PAUSE,
    range_scale: float = 1000,
    screen_size: tuple[int, int] = (1920, 1080),
    enable_web_search: bool = False,
    todo_items: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Parse and capability-check a model-authored code block."""
    intent_match = _INTENT_RE.search(content)
    intent = intent_match.group(1).strip() if intent_match and features.enable_intent else ""
    reasoning = "\n".join(part.strip() for part in _THINK_RE.findall(content) if part.strip())
    body = _THINK_RE.sub("", content)
    code, source, error = _extract_code(body)
    if error:
        return {"action_type": "FAIL", "error": error}, reasoning, intent

    tree = ast.parse(code, mode="exec")
    calls = _called_names(tree)
    blocked: list[str] = []
    if "bash" in calls and not features.enable_bash:
        blocked.append("bash")
    if "ask_user" in calls and not features.enable_ask_user:
        blocked.append("ask_user")
    if "web_search" in calls and not enable_web_search:
        blocked.append("web_search")
    if any(name.startswith("todo.") for name in calls) and not features.enable_todo:
        blocked.append("todo")
    if "screenshot" in calls and features.enable_screenshot_action is False:
        # In automatic mode screenshot() remains valid for in-script PIL inspection.
        pass
    if blocked:
        return {
            "action_type": "FAIL",
            "error": "disabled helper(s): " + ", ".join(sorted(set(blocked))),
        }, reasoning, intent
    if not features.enable_batch_actions and len(tree.body) > 1:
        return {
            "action_type": "FAIL",
            "error": "multiple top-level statements require enable_batch_actions=True",
        }, reasoning, intent

    finished = _termination_request(code)
    if finished is not None:
        return finished, reasoning, intent
    return {
        "action_type": "code_exec",
        "code": code,
        "code_source": source,
        "runtime": {
            "screen_w": int(screen_size[0]),
            "screen_h": int(screen_size[1]),
            "range_scale": float(range_scale),
            "infer_scale": True,
            "todo_items": list(todo_items or []),
            "pyautogui_pause": max(0.0, float(pyautogui_pause)),
            "enabled_helpers": {
                "bash": features.enable_bash,
                "ask_user": features.enable_ask_user,
                "screenshot_action": features.enable_screenshot_action,
                "todo": features.enable_todo,
                "web_search": bool(enable_web_search),
            },
        },
        "timeout_s": max(5.0, float(code_exec_timeout_s)),
    }, reasoning, intent


class MaiUICodeAgent(MaiUIAgent):
    """Standalone MAIUI agent whose model action is one Python script."""

    def __init__(
        self,
        model_name: str,
        *,
        code_exec_timeout_s: float = DEFAULT_CODE_EXEC_TIMEOUT_S,
        pyautogui_pause: float = DEFAULT_PYAUTOGUI_PAUSE,
        enable_web_search: bool = False,
        screen_size: tuple[int, int] = (1920, 1080),
        **kwargs: Any,
    ) -> None:
        self.code_exec_timeout_s = max(5.0, float(code_exec_timeout_s))
        self.pyautogui_pause = max(0.0, float(pyautogui_pause))
        self.enable_web_search = bool(enable_web_search)
        self.screen_size = (int(screen_size[0]), int(screen_size[1]))
        super().__init__(model_name, **kwargs)
        self.system_prompt = self._build_system_prompt()

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | Any) -> "MaiUICodeAgent":
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
        screen = runtime.get("screen_size", (1920, 1080))
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
            code_exec_timeout_s=runtime.get("code_exec_timeout_s", DEFAULT_CODE_EXEC_TIMEOUT_S),
            pyautogui_pause=runtime.get("pyautogui_pause", DEFAULT_PYAUTOGUI_PAUSE),
            enable_web_search=runtime.get("enable_web_search", False),
            screen_size=(int(screen[0]), int(screen[1])),
        )

    from_agent_config = from_config

    def _build_system_prompt(self) -> str:
        return build_code_prompt(
            self.features,
            password=self.password,
            range_scale=self.range_scale,
            code_exec_timeout_s=self.code_exec_timeout_s,
            pyautogui_pause=self.pyautogui_pause,
            enable_web_search=self.enable_web_search,
        )

    def _parse_response(self, content: str) -> tuple[dict[str, Any], str, str]:
        todo_items = [asdict(item) for item in self.todos.values()] if self.features.enable_todo else []
        return parse_code_response(
            content,
            self.features,
            code_exec_timeout_s=self.code_exec_timeout_s,
            pyautogui_pause=self.pyautogui_pause,
            range_scale=self.range_scale,
            screen_size=self.screen_size,
            enable_web_search=self.enable_web_search,
            todo_items=todo_items,
        )

    def _update_todos(self, action: dict[str, Any]) -> None:
        if action.get("action_type") != "code_exec" or not self.features.enable_todo:
            return
        try:
            tree = ast.parse(str(action.get("code", "")), mode="exec")
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "todo":
                continue
            if node.func.attr != "update":
                continue
            try:
                kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}
            except (ValueError, TypeError):
                continue
            item_id = str(kwargs.get("id") or "").strip()
            if not item_id:
                continue
            status = str(kwargs.get("status") or "pending")
            if status == "delete":
                self.todos.pop(item_id, None)
            else:
                previous = self.todos.get(item_id)
                self.todos[item_id] = TodoItem(
                    id=item_id,
                    content=str(kwargs.get("content") or (previous.content if previous else "")),
                    kind=str(kwargs.get("kind") or (previous.kind if previous else "task")),
                    status=status,
                    updated_step=len(self.turns) + 1,
                )
            self._todo_changed = True


MAIUICodeAgent = MaiUICodeAgent
MAIUICodeAgentPolicy = MaiUICodeAgent

__all__ = [
    "DEFAULT_CODE_EXEC_TIMEOUT_S",
    "DEFAULT_PYAUTOGUI_PAUSE",
    "MAIUICodeAgent",
    "MAIUICodeAgentPolicy",
    "MaiUICodeAgent",
    "build_code_prompt",
    "parse_code_response",
]
