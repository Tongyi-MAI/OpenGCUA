from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import io
import json
import sys
import types

import pytest

import agent_events
from agent_events import (
    AgentEvent,
    AgentPrompt,
    ForegroundApprovalPrompt,
    PlainFrontend,
    TerminalCommand,
    emit_safely,
)
from console_protocol import encode_console_message


class RecordingFrontend:
    interactive = False

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)

    async def read(self, _prompt: AgentPrompt) -> str | None:
        return None

    async def choose_approval(self, _prompt: ForegroundApprovalPrompt) -> str:
        return "allow"

    def close(self) -> None:
        pass


def test_agent_event_is_immutable_and_preserves_payload():
    event = AgentEvent(
        kind="phase",
        text="planning",
        step=3,
        payload={"attempt": 2},
    )

    assert event.kind == "phase"
    assert event.text == "planning"
    assert event.step == 3
    assert dict(event.payload) == {"attempt": 2}
    with pytest.raises(FrozenInstanceError):
        event.text = "execution"  # type: ignore[misc]
    with pytest.raises(TypeError):
        event.payload["attempt"] = 4  # type: ignore[index]


def test_resume_handoff_is_typed_and_tui_supervisor_waits_for_new_message(monkeypatch, tmp_path):
    import argparse
    from pathlib import Path
    import mac_agent
    from resume_catalog import ResumeCandidate

    old = tmp_path / "old"
    selected = tmp_path / "saved run"
    selected.mkdir()
    calls = []

    async def fake_run(args, *, frontend):
        calls.append((args.resume, args.task))
        if len(calls) == 1:
            return mac_agent.ResumeHandoff(selected, old)
        return 0

    class Frontend:
        interactive = True
        terminal_error = None
        def __init__(self):
            self.messages = ["new request"]
            self.events = []
        def start(self): self.events.append("start")
        async def stop(self): self.events.append("stop")
        def close(self): self.events.append("close")
        def emit(self, event): self.events.append(event.kind)
        async def read(self, prompt):
            self.events.append("read:" + prompt.kind)
            return self.messages.pop(0)
        def suspend_queue_for_resume(self): self.events.append("suspend")

    monkeypatch.setattr(mac_agent, "run_agent", fake_run)
    monkeypatch.setattr(mac_agent, "inspect_resume_run", lambda path, *, ui_api: ResumeCandidate(path, "s", "old task", "success", "now", 1, 0))
    monkeypatch.setattr("prompt_toolkit.patch_stdout.patch_stdout", lambda **_kw: __import__("contextlib").nullcontext())
    ui = Frontend()
    result = asyncio.run(mac_agent.run_interactive_agent(argparse.Namespace(task="original", resume=None), ui))
    assert result == 0
    assert calls == [(None, "original"), (str(selected), "new request")]
    assert ui.events.index("suspend") < ui.events.index("read:task")
    assert ui.events.count("start") == 1
    assert ui.events[-2:] == ["stop", "close"]


def test_resume_wait_accepts_skill_commands_before_new_message(monkeypatch, tmp_path):
    import argparse
    import mac_agent
    from resume_catalog import ResumeCandidate

    selected = tmp_path / "saved-run"
    selected.mkdir()
    bundled = tmp_path / "bundle"
    example = bundled / "example"
    example.mkdir(parents=True)
    (example / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example workflow\n---\n\nUse this.\n"
    )
    monkeypatch.setattr(mac_agent, "default_skill_roots", lambda _file: (bundled, tmp_path / "personal"))
    monkeypatch.setattr(mac_agent, "inspect_resume_run", lambda path, *, ui_api: ResumeCandidate(path, "s", "old", "success", "now", 1, 0))
    monkeypatch.setattr("prompt_toolkit.patch_stdout.patch_stdout", lambda **_kw: __import__("contextlib").nullcontext())
    calls = []

    async def fake_run(args, *, frontend):
        calls.append((args.resume, args.task, getattr(args, "preselected_skill", None)))
        return mac_agent.ResumeHandoff(selected) if len(calls) == 1 else 0

    class Frontend:
        terminal_error = None
        def __init__(self):
            self.messages = [TerminalCommand("skills_list"), TerminalCommand("skill_select", "example"), "new request"]
            self.events = []
        def start(self): pass
        async def stop(self): pass
        def close(self): pass
        def emit(self, event): self.events.append(event)
        async def read(self, _prompt): return self.messages.pop(0)

    monkeypatch.setattr(mac_agent, "run_agent", fake_run)
    ui = Frontend()
    result = asyncio.run(mac_agent.run_interactive_agent(
        argparse.Namespace(task="original", resume=None, runs_dir=tmp_path, skills_dirs=[]), ui
    ))
    assert result == 0
    assert calls == [(None, "original", None), (str(selected), "new request", "example")]
    assert any(event.kind == "skills_catalog" for event in ui.events)
    assert any(event.kind == "skill_selected" for event in ui.events)


