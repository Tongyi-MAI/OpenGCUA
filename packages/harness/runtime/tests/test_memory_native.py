"""NativeHost 记忆 wire 与 TUI/Plain 本地命令：宿主控制，不进模型、不占 FIFO、不扰授权。"""
from __future__ import annotations

import asyncio
import io

import pytest

from agent_events import AgentPrompt, PlainFrontend
from event_protocol import validate_event
from memory_runtime import MemoryService
from native_host import NativeHost, task_arguments
from terminal_tui import RichTerminalFrontend


def seeded(tmp_path, approve=False):
    """宿主根 = runs 的父目录；返回 (runs_dir, service, candidate)。"""
    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "钉钉周报流程", "content": "发送前先确认收件人", "app": "钉钉"},
                              {"session_id": "s0", "turn": 0, "step": 1})
    if approve:
        service.mutate("approve", created["memory_id"], 1)
    return tmp_path / "runs", service, created


def start_command(task_id="a"):
    return {"command": "start", "task_id": task_id, "text": "test", "config": {
        "endpoint": "http://127.0.0.1:9999/v1", "model": "fixture", "api_key": "private-test-key"}}


def test_idle_list_is_connection_scoped_lazy_and_needs_no_driver(tmp_path, monkeypatch):
    monkeypatch.setenv("GENERALIST_DRIVER_APP", str(tmp_path / "missing.app"))

    async def scenario():
        events = []
        host = NativeHost(events.append, tmp_path / "runs")
        await host.command({"command": "memory_list", "command_id": "list-1"})
        assert [event["event"] for event in events] == ["command_ack", "memory_catalog"]
        catalog = events[1]
        assert catalog["kind"] == "memory_catalog" and catalog["task_id"] is None
        assert "schema_version" not in catalog and "native_task_id" not in catalog
        assert catalog["source_command_id"] == "list-1" and catalog["entries"] == []
        assert catalog["generation"] == 0 and catalog["policy"] == "candidate_review"
        assert catalog["root"] == str(tmp_path / "memory") and catalog["available"] is True
        assert {"candidate_count", "approved_count", "omitted"} <= set(catalog)
        assert not (tmp_path / "memory").exists() and host.driver is None and host.task is None
        await host.close()
    asyncio.run(scenario())


def test_idle_mutation_ack_then_result_and_catalog_with_same_generation(tmp_path):
    runs, service, created = seeded(tmp_path)

    async def scenario():
        events = []
        host = NativeHost(events.append, runs)
        command = {"command": "memory_approve", "command_id": "approve-1",
                   "memory_id": created["memory_id"], "revision": 1}
        await host.command(command)
        ack, result, catalog = events
        assert ack == {"event": "command_ack", "command_id": "approve-1", "task_id": None, "command": "memory_approve"}
        assert result["event"] == "control_result" and result["task_id"] is None and "schema_version" not in result
        assert result["command_id"] == "approve-1" and result["status"] == "completed"
        assert result["memory_id"] == created["memory_id"] and result["revision"] == 2 and result["error_code"] is None
        assert catalog["event"] == "memory_catalog" and catalog["source_command_id"] == "approve-1"
        assert catalog["generation"] == result["generation"] == created["generation"] + 1
        assert catalog["approved_count"] == 1 and catalog["entries"][0]["status"] == "approved"
        # 重复 command_id 只重发受理回执，不重复执行。
        await host.command(dict(command))
        assert [event["event"] for event in events[3:]] == ["command_ack"]
        # 过期 revision：受理后独立失败结果，消息只有错误码说明，不带正文。
        await host.command({**command, "command_id": "approve-2"})
        ack, failed, refreshed = events[4:]
        assert ack["event"] == "command_ack" and failed["status"] == "failed"
        assert failed["error_code"] == "MEMORY_REVISION_CONFLICT" and "发送前先确认收件人" not in failed["text"]
        assert refreshed["generation"] == failed["generation"] == catalog["generation"]
        await host.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    {"revision": 0}, {"revision": True}, {"revision": "1"}, {"memory_id": ""}, {"memory_id": 7},
    {"memory_id": "m" * 65}, {"command_id": None}, {"command": "memory_update"},
    {"command": "memory_update", "content": 3}, {"command": "memory_update", "app": 5},
])
def test_invalid_mutation_fields_rejected_before_ack(tmp_path, change):
    runs, _, created = seeded(tmp_path)

    async def scenario():
        events = []
        host = NativeHost(events.append, runs)
        command = {"command": "memory_forget", "command_id": "bad", "memory_id": created["memory_id"],
                   "revision": 1, **change}
        with pytest.raises(ValueError):
            await host.command(command)
        assert events == [] and "bad" not in host.command_receipts
        await host.close()
    asyncio.run(scenario())


def test_memory_list_rejects_stale_task_and_invalid_offset_before_ack(tmp_path):
    async def scenario():
        events = []
        host = NativeHost(events.append, tmp_path / "runs")
        for fields in ({"task_id": "old"}, {"offset": -1}, {"offset": True}, {"offset": 385}, {"command_id": None}):
            with pytest.raises(ValueError):
                await host.command({"command": "memory_list", "command_id": "list", **fields})
        assert events == []
        await host.close()
    asyncio.run(scenario())


def test_task_scoped_mutation_requires_current_live_task(tmp_path):
    runs, service, created = seeded(tmp_path)

    async def scenario():
        events = []
        host = NativeHost(events.append, runs)
        with pytest.raises(ValueError, match="过期"):
            await host.command({"command": "memory_forget", "command_id": "f1", "task_id": "gone",
                                "memory_id": created["memory_id"], "revision": 1})
        assert events == [] and service.catalog_fields()["candidate_count"] == 1
        await host.close()
    asyncio.run(scenario())


