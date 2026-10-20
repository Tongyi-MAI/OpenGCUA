"""Rich, normal-scrollback terminal frontend for the macOS agent."""

from __future__ import annotations

from collections.abc import Callable
import os
from typing import Literal

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from agent_events import AgentEvent, AgentPrompt


UiMode = Literal["tui", "plain"]


def resolve_ui_mode(
    requested: str,
    *,
    stdin_tty: bool,
    stdout_tty: bool,
    term: str | None,
) -> UiMode:
    """Resolve auto mode without importing or initializing a terminal application."""

    if requested == "plain":
        return "plain"
    if requested == "tui":
        return "tui"
    usable_term = bool(term and term.lower() != "dumb")
    return "tui" if stdin_tty and stdout_tty and usable_term else "plain"


def _prompt_session() -> PromptSession:
    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _newline(event) -> None:
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=InMemoryHistory(),
        key_bindings=bindings,
        multiline=False,
        enable_history_search=True,
    )


class RichTerminalFrontend:
    """Semantic event renderer with an asynchronous, history-aware prompt."""

    interactive = True

    def __init__(
        self,
        *,
        console: Console | None = None,
        prompt_session=None,
        clear_screen: Callable[[], None] | None = None,
    ) -> None:
        self.console = console or Console()
        self._session = prompt_session or _prompt_session()
        self._owns_prompt_session = prompt_session is None
        self._clear_screen = clear_screen or self.console.clear
        self.show_reasoning = False
        self.show_code = False
        self.session_id = ""
        self.model = ""
        self.phase = "waiting"
        self.step: int | None = None
        self.turn_active = False
        self._last_phase: tuple[int | None, str] | None = None
        self._reasoning_open = False
        self._intent_open = False
        self._code_open = False
        self._code_step: int | None = None
        self._code_block: int | None = None
        self._code_preview: dict[int, str] = {}
        self._abort_callback: Callable[[], None] | None = None
        self._abort_requested = False

    def bind_abort(self, callback: Callable[[], None]) -> None:
        self._abort_callback = callback

    def handle_sigint(self) -> None:
        """Request the existing turn abort; prompt-time Ctrl-C stays prompt-local."""

        if not self.turn_active:
            raise KeyboardInterrupt
        if self._abort_requested:
            return
        self._abort_requested = True
        if self._abort_callback is not None:
            self._abort_callback()
        self.console.print(
            "[yellow]正在请求中止当前轮次；已经发出的操作不会回滚。[/yellow]"
        )

    def _toolbar(self):
        step = "-" if self.step is None else str(self.step)
        detail = f"session {self.session_id or '-'}  ·  step {step}  ·  {self.phase}"
        if self.model:
            detail += f"  ·  {self.model}"
        return FormattedText([
            ("class:bottom-toolbar", detail),
            ("", "  ·  Alt+Enter newline  ·  /help"),
        ])

    def _phase_event(self, event: AgentEvent) -> None:
        self.phase = str(event.payload.get("phase") or event.text or "working")
        self.step = event.step
        active_phases = {"planning", "compaction", "execution", "postprocess", "recovery"}
        self.turn_active = self.phase in active_phases
        if self.turn_active:
            self._abort_requested = False
        marker = (event.step, self.phase)
        if marker == self._last_phase:
            return
        self._last_phase = marker
        label = f"Step {event.step:03d}" if event.step is not None else "Agent"
        self.console.print(f"[bold cyan]● {label}[/bold cyan]  {self.phase}")

    def emit(self, event: AgentEvent) -> None:
        kind = event.kind
        if kind == "session":
            self.session_id = str(event.payload.get("session_id") or "")
            self.model = str(event.payload.get("model") or "")
            mode = str(event.payload.get("mode") or "balanced")
            body = Text.assemble(
                (f"{self.model or 'model not set'}", "bold"),
                f"  ·  {mode} mode\n",
                f"Session {self.session_id or '-'}",
            )
            self.console.print(Panel(body, title="OpenGCUA", border_style="cyan"))
            return
        if kind == "trajectory":
            path = str(event.payload.get("path") or event.text)
            self.console.print(f"[dim]Trajectory:[/dim] {path}")
            return
        if kind == "phase":
            self._phase_event(event)
            return
        if kind == "reasoning_delta":
            if not self.show_reasoning:
                return
            if not self._reasoning_open:
                self.console.print("[dim italic]Reasoning[/dim italic]")
                self._reasoning_open = True
            self.console.print(event.text, end="", style="dim")
            return
        if kind == "reasoning_end":
            if self.show_reasoning and self._reasoning_open:
                self.console.print()
            self._reasoning_open = False
            return
        if kind == "intent_delta":
            if not self._intent_open:
                self.console.print("[bold magenta]Intent[/bold magenta] ", end="")
                self._intent_open = True
            self.console.print(event.text, end="")
            return
        if kind == "intent_end":
            if self._intent_open:
                self.console.print()
            self._intent_open = False
            return
        if kind == "code_reset":
            if self._code_open and not self._code_preview.get(self._code_block or 1, "").endswith("\n"):
                self.console.print()
            self._code_open = False
            self._code_step = event.step
            self._code_block = None
            self._code_preview.clear()
            return
        if kind == "code_start":
            if not self.show_code:
                return
            if self._code_open and not self._code_preview.get(self._code_block or 1, "").endswith("\n"):
                self.console.print()
            if self._code_step != event.step:
                self._code_preview.clear()
            self._code_step = event.step
            self._code_block = int(event.payload.get("block_index") or 1)
            self._code_preview[self._code_block] = ""
            self._code_open = True
            self.console.print("[dim]Code preview[/dim]")
            return
        if kind == "code_delta":
            if self.show_code and self._code_open and self._code_step == event.step:
                block = self._code_block or 1
                self._code_preview[block] = self._code_preview.get(block, "") + event.text
                self.console.print(Text(event.text), end="", soft_wrap=True)
            return
        if kind == "code_end":
            if self._code_open and not self._code_preview.get(self._code_block or 1, "").endswith("\n"):
                self.console.print()
            self._code_open = False
            self._code_block = None
            return
        if kind == "action":
            intent = str(event.payload.get("intent") or event.text or "Action")
            self.console.print(Panel(intent, title=f"Step {event.step or 0} · Action", border_style="magenta"))
            code = str(event.payload.get("code") or "")
            streamed_code = "\n".join(self._code_preview[index].strip()
                                      for index in sorted(self._code_preview))
            if self.show_code and code and (self._code_step != event.step or streamed_code != code):
                self.console.print(Syntax(code, "python", line_numbers=False, word_wrap=True))
            self._code_preview.clear()
            return
        if kind == "execution":
            status = event.payload.get("status") or {}
            state = str(status.get("execution_state") or "completed")
            if event.payload.get("timed_out"):
                state = "timed out"
            style = "red" if state in {"error", "failed", "timed out"} else "green"
            self.console.print(f"[{style}]Execution: {state}[/{style}]")
            return
        if kind == "question":
            return
        if kind == "answer":
            self.turn_active = False
            self.phase = "waiting"
            self.console.print(Panel(Markdown(event.text), title="Agent", border_style="green"))
            return
        if kind == "aborted":
            self.turn_active = False
            self.phase = "waiting"
            self._abort_requested = False
            text = event.text
            if "不会回滚" not in text:
                text = text.rstrip("。") + "；已经发生的操作不会回滚。"
            self.console.print(Panel(text, title="Turn aborted", border_style="yellow"))
            return
        if kind == "warning":
            self.console.print(Panel(event.text, title="Warning", border_style="yellow"))
            return
        if kind == "error":
            self.console.print(Panel(event.text[:2000], title="Error", border_style="red"))
            return
        if kind == "finished":
            self.turn_active = False
            self.phase = event.text
            answer = str(event.payload.get("answer") or "")
            path = str(event.payload.get("trajectory") or "")
            body = f"Status: {event.text}"
            if answer and not event.payload.get("answer_displayed"):
                body += f"\n\n{answer}"
            if path:
                body += f"\n\nTrajectory: {path}"
            self.console.print(Panel(body, title="Finished", border_style="green"))
            return
        if kind == "resume_hint":
            self.console.print(f"[yellow]Resume:[/yellow] {event.text}")
            return
        if kind == "ready":
            self.console.print("[green]Ready[/green]")
            return
        if kind == "notice":
            if event.payload.get("role") == "user":
                self.console.print(Panel(event.text.removeprefix("你> "), title="You", border_style="blue"))
            else:
                self.console.print(event.text)
            return

        # Forward compatibility: only the public text is safe to render. Payloads may
        # contain provider-specific credentials or request metadata.
        if event.text:
            self.console.print(event.text)

    def _help(self) -> None:
        self.console.print(Panel(
            "Enter 发送 · Alt/Option+Enter 换行 · Ctrl-D 退出\n"
            "/new <消息> 新任务 · /reasoning on|off · /code on|off · /clear · /exit",
            title="Terminal commands",
            border_style="cyan",
        ))

    def _local_command(self, value: str) -> bool:
        command = value.strip().lower()
        if command == "/help":
            self._help()
            return True
        if command == "/clear":
            self._clear_screen()
            return True
        for prefix, attribute, label in (
            ("/reasoning", "show_reasoning", "Reasoning"),
            ("/code", "show_code", "Code"),
        ):
            if command == prefix or command.startswith(prefix + " "):
                parts = command.split()
                if len(parts) == 2 and parts[1] in {"on", "off"}:
                    enabled = parts[1] == "on"
                    setattr(self, attribute, enabled)
                    self.console.print(f"{label}: {'shown' if enabled else 'hidden'} for future events")
                else:
                    self.console.print(f"[yellow]Usage: {prefix} on|off[/yellow]")
                return True
        return False

    async def read(self, prompt: AgentPrompt) -> str | None:
        self.turn_active = False
        self.phase = "waiting"
        if prompt.kind == "question":
            self.console.print(Panel(prompt.text, title="Agent needs input", border_style="yellow"))
        elif prompt.kind == "retry":
            self.console.print(Panel(prompt.text, title="Model request failed", border_style="red"))

        while True:
            try:
                if self._owns_prompt_session:
                    with patch_stdout(raw=True):
                        value = await self._session.prompt_async(
                            "❯ ", bottom_toolbar=self._toolbar,
                        )
                else:
                    value = await self._session.prompt_async(
                        "❯ ", bottom_toolbar=self._toolbar,
                    )
            except (EOFError, KeyboardInterrupt):
                return None
            command = value.strip().lower()
            if command in {"/exit", "/quit", "exit", "quit", "q"}:
                return None
            if self._local_command(value):
                continue
            if command:
                return value
            if prompt.kind == "retry":
                return ""
            if prompt.empty_exits:
                return None

    def close(self) -> None:
        return None


def build_rich_frontend() -> RichTerminalFrontend:
    """Factory kept separate so plain mode never initializes prompt-toolkit."""

    return RichTerminalFrontend()


__all__ = ["RichTerminalFrontend", "build_rich_frontend", "resolve_ui_mode"]