def test_plain_frontend_decodes_framed_multiline_as_one_message():
    frames = [encode_console_message("first line\nsecond line").decode("utf-8")]

    async def read(_prompt: str) -> str:
        return frames.pop(0)

    frontend = PlainFrontend(
        read,
        interactive=False,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    result = asyncio.run(frontend.read(AgentPrompt(kind="task")))

    assert result == "first line\nsecond line"
    assert frames == []


def test_plain_frontend_ctrl_c_aborts_active_turn_and_force_stops_on_repeat():
    async def read(_prompt: str) -> str:
        return "unused"

    frontend = PlainFrontend(read, interactive=True, stdout=io.StringIO(), stderr=io.StringIO())
    requests = []
    frontend.bind_abort(requests.append)
    frontend.emit(AgentEvent(kind="phase", step=0, payload={"phase": "planning"}))
    frontend.handle_sigint()
    frontend.handle_sigint()
    assert requests == [False, True]


def test_plain_frontend_ctrl_c_at_prompt_clears_input_and_reprompts():
    async def scenario():
        calls = []
        pending = asyncio.Event()

        async def read(_prompt: str) -> str:
            calls.append(1)
            if len(calls) == 1:
                pending.set()
                await asyncio.Future()
            return "corrected task"

        frontend = PlainFrontend(read, interactive=True, stdout=io.StringIO(), stderr=io.StringIO())
        frontend.bind_abort(lambda _force: pytest.fail("prompt Ctrl-C must not abort a turn"))
        reading = asyncio.create_task(frontend.read(AgentPrompt(kind="task")))
        await pending.wait()
        frontend.handle_sigint()
        assert await asyncio.wait_for(reading, 1) == "corrected task"
        assert len(calls) == 2

    asyncio.run(scenario())


def test_plain_frontend_ctrl_c_denies_pending_approval_and_aborts_turn():
    async def scenario():
        entered = asyncio.Event()

        async def read(_prompt: str) -> str:
            entered.set()
            await asyncio.Future()

        frontend = PlainFrontend(read, interactive=True, stdout=io.StringIO(), stderr=io.StringIO())
        requests = []
        frontend.bind_abort(requests.append)
        reading = asyncio.create_task(frontend.read(AgentPrompt(kind="approval", text="Allow?")))
        await entered.wait()
        frontend.handle_sigint()
        assert await asyncio.wait_for(reading, 1) is None
        assert requests == [False]

    asyncio.run(scenario())


def test_plain_frontend_exit_and_empty_semantics():
    values = iter(["", "continue", ""])

    async def read(_prompt: str) -> str:
        return next(values)

    frontend = PlainFrontend(
        read,
        interactive=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert asyncio.run(frontend.read(AgentPrompt(kind="task"))) == "continue"
    assert asyncio.run(frontend.read(AgentPrompt(kind="task", empty_exits=True))) is None

    async def exit_read(_prompt: str) -> str:
        return "/exit"

    exiting = PlainFrontend(
        exit_read,
        interactive=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert asyncio.run(exiting.read(AgentPrompt(kind="task"))) is None


def test_plain_frontend_retry_keeps_empty_enter_as_retry():
    async def read(_prompt: str) -> str:
        return ""

    frontend = PlainFrontend(
        read,
        interactive=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert asyncio.run(frontend.read(AgentPrompt(
        kind="retry", empty_exits=True,
    ))) == ""


@pytest.mark.parametrize("response,expected", [
    ("yes", "allow"), ("同意", "allow"), ("no", "reject"),
    ("拒绝", "reject"), ("maybe", "cancel"), ("", "cancel"),
    ("/exit", "cancel"),
])
def test_plain_frontend_choice_requires_explicit_answer(response, expected):
    async def read(_prompt: str) -> str:
        return response

    frontend = PlainFrontend(read, interactive=True, stdout=io.StringIO())
    prompt = ForegroundApprovalPrompt("r1", "Allow foreground?", 0, "app:a", "window:7")
    assert asyncio.run(frontend.choose_approval(prompt)) == expected


def test_noninteractive_frontend_cannot_grant_foreground():
    async def read(_prompt: str) -> str:
        pytest.fail("noninteractive approval must not read input")

    frontend = PlainFrontend(read, interactive=False, stdout=io.StringIO())
    prompt = ForegroundApprovalPrompt("r1", "Allow foreground?", 0, "app:a", "window:7")
    assert asyncio.run(frontend.choose_approval(prompt)) == "cancel"


def test_plain_frontend_hub_code_preview_preserves_multiline_delta():
    output = io.StringIO()
    frontend = PlainFrontend(
        lambda _prompt: None,
        interactive=False,
        stdout=output,
        hub_events=True,
    )
    frontend.emit(AgentEvent("code_start", step=3, payload={"block_index": 2}))
    frontend.emit(AgentEvent("code_delta", "print('a')\nprint('b')", step=3,
                             payload={"block_index": 2}))
    frontend.emit(AgentEvent("code_end", step=3, payload={"block_index": 2}))

    lines = output.getvalue().splitlines()
    assert len(lines) == 3
    assert [agent_events.decode_hub_event(line) for line in lines] == [
        {"type": "code_start", "step": 3, "block_index": 2, "text": ""},
        {"type": "code_delta", "step": 3, "block_index": 2,
         "text": "print('a')\nprint('b')"},
        {"type": "code_end", "step": 3, "block_index": 2, "text": ""},
    ]

    ordinary = io.StringIO()
    PlainFrontend(lambda _prompt: None, interactive=False, stdout=ordinary).emit(
        AgentEvent("code_delta", "private preview", step=3, payload={"block_index": 2})
    )
    assert ordinary.getvalue() == ""


def test_plain_frontend_hub_skill_load_is_structured():
    output = io.StringIO()
    frontend = PlainFrontend(lambda _prompt: None, interactive=False, stdout=output, hub_events=True)
    frontend.emit(AgentEvent("skill_loaded", "Skill loaded: example", step=4,
                             payload={"name": "example", "source": "bundled"}))
    event = agent_events.decode_hub_event(output.getvalue().strip())
    assert event["type"] == "skill_loaded"
    assert event["name"] == "example"
    assert event["source"] == "bundled"


def test_mcp_status_event_contains_only_safe_metadata():
    from agent_events import mcp_status_event

    event = mcp_status_event({
        "type": "mcp_call_started", "server": "dingtalk", "tool": "getUser",
        "content": "private-contact", "secret": "private-credential",
    }, step=4)
    assert event is not None
    assert event.kind == "mcp_status"
    assert event.payload == {"stage": "running", "server": "dingtalk", "tool": "getUser"}
    assert "private-contact" not in event.text + str(event.payload)
    assert "private-credential" not in event.text + str(event.payload)


def test_frontend_failure_does_not_escape_agent_event_delivery():
    class BrokenFrontend:
        interactive = True

        def emit(self, _event: AgentEvent) -> None:
            raise RuntimeError("renderer broke")

        async def read(self, _prompt: AgentPrompt) -> str | None:
            return None

        def close(self) -> None:
            pass

    errors: list[str] = []

    delivered = emit_safely(
        BrokenFrontend(),
        AgentEvent("notice", "still running"),
        on_error=lambda exc: errors.append(str(exc)),
    )

    assert delivered is False
    assert errors == ["renderer broke"]


def _run_fake_agent(tmp_path, monkeypatch, *, extra_model_events=(), abort_then_correct=False,
                    simulate_gui_approval=False, simulate_question_abort=False,
                    resume_target=None, manual_command=False, manual_cancel=False,
                    return_model_inputs=False, task_message="hello", skills_dir=None,
                    worker_skill_event=None, initial_commands=(), mcp_servers=None,
                    fail_plan=False):
    import llm_entry
    import mac_agent
    from code_executor import CodeExecutionResult, empty_control
    from model_stream import CodeAction

    monkeypatch.setitem(sys.modules, "mai_ui", types.SimpleNamespace(Runtime=object))

    class Desktop:
        generation = 0
        permission_status = {"accessibility": True, "screen_recording": True}

        def __init__(self, **_kwargs):
            pass

        async def start(self):
            pass

        async def close(self):
            pass

        def invalidate_references(self):
            self.generation += 1

    class RpcServer:
        def __init__(self, _desktop, _socket):
            self.calls = []

        async def start(self):
            pass

        async def close(self):
            pass

    class Executor:
        def __init__(self, **kwargs):
            self.approval_handler = kwargs.get("approval_handler")
            self.calls = 0

        def interrupt_active(self, *, force=False):
            pass

        async def force_stop_active(self):
            pass

        def reset_output_context(self):
            pass

        async def run(self, _code, _todos):
            self.calls += 1
            if simulate_gui_approval:
                assert await self.approval_handler({
                    "request_id": "test-approval", "app_id": "app:a", "window_id": "window:7",
                })
            control = empty_control()
            if worker_skill_event is not None and self.calls == 1:
                control["events"].append(worker_skill_event)
            if simulate_question_abort and self.calls == 1:
                control["ask_user"] = {"question": "Which file?"}
            elif (worker_skill_event is None or self.calls > 1) and (not manual_command or self.calls >= 3):
                control["terminate"] = {"status": "success", "answer": "Verified result"}
            return CodeExecutionResult("long result " * 500 if manual_command else "", "", 0, False, control)

        async def close(self):
            pass

    class Client:
        on_event = None
        on_attempt = None
        on_parse_event = None
        on_compaction_event = None
        token_estimator = None
        context_limit = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def complete(self, *_args, **_kwargs):
            if manual_cancel:
                await asyncio.sleep(2)
            return ("<summary><constraints>c</constraints><progress>p</progress>"
                    "<findings>f</findings><state>s</state></summary>", "", {})

    client = Client()

    model_inputs = []

    async def plan(current_client, _messages, **_kwargs):
        if fail_plan:
            raise RuntimeError("fixture planning failure")
        model_inputs.append(_messages)
        if abort_then_correct and len(model_inputs) == 1:
            mac_agent._abort_event().set()
            await asyncio.sleep(0)
        current_client.on_event({"type": "request_start", "purpose": "action", "attempt": 1})
        current_client.on_event({"type": "reasoning_delta", "purpose": "action", "delta": "brief"})
        current_client.on_event({"type": "intent_start", "purpose": "action", "block_index": 1})
        current_client.on_event({"type": "intent_delta", "purpose": "action", "delta": "reply"})
        current_client.on_event({"type": "intent_end", "purpose": "action"})
        for event in extra_model_events:
            current_client.on_event({"purpose": "action", **event})
        current_client.on_event({"type": "response_complete", "purpose": "action"})
        action = CodeAction(
            intent="reply",
            code='terminate(status="success", answer="Verified result")',
        )
        return action, action.as_response(), "brief", {}

    monkeypatch.setattr(mac_agent, "CuaDesktop", Desktop)
    monkeypatch.setattr(mac_agent, "CuaRpcServer", RpcServer)
    monkeypatch.setattr(mac_agent, "CodeExecutor", Executor)
    monkeypatch.setattr(mac_agent, "plan_code", plan)
    monkeypatch.setattr(llm_entry, "build_model_client", lambda *_args, **_kwargs: client)

    argv = [
        task_message,
        "--allow-empty-api-key",
        "--countdown", "0",
        "--startup-observation", "none",
        "--runs-dir", str(tmp_path),
    ]
    if skills_dir is not None:
        argv += ["--skills-dir", str(skills_dir)]
    if resume_target is None and not manual_command:
        argv.append("--disable-multi-task")
    args = mac_agent.build_parser().parse_args(argv)
    if mcp_servers is not None:
        args.mcp_servers = mcp_servers
    frontend = RecordingFrontend()
    if initial_commands:
        args.task = None
        frontend.interactive = True
        pending_inputs = [*initial_commands, task_message]
        async def read_initial(_prompt):
            return pending_inputs.pop(0) if pending_inputs else None
        frontend.read = read_initial
    if resume_target is not None:
        frontend.interactive = True
        async def switch(_prompt):
            return agent_events.TerminalCommand("resume", str(resume_target))
        frontend.read = switch
    if manual_command:
        frontend.interactive = True
        args.llm_compaction = False
        received = 0
        async def manual(_prompt):
            nonlocal received
            received += 1
            return agent_events.TerminalCommand("compaction") if received == 1 else None
        frontend.read = manual
        if manual_cancel:
            frontend.bind_abort = lambda callback: setattr(frontend, "_abort_callback", callback)
            frontend.handle_sigint = lambda: frontend._abort_callback(False)
            original_emit = frontend.emit
            def emit_and_cancel(event):
                original_emit(event)
                if event.kind == "phase" and event.payload.get("phase") == "compaction":
                    asyncio.get_running_loop().call_later(.01, frontend._abort_callback, False)
            frontend.emit = emit_and_cancel
    if abort_then_correct:
        frontend.interactive = True
        async def correction(_prompt):
            return "请按原任务继续，但先检查状态"
        frontend.read = correction
    if simulate_question_abort:
        frontend.interactive = True

        async def abort_question(prompt):
            if prompt.kind == "question":
                mac_agent._abort_event().set()
                return None
            return "问题已中止，请按原任务继续"

        frontend.read = abort_question
    observer_events: list[dict] = []
    result = asyncio.run(mac_agent.run_agent(
        args,
        frontend=frontend,
        on_model_event=observer_events.append,
    ))
    if abort_then_correct or simulate_question_abort:
        trajectory = json.loads(next(tmp_path.glob("*/traj.json")).read_text(encoding="utf-8"))
        return result, frontend.events, observer_events, model_inputs, trajectory
    if return_model_inputs:
        return result, frontend.events, observer_events, model_inputs
    return result, frontend.events, observer_events


def test_run_agent_owns_opt_in_mcp_broker_and_closes_on_success_or_error(tmp_path, monkeypatch):
    import mac_agent

    created = []

    class FakeBroker:
        def __init__(self, specs, environ, on_event):
            self.specs = specs
            self.on_event = on_event
            self.closed = False
            created.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(mac_agent, "McpBroker", FakeBroker, raising=False)
    config = {"fixture": {
        "transport": "stdio", "command": sys.executable, "args": [],
        "env_from_host": [], "read_only_tools": ["read_value"],
    }}
    result, _events, _observer, model_inputs = _run_fake_agent(
        tmp_path / "success", monkeypatch, mcp_servers=config, return_model_inputs=True,
    )
    assert result == 0
    assert len(created) == 1 and created[0].closed
    assert "mcp.list" in str(model_inputs[0])
    assert "read_value" not in str(model_inputs[0])
    failed, _events, _observer = _run_fake_agent(
        tmp_path / "error", monkeypatch, mcp_servers=config, fail_plan=True,
    )
    assert failed != 0
    assert len(created) == 2 and created[1].closed


def test_unconfigured_agent_has_no_mcp_broker_or_catalog_prompt(tmp_path, monkeypatch):
    import mac_agent

    class UnexpectedBroker:
        def __init__(self, *_args):
            raise AssertionError("MCP broker must stay disabled by default")

    monkeypatch.setattr(mac_agent, "McpBroker", UnexpectedBroker)
    result, _events, _observer, inputs = _run_fake_agent(
        tmp_path, monkeypatch, return_model_inputs=True,
    )
    assert result == 0
    assert "mcp.list" not in str(inputs[0])


def test_run_agent_emits_structured_lifecycle_action_and_answer(tmp_path, monkeypatch):
    result, events, _observer = _run_fake_agent(tmp_path, monkeypatch)

    kinds = [event.kind for event in events]
    assert result == 0
    assert kinds[0] == "session"
    assert "trajectory" in kinds
    assert "ready" in kinds
    assert "phase" in kinds
    assert "action" in kinds
    assert "answer" in kinds
    assert kinds[-1] == "finished"
    assert next(event for event in events if event.kind == "action").payload["intent"] == "reply"
    assert next(event for event in events if event.kind == "answer").text == "Verified result"


def test_explicit_skill_marker_is_loaded_before_first_model_request(tmp_path, monkeypatch):
    folder = tmp_path / "skills" / "example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example workflow\n---\n\nSpecific workflow body.\n",
        encoding="utf-8",
    )
    result, events, _observer, model_inputs = _run_fake_agent(
        tmp_path / "runs", monkeypatch, task_message="$example do the task",
        skills_dir=tmp_path / "skills", return_model_inputs=True,
    )
    assert result == 0
    assert "Specific workflow body" in str(model_inputs[0])
    assert "$example" not in str(model_inputs[0])
    trajectory = json.loads(next((tmp_path / "runs").glob("*/traj.json")).read_text())
    assert trajectory["tasks"][0]["task"] == "$example do the task"
    assert trajectory["tasks"][0]["skills"][0]["name"] == "example"
    assert any(event.kind == "skill_loaded" for event in events)


def test_model_selected_skill_appears_only_after_load_event(tmp_path, monkeypatch):
    from skill_registry import SkillRegistry

    folder = tmp_path / "skills" / "example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example workflow\n---\n\nSpecific workflow body.\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path / "none", tmp_path / "other", (tmp_path / "skills",))
    info = registry.discover()[0]
    event = {"type": "skill_loaded", "name": "example", "digest": info.digest, "source": info.source}
    result, _events, _observer, model_inputs = _run_fake_agent(
        tmp_path / "runs", monkeypatch, task_message="do the task", skills_dir=tmp_path / "skills",
        worker_skill_event=event, return_model_inputs=True,
    )
    assert result == 0
    assert len(model_inputs) == 2
    assert "Example workflow" in str(model_inputs[0])
    assert "Specific workflow body" not in str(model_inputs[0])
    assert str(model_inputs[1]).count("Specific workflow body") == 1
    trajectory = json.loads(next((tmp_path / "runs").glob("*/traj.json")).read_text())
    assert trajectory["tasks"][0]["skills"][0]["digest"] == info.digest


def test_host_skill_command_selects_next_message_without_becoming_task_text(tmp_path, monkeypatch):
    folder = tmp_path / "skills" / "example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example workflow\n---\n\nSpecific workflow body.\n",
        encoding="utf-8",
    )
    result, events, _observer, inputs = _run_fake_agent(
        tmp_path / "runs", monkeypatch, task_message="do the task", skills_dir=tmp_path / "skills",
        initial_commands=(TerminalCommand("skills_list"), TerminalCommand("skill_select", "example")),
        return_model_inputs=True,
    )
    assert result == 0
    assert "Specific workflow body" in str(inputs[0])
    assert any(event.kind == "skill_selected" for event in events)
    assert not any("/skill example" in str(request) for request in inputs)


def test_resume_with_new_message_appends_task_without_replaying_old_one(tmp_path, monkeypatch):
    import mac_agent

    first, _, _ = _run_fake_agent(tmp_path, monkeypatch)
    assert first == 0
    run = next(tmp_path.glob("*/traj.json")).parent
    previous = json.loads((run / "traj.json").read_text(encoding="utf-8"))
    assert len(previous["tasks"]) == 1 and len(previous["steps"]) == 1

    args = mac_agent.build_parser().parse_args([
        "new request", "--resume", str(run), "--allow-empty-api-key",
        "--countdown", "0", "--disable-multi-task", "--startup-observation", "none",
        "--runs-dir", str(tmp_path),
    ])
    result = asyncio.run(mac_agent.run_agent(args, frontend=RecordingFrontend()))
    assert result == 0
    restored = json.loads((run / "traj.json").read_text(encoding="utf-8"))
    assert [item["task"] for item in restored["tasks"]] == ["hello", "new request"]
    assert [item["first_step"] for item in restored["tasks"]] == [0, 1]
    assert len(restored["steps"]) == 2
    assert len(restored["resume_history"]) == 1


def test_resume_handoff_closes_current_runtime_without_writing_selected_run(tmp_path, monkeypatch):
    import mac_agent

    target_root = tmp_path / "target"
    target_root.mkdir()
    _run_fake_agent(target_root, monkeypatch)
    target = next(target_root.glob("*/traj.json")).parent
    target_before = (target / "traj.json").read_bytes()
    current_root = tmp_path / "current"
    current_root.mkdir()
    result, events, _ = _run_fake_agent(current_root, monkeypatch, resume_target=target)
    assert isinstance(result, mac_agent.ResumeHandoff)
    assert result.path == target.resolve()
    assert result.previous_path == next(current_root.glob("*/traj.json")).parent.resolve()
    assert (target / "traj.json").read_bytes() == target_before
    assert not any(event.kind == "error" for event in events)


def test_manual_compaction_runs_at_safe_boundary_without_action_or_auto_trigger(tmp_path, monkeypatch):
    result, events, _ = _run_fake_agent(tmp_path, monkeypatch, manual_command=True)
    data = json.loads(next(tmp_path.glob("*/traj.json")).read_text(encoding="utf-8"))
    assert result == 0
    assert len(data["steps"]) == 3
    assert len(data["compactions"]) == 1
    assert data["compactions"][0]["status"] == "completed"
    assert data["compactions"][0]["trigger_reasons"] == ["manual"]
    assert data["llm_compaction"]["revision"] == 1
    assert any(event.kind == "phase" and event.payload.get("phase") == "compaction" for event in events)
    assert any(event.kind == "control_result" and event.payload.get("status") == "completed" for event in events)


def test_successful_manual_summary_survives_resume_with_auto_disabled(tmp_path, monkeypatch):
    import mac_agent

    _result, _events, _observer, model_inputs = _run_fake_agent(
        tmp_path, monkeypatch, manual_command=True, return_model_inputs=True,
    )
    run = next(tmp_path.glob("*/traj.json")).parent
    prior = json.loads((run / "traj.json").read_text(encoding="utf-8"))
    assert prior["llm_compaction"]["source_digest"]
    before = len(model_inputs)
    args = mac_agent.build_parser().parse_args([
        "new request", "--resume", str(run), "--allow-empty-api-key",
        "--disable-llm-compaction", "--disable-multi-task", "--countdown", "0",
        "--startup-observation", "none", "--runs-dir", str(tmp_path),
    ])
    assert asyncio.run(mac_agent.run_agent(args, frontend=RecordingFrontend())) == 0
    assert len(model_inputs) > before
    assert "# Previous summary" in str(model_inputs[before])
    restored = json.loads((run / "traj.json").read_text(encoding="utf-8"))
    assert restored["llm_compaction"]["source_digest"] == prior["llm_compaction"]["source_digest"]
    assert restored["tasks"][-1]["task"] == "new request"


def test_manual_compaction_ctrl_c_cancels_without_committing_or_new_action(tmp_path, monkeypatch):
    result, events, _ = _run_fake_agent(tmp_path, monkeypatch,
                                        manual_command=True, manual_cancel=True)
    data = json.loads(next(tmp_path.glob("*/traj.json")).read_text(encoding="utf-8"))
    assert result == 0
    assert len(data["steps"]) == 3
    assert not data.get("llm_compaction")
    assert not data.get("compactions")
    assert any(event.kind == "control_result" and event.payload.get("status") == "cancelled" for event in events)


def test_legacy_cli_resume_without_new_message_keeps_existing_task(tmp_path, monkeypatch):
    import mac_agent

    _run_fake_agent(tmp_path, monkeypatch)
    run = next(tmp_path.glob("*/traj.json")).parent
    args = mac_agent.build_parser().parse_args([
        "--resume", str(run), "--allow-empty-api-key", "--countdown", "0",
        "--disable-multi-task", "--startup-observation", "none", "--runs-dir", str(tmp_path),
    ])
    assert asyncio.run(mac_agent.run_agent(args, frontend=RecordingFrontend())) == 0
    restored = json.loads((run / "traj.json").read_text(encoding="utf-8"))
    assert [item["task"] for item in restored["tasks"]] == ["hello"]
    assert len(restored["steps"]) == 2


def test_typed_foreground_choice_is_task_scoped_and_cleared_on_completion(tmp_path, monkeypatch):
    result, events, _observer = _run_fake_agent(
        tmp_path, monkeypatch, simulate_gui_approval=True,
    )
    states = [event.payload["state"] for event in events if event.kind == "foreground_consent"]
    assert result == 0
    assert states == ["allowed", "unset", "unset"]
    trajectory = json.loads(next(tmp_path.glob("*/traj.json")).read_text(encoding="utf-8"))
    assert trajectory["foreground_consent"] == [{
        "task_index": 0, "request_id": "test-approval",
        "app_id": "app:a", "window_id": "window:7", "decision": "allow",
        "scope": "task", "explicit_retry": False,
    }]


def test_plain_frontend_model_events_keep_existing_observer_callback(tmp_path, monkeypatch):
    _result, events, observer = _run_fake_agent(tmp_path, monkeypatch)

    assert [event["type"] for event in observer] == [
        "request_start",
        "reasoning_delta",
        "intent_start",
        "intent_delta",
        "intent_end",
        "response_complete",
    ]
    assert any(event.kind == "reasoning_delta" and event.text == "brief" for event in events)
    assert any(event.kind == "intent_delta" and event.text == "reply" for event in events)


def test_run_agent_forwards_code_preview_and_attempt_reset(tmp_path, monkeypatch):
    _result, events, _observer = _run_fake_agent(tmp_path, monkeypatch,
        extra_model_events=(
            {"type": "code_start", "block_index": 1},
            {"type": "code_delta", "block_index": 1, "delta": "print(1)\n"},
            {"type": "code_end", "block_index": 1},
        ))

    preview = [event for event in events if event.kind.startswith("code_")]
    assert [(event.kind, event.step, event.text, event.payload.get("block_index"))
            for event in preview] == [
        ("code_reset", 0, "", None),
        ("code_start", 0, "", 1),
        ("code_delta", 0, "print(1)\n", 1),
        ("code_end", 0, "", 1),
    ]


def test_abort_correction_remains_in_same_task_and_reaches_next_model_input(tmp_path, monkeypatch):
    result, events, _observer, inputs, trajectory = _run_fake_agent(
        tmp_path, monkeypatch, abort_then_correct=True,
    )

    assert result == 0
    assert len(trajectory["tasks"]) == 1
    assert trajectory["tasks"][0]["index"] == 0
    assert any(event.kind == "aborted" for event in events)
    assert len(inputs) == 2
    assert "请按原任务继续，但先检查状态" in json.dumps(inputs[1], ensure_ascii=False)
    assert trajectory["steps"][-1]["task_index"] == 0


def test_ctrl_c_during_agent_question_returns_to_same_task(tmp_path, monkeypatch):
    result, events, _observer, inputs, trajectory = _run_fake_agent(
        tmp_path, monkeypatch, simulate_question_abort=True,
    )
    assert result == 0
    assert len(trajectory["tasks"]) == 1
    assert any(event.kind == "aborted" for event in events)
    assert len(inputs) == 2
    assert "问题已中止，请按原任务继续" in json.dumps(inputs[1], ensure_ascii=False)


def test_interactive_wrapper_starts_and_stops_ui_even_on_error(monkeypatch):
    import mac_agent

    class Frontend:
        def __init__(self):
            self.events = []

        def start(self):
            self.events.append("start")

        async def stop(self):
            self.events.append("stop")

        def close(self):
            self.events.append("close")

    async def fail(_args, *, frontend):
        frontend.events.append("run")
        raise RuntimeError("failure")

    monkeypatch.setattr(mac_agent, "run_agent", fail)
    frontend = Frontend()
    with pytest.raises(RuntimeError, match="failure"):
        asyncio.run(mac_agent.run_interactive_agent(object(), frontend))
    assert frontend.events == ["start", "run", "stop", "close"]


def test_interactive_wrapper_reports_terminal_failure(monkeypatch):
    import mac_agent

    class Frontend:
        terminal_error = EOFError()

        def start(self):
            pass

        async def stop(self):
            pass

        def close(self):
            pass

    async def finish(_args, *, frontend):
        return 0

    monkeypatch.setattr(mac_agent, "run_agent", finish)
    with pytest.raises(SystemExit, match="terminal closed unexpectedly"):
        asyncio.run(mac_agent.run_interactive_agent(object(), Frontend()))
