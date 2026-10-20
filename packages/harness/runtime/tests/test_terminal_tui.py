from __future__ import annotations

import asyncio
from pathlib import Path
import tomllib

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent_events import AgentEvent, AgentPrompt, ForegroundApprovalPrompt
from terminal_tui import RichTerminalFrontend, resolve_ui_mode


def approval_prompt(request_id="r1"):
    return ForegroundApprovalPrompt(request_id, "Agent 将接管前台键鼠直到本任务结束", 0, "app:a", "window:7")


def test_auto_mode_requires_usable_terminal():
    assert resolve_ui_mode("auto", stdin_tty=True, stdout_tty=True, term="xterm") == "tui"
    assert resolve_ui_mode("auto", stdin_tty=False, stdout_tty=True, term="xterm") == "plain"


def test_slash_completion_lists_commands_and_filters_prefixes():
    ui = RichTerminalFrontend()
    completer = ui._input.buffer.completer

    def candidates(value):
        document = Document(value, cursor_position=len(value))
        return list(completer.get_completions(document, CompleteEvent(completion_requested=True)))

    assert {item.display_text for item in candidates("/")} >= {
        "/help", "/new", "/queue", "/status", "/clear", "/reasoning", "/code", "/exit",
    }
    assert [(item.display_text, item.text) for item in candidates("/sta")] == [("/status", "/status")]
    assert [(item.display_text, item.text) for item in candidates("/reasoning of")] == [("off", "off")]
    assert candidates("hello /sta") == []
    assert candidates("/new find a file") == []


def test_bare_slash_enter_does_not_send_a_task():
    ui = RichTerminalFrontend()
    ui._input.text = "/"
    ui._submit_enter()
    assert ui._input.text == "/"
    assert not ui._ready_direct


def test_initial_new_command_delivers_task_without_slash_prefix():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui._input.text = "/new find a file"
        ui._submit_enter()
        assert await asyncio.wait_for(ui.read(AgentPrompt(kind="task")), .1) == "find a file"

    asyncio.run(scenario())


def test_initial_new_without_message_waits_for_task_text():
    ui = RichTerminalFrontend()
    ui._input.text = "/new"
    ui._submit_enter()
    assert ui._input.text == "/new "
    assert not ui._ready_direct


def test_new_command_after_session_start_reaches_agent_loop():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui.emit(AgentEvent("session", payload={"session_id": "s1", "model": "m1"}))
        ui._input.text = "/new find a file"
        ui._submit_enter()
        assert await asyncio.wait_for(ui.read(AgentPrompt(kind="task")), .1) == "/new find a file"

    asyncio.run(scenario())


def test_exit_while_turn_active_closes_future_reads():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        aborts = []
        ui.bind_abort(aborts.append)
        ui.emit(AgentEvent("phase", step=0, payload={"phase": "execution"}))
        ui._request_exit()
        assert aborts == [True]
        assert await asyncio.wait_for(ui.read(AgentPrompt(kind="task")), .1) is None

    asyncio.run(scenario())


def test_cancelled_terminal_task_releases_current_read():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        pending = asyncio.create_task(ui.read(AgentPrompt(kind="task")))
        await asyncio.sleep(0)
        done = asyncio.get_running_loop().create_future()
        done.cancel()
        ui._on_application_done(done)
        assert await asyncio.wait_for(pending, .1) is None

    asyncio.run(scenario())


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


def test_successful_step_keeps_one_intent_and_omits_internal_phase_noise():
    ui = RichTerminalFrontend()
    ui.emit(AgentEvent("phase", step=0, payload={"phase": "planning"}))
    ui.emit(AgentEvent("intent_delta", "回复问候", step=0))
    ui.emit(AgentEvent("intent_end", step=0))
    ui.emit(AgentEvent("action", step=0, payload={"intent": "回复问候", "code": "pass"}))
    ui.emit(AgentEvent("phase", step=0, payload={"phase": "execution"}))
    ui.emit(AgentEvent("execution", step=0, payload={"status": {"execution_state": "completed"}}))
    ui.emit(AgentEvent("phase", step=0, payload={"phase": "postprocess"}))
    ui.emit(AgentEvent("phase", step=0, payload={"phase": "complete"}))
    ui.emit(AgentEvent("answer", "你好！", step=0))
    assert ui.transcript.count("回复问候") == 1
    assert "Step 0 ·" not in ui.transcript
    assert "Execution: completed" not in ui.transcript
    assert "Agent: 你好！" in ui.transcript


