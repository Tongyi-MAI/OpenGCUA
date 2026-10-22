"""真实 run_agent：召回只注入已批准记忆、候选不注入、遗忘后下一次请求即排除。"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from agent_events import AgentEvent, AgentPrompt, ForegroundApprovalPrompt, handle_memory_command
from event_protocol import validate_event
from memory_runtime import MemoryService
from native_host import NativeHost


class Frontend:
    interactive = True

    def __init__(self, replies):
        self.events: list[AgentEvent] = []
        self.replies = replies

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)

    async def read(self, prompt: AgentPrompt) -> str | None:
        if not self.replies:
            return None
        reply = self.replies.pop(0)
        return reply(self) if callable(reply) else reply

    async def choose_approval(self, _prompt: ForegroundApprovalPrompt) -> str:
        return "cancel"

    def close(self) -> None:
        pass


def install_fakes(monkeypatch, model_inputs, on_run=None):
    import llm_entry
    import mac_agent
    from code_executor import CodeExecutionResult, empty_control
    from model_stream import CodeAction

    monkeypatch.setitem(sys.modules, "mai_ui", types.SimpleNamespace(Runtime=object))
    monkeypatch.delenv("GENERALIST_DRIVER_APP", raising=False)
    executors = []

    class Desktop:
        generation = 0
        permission_status = {"accessibility": True, "screen_recording": True}
        def __init__(self, **_kwargs): pass
        async def start(self): pass
        async def close(self): pass
        def invalidate_references(self): self.generation += 1

    class RpcServer:
        def __init__(self, *_args): self.calls = []
        async def start(self): pass
        async def close(self): pass

    class Executor:
        def __init__(self, **kwargs):
            self.memory_handler = kwargs.get("memory_handler")
            self.calls = 0
            executors.append(self)
        def interrupt_active(self, *, force=False): pass
        async def force_stop_active(self): pass
        def reset_output_context(self): pass
        async def close(self): pass
        async def run(self, _code, _todos):
            self.calls += 1
            if on_run is not None:
                await on_run(self)
            control = empty_control()
            control["terminate"] = {"status": "success", "answer": "Verified result"}
            return CodeExecutionResult("", "", 0, False, control)

    class Client:
        on_event = on_attempt = on_parse_event = on_compaction_event = token_estimator = context_limit = None
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass

    async def plan(_client, messages, **_kwargs):
        model_inputs.append(messages)
        action = CodeAction(intent="reply", code='terminate(status="success", answer="Verified result")')
        return action, action.as_response(), "", {}

    monkeypatch.setattr(mac_agent, "CuaDesktop", Desktop)
    monkeypatch.setattr(mac_agent, "CuaRpcServer", RpcServer)
    monkeypatch.setattr(mac_agent, "CodeExecutor", Executor)
    monkeypatch.setattr(mac_agent, "ModelClient", Client)
    monkeypatch.setattr(mac_agent, "plan_code", plan)
    monkeypatch.setattr(llm_entry, "build_model_client", lambda *_args, **_kwargs: Client())
    return executors


def run_cli(tmp_path, monkeypatch, replies, *extra, task="帮我在钉钉发周报"):
    import mac_agent

    model_inputs = []
    executors = install_fakes(monkeypatch, model_inputs)
    argv = [task, "--allow-empty-api-key", "--countdown", "0", "--startup-observation", "none",
            "--runs-dir", str(tmp_path / "runs"), "--memory-dir", str(tmp_path / "memory"), *extra]
    frontend = Frontend(list(replies))
    asyncio.run(mac_agent.run_agent(mac_agent.build_parser().parse_args(argv), frontend=frontend))
    return model_inputs, frontend, executors


def memory_messages(messages):
    return [message for message in messages
            if message["role"] == "user" and isinstance(message["content"], str)
            and message["content"].startswith("# Long-term memory")
            or message["role"] == "user" and isinstance(message["content"], str)
            and message["content"].startswith("## 已失效的长期记忆")]


def seed(tmp_path):
    service = MemoryService(tmp_path / "memory")
    approved = service.propose({"title": "钉钉周报流程", "content": "已批准正文：发送前先确认收件人", "app": "钉钉"})
    service.mutate("approve", approved["memory_id"], 1)
    candidate = service.propose({"title": "钉钉周报候选", "content": "候选正文：不应注入模型"})
    return service, approved["memory_id"], candidate["memory_id"]


def test_recall_injects_only_approved_as_data_and_forget_excludes_next_request(tmp_path, monkeypatch):
    service, approved_id, candidate_id = seed(tmp_path)
    forgotten = []

    def forget_then_follow_up(frontend):
        # 用户在两轮之间经宿主命令遗忘；该命令不进入模型。
        forgotten.append(handle_memory_command(frontend, f"forget {approved_id} 2"))
        return "再帮我在钉钉发一次周报"

    model_inputs, frontend, executors = run_cli(tmp_path, monkeypatch, [forget_then_follow_up])
    assert len(model_inputs) == 2 and forgotten[0].startswith("已遗忘")
    first, second = model_inputs
    system = json.dumps([m for m in first if m["role"] == "system"], ensure_ascii=False)
    assert "主动记忆候选" in system and "已批准正文" not in system
    blocks = memory_messages(first)
    assert len(blocks) == 1 and "已批准正文：发送前先确认收件人" in blocks[0]["content"]
    assert "不构成新的授权" in blocks[0]["content"]
    for request in model_inputs:
        text = json.dumps(request, ensure_ascii=False)
        assert "候选正文" not in text and candidate_id not in text
        assert "/memory" not in json.dumps([m for m in request if m["role"] == "user"
                                            and m not in memory_messages(request)], ensure_ascii=False)
    # 历史不可抹：第二次请求不再注入正文，并明确旧 id 已失效。
    current = memory_messages(second)
    assert len(current) == 1 and "已批准正文" not in current[0]["content"]
    assert "已失效" in current[0]["content"] and approved_id in current[0]["content"]
    assert executors[0].memory_handler is not None
    assert service.catalog_fields()["approved_count"] == 0


def test_reapproved_memory_invalidates_old_revision_in_cli_and_native_requests(tmp_path, monkeypatch):
    service, memory_id, _ = seed(tmp_path)

    def revise_then_follow_up(frontend):
        assert handle_memory_command(frontend, f"update {memory_id} 2 新版正文：周报先核对附件").startswith("已修改")
        assert service.catalog_fields()["approved_count"] == 0
        assert handle_memory_command(frontend, f"approve {memory_id} 3").startswith("已批准")
        return "再帮我在钉钉发一次周报"

    requests, frontend, _ = run_cli(tmp_path, monkeypatch, [revise_then_follow_up, "继续处理钉钉周报"])

    def check_requests(captured):
        assert len(captured) == 3
        first = memory_messages(captured[0])[0]["content"]
        assert "已批准正文：发送前先确认收件人" in first and f"{memory_id} · r2" in first
        for request in captured[1:]:
            block = memory_messages(request)[0]["content"]
            assert "新版正文：周报先核对附件" in block and f"{memory_id} · r4" in block
            assert "已失效" in block and f"{memory_id} r2" in block
            assert "已批准正文：发送前先确认收件人" not in block
            assert "不构成新的授权" in block

    check_requests(requests)
    statuses = [event.payload for event in frontend.events if event.kind == "context_status"]
    assert [status["memory_count"] for status in statuses] == [1, 1, 1]

    # NativeHost 真实控制命令与共享 run_agent 接线；模型、桌面和 SDK 均为离线 fixture。
    native_service = MemoryService(tmp_path / "native" / "memory")
    created = native_service.propose({"title": "钉钉周报流程", "content": "已批准正文：发送前先确认收件人", "app": "钉钉"})
    memory_id = created["memory_id"]
    assert native_service.mutate("approve", memory_id, 1)[0]["ok"]
    native_requests = []
    install_fakes(monkeypatch, native_requests)

    async def scenario():
        events = []
        host = NativeHost(events.append, tmp_path / "native" / "runs")
        try:
            await host.command({"command": "start", "task_id": "versions", "text": "帮我在钉钉发周报",
                                "config": {"llm_base_url": "https://offline.invalid/v1", "model_name": "fixture",
                                           "allow_empty_api_key": True, "startup_observation": "none", "countdown": 0}})
            await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == 0)
            control = host.control
            before = (control.foreground_allowed, control.authorization_epoch, control.policy_revision)
            await host.command({"command": "memory_update", "command_id": "edit", "task_id": "versions",
                                "memory_id": memory_id, "revision": 2, "content": "新版正文：周报先核对附件"})
            assert host.memory_service.catalog_fields()["approved_count"] == 0
            await host.command({"command": "memory_approve", "command_id": "review", "task_id": "versions",
                                "memory_id": memory_id, "revision": 3})
            assert (control.foreground_allowed, control.authorization_epoch, control.policy_revision) == before
            for index in (1, 2):
                await host.command({"command": "message", "task_id": "versions", "message_id": f"next-{index}",
                                    "text": "继续处理钉钉周报"})
                await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == index)
        finally:
            await host.close()

    asyncio.run(scenario())
    check_requests(native_requests)


def test_request_memory_budget_and_usage_count_include_only_rendered_entries(tmp_path, monkeypatch):
    from memory_store import RECALL_CHARS

    service = MemoryService(tmp_path / "memory")
    for index in range(2):
        created = service.propose({"title": f"钉钉周报流程 {index}",
                                   "content": f"{index}" + "说明" * 950, "app": "钉钉"})
        assert service.mutate("approve", created["memory_id"], 1)[0]["ok"]
    short = service.propose({"title": "钉钉周报", "content": "先核对附件", "app": "钉钉"})
    service.mutate("approve", short["memory_id"], 1)

    requests, frontend, _ = run_cli(tmp_path, monkeypatch, ["再按流程发一次钉钉周报"],
                                    task="帮我按流程在钉钉发周报")

    assert len(requests) == 2
    for request in requests:
        blocks = memory_messages(request)
        assert len(blocks) == 1 and len(blocks[0]["content"]) <= RECALL_CHARS
        assert blocks[0]["content"].count("- [m_") == 2
        assert "先核对附件" in blocks[0]["content"]
    # 真 run_agent 的共享状态接线；模型、SDK 和桌面均为离线 fixture，不是 GUI 验收。
    statuses = [event.payload for event in frontend.events if event.kind == "context_status"]
    assert len(statuses) == 2 and all(status["memory_count"] == 2 for status in statuses)


def test_request_refills_memory_after_final_budget_skip_in_cli_and_native(tmp_path, monkeypatch):
    from memory_store import RECALL_CHARS, RECALL_LIMIT

    def prepare(root):
        service = MemoryService(root)
        for index in range(5):
            created = service.propose({"title": f"钉钉周报流程 {index}",
                                       "content": f"{index}" + "说" * 739, "app": "钉钉"})
            assert service.mutate("approve", created["memory_id"], 1)[0]["ok"]
        short = service.propose({"title": "钉钉周报", "content": "补召回正文：先核对附件", "app": "钉钉"})
        assert service.mutate("approve", short["memory_id"], 1)[0]["ok"]
        return short["memory_id"]

    def check(requests, memory_id):
        assert len(requests) == 2
        for request in requests:
            blocks = memory_messages(request)
            assert len(blocks) == 1
            block = blocks[0]["content"]
            assert len(block) <= RECALL_CHARS and block.count("- [m_") == RECALL_LIMIT
            assert f"- [{memory_id} · r2" in block and "补召回正文：先核对附件" in block

    memory_id = prepare(tmp_path / "memory")
    requests, frontend, _ = run_cli(tmp_path, monkeypatch, ["继续按流程处理钉钉周报"],
                                    task="帮我按流程在钉钉发周报")
    check(requests, memory_id)
    statuses = [event.payload for event in frontend.events if event.kind == "context_status"]
    assert [status["memory_count"] for status in statuses] == [RECALL_LIMIT, RECALL_LIMIT]

    # 真 NativeHost/run_agent 与事件投影；模型、SDK、桌面均为离线 fixture。
    memory_id = prepare(tmp_path / "native" / "memory")
    native_requests = []
    install_fakes(monkeypatch, native_requests)

    async def scenario():
        events = []
        host = NativeHost(events.append, tmp_path / "native" / "runs")
        try:
            await host.command({"command": "start", "task_id": "refill", "text": "帮我按流程在钉钉发周报",
                                "config": {"llm_base_url": "https://offline.invalid/v1", "model_name": "fixture",
                                           "allow_empty_api_key": True, "startup_observation": "none", "countdown": 0}})
            await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == 0)
            await host.command({"command": "message", "task_id": "refill", "message_id": "next",
                                "text": "继续按流程处理钉钉周报"})
            await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == 1)
        finally:
            await host.close()
        statuses = [event for event in events if event.get("kind") == "context_status"]
        assert [status["memory_count"] for status in statuses] == [RECALL_LIMIT, RECALL_LIMIT]
        for status in statuses:
            validate_event(dict(status))

    asyncio.run(scenario())
    check(native_requests, memory_id)


def test_request_app_scope_does_not_recall_shared_vendor_experience(tmp_path, monkeypatch):
    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "保存文件", "content": "Chrome 专属经验：保存后核对文件格式",
                               "app": "Google Chrome"})
    assert service.mutate("approve", created["memory_id"], 1)[0]["ok"]

    requests, frontend, _ = run_cli(tmp_path, monkeypatch, ["改用 Google Chrome 保存文件"],
                                    task="在 Google Docs 保存文件")

    assert len(requests) == 2
    assert memory_messages(requests[0]) == []
    assert "Chrome 专属经验" not in json.dumps(requests[0], ensure_ascii=False)
    blocks = memory_messages(requests[1])
    assert len(blocks) == 1 and "Chrome 专属经验" in blocks[0]["content"]
    statuses = [event.payload for event in frontend.events if event.kind == "context_status"]
    assert [status["memory_count"] for status in statuses] == [0, 1]

    # 原生宿主走同一真实 run_agent；桌面和模型仍为离线 fixture。
    native_requests = []
    install_fakes(monkeypatch, native_requests)

    async def native_scenario():
        events = []
        host = NativeHost(events.append, tmp_path / "native-runs")
        try:
            await host.command({"command": "start", "task_id": "scope", "text": "在 Google Docs 保存文件",
                                "config": {"llm_base_url": "https://offline.invalid/v1",
                                           "model_name": "fixture", "allow_empty_api_key": True,
                                           "startup_observation": "none", "countdown": 0}})
            await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == 0)
            await host.command({"command": "message", "task_id": "scope", "message_id": "next",
                                "text": "改用 Google Chrome 保存文件"})
            await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == 1)
        finally:
            await host.close()

    asyncio.run(native_scenario())
    assert len(native_requests) == 2
    assert memory_messages(native_requests[0]) == []
    assert "Chrome 专属经验" not in json.dumps(native_requests[0], ensure_ascii=False)
    native_blocks = memory_messages(native_requests[1])
    assert len(native_blocks) == 1 and "Chrome 专属经验" in native_blocks[0]["content"]


@pytest.mark.parametrize("failure_mode", ["exclusive_lock", "connection_setup"])
@pytest.mark.parametrize("frontend_mode", ["cli", "native"])
def test_memory_read_outage_and_recovery_reach_requests_without_false_revocation(tmp_path, monkeypatch, frontend_mode, failure_mode):
    import sqlite3
    import memory_store

    _, memory_id, candidate_id = seed(tmp_path)
    monkeypatch.setattr(memory_store, "_BUSY_TIMEOUT", 0.01)
    connection = sqlite3.connect(tmp_path / "memory" / memory_store.DB_NAME)
    setup_failed = False
    original_connect = sqlite3.connect

    class SetupFailure(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if setup_failed and statement == "PRAGMA foreign_keys=ON":
                raise sqlite3.OperationalError("private-setup-diagnostic")
            return super().execute(statement, *args, **kwargs)

    if failure_mode == "connection_setup":
        monkeypatch.setattr(memory_store.sqlite3, "connect",
                            lambda *args, **kwargs: original_connect(*args, factory=SetupFailure, **kwargs))

    def begin_failure():
        nonlocal setup_failed
        if failure_mode == "exclusive_lock":
            connection.execute("BEGIN EXCLUSIVE")
        else:
            setup_failed = True

    def end_failure():
        nonlocal setup_failed
        connection.rollback()
        setup_failed = False

    try:
        if frontend_mode == "cli":
            def lock_then_continue(_frontend):
                begin_failure()
                return "继续处理钉钉周报"

            def unlock_then_continue(_frontend):
                end_failure()
                return "继续处理钉钉周报"

            requests, frontend, _ = run_cli(tmp_path, monkeypatch, [lock_then_continue, unlock_then_continue])
            statuses = [event.payload for event in frontend.events if event.kind == "context_status"]
        else:
            requests = []
            install_fakes(monkeypatch, requests)

            async def scenario():
                events = []
                host = NativeHost(events.append, tmp_path / "runs")
                try:
                    await host.command({"command": "start", "task_id": "outage", "text": "帮我在钉钉发周报",
                                        "config": {"llm_base_url": "https://offline.invalid/v1", "model_name": "fixture",
                                                   "allow_empty_api_key": True, "startup_observation": "none", "countdown": 0}})
                    await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == 0)
                    control = host.control
                    before = (control.foreground_allowed, control.authorization_epoch, control.policy_revision)
                    begin_failure()
                    for index in (1, 2):
                        if index == 2:
                            end_failure()
                        await host.command({"command": "message", "task_id": "outage", "message_id": f"next-{index}",
                                            "text": "继续处理钉钉周报"})
                        await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == index)
                    assert (control.foreground_allowed, control.authorization_epoch, control.policy_revision) == before
                finally:
                    await host.close()
                return [event for event in events if event.get("kind") == "context_status"]

            statuses = asyncio.run(scenario())
            for status in statuses:
                validate_event(dict(status))
    finally:
        connection.rollback()
        connection.close()

    # 真 SQLite 排他锁或初始化故障注入 + 共享 run_agent/NativeHost；模型、SDK、桌面仍是离线 fixture。
    assert len(requests) == 3
    assert [status["memory_count"] for status in statuses] == [1, 0, 1]
    for index in (0, 2):
        block = memory_messages(requests[index])[0]["content"]
        assert "已批准正文：发送前先确认收件人" in block and f"{memory_id} · r2" in block
        assert "无法核验" not in block and "## 已失效" not in block
    blocked = memory_messages(requests[1])[0]["content"]
    assert "MEMORY_UNAVAILABLE" in blocked and "无法核验" in blocked and "不得作为依据" in blocked
    assert "已批准正文" not in blocked and "已被用户删除或修改" not in blocked
    assert len(blocked) <= memory_store.RECALL_CHARS
    assert all(candidate_id not in json.dumps(request, ensure_ascii=False) for request in requests)
    assert "private-setup-diagnostic" not in json.dumps(requests, ensure_ascii=False)


@pytest.mark.parametrize("frontend_mode", ["cli", "native"])
def test_initial_memory_directory_outage_does_not_abort_task_and_recovers(tmp_path, monkeypatch, frontend_mode):
    import os
    import memory_store

    _, memory_id, candidate_id = seed(tmp_path)
    root = tmp_path / "memory"
    original_lstat = memory_store.os.lstat
    unavailable = True

    def lstat(path, *args, **kwargs):
        if unavailable and os.fspath(path) == str(root):
            raise PermissionError("private-directory-diagnostic")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(memory_store.os, "lstat", lstat)
    if frontend_mode == "cli":
        def recover_then_continue(_frontend):
            nonlocal unavailable
            unavailable = False
            return "继续处理钉钉周报"

        requests, frontend, _ = run_cli(tmp_path, monkeypatch, [recover_then_continue])
        statuses = [event.payload for event in frontend.events if event.kind == "context_status"]
    else:
        requests = []
        install_fakes(monkeypatch, requests)

        async def scenario():
            nonlocal unavailable
            events = []
            host = NativeHost(events.append, tmp_path / "runs")
            try:
                await host.command({"command": "memory_list", "command_id": "unavailable"})
                catalog = next(event for event in events if event.get("event") == "memory_catalog")
                assert catalog["available"] is False and catalog["error_code"] == "MEMORY_UNAVAILABLE"
                # 无 live 会话的连接级目录沿用 legacy 帧，不套任务 v1 信封。
                assert catalog["task_id"] is None and "schema_version" not in catalog
                assert catalog["source_command_id"] == "unavailable"
                await host.command({"command": "start", "task_id": "initial-outage", "text": "帮我在钉钉发周报",
                                    "config": {"llm_base_url": "https://offline.invalid/v1", "model_name": "fixture",
                                               "allow_empty_api_key": True, "startup_observation": "none", "countdown": 0}})
                await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == 0)
                control = host.control
                before = (control.foreground_allowed, control.authorization_epoch, control.policy_revision)
                unavailable = False
                await host.command({"command": "message", "task_id": "initial-outage", "message_id": "next",
                                    "text": "继续处理钉钉周报"})
                await wait_for(events, lambda event: event["event"] == "turn_finished" and event["turn_index"] == 1)
                assert (control.foreground_allowed, control.authorization_epoch, control.policy_revision) == before
                assert host.memory_service.catalog_fields()["available"]
                assert "private-directory-diagnostic" not in json.dumps(events, ensure_ascii=False)
            finally:
                await host.close()
            return [event for event in events if event.get("kind") == "context_status"]

        statuses = asyncio.run(scenario())
        for status in statuses:
            validate_event(dict(status))

    # 真目录检查故障与 run_agent/NativeHost 接线；模型、SDK、桌面为离线 fixture。
    assert len(requests) == 2 and [status["memory_count"] for status in statuses] == [0, 1]
    blocked = memory_messages(requests[0])[0]["content"]
    assert "MEMORY_UNAVAILABLE" in blocked and "无法核验" in blocked and "不得作为依据" in blocked
    assert "已批准正文" not in blocked and "已被用户删除或修改" not in blocked
    recovered = memory_messages(requests[1])[0]["content"]
    assert "已批准正文：发送前先确认收件人" in recovered and f"{memory_id} · r2" in recovered
    assert "无法核验" not in recovered and "## 已失效" not in recovered
    assert candidate_id not in json.dumps(requests, ensure_ascii=False)
    assert "private-directory-diagnostic" not in json.dumps(requests, ensure_ascii=False)


def test_no_match_and_disabled_memory_change_nothing(tmp_path, monkeypatch):
    seed(tmp_path)
    model_inputs, _, _ = run_cli(tmp_path, monkeypatch, [], task="打开计算器算 1+1")
    assert memory_messages(model_inputs[0]) == []
    assert "Long-term memory" not in json.dumps(model_inputs[0][1:], ensure_ascii=False)
    model_inputs, frontend, executors = run_cli(tmp_path, monkeypatch, [], "--disable-memory")
    text = json.dumps(model_inputs[0], ensure_ascii=False)
    assert "主动记忆候选" not in text and "已批准正文" not in text
    assert executors[0].memory_handler is None
    assert "未启用" in handle_memory_command(frontend, "list")


def test_native_proposal_uses_host_identity_publishes_catalog_and_is_not_recalled(tmp_path, monkeypatch):
    stored = []

    async def propose_once(executor):
        if executor.calls == 1:
            stored.append(await executor.memory_handler(
                {"title": "钉钉日报流程", "content": "候选正文：日报先发草稿给自己", "kind": "workflow", "app": "钉钉"},
                {"session_id": "forged", "step": 0, "task_id": "forged"}))

    model_inputs = []
    install_fakes(monkeypatch, model_inputs, on_run=propose_once)
    (tmp_path / "runs").mkdir()

    async def scenario():
        events = []
        host = NativeHost(events.append, tmp_path / "runs")
        await host.command({"command": "start", "task_id": "a", "text": "帮我写钉钉日报", "config": {
            "endpoint": "http://127.0.0.1:9999/v1", "model": "fixture", "api_key": "private-test-key"}})
        try:
            await wait_for(events, lambda e: e["event"] == "turn_finished" and e["turn_index"] == 0)
            await host.command({"command": "message", "task_id": "a", "message_id": "m2", "text": "再写一次钉钉日报"})
            await wait_for(events, lambda e: e["event"] == "turn_finished" and e["turn_index"] == 1)
            run_dir = host.control.run_dir
            await host.command({"command": "end", "task_id": "a", "command_id": "end"})
        finally:
            await host.close()
        return events, run_dir

    events, run_dir = asyncio.run(scenario())
    assert stored[0]["ok"] and stored[0]["status"] == "candidate"
    memory_id = stored[0]["memory_id"]
    catalogs = [event for event in events if event["event"] == "memory_catalog"]
    assert len(catalogs) == 1 and catalogs[0]["source_command_id"] is None
    assert catalogs[0]["native_task_id"] == "a" and catalogs[0]["candidate_count"] == 1
    validate_event(dict(catalogs[0]))
    entry = catalogs[0]["entries"][0]
    assert entry["id"] == memory_id and entry["status"] == "candidate" and entry["validation"] == "pending_user_review"
    trajectory = json.loads((run_dir / "traj.json").read_text())
    assert entry["source"]["session_id"] not in (None, "forged") and entry["source"]["step"] == 0
    if trajectory.get("session_id"):
        assert entry["source"]["session_id"] == trajectory["session_id"]
    assert trajectory["memory_proposals"][0]["memory_id"] == memory_id
    assert "候选正文" not in json.dumps(trajectory, ensure_ascii=False)
    assert len(model_inputs) == 2
    assert all("候选正文" not in json.dumps(request, ensure_ascii=False) for request in model_inputs)
    assert all(memory_messages(request) == [] for request in model_inputs)


async def wait_for(events, predicate):
    async def wait():
        while not any(predicate(event) for event in events):
            await asyncio.sleep(0.01)
    try:
        await asyncio.wait_for(wait(), 5)
    except TimeoutError:
        raise AssertionError([(e.get("event"), e.get("turn_index"), str(e.get("text") or e.get("message") or "")[:160]) for e in events]) from None
