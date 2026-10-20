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
    PlainFrontend,
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


def _run_fake_agent(tmp_path, monkeypatch, *, extra_model_events=(), abort_then_correct=False):
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
        def __init__(self, **_kwargs):
            pass

        def interrupt_active(self, *, force=False):
            pass

        async def force_stop_active(self):
            pass

        def reset_output_context(self):
            pass

        async def run(self, _code, _todos):
            control = empty_control()
            control["terminate"] = {"status": "success", "answer": "Verified result"}
            return CodeExecutionResult("", "", 0, False, control)

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

    client = Client()

    model_inputs = []

    async def plan(current_client, _messages, **_kwargs):
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

    args = mac_agent.build_parser().parse_args([
        "hello",
        "--allow-empty-api-key",
        "--countdown", "0",
        "--disable-multi-task",
        "--startup-observation", "none",
        "--runs-dir", str(tmp_path),
    ])
    frontend = RecordingFrontend()
    if abort_then_correct:
        frontend.interactive = True
        async def correction(_prompt):
            return "请按原任务继续，但先检查状态"
        frontend.read = correction
    observer_events: list[dict] = []
    result = asyncio.run(mac_agent.run_agent(
        args,
        frontend=frontend,
        on_model_event=observer_events.append,
    ))
    if abort_then_correct:
        trajectory = json.loads(next(tmp_path.glob("*/traj.json")).read_text(encoding="utf-8"))
        return result, frontend.events, observer_events, model_inputs, trajectory
    return result, frontend.events, observer_events


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
