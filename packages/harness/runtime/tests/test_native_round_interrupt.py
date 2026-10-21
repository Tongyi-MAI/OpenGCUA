"""真实 Agent 主循环的轮级取消契约；替换模型和执行器，不请求网络或操作桌面。"""
import asyncio
import json
import sys
import types

import pytest

import mac_agent
from code_executor import CodeExecutionResult, empty_control
from model_stream import AgentError, CodeAction
from native_host import NativeHost


async def until(predicate):
    async def waiting():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(waiting(), 5)


@pytest.mark.parametrize("mode", ["question", "question_disabled", "pause", "api_failure"])
def test_interrupt_waits_preserves_live_session_and_accepts_correction(tmp_path, monkeypatch, mode):
    monkeypatch.setitem(sys.modules, "mai_ui", types.SimpleNamespace(Runtime=object))
    executions, requests, closed = [], [], []
    planned, release = asyncio.Event(), asyncio.Event()

    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class Executor:
        def __init__(self, **kwargs):
            pass
        def reset_output_context(self):
            pass
        async def close(self):
            closed.append(True)
        async def run(self, code, todos):
            executions.append(code)
            control = empty_control()
            if len(executions) == 1 and mode.startswith("question"):
                control["ask_user"] = {"question": "fixture business question"}
            else:
                control["terminate"] = {"status": "success", "answer": "correction accepted"}
            return CodeExecutionResult("", "", 0, False, control)

    async def plan(client, messages, **kwargs):
        requests.append(json.dumps(messages, ensure_ascii=False))
        planned.set()
        if len(requests) == 1:
            if mode == "pause":
                await release.wait()
            if mode == "api_failure":
                raise AgentError("fixture API failure after retries")
        code = f"pass # {len(requests)}"
        return CodeAction("fixture", code), f"<intent>fixture</intent><code>{code}</code>", "", {}

    monkeypatch.setattr(mac_agent, "ModelClient", Client)
    monkeypatch.setattr(mac_agent, "CodeExecutor", Executor)
    monkeypatch.setattr(mac_agent, "plan_code", plan)

    async def scenario():
        events = []
        host = NativeHost(events.append, tmp_path)
        try:
            await host.command({"command": "start", "task_id": "session", "text": "initial", "config": {
                "llm_base_url": "https://fixture.invalid/v1", "model_name": "fixture", "allow_empty_api_key": True,
            }})
            if mode == "pause":
                await asyncio.wait_for(planned.wait(), 5)
                await host.command({"command": "pause", "task_id": "session"})
                release.set()
                await until(lambda: host.control.paused)
                await host.command({"command": "interrupt", "task_id": "session", "command_id": "abort"})
            elif mode.startswith("question"):
                await until(lambda: host.control.question_id is not None)
                if mode == "question_disabled":
                    await host.command({"command": "set_policy", "task_id": "session", "command_id": "disable", "ask_user_enabled": False})
                else:
                    await host.command({"command": "interrupt", "task_id": "session", "command_id": "abort"})
            await until(lambda: any(e["event"] == "interrupted" for e in events))
            assert not host.task.done() and not closed
            assert host.control.suspended and host.control.question_id is None
            assert not any(e["event"] == "finished" for e in events)
            await host.command({"command": "message", "task_id": "session", "message_id": "correction", "text": "new correction"})
            await until(lambda: any(e["event"] == "turn_finished" for e in events))
            assert "new correction" in requests[-1]
            assert not host.task.done() and not closed
            assert any(e["event"] == "turn_started" and e["turn_index"] >= 1 for e in events)
            if mode == "question_disabled":
                assert "## ask_user 已关闭" in requests[-1]
            if mode == "api_failure":
                assert "fixture API failure" in next(e["text"] for e in events if e["event"] == "interrupted")
        finally:
            await host.close()
        assert closed

    asyncio.run(scenario())
