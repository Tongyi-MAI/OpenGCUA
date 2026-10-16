"""Native IPC lifecycle tests; no desktop actions or external model requests."""
import asyncio
import json
from pathlib import Path
import sys
import types

import pytest

from native_host import NativeHost, TaskControl, validate_endpoint, require_mac_permissions, task_arguments


def test_source_config_options_map_to_native_tasks_without_replacing_host_ownership(tmp_path):
    args = task_arguments({
        "llm_base_url": "https://example.com/v1/", "model_name": "fixture",
        "llm_protocol": "anthropic", "reasoning_effort": "high", "max_tokens": 4096,
        "context_limit": 64000, "api_retries": 4, "api_timeout": 90,
        "background_mode": True, "history_turns": 8, "llm_compaction": False,
        "runs_dir": "/unrelated", "driver_socket": "/unrelated.sock", "multi_task": True,
        "ui": "tui", "resume": "/unrelated-run", "countdown": 20,
        "_comment": "ignored", "unknown_setting": "ignored",
    }, tmp_path)
    assert args.llm_base_url == "https://example.com/v1"
    assert args.llm_protocol == "anthropic" and args.reasoning_effort == "high"
    assert args.max_tokens == 4096 and args.context_limit == 64000
    assert args.api_retries == 4 and args.api_timeout == 90
    assert args.background_mode and args.history_turns == 8 and not args.llm_compaction
    assert args.runs_dir == tmp_path and args.resume is None
    assert not args.multi_task and args.ui == "plain" and args.countdown == 0
    assert args.driver_socket != "/unrelated.sock"


@pytest.mark.parametrize("field,value", [
    ("api_retries", "3"), ("max_tokens", True), ("background_mode", 1),
    ("api_timeout", float("inf")), ("llm_protocol", "unsupported"),
    ("model_name", 17), ("llm_base_url", "https://example.com/v1?key=secret"),
])
def test_invalid_file_config_rejected(field, value, tmp_path):
    config = {"llm_base_url": "https://example.com/v1", "model_name": "fixture", field: value}
    with pytest.raises(ValueError):
        task_arguments(config, tmp_path)


def test_background_and_demo_modes_are_exclusive(tmp_path):
    with pytest.raises(ValueError):
        task_arguments({"endpoint": "https://example.com/v1", "model": "fixture",
                        "background_mode": True, "demo_mode": True}, tmp_path)


def start_command(task_id="a"):
    return {"command": "start", "task_id": task_id, "text": "test", "config": {
        "endpoint": "http://127.0.0.1:9999/v1", "model": "fixture", "api_key": "private-test-key"}}


def test_endpoint_credentials_and_remote_cleartext_rejected():
    for endpoint in ["http://example.com/v1", "https://user:secret@example.com/v1", "https://example.com/v1?token=x", "file:///tmp/model", "https://example.com/#key"]:
        with pytest.raises(ValueError):
            validate_endpoint(endpoint)
    assert validate_endpoint(" http://127.0.0.1:80/v1/ ") == "http://127.0.0.1:80/v1"
    assert validate_endpoint("https://example.com/v1") == "https://example.com/v1"


def test_permissions_must_be_explicitly_granted_before_model_request():
    require_mac_permissions({"permissions": {"accessibility": True, "screen_recording": True}})
    for permissions in ({}, {"accessibility": True}, {"accessibility": False, "screen_recording": True},
                        {"accessibility": True, "screen_recording": False}):
        with pytest.raises(RuntimeError, match="尚未发送模型请求"):
            require_mac_permissions({"permissions": permissions})


def test_pause_acknowledged_only_at_boundary_and_resume_preserves_question():
    async def scenario():
        events = []
        c = TaskControl("a", events.append)
        c.pause()
        assert events[-1]["state"] == "pausing"
        boundary = asyncio.create_task(c.boundary())
        await asyncio.sleep(0)
        assert not boundary.done() and events[-1]["state"] == "paused"
        c.resume()
        await boundary
        question = asyncio.create_task(c.ask("allow?"))
        await asyncio.sleep(0)
        c.resume()
        assert not question.done() and events[-1]["state"] == "waiting"
        with pytest.raises(ValueError): c.reply("stale-question", "yes")
        c.reply(c.question_id, "no")
        with pytest.raises(ValueError): c.reply(c.question_id, "duplicate")
        assert await question == "no"
    asyncio.run(scenario())


