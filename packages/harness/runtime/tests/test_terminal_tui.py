from __future__ import annotations

import asyncio
from pathlib import Path
import tomllib

from agent_events import AgentEvent, AgentPrompt, ForegroundApprovalPrompt
from terminal_tui import RichTerminalFrontend, resolve_ui_mode


def approval_prompt(request_id="r1"):
    return ForegroundApprovalPrompt(request_id, "Agent 将接管前台键鼠直到本任务结束", 0, "app:a", "window:7")


def test_auto_mode_requires_usable_terminal():
    assert resolve_ui_mode("auto", stdin_tty=True, stdout_tty=True, term="xterm") == "tui"
    assert resolve_ui_mode("auto", stdin_tty=False, stdout_tty=True, term="xterm") == "plain"


def test_streamed_intent_and_code_preserve_lines_and_hide_private_by_default():
    ui = RichTerminalFrontend()
    ui.emit(AgentEvent("reasoning_delta", "SECRET_THOUGHT", step=0))
    ui.emit(AgentEvent("intent_delta", "first\nsecond", step=0))
    ui.emit(AgentEvent("intent_end", step=0))
    ui.emit(AgentEvent("code_start", step=0, payload={"block_index": 1}))
    ui.emit(AgentEvent("code_delta", "print('secret')\n", step=0))
    ui.emit(AgentEvent("code_end", step=0))
    assert "first\nsecond" in ui.transcript
    assert "SECRET_THOUGHT" not in ui.transcript
    assert "print('secret')" not in ui.transcript


def test_action_does_not_duplicate_streamed_code_and_unknown_payload_is_hidden():
    ui = RichTerminalFrontend()
    ui.show_code = True
    code = "print('a')\nprint('b')"
    ui.emit(AgentEvent("code_start", step=1, payload={"block_index": 1}))
    ui.emit(AgentEvent("code_delta", code, step=1))
    ui.emit(AgentEvent("code_end", step=1))
    ui.emit(AgentEvent("action", step=1, payload={"intent": "Run", "code": code}))
    ui.emit(AgentEvent("future_event", "Public", payload={"api_key": "SECRET"}))
    assert ui.transcript.count(code) == 1
    assert "Public" in ui.transcript
    assert "SECRET" not in ui.transcript


def test_approval_choice_rejects_by_default_and_preserves_chat_draft():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None  # Exercise the choice state without opening this test runner's stdin.
        ui._input.text = "unrelated draft"
        pending = asyncio.create_task(ui.choose_approval(approval_prompt()))
        await asyncio.sleep(0)
        ui._submit_enter()
        assert await pending == "reject"
        assert ui._input.text == "unrelated draft"
    asyncio.run(scenario())


def test_approval_allow_needs_selection_and_ctrl_c_cancels():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        pending = asyncio.create_task(ui.choose_approval(approval_prompt()))
        await asyncio.sleep(0)
        ui._select_approval(1)
        ui._submit_enter()
        assert await pending == "allow"
        pending2 = asyncio.create_task(ui.choose_approval(approval_prompt("r2")))
        await asyncio.sleep(0)
        ui.handle_sigint()
        assert await pending2 == "cancel"
    asyncio.run(scenario())


def test_question_uses_separate_input_and_restores_draft_without_eating_queue():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui._interaction.begin_turn()
        ui._interaction.submit("queued task")
        ui._input.text = "unfinished draft"
        pending = asyncio.create_task(ui.read(AgentPrompt(kind="question", text="Which file?")))
        await asyncio.sleep(0)
        assert ui._input.text == ""
        ui._input.text = "report.pdf"
        ui._submit_enter()
        assert await pending == "report.pdf"
        assert ui._input.text == "unfinished draft"
        assert ui.queued_messages() == ("queued task",)
    asyncio.run(scenario())


def test_messages_queue_during_active_turn_and_status_shows_consent():
    ui = RichTerminalFrontend()
    ui.emit(AgentEvent("phase", step=2, payload={"phase": "planning"}))
    ui._input.text = "send later"
    ui._submit_enter()
    assert ui.queued_messages() == ("send later",)
    ui.emit(AgentEvent("foreground_consent", payload={"state": "allowed"}))
    assert "foreground: allowed" in ui.status_text
    ui.emit(AgentEvent("answer", "done"))
    assert ui._interaction.next_message() == "send later"


def test_abort_suspends_queue_and_disclaims_rollback():
    ui = RichTerminalFrontend()
    calls = []
    ui.bind_abort(calls.append)
    ui.emit(AgentEvent("phase", step=0, payload={"phase": "planning"}))
    ui._input.text = "queued"
    ui._submit_enter()
    ui.handle_sigint()
    ui.handle_sigint()
    ui.emit(AgentEvent("aborted", "本轮已停止"))
    assert calls == [False, True]
    assert ui._interaction.queue_suspended
    assert ui._interaction.next_message() is None
    assert "不会回滚" in ui.transcript


def test_package_lists_terminal_interaction_module():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    modules = set(tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"]["py-modules"])
    assert {"terminal_tui", "terminal_interaction", "agent_events"} <= modules
