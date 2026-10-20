from __future__ import annotations

import asyncio
import io
from pathlib import Path
import tomllib

import pytest

from agent_events import AgentEvent, AgentPrompt, PlainFrontend
from terminal_tui import RichTerminalFrontend, resolve_ui_mode
from rich.console import Console
from prompt_toolkit.formatted_text import to_formatted_text


class PromptSession:
    def __init__(self, *values: str) -> None:
        self.values = list(values)

    async def prompt_async(self, *_args, **_kwargs) -> str:
        return self.values.pop(0)


def frontend(*values: str, clear=None):
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)
    ui = RichTerminalFrontend(
        console=console,
        prompt_session=PromptSession(*values),
        clear_screen=clear or (lambda: None),
    )
    return ui, output


def test_auto_ui_requires_two_ttys_and_non_dumb_term():
    assert resolve_ui_mode("auto", stdin_tty=True, stdout_tty=True, term="xterm-256color") == "tui"
    assert resolve_ui_mode("auto", stdin_tty=False, stdout_tty=True, term="xterm-256color") == "plain"
    assert resolve_ui_mode("auto", stdin_tty=True, stdout_tty=False, term="xterm-256color") == "plain"
    assert resolve_ui_mode("auto", stdin_tty=True, stdout_tty=True, term="dumb") == "plain"
    assert resolve_ui_mode("auto", stdin_tty=True, stdout_tty=True, term="") == "plain"


def test_explicit_plain_always_wins():
    assert resolve_ui_mode("plain", stdin_tty=True, stdout_tty=True, term="xterm-256color") == "plain"


def test_plain_frontend_redirected_output_has_no_ansi():
    output = io.StringIO()

    async def read(_prompt: str) -> str:
        return "/exit"

    plain = PlainFrontend(read, interactive=False, stdout=output, stderr=output)
    plain.emit(AgentEvent("warning", "plain warning"))

    assert output.getvalue() == "plain warning\n"
    assert "\x1b" not in output.getvalue()


def test_parser_exposes_all_ui_modes():
    import mac_agent

    assert mac_agent.build_parser().parse_args([]).ui == "auto"
    assert mac_agent.build_parser().parse_args(["--ui", "tui"]).ui == "tui"
    assert mac_agent.build_parser().parse_args(["--ui", "plain"]).ui == "plain"


def test_tui_hides_reasoning_and_code_by_default():
    ui, output = frontend()

    ui.emit(AgentEvent("reasoning_delta", "PRIVATE_REASONING", step=1))
    ui.emit(AgentEvent(
        "action",
        "legacy response",
        step=1,
        payload={"intent": "Open the report", "code": "PRIVATE_CODE"},
    ))

    rendered = output.getvalue()
    assert "Open the report" in rendered
    assert "PRIVATE_REASONING" not in rendered
    assert "PRIVATE_CODE" not in rendered


def test_tui_ignores_streamed_code_when_code_view_is_off():
    ui, output = frontend()
    ui.emit(AgentEvent("code_start", step=0, payload={"block_index": 1}))
    ui.emit(AgentEvent("code_delta", "terminate", step=0, payload={"block_index": 1}))
    ui.emit(AgentEvent("code_end", step=0, payload={"block_index": 1}))

    assert output.getvalue() == ""


def test_tui_streamed_code_preserves_model_line_breaks_without_duplication():
    ui, output = frontend()
    ui.show_code = True
    code = 'terminate(status="success", answer="你好！有什么我可以帮你的吗？")'
    ui.emit(AgentEvent("code_start", step=0, payload={"block_index": 1}))
    ui.emit(AgentEvent("code_delta", "terminate", step=0, payload={"block_index": 1}))
    ui.emit(AgentEvent("code_delta", code[len("terminate"):], step=0,
                       payload={"block_index": 1}))
    ui.emit(AgentEvent("code_end", step=0, payload={"block_index": 1}))
    ui.emit(AgentEvent("action", step=0, payload={"intent": "问候用户并终止", "code": code}))

    rendered = output.getvalue()
    assert code in rendered
    assert rendered.count(code) == 1
    assert "terminate\n(status" not in rendered