def test_conversation_renders_user_and_agent_with_distinct_visual_roles():
    ui = RichTerminalFrontend()
    ui._input.text = "你好"
    ui._submit_enter()
    ui.emit(AgentEvent("answer", "你好！", step=0))
    document = ui._output.buffer.document
    lexer = ui._output.lexer
    user_line = next(index for index, line in enumerate(document.lines) if line.startswith("You:"))
    agent_line = next(index for index, line in enumerate(document.lines) if line.startswith("Agent:"))
    user_style = lexer.lex_document(document)(user_line)[0][0]
    agent_style = lexer.lex_document(document)(agent_line)[0][0]
    assert user_style != agent_style
    assert user_style and agent_style


def test_conversation_text_has_explicit_high_contrast_background():
    ui = RichTerminalFrontend()
    attrs = ui._application.style.get_attrs_for_style_str("class:conversation")

    def luminance(value):
        channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
                  for channel in channels]
        return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))

    assert attrs.color and attrs.bgcolor
    foreground, background = luminance(attrs.color), luminance(attrs.bgcolor)
    assert (max(foreground, background) + .05) / (min(foreground, background) + .05) >= 4.5


def test_session_metadata_stays_in_header_instead_of_repeating_in_conversation():
    ui = RichTerminalFrontend()
    ui.emit(AgentEvent("session", payload={"session_id": "mac-123", "model": "gpt-6-astra"}))
    assert "OpenGCUA" in "".join(text for _style, text in ui._header_text())
    assert "gpt-6-astra" in "".join(text for _style, text in ui._header_text())
    assert "session mac-123" not in ui.transcript


def test_initial_task_notice_does_not_duplicate_a_message_entered_in_tui():
    ui = RichTerminalFrontend()
    ui._input.text = "你好"
    ui._submit_enter()
    ui.emit(AgentEvent("notice", "你> 你好", payload={"role": "user"}))
    assert ui.transcript.count("你好") == 1


def test_initial_task_notice_displays_cli_argument_once():
    ui = RichTerminalFrontend()
    ui.emit(AgentEvent("notice", "你> 查找文件", payload={"role": "user"}))
    assert ui.transcript.count("查找文件") == 1
    assert "You:" in ui.transcript


def test_cli_initial_task_remains_visible_if_user_types_another_message_early():
    ui = RichTerminalFrontend()
    ui._input.text = "second message"
    ui._submit_enter()
    ui.emit(AgentEvent("notice", "你> initial task", payload={"role": "user"}))
    assert "second message" in ui.transcript
    assert "initial task" in ui.transcript


def test_repeated_chat_instructions_do_not_clutter_conversation():
    ui = RichTerminalFrontend()
    ui.emit(AgentEvent("answer", "任务完成", step=0))
    ui.emit(AgentEvent("notice", "继续对话请直接输入；/new <消息> 清空 Python 变量与 UI 依据并开新任务；空行或 /exit 结束。"))
    assert "继续对话请直接输入" not in ui.transcript


def test_startup_input_hint_is_carried_by_composer_not_conversation():
    ui = RichTerminalFrontend()
    ui.emit(AgentEvent("notice", "Mac Agent · 直接输入消息开始对话，/exit 或 Ctrl-D 退出。"))
    assert "Mac Agent" not in ui.transcript


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


def test_running_status_shows_elapsed_time_and_api_wait_without_claiming_a_stall():
    ui = RichTerminalFrontend()
    ui.emit(AgentEvent("phase", step=4, payload={"phase": "planning"}))
    ui.emit(AgentEvent("model_status", "正在等待 API 响应", step=4,
                       payload={"stage": "waiting"}))
    ui._turn_started_at -= 27
    ui._last_progress_at -= 27
    header = "".join(text for _, text in ui._header_text())
    assert "RUNNING" in header
    assert "00:27" in ui.status_text
    assert "API" in ui.status_text
    assert "27s" in ui.status_text
    assert "stuck" not in ui.status_text.lower()
    ui.emit(AgentEvent("answer", "好了", step=4))
    assert "RUNNING" not in "".join(text for _, text in ui._header_text())