def test_single_owner_stale_commands_and_cancel_cleanup(tmp_path):
    async def scenario():
        events, cleanup = [], asyncio.Event()
        async def runner(args, host_control):
            try: await asyncio.Event().wait()
            finally: cleanup.set()
        host = NativeHost(events.append, tmp_path, runner)
        await host.command(start_command())
        await asyncio.sleep(0)
        with pytest.raises(ValueError): await host.command(start_command("b"))
        with pytest.raises(ValueError): await host.command({"command": "stop", "task_id": "stale"})
        assert not host.task.done()
        await host.command({"command": "stop", "task_id": "a"})
        await host.task
        assert cleanup.is_set()
        assert events[-1]["state"] == "stopped"
        await host.close()
    asyncio.run(scenario())


def test_stop_before_runner_starts_still_emits_terminal_state(tmp_path):
    async def scenario():
        events = []
        async def never_run(*args, **kwargs): raise AssertionError("should not run")
        host = NativeHost(events.append, tmp_path, never_run)
        await host.command(start_command())
        await host.command({"command": "stop", "task_id": "a"})
        await host.close()
        await asyncio.sleep(0)
        assert events[-1]["state"] == "stopped"
    asyncio.run(scenario())


def test_terminal_result_uses_trajectory_and_redacts_credentials(tmp_path):
    async def scenario():
        events = []
        async def runner(args, host_control):
            assert args.api_key == "private-test-key"
            assert args.multi_task is False
            host_control.emit("trajectory", path=str(tmp_path))
            (tmp_path / "traj.json").write_text(json.dumps({"status": "success", "answer": "done private-test-key"}))
            return 1  # the persisted result, not this return value, is authoritative
        host = NativeHost(events.append, tmp_path, runner)
        await host.command(start_command())
        await host.task
        assert events[-1]["state"] == "completed"
        assert events[-1]["text"] == "done [REDACTED]"
        assert "private-test-key" not in json.dumps(events)
    asyncio.run(scenario())


@pytest.mark.parametrize("empty_key", [False, True])
@pytest.mark.parametrize("steering", [False, True])
def test_real_agent_hooks_pause_before_execution_ask_and_finish(tmp_path, monkeypatch, empty_key, steering):
    """Exercise run_agent with a fake planner/executor, retaining real trajectories."""
    import mac_agent
    from code_executor import CodeExecutionResult, empty_control
    from model_stream import CodeAction
    monkeypatch.setitem(sys.modules, "mai_ui", types.SimpleNamespace(Runtime=object))
    calls = []
    plans = []
    closed = []
    planning = None
    allow_plan = None

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    class Executor:
        def __init__(self, **kwargs): pass
        def reset_output_context(self): pass
        async def close(self): closed.append(True)
        async def run(self, code, todos):
            calls.append(code)
            control = empty_control()
            if len(calls) == 1: control["ask_user"] = {"question": "Continue?"}
            else: control["terminate"] = {"status": "success", "answer": "Verified result"}
            return CodeExecutionResult("", "", 0, False, control)

    async def plan(*args, **kwargs):
        plans.append(json.dumps(args[1], ensure_ascii=False))
        planning.set()
        await allow_plan.wait()
        code = f"pass # plan {len(plans)}"
        return CodeAction(intent="test", code=code), f"<intent>test</intent><code>{code}</code>", "", {}

    monkeypatch.setattr(mac_agent, "ModelClient", Client)
    monkeypatch.setattr(mac_agent, "CodeExecutor", Executor)
    monkeypatch.setattr(mac_agent, "plan_code", plan)
    monkeypatch.setattr(mac_agent, "_read_local_api_key", lambda: (_ for _ in ()).throw(AssertionError("must not read CLI key")))
    monkeypatch.setenv("API_KEY", "unrelated-cli-key")

    async def scenario():
        nonlocal planning, allow_plan
        planning, allow_plan = asyncio.Event(), asyncio.Event()
        events = []
        host = NativeHost(events.append, tmp_path)
        command = start_command()
        if empty_key:
            command["config"].update(api_key="", allow_empty_key=True)
        await host.command(command)
        await asyncio.wait_for(planning.wait(), 5)
        await host.command({"command": "pause", "task_id": "a"})
        if steering:
            with pytest.raises(ValueError):
                await host.command({"command": "message", "task_id": "stale", "message_id": "u1", "text": "wrong task"})
            await host.command({"command": "message", "task_id": "a", "message_id": "u1", "text": "Only browse Chrome"})
        allow_plan.set()
        for _ in range(100):
            if events[-1].get("state") == "paused": break
            await asyncio.sleep(0.01)
        assert events[-1].get("state") == "paused" and not calls
        await host.command({"command": "resume", "task_id": "a"})
        for _ in range(100):
            if host.control.question_id: break
            await asyncio.sleep(0.01)
        assert host.control.question_id
        await host.command({"command": "reply", "task_id": "a", "question_id": host.control.question_id, "text": "yes"})
        await asyncio.wait_for(host.task, 5)
        assert events[-1]["state"] == "completed", events[-1]
        assert events[-1]["text"] == "Verified result"
        assert len(calls) == 2 and closed
        first_executed = 1 if steering else 0
        data = json.loads((host.control.run_dir / f"steps/step_{first_executed}.json").read_text())
        assert data["execution"]["user_response"] == "yes"
        if steering:
            assert all("plan 1" not in call for call in calls)
            assert "Only browse Chrome" in plans[1]
            trajectory = json.loads((host.control.run_dir / "traj.json").read_text())
            assert trajectory["superseded_plans"][0]["step"] == 0
            assert trajectory["user_messages"][0]["text"] == "Only browse Chrome"
            assert [e["state"] for e in events if e["event"] == "message"] == ["queued", "applied"]
    asyncio.run(scenario())