def test_tui_does_not_repeat_validated_code_after_trailing_stream_newline():
    ui, output = frontend()
    ui.show_code = True
    code = 'terminate(status="success", answer="你好！")'
    ui.emit(AgentEvent("code_start", step=0, payload={"block_index": 1}))
    ui.emit(AgentEvent("code_delta", code + "\n", step=0, payload={"block_index": 1}))
    ui.emit(AgentEvent("code_end", step=0, payload={"block_index": 1}))
    ui.emit(AgentEvent("action", step=0, payload={"intent": "问候", "code": code}))

    assert output.getvalue().count(code) == 1


def test_tui_toggles_apply_only_to_future_events():
    ui, output = frontend("/reasoning on", "continue")
    ui.emit(AgentEvent("reasoning_delta", "HIDDEN_EARLIER", step=1))

    message = asyncio.run(ui.read(AgentPrompt(kind="task")))
    ui.emit(AgentEvent("reasoning_delta", "VISIBLE_LATER", step=2))
    ui.emit(AgentEvent("reasoning_end", step=2))

    assert message == "continue"
    assert "HIDDEN_EARLIER" not in output.getvalue()
    assert "VISIBLE_LATER" in output.getvalue()


def test_tui_unknown_event_does_not_leak_payload():
    ui, output = frontend()

    ui.emit(AgentEvent(
        "future_event",
        "A future update occurred",
        payload={"api_key": "SECRET_KEY", "cookie": "SECRET_COOKIE"},
    ))

    rendered = output.getvalue()
    assert "A future update occurred" in rendered
    assert "SECRET_KEY" not in rendered
    assert "SECRET_COOKIE" not in rendered
    assert "api_key" not in rendered


def test_tui_local_commands_do_not_reach_agent():
    clears: list[bool] = []
    ui, _output = frontend(
        "/help",
        "/code on",
        "/clear",
        "first line\nsecond line",
        clear=lambda: clears.append(True),
    )

    message = asyncio.run(ui.read(AgentPrompt(kind="task")))
    ui.emit(AgentEvent("action", payload={"intent": "Run", "code": "print('shown')"}))

    assert message == "first line\nsecond line"
    assert ui.show_code is True
    assert "print('shown')" in _output.getvalue()
    assert clears == [True]


def test_tui_retry_keeps_empty_enter_as_retry():
    ui, _output = frontend("")

    assert asyncio.run(ui.read(AgentPrompt(
        kind="retry", empty_exits=True,
    ))) == ""


def test_tui_toolbar_renders_model_and_session_as_literal_text():
    ui, _output = frontend()
    ui.model = "model<a>&b"
    ui.session_id = "session<1>"

    rendered = "".join(fragment[1] for fragment in to_formatted_text(ui._toolbar()))

    assert "model<a>&b" in rendered
    assert "session<1>" in rendered


def test_tui_abort_notice_disclaims_rollback():
    ui, output = frontend()

    ui.emit(AgentEvent("aborted", "本轮已停止", step=4))

    rendered = output.getvalue()
    assert "本轮已停止" in rendered
    assert "不会回滚" in rendered


def test_tui_sigint_requests_abort_once_only_while_turn_is_active():
    ui, output = frontend()
    requests: list[bool] = []
    ui.bind_abort(lambda: requests.append(True))

    with pytest.raises(KeyboardInterrupt):
        ui.handle_sigint()
    ui.emit(AgentEvent("phase", "planning", step=2, payload={"phase": "planning"}))
    ui.handle_sigint()
    ui.handle_sigint()
    ui.emit(AgentEvent("aborted", "本轮已停止", step=2))
    with pytest.raises(KeyboardInterrupt):
        ui.handle_sigint()

    assert requests == [True]
    assert output.getvalue().count("不会回滚") == 2  # request notice + terminal abort panel


def test_runtime_package_lists_frontend_and_console_protocol_modules():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    modules = set(tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"]["py-modules"])

    assert {"agent_events", "terminal_tui", "console_protocol"} <= modules