def test_approval_card_is_visible_in_composer_and_restores_focus_and_draft():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui._input.text = "draft for later"
        pending = asyncio.create_task(ui.choose_approval(approval_prompt()))
        await asyncio.sleep(0)
        assert ui._application.layout.current_control is ui._approval_card.content
        card = "".join(text for _, text in ui._approval_card_text())
        assert "前台键鼠" in card
        assert "任务结束" in card
        assert "拒绝" in card and "允许" in card
        assert "默认" in card
        assert ui._input.text == "draft for later"
        ui._submit_enter()
        assert await pending == "reject"
        assert ui._application.layout.current_control is ui._input.control
        assert ui._input.text == "draft for later"
        assert "Permission rejected" in ui.transcript
        assert "Permission needed" not in "".join(text for _, text in ui._header_text())
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


def test_ctrl_c_during_question_releases_read_and_keeps_existing_draft():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui._interaction.begin_turn()
        calls = []
        ui.bind_abort(calls.append)
        ui._input.text = "draft correction"
        pending = asyncio.create_task(ui.read(AgentPrompt(kind="question", text="Need help?")))
        await asyncio.sleep(0)
        ui.handle_sigint()
        assert await asyncio.wait_for(pending, 1) is None
        assert ui._input.text == "draft correction"
        assert calls == [False]
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


def test_read_drains_queued_messages_in_order_after_normal_completion():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui.emit(AgentEvent("phase", step=0, payload={"phase": "execution"}))
        ui._input.text = "first"
        ui._submit_enter()
        ui._input.text = "second"
        ui._submit_enter()
        ui.emit(AgentEvent("answer", "done"))
        assert await ui.read(AgentPrompt(kind="task")) == "first"
        assert await ui.read(AgentPrompt(kind="task")) == "second"
    asyncio.run(scenario())


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


def test_correction_typed_while_interrupt_is_settling_is_delivered_after_abort():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui.bind_abort(lambda _force: None)
        ui.emit(AgentEvent("phase", step=0, payload={"phase": "execution"}))
        ui._input.text = "older queued message"
        ui._submit_enter()
        ui.handle_sigint()
        ui._input.text = "correct immediately"
        ui._submit_enter()
        assert ui.queued_messages() == ("older queued message",)
        ui.emit(AgentEvent("aborted", "stopped"))
        assert await ui.read(AgentPrompt(kind="task")) == "correct immediately"
        assert ui._interaction.queue_suspended
        assert ui.queued_messages() == ("older queued message",)
    asyncio.run(scenario())


def test_input_between_abort_event_and_next_read_is_not_lost():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui.emit(AgentEvent("phase", step=0, payload={"phase": "execution"}))
        ui.emit(AgentEvent("aborted", "stopped"))
        ui._input.text = "correct after abort"
        ui._submit_enter()
        assert await ui.read(AgentPrompt(kind="task")) == "correct after abort"
    asyncio.run(scenario())


def test_older_queue_precedes_new_idle_input_after_normal_completion():
    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui.emit(AgentEvent("phase", step=0, payload={"phase": "execution"}))
        ui._input.text = "older queued"
        ui._submit_enter()
        ui.emit(AgentEvent("answer", "done"))
        ui._input.text = "new idle message"
        ui._submit_enter()
        assert await ui.read(AgentPrompt(kind="task")) == "older queued"
        assert await ui.read(AgentPrompt(kind="task")) == "new idle message"
    asyncio.run(scenario())


def test_terminal_eof_warns_about_unsent_queue_and_aborts_running_turn(capsys):
    async def scenario():
        ui = RichTerminalFrontend()
        calls = []
        ui.bind_abort(calls.append)
        ui.emit(AgentEvent("phase", step=0, payload={"phase": "execution"}))
        ui._input.text = "unsent"
        ui._submit_enter()
        done = asyncio.get_running_loop().create_future()
        done.set_exception(EOFError())
        ui._on_application_done(done)
        assert calls == [True]

    asyncio.run(scenario())
    assert "unsent queued messages" in capsys.readouterr().err


def test_package_lists_terminal_interaction_module():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    modules = set(tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"]["py-modules"])
    assert {"terminal_tui", "terminal_interaction", "agent_events"} <= modules