def test_message_queue_duplicate_bounds_and_question_fence():
    async def scenario():
        events = []
        c = TaskControl("a", events.append)
        c.pause()
        c.message("first", "  use Chrome  ")
        c.message("first", "different duplicate")
        assert c.messages == [{"id": "first", "text": "use Chrome"}]
        assert not c.resume_event.is_set()  # Sending cannot resume a paused task.
        updates = c.take_messages()
        assert updates[0]["text"] == "use Chrome"
        c.message("first", "retry")
        assert events[-1]["state"] == "applied" and not c.messages
        for i in range(20): c.message(str(i), "bounded")
        with pytest.raises(ValueError): c.message("overflow", "bounded")
        c.take_messages()
        with pytest.raises(ValueError): c.message("empty", "  ")
        with pytest.raises(ValueError): c.message("long", "x" * 8001)
        c.resume()
        waiting = asyncio.create_task(c.ask("Allow?"))
        await asyncio.sleep(0)
        with pytest.raises(ValueError): c.message("not-a-reply", "yes")
        c.reply(c.question_id, "no")
        assert await waiting == "no"
    asyncio.run(scenario())


def test_voice_receipts_follow_acceptance_and_never_ack_stale_task(tmp_path):
    async def scenario():
        events = []
        async def runner(args, host_control):
            await asyncio.Event().wait()
        host = NativeHost(events.append, tmp_path, runner)
        await host.command({**start_command(), "command_id": "start-voice"})
        await asyncio.sleep(0)
        await host.command({"command": "message", "task_id": "a", "message_id": "msg", "text": "use Chrome", "command_id": "queue-voice"})
        assert host.control.take_messages() == [{"id": "msg", "text": "use Chrome"}]
        with pytest.raises(ValueError):
            await host.command({"command": "stop", "task_id": "stale", "command_id": "stale-voice"})
        await host.command({"command": "stop", "task_id": "a", "command_id": "stop-voice"})
        await host.close()
        receipts = [e for e in events if e["event"] == "command_ack"]
        assert [e["command_id"] for e in receipts] == ["start-voice", "queue-voice", "stop-voice"]
        assert all(e["task_id"] == "a" for e in receipts)
        assert not any("api_key" in e for e in receipts)
        assert events[-1]["state"] == "stopped"
    asyncio.run(scenario())