def test_live_mutation_uses_v1_envelope_without_touching_fifo_or_authorization(tmp_path):
    runs, service, created = seeded(tmp_path)

    async def scenario():
        events = []

        async def runner(args, host_control):
            assert host_control.memory_service is host.memory_service
            await asyncio.Event().wait()
        host = NativeHost(events.append, runs, runner)
        await host.command({**start_command(), "command_id": "start"})
        await asyncio.sleep(0)
        c = host.control
        await host.command({"command": "message", "task_id": "a", "message_id": "m1", "text": "use Chrome"})
        before = (list(c.messages), list(c.commands), c.foreground_allowed, c.authorization_epoch,
                  c.policy_revision, c.answer, c.approval_wait)
        mark = len(events)
        await host.command({"command": "memory_update", "command_id": "upd", "task_id": "a",
                            "memory_id": created["memory_id"], "revision": 1,
                            "content": "发送前先确认收件人和抄送", "app": None})
        ack, result, catalog = events[mark:]
        assert ack["event"] == "command_ack" and ack["task_id"] == "a"
        for frame in (result, catalog):
            validate_event(dict(frame))
            assert frame["native_task_id"] == "a" and frame["origin"] == "native_host"
        assert result["kind"] == "control_result" and result["status"] == "completed" and result["revision"] == 2
        assert catalog["kind"] == "memory_catalog" and catalog["source_command_id"] == "upd"
        assert catalog["generation"] == result["generation"]
        entry = catalog["entries"][0]
        assert entry["status"] == "candidate" and entry["app"] is None and "抄送" in entry["content"]
        after = (list(c.messages), list(c.commands), c.foreground_allowed, c.authorization_epoch,
                 c.policy_revision, c.answer, c.approval_wait)
        assert after == before and c.messages == [{"id": "m1", "text": "use Chrome"}]
        assert not any(event["event"] in {"message", "queue", "policy"} for event in events[mark:])
        mark = len(events)
        await host.command({"command": "memory_list", "task_id": "a", "command_id": "live-list"})
        assert events[-1]["kind"] == "memory_catalog" and events[-1]["native_task_id"] == "a"
        validate_event(dict(events[-1]))
        await host.command({"command": "stop", "task_id": "a"})
        await host.close()
        # 会话结束后连接级仍可审核。
        events.clear()
        await host.command({"command": "memory_approve", "command_id": "idle", "memory_id": created["memory_id"],
                            "revision": 2})
        assert events[1]["task_id"] is None and events[1]["status"] == "completed"
    asyncio.run(scenario())


def test_app_config_cannot_redirect_memory_root(tmp_path):
    args = task_arguments({"llm_base_url": "https://example.com/v1", "model_name": "fixture",
                           "memory_dir": str(tmp_path / "elsewhere")}, tmp_path / "runs")
    assert args.memory_dir is None and args.memory is True
    host = NativeHost(lambda event: None, tmp_path / "runs")
    assert host.memory_service.root == tmp_path / "memory"


def test_tui_memory_commands_run_locally_and_never_reach_model(tmp_path):
    _, service, created = seeded(tmp_path)

    async def scenario():
        ui = RichTerminalFrontend()
        ui.start = lambda: None
        ui.memory_service = service
        ui._interaction.begin_turn()
        question = asyncio.create_task(ui.read(AgentPrompt(kind="question", text="Which one?")))
        await asyncio.sleep(0)
        ui._input.text = "/memory list"
        ui._submit_enter()
        ui._input.text = f"/memory approve {created['memory_id']} 1"
        ui._submit_enter()
        assert not question.done() and not ui._interaction.queued_items()
        assert "[候选] " + created["memory_id"] in ui._transcript and "发送前先确认收件人" in ui._transcript
        assert "已批准" in ui._transcript
        ui._input.text = "answer"
        ui._submit_enter()
        assert await question == "answer"
        ui._input.text = "/help"
        ui._submit_enter()
        assert "/memory approve|reject|forget" in ui._transcript
        ui.memory_service = None
        ui._input.text = "/memory"
        ui._submit_enter()
        assert "长期记忆未启用" in ui._transcript
    asyncio.run(scenario())
    assert service.catalog_fields()["approved_count"] == 1


def test_plain_memory_command_is_consumed_locally(tmp_path, capsys):
    _, service, created = seeded(tmp_path)
    lines = ["/memory list", f"/MEMORY forget {created['memory_id']} 1", "real task"]

    async def read(_label):
        return lines.pop(0)

    frontend = PlainFrontend(read, interactive=True, stdout=io.StringIO(), stderr=io.StringIO())
    frontend.memory_service = service
    assert asyncio.run(frontend.read(AgentPrompt(kind="task"))) == "real task"
    printed = capsys.readouterr().out
    assert created["memory_id"] in printed and "已遗忘" in printed
    assert service.catalog_fields()["entries"] == []


def test_completion_and_help_topic_cover_memory():
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document
    import rich_help

    ui = RichTerminalFrontend()
    completer = ui._input.buffer.completer
    document = Document("/memory ap", cursor_position=len("/memory ap"))
    assert [item.text for item in completer.get_completions(document, CompleteEvent(completion_requested=True))] == ["approve"]
    guide = rich_help.build_help("memory")
    assert "memory.propose" in guide and "沙箱内没有对应接口" in guide
    assert "memory" in rich_help.build_help("core")
