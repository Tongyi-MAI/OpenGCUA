"""Persistent terminal conversation with an always-available input area."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import sys
import time
from typing import Literal

from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion, CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from agent_events import AgentEvent, AgentPrompt, ConsentChoice, ForegroundApprovalPrompt
from terminal_interaction import TerminalInteraction


UiMode = Literal["tui", "plain"]


class SlashCommandCompleter(Completer):
    """Complete local terminal commands without touching ordinary chat text."""

    _COMMANDS = (
        ("/help", "查看命令和快捷键", False),
        ("/new", "开始新任务：/new <消息>", True),
        ("/queue", "查看或管理排队消息", False),
        ("/status", "查看当前会话状态", False),
        ("/reasoning", "显示或隐藏后续 reasoning", True),
        ("/code", "显示或隐藏后续代码", True),
        ("/clear", "清空可见输出，不清除会话", False),
        ("/exit", "退出程序", False),
    )
    _ARGUMENTS = {
        "/queue": ("send", "clear"),
        "/reasoning": ("on", "off"),
        "/code": ("on", "off"),
    }

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        value = document.text_before_cursor
        if document.cursor_position != len(document.text) or "\n" in value or not value.startswith("/"):
            return
        if " " not in value:
            for name, description, requires_argument in self._COMMANDS:
                if name.startswith(value):
                    yield Completion(
                        name + (" " if requires_argument else ""),
                        start_position=-len(value), display=name, display_meta=description,
                    )
            return
        name, argument = value.split(" ", 1)
        if " " in argument:
            return
        for option in self._ARGUMENTS.get(name, ()):
            if option.startswith(argument):
                yield Completion(option, start_position=-len(argument), display=option)


class ConversationLexer(Lexer):
    """Give conversation roles priority over operational details."""

    _LABELS = (
        ("You", "conversation.user"),
        ("Agent", "conversation.agent"),
        ("Intent:", "conversation.progress"),
        ("Execution:", "conversation.warning"),
        ("GUI permission:", "conversation.warning"),
        ("warning:", "conversation.warning"),
        ("error:", "conversation.error"),
        ("Step ", "conversation.muted"),
        ("Reasoning:", "conversation.muted"),
        ("Code preview:", "conversation.muted"),
    )

    def lex_document(self, document: Document):
        def line_fragments(line_number: int):
            line = document.lines[line_number]
            for prefix, style in self._LABELS:
                if line.startswith(prefix):
                    return [(f"class:{style}", prefix), ("", line[len(prefix):])]
            return [("", line)]
        return line_fragments


TERMINAL_STYLE = Style.from_dict({
    "conversation": "bg:#111a24 #dae2ed",
    "conversation.user": "bold #75c6ff",
    "conversation.agent": "bold #7de0b3",
    "conversation.progress": "#8fa4bb",
    "conversation.warning": "bold #edbd72",
    "conversation.error": "bold #f38c8c",
    "conversation.muted": "#748398",
    "header": "bg:#1d2a36 #dce9f4",
    "header.brand": "bold #7bd5d0",
    "header.phase": "bold #9dd9b5",
    "header.running": "bold #10221c bg:#84efb7",
    "header.permission": "bold #30200a bg:#ffd080",
    "header.meta": "#a7b6c8",
    "status": "bg:#253340 #aebdd0",
    "composer.title": "bg:#172531 #72c7d6 bold",
    "composer": "bg:#172531 #e7edf5",
    "composer.prompt": "bold #72d5cf",
    "hint": "bg:#172531 #91a3b7",
    "approval": "bg:#49371c #fff0d1 bold",
    "approval.selected": "bg:#fff0d1 #30200a bold",
    "approval.unselected": "bg:#49371c #fff0d1",
    "completion-menu": "bg:#243547 #dfeaf5",
    "completion-menu.completion.current": "bg:#40617e #ffffff bold",
    "completion-menu.meta.completion": "bg:#243547 #a5b8c9",
    "completion-menu.meta.completion.current": "bg:#40617e #ffffff",
})


def resolve_ui_mode(
    requested: str, *, stdin_tty: bool, stdout_tty: bool, term: str | None,
) -> UiMode:
    if requested == "plain":
        return "plain"
    if requested == "tui":
        return "tui"
    return "tui" if stdin_tty and stdout_tty and term and term.lower() != "dumb" else "plain"


class RichTerminalFrontend:
    """A full-session prompt_toolkit app; streamed output never writes into input."""

    interactive = True
    _MAX_TRANSCRIPT = 250_000

    def __init__(self) -> None:
        self.show_reasoning = False
        self.show_code = False
        self.session_id = ""
        self.model = ""
        self.phase = "waiting"
        self.step: int | None = None
        self.foreground_consent = "unset"
        self._transcript = ""
        self._interaction = TerminalInteraction()
        self._input = TextArea(
            height=3, multiline=True, wrap_lines=True,
            prompt=[("class:composer.prompt", " ❯ ")], style="class:composer",
            completer=SlashCommandCompleter(), complete_while_typing=True,
        )
        self._output = TextArea(
            read_only=True, scrollbar=False, wrap_lines=True, focusable=False,
            lexer=ConversationLexer(), style="class:conversation",
        )
        self._header = Window(height=1, content=FormattedTextControl(self._header_text), style="class:header")
        self._status = Window(height=1, content=FormattedTextControl(lambda: self.status_text), style="class:status")
        self._composer_title = Window(
            height=1, content=FormattedTextControl("  MESSAGE  ─────────────────────────────"),
            style="class:composer.title",
        )
        self._choice = Window(height=1, content=FormattedTextControl(self._choice_text), style="class:hint")
        self._approval_card = Window(
            height=4, content=FormattedTextControl(self._approval_card_text, focusable=True),
            style="class:approval", wrap_lines=False,
        )
        self._normal_composer = ConditionalContainer(
            HSplit([self._composer_title, self._input, self._choice]),
            filter=Condition(lambda: self._approval is None),
        )
        self._approval_composer = ConditionalContainer(
            self._approval_card, filter=Condition(lambda: self._approval is not None),
        )
        self._application = Application(
            layout=Layout(
                FloatContainer(
                    HSplit([self._header, self._output, self._status,
                            self._normal_composer, self._approval_composer]),
                    [Float(xcursor=True, ycursor=True, transparent=True,
                           content=CompletionsMenu(max_height=8))],
                ),
                focused_element=self._input,
            ),
            key_bindings=self._bindings(), full_screen=True, mouse_support=False,
            style=TERMINAL_STYLE,
        )
        self._app_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._events: asyncio.Queue[AgentEvent | None] | None = None
        self._event_task: asyncio.Task | None = None
        self._pending_read: asyncio.Future[str | None] | None = None
        self._pending_kind: str | None = None
        self._saved_draft: str | None = None
        self._approval: ForegroundApprovalPrompt | None = None
        self._approval_answer: asyncio.Future[ConsentChoice] | None = None
        self._approval_index = 0  # Reject is the default.
        self._turn_started_at: float | None = None
        self._last_progress_at: float | None = None
        self._model_status = ""
        self._abort_callback: Callable[[bool], None] | None = None
        self._abort_requested = False
        self._pending_correction: str | None = None
        self._ready_direct: deque[str] = deque()
        self._code_open = False
        self._code_step: int | None = None
        self._code_preview: dict[int, str] = {}
        self._code_block = 1
        self._reasoning_open = False
        self._intent_open = False
        self._last_intent = ""
        self._last_intent_step: int | None = None
        self._initial_user_text: str | None = None
        self._exit_requested = False
        self._closed = False
        self.terminal_error: BaseException | None = None

    @property
    def transcript(self) -> str:
        return self._transcript

    @property
    def turn_active(self) -> bool:
        return self._interaction.turn_active

    @property
    def status_text(self) -> str:
        step = "-" if self.step is None else str(self.step)
        queued = len(self._interaction.queued_messages()) + len(self._ready_direct)
        if self._pending_correction:
            queued += 1
        suspended = " (paused)" if self._interaction.queue_suspended else ""
        if self.turn_active and self._turn_started_at is not None:
            now = time.monotonic()
            elapsed = max(0, int(now - self._turn_started_at))
            elapsed_text = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
            silence = max(0, int(now - (self._last_progress_at or now)))
            detail = f"  ·  {self._model_status}" if self._model_status else ""
            if silence >= 20:
                detail += f"  ·  no update {silence}s"
            return (f"  RUNNING {elapsed_text}  ·  step {step}{detail}"
                    f"  ·  queued {queued}{suspended}  ·  foreground: {self.foreground_consent} ")
        return f"  step {step}  ·  queued {queued}{suspended}  ·  foreground: {self.foreground_consent} "

    def _header_text(self):
        labels = {
            "waiting": "Ready", "planning": "Thinking", "execution": "Acting",
            "postprocess": "Checking", "compaction": "Summarizing",
            "recovery": "Recovering", "interrupting": "Stopping",
            "waiting for input": "Needs input", "waiting for approval": "Permission needed",
        }
        phase = labels.get(self.phase, self.phase.replace("_", " ").title())
        if self._approval is not None:
            badge = ("class:header.permission", "   ● PERMISSION REQUIRED ")
        elif self.turn_active:
            badge = ("class:header.running", "   ● RUNNING ")
        else:
            badge = ("class:header.phase", "   ● Ready")
        return [
            ("class:header.brand", "  OpenGCUA"),
            badge,
            ("class:header.phase", f"  {phase}" if self.turn_active else ""),
            ("class:header.meta", f"   {self.model}" if self.model else ""),
        ]

    def _approval_card_text(self):
        choices = (
            (("class:approval.selected", " ❯ 拒绝（默认） "),
             ("class:approval.unselected", "   允许本次任务 "))
            if self._approval_index == 0 else
            (("class:approval.unselected", "   拒绝 "),
             ("class:approval.selected", " ❯ 允许本次任务 "))
        )
        return [
            ("class:approval", "  ⚠ 前台键鼠授权 · 等待你的选择\n"),
            ("class:approval", "  Agent 可接管鼠标和键盘，直到本次任务结束\n"),
            *choices,
            ("class:approval", "\n  ←/→ 或 Tab 选择 · Enter 确认 · Ctrl-C 取消"),
        ]

    def _choice_text(self) -> str:
        if self._approval is None:
            return "  Enter send  ·  Alt+Enter newline  ·  Ctrl-C stop  ·  / commands "
        choices = ("[Reject]  Allow", "Reject  [Allow]")
        return f" GUI permission · {choices[self._approval_index]} · Arrow/Tab select, Enter confirm "

    def _bindings(self) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("enter", eager=True)
        def submit(_event) -> None:
            self._submit_enter()

        @keys.add("escape", "enter")
        def newline(_event) -> None:
            if self._approval is None:
                self._input.buffer.insert_text("\n")

        @keys.add("c-c", eager=True)
        def interrupt(_event) -> None:
            self.handle_sigint()

        @keys.add("c-d")
        def exit_session(_event) -> None:
            self._request_exit()

        @keys.add("tab", filter=Condition(lambda: self._approval is not None))
        def toggle(_event) -> None:
            self._select_approval(1 - self._approval_index)

        @keys.add("tab", filter=Condition(lambda: self._approval is None))
        def complete(_event) -> None:
            self._complete_slash()

        @keys.add("down", filter=Condition(lambda: self._slash_menu_open()))
        def next_completion(_event) -> None:
            self._input.buffer.complete_next()

        @keys.add("up", filter=Condition(lambda: self._slash_menu_open()))
        def previous_completion(_event) -> None:
            self._input.buffer.complete_previous()

        @keys.add("left", filter=Condition(lambda: self._approval is not None))
        def select_reject(_event) -> None:
            self._select_approval(0)

        @keys.add("right", filter=Condition(lambda: self._approval is not None))
        def select_allow(_event) -> None:
            self._select_approval(1)

        return keys

    def _slash_menu_open(self) -> bool:
        return self._approval is None and self._input.buffer.complete_state is not None

    def _complete_slash(self) -> None:
        buffer = self._input.buffer
        if buffer.complete_state is not None and buffer.complete_state.current_completion is not None:
            completion = buffer.complete_state.current_completion
        else:
            document = buffer.document
            completion = next(iter(buffer.completer.get_completions(
                document, CompleteEvent(completion_requested=True))), None)
        if completion is not None:
            buffer.apply_completion(completion)
            self._invalidate()

    def start(self) -> None:
        if self._closed or self._app_task is not None:
            return
        loop = asyncio.get_running_loop()
        self._events = asyncio.Queue()
        self._event_task = loop.create_task(self._event_loop())
        self._app_task = loop.create_task(self._application.run_async())
        self._heartbeat_task = loop.create_task(self._heartbeat())
        self._app_task.add_done_callback(self._on_application_done)

    async def _heartbeat(self) -> None:
        while not self._closed:
            await asyncio.sleep(1)
            if self.turn_active or self._approval is not None:
                self._invalidate()

    def _on_application_done(self, task: asyncio.Task) -> None:
        was_closing = self._closed
        self._closed = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
        error = RuntimeError("terminal task cancelled") if task.cancelled() else task.exception()
        self.terminal_error = error
        if self.turn_active and not was_closing and self._abort_callback is not None:
            self._abort_callback(True)
        if self._approval_answer is not None:
            self._finish_approval("cancel")
        if self._pending_read is not None:
            self._finish_read(None)
        if (self._interaction.queued_messages() or self._pending_correction or self._ready_direct) and not self._exit_requested:
            print("OpenGCUA: terminal closed with unsent queued messages.", file=sys.stderr)
        if error is not None:
            self._append(f"\nTerminal unavailable: {type(error).__name__}\n")
            print(f"OpenGCUA terminal unavailable: {type(error).__name__}: {error}", file=sys.stderr)

    async def stop(self) -> None:
        self._closed = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._events is not None:
            await self._events.put(None)
        if self._event_task is not None:
            await self._event_task
            self._event_task = None
        if self._app_task is not None:
            if self._application.is_running:
                self._application.exit()
            try:
                await self._app_task
            except (EOFError, KeyboardInterrupt):
                pass
            self._app_task = None

    async def _event_loop(self) -> None:
        assert self._events is not None
        while (event := await self._events.get()) is not None:
            self._consume(event)
            self._invalidate()

    def _invalidate(self) -> None:
        if self._application.is_running:
            self._application.invalidate()

    def _append(self, text: str) -> None:
        self._transcript = (self._transcript + text)[-self._MAX_TRANSCRIPT:]
        self._output.text = self._transcript
        self._output.buffer.cursor_position = len(self._output.text)

    def bind_abort(self, callback: Callable[[bool], None]) -> None:
        self._abort_callback = callback

    def handle_sigint(self) -> None:
        if self._approval_answer is not None:
            self._finish_approval("cancel")
        if self._pending_kind in {"question", "retry"}:
            self._finish_read(None)
        if self.turn_active:
            force = self._abort_requested
            self._abort_requested = True
            self.phase = "interrupting"
            if self._abort_callback is not None:
                self._abort_callback(force)
            if not force:
                self._append("\n正在中止当前轮；已发生的操作不会回滚。\n")
        else:
            self._input.text = ""
        self._invalidate()

    def _select_approval(self, index: int) -> None:
        if self._approval is not None:
            self._approval_index = index
            self._invalidate()

    def _finish_approval(self, choice: ConsentChoice) -> None:
        answer = self._approval_answer
        if answer is None:
            return
        self._approval = None
        self._approval_answer = None
        self._approval_index = 0
        self._application.layout.focus(self._input)
        if self.phase == "waiting for approval":
            self.phase = "execution" if self.turn_active else "waiting"
        self._append("GUI permission: " + {
            "allow": "Permission allowed for this task",
            "reject": "Permission rejected",
            "cancel": "Permission cancelled",
        }[choice] + "\n")
        if not answer.done():
            answer.set_result(choice)
        self._invalidate()

    def _request_exit(self) -> None:
        if (self._interaction.queued_messages() or self._pending_correction or self._ready_direct) and not self._exit_requested:
            self._exit_requested = True
            self._append("\n仍有排队消息未发送；再按 Ctrl-D 或输入 /exit 确认退出。\n")
            return
        self._closed = True
        if self._approval_answer is not None:
            self._finish_approval("cancel")
        if self.turn_active and self._abort_callback is not None:
            self._abort_callback(True)
        if self._pending_read is not None and not self._pending_read.done():
            self._finish_read(None)
        if self._application.is_running:
            self._application.exit()

    def _local_command(self, value: str) -> bool:
        command = value.strip().lower()
        if command == "/help":
            self._append("\nEnter 发送 · Alt+Enter 换行 · Ctrl-C 中止当前轮 · Ctrl-D 退出\n"
                         "输入 / 可浏览命令；方向键选择，Tab 补全，Enter 选中或执行\n"
                         "/queue 查看 · /queue send 继续队列 · /queue clear 清空队列\n"
                         "/new <消息> · /status · /reasoning on|off · /code on|off · /clear · /exit\n")
            return True
        if command == "/status":
            self._append("\nSession: " + (self.session_id or "(not started)")
                         + " · Model: " + (self.model or "(not set)")
                         + " · Step: " + ("-" if self.step is None else str(self.step))
                         + " · Phase: " + self.phase
                         + " · Queued: " + str(len(self.queued_messages()) + len(self._ready_direct)
                                               + int(self._pending_correction is not None))
                         + " · Foreground: " + self.foreground_consent + "\n")
            return True
        if command == "/queue":
            queued = list(self._ready_direct) + list(self._interaction.queued_messages())
            if self._pending_correction:
                queued.insert(0, self._pending_correction)
            self._append("\nQueued messages:\n" + ("\n".join(f"{i}. {s}" for i, s in enumerate(queued, 1)) if queued else "(empty)") + "\n")
            return True
        if command == "/queue send":
            self._interaction.resume_queue()
            self._release_next_queued()
            return True
        if command == "/queue clear":
            self._interaction.clear_queue()
            self._append("\nQueued messages cleared.\n")
            return True
        if command == "/clear":
            self._transcript = ""
            self._output.text = ""
            return True
        for prefix, attr in (("/reasoning", "show_reasoning"), ("/code", "show_code")):
            if command.startswith(prefix):
                parts = command.split()
                if len(parts) == 2 and parts[1] in {"on", "off"}:
                    setattr(self, attr, parts[1] == "on")
                    self._append(f"\n{prefix[1:]}: {parts[1]} for future events\n")
                else:
                    self._append(f"\nUsage: {prefix} on|off\n")
                return True
        return False

    def _submit_enter(self) -> None:
        if self._approval_answer is not None:
            self._finish_approval("allow" if self._approval_index else "reject")
            return
        buffer = self._input.buffer
        state = buffer.complete_state
        if state is not None and self._input.text.startswith("/"):
            completion = state.current_completion
            if completion is None and len(state.completions) == 1:
                completion = state.completions[0]
            if completion is not None:
                buffer.apply_completion(completion)
                if completion.text.endswith(" "):
                    return
            elif self._input.text == "/":
                return
        if self._input.text == "/":
            return
        value = self._input.text
        command = value.strip().lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            self._request_exit()
            self._input.text = ""
            return
        if self._local_command(value):
            self._input.text = ""
            self._invalidate()
            return
        if not self.session_id and (command == "/new" or command.startswith("/new ")):
            initial_task = value.strip()[4:].lstrip()
            if not initial_task:
                self._input.text = "/new "
                return
            value = initial_task
        if self._pending_kind == "retry" and not value.strip():
            self._finish_read("")
            return
        if not value.strip():
            return
        self._input.text = ""
        if self._pending_kind == "question":
            self._finish_read(value)
            return
        if self._abort_requested and self._interaction.turn_active and self._pending_correction is None:
            self._pending_correction = value
            self._append(f"\nYou (after interrupt): {value}\n")
            self._invalidate()
            return
        route = self._interaction.submit(value)
        if route == "queued":
            self._append(f"\nYou (queued): {value}\n")
        elif route == "send":
            self._append(f"\nYou: {value}\n")
            if self._initial_user_text is None:
                self._initial_user_text = value
            if self._pending_read is None:
                self._ready_direct.append(value)
            else:
                self._finish_read(value)
        self._invalidate()

    def _finish_read(self, value: str | None) -> None:
        pending = self._pending_read
        self._pending_read = None
        self._pending_kind = None
        if self._saved_draft is not None:
            self._input.text = self._saved_draft
            self._saved_draft = None
        if pending is not None and not pending.done():
            pending.set_result(value)

    def _release_next_queued(self) -> None:
        if self._pending_kind != "task" or self._pending_read is None:
            return
        next_message = self._interaction.next_message()
        if next_message is not None:
            self._finish_read(next_message)

    async def read(self, prompt: AgentPrompt) -> str | None:
        if self._closed:
            return None
        if self._app_task is None:
            self.start()
        if self._pending_read is not None:
            raise RuntimeError("another terminal read is already pending")
        if prompt.kind == "task":
            if self._pending_correction is not None:
                correction = self._pending_correction
                self._pending_correction = None
                return correction
            if not self._interaction.queue_suspended:
                queued = self._interaction.next_message()
                if queued is not None:
                    return queued
            if self._ready_direct:
                return self._ready_direct.popleft()
        if prompt.kind == "question":
            self._saved_draft = self._input.text
            self._input.text = ""
            self.phase = "waiting for input"
            self._append("\nAgent question: " + prompt.text + "\n")
        elif prompt.kind == "retry":
            self._append("\nModel request failed: " + prompt.text + "\n")
        elif prompt.kind == "approval":
            raise RuntimeError("use choose_approval for typed GUI consent")
        self._pending_kind = prompt.kind
        self._pending_read = asyncio.get_running_loop().create_future()
        self._invalidate()
        try:
            return await self._pending_read
        finally:
            if self._pending_read is not None:
                self._finish_read(None)

    async def choose_approval(self, prompt: ForegroundApprovalPrompt) -> ConsentChoice:
        if self._closed:
            return "cancel"
        if self._app_task is None:
            self.start()
        if self._approval_answer is not None:
            return "cancel"
        self._approval = prompt
        self._approval_index = 0
        self._approval_answer = asyncio.get_running_loop().create_future()
        self.phase = "waiting for approval"
        self._append("\nGUI permission: " + prompt.text + "\n")
        self._application.layout.focus(self._approval_card)
        self._invalidate()
        try:
            return await self._approval_answer
        finally:
            if self._approval_answer is not None:
                self._finish_approval("cancel")

    def queued_messages(self) -> tuple[str, ...]:
        return self._interaction.queued_messages()

    def emit(self, event: AgentEvent) -> None:
        if self._events is not None:
            self._events.put_nowait(event)
        else:
            self._consume(event)

    def _consume(self, event: AgentEvent) -> None:
        kind = event.kind
        now = time.monotonic()
        if self.turn_active and kind not in {"session", "foreground_consent"}:
            self._last_progress_at = now
        if kind == "session":
            self.session_id = str(event.payload.get("session_id") or "")
            self.model = str(event.payload.get("model") or "")
        elif kind == "foreground_consent":
            state = event.payload.get("state")
            if state in {"unset", "allowed", "denied"}:
                self.foreground_consent = str(state)
        elif kind == "phase":
            self.phase = str(event.payload.get("phase") or event.text or "working")
            self.step = event.step
            if self.phase in {"planning", "compaction", "execution", "postprocess", "recovery"}:
                was_active = self._interaction.turn_active
                self._interaction.begin_turn()
                if not was_active:
                    self._turn_started_at = now
                    self._last_progress_at = now
                if self.phase == "planning" and not was_active:
                    self._abort_requested = False
                if self.phase != "planning":
                    self._model_status = ""
        elif kind == "model_status":
            stage = str(event.payload.get("stage") or "")
            self._model_status = {
                "waiting": "waiting for API", "generating": "API responding",
                "validating": "validating reply", "retrying": "retrying API",
            }.get(stage, "")
        elif kind == "reasoning_delta":
            if self.show_reasoning:
                if not self._reasoning_open:
                    self._append("\nReasoning: ")
                    self._reasoning_open = True
                self._append(event.text)
        elif kind == "reasoning_end":
            if self._reasoning_open:
                self._append("\n")
            self._reasoning_open = False
        elif kind == "intent_delta":
            if not self._intent_open:
                self._last_intent = ""
                self._last_intent_step = event.step
                self._append("\nIntent: ")
                self._intent_open = True
            self._last_intent += event.text
            self._append(event.text)
        elif kind == "intent_end":
            if self._intent_open:
                self._append("\n")
            self._intent_open = False
        elif kind == "code_reset":
            self._code_preview.clear()
            self._code_open = False
            self._code_step = event.step
        elif kind == "code_start":
            self._code_step = event.step
            self._code_block = int(event.payload.get("block_index") or 1)
            self._code_preview[self._code_block] = ""
            self._code_open = self.show_code
            if self._code_open:
                self._append("\nCode preview:\n")
        elif kind == "code_delta":
            if self._code_open and self._code_step == event.step:
                self._code_preview[self._code_block] += event.text
                self._append(event.text)
        elif kind == "code_end":
            if self._code_open and not self._transcript.endswith("\n"):
                self._append("\n")
            self._code_open = False
        elif kind == "action":
            intent = str(event.payload.get("intent") or event.text or "Action")
            if event.step != self._last_intent_step or intent.strip() != self._last_intent.strip():
                self._append(f"\nStep {event.step or 0} · Action: {intent}\n")
            code = str(event.payload.get("code") or "")
            streamed = "\n".join(self._code_preview[k].strip() for k in sorted(self._code_preview))
            if self.show_code and code and (self._code_step != event.step or streamed != code):
                self._append(code + "\n")
            self._code_preview.clear()
        elif kind == "execution":
            status = event.payload.get("status") or {}
            state = status.get("execution_state") or "completed"
            if state != "completed":
                self._append(f"Execution: {state}\n")
        elif kind == "answer":
            self._interaction.finish_turn(normal=True)
            self.phase = "waiting"
            self._model_status = ""
            self._append("\nAgent: " + event.text + "\n")
            self._release_next_queued()
        elif kind == "aborted":
            self._interaction.finish_turn(normal=False)
            self._abort_requested = False
            self.phase = "waiting"
            self._model_status = ""
            self._append("\nTurn aborted: " + event.text.rstrip("。") + "；已经发生的操作不会回滚。\n")
        elif kind == "finished":
            self._interaction.finish_turn(normal=True)
            self.phase = event.text
            self._model_status = ""
            self._append(f"\nFinished: {event.text}\n")
        elif kind == "trajectory":
            self._append(f"Trajectory: {event.payload.get('path') or event.text}\n")
        elif kind == "notice":
            if event.payload.get("role") == "user":
                message = event.text.removeprefix("你> ")
                if message != self._initial_user_text:
                    self._append(f"\nYou: {message}\n")
                self._initial_user_text = message
            elif not event.text.startswith((
                "继续对话请直接输入；", "Mac Agent · 直接输入消息开始对话，",
            )):
                self._append(event.text + "\n")
        elif kind in {"warning", "error", "resume_hint", "ready"}:
            self._append(f"{kind}: {event.text}\n")
        elif kind not in {"question", "model_status"} and event.text:
            # Unknown provider payloads may contain secrets: render public text only.
            self._append(event.text + "\n")

    def close(self) -> None:
        if self._application.is_running:
            self._application.exit()


def build_rich_frontend() -> RichTerminalFrontend:
    return RichTerminalFrontend()


__all__ = ["RichTerminalFrontend", "build_rich_frontend", "resolve_ui_mode"]
