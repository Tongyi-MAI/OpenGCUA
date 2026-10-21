"""原生 session 离线契约：真实控制对象，fixture runner，不请求模型或操作桌面。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import socket
import subprocess

import pytest

from agent_events import AgentEvent, AgentPrompt, ForegroundApprovalPrompt
from gui_approval import GuiGrantStore
from native_host import NativeFrontend, NativeHost, TaskControl
from skill_context import SkillContext
from skill_registry import SkillRegistry


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 3)


def approval(request_id="approval-1", task_index=0):
    return ForegroundApprovalPrompt(request_id, "允许本次任务的前台操作？", task_index, "fixture.app", "window-1")


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("离线契约禁止网络连接或启动外部进程")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    import skill_registry
    bundle, personal = tmp_path / "skills", tmp_path / "personal"
    for name in ("example", "other"):
        folder = bundle / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Fixture {name}\n---\n\nUNIQUE_{name}_BODY\n",
            encoding="utf-8",
        )
    # 不扫描用户的 Skill、配置或凭据目录。
    monkeypatch.setattr(skill_registry, "default_skill_roots", lambda _: (bundle, personal))
    result = SkillRegistry(bundle, personal)
    result.discover()
    return result


class OfflineRunner:
    """仅替代执行层；控制分派、队列、审批、关联 ID 均来自真实实现。"""
    def __init__(self, registry):
        self.skills = SkillContext(registry)
        self.grants = GuiGrantStore()
        self.ready = asyncio.Event()
        self.finish_turn = asyncio.Event()
        self.compaction_started = asyncio.Event()
        self.compaction_release = asyncio.Event()
        self.compaction_release.set()
        self.compaction_cancelled = asyncio.Event()
        self.compacting = False
        self.compaction_calls = []
        self.commits = []
        self.skill_calls = []
        self.skill_failure = False
        self.compaction_failure = False
        self.policy_calls = []
        self.interrupts = []
        self.turns = []
        self.closed = False

    def policy(self):
        c = self.control
        self.grants.set_session_allowed(c.foreground_allowed)
        self.policy_calls.append((c.foreground_allowed, c.ask_user_enabled))

    def select(self, name):
        self.skill_calls.append(name)
        if self.skill_failure:
            raise RuntimeError("fixture Skill callback failed")
        self.skills.select(name)
        self.control.config["selected_skill"] = self.skills.selected_name
        record = (self.skills.records() or [{}])[0]
        self.control.emit("skill_state", name=name, pending=False,
                          source=record.get("source", ""), digest=record.get("digest", ""))

    async def compact(self):
        c = self.control
        self.compaction_calls.append(c.current_command_id)
        self.compacting = True
        self.compaction_started.set()
        try:
            await self.compaction_release.wait()
            if self.compaction_failure:
                raise RuntimeError("fixture compaction callback failed")
            status = "cancelled" if self.compaction_cancelled.is_set() else "completed"
            if status == "completed":
                self.commits.append(c.current_command_id)
            self.frontend.emit(AgentEvent("control_result", "fixture summary result", payload={
                "status": status, "budget_before": 100, "budget_after": 40 if self.commits else 100,
            }))
        finally:
            self.compacting = False

    def interrupt(self, force):
        self.interrupts.append(force)
        if self.compacting:
            self.compaction_cancelled.set()
            self.compaction_release.set()
        else:
            self.frontend.emit(AgentEvent("aborted", "本轮已中止"))
            self.finish_turn.set()

    async def __call__(self, args, host_control):
        self.control = host_control
        self.frontend = NativeFrontend(host_control)
        self.frontend.bind_abort(self.interrupt)
        host_control.bind_runtime(policy=self.policy, skill=self.select, compaction=self.compact)
        if host_control.config.get("selected_skill"):
            self.select(host_control.config["selected_skill"])
        self.turns.append((args.task, self.skills.selected_name))
        self.ready.set()
        try:
            while True:
                await self.finish_turn.wait()
                self.finish_turn.clear()
                if host_control.active_turn:
                    host_control.complete_turn("success", "fixture turn result")
                text = await host_control.follow_up()
                if text is None:
                    break
                self.skills.start_task(host_control.turn_index + 1)
                host_control.begin_turn(text)
                self.turns.append((text, self.skills.selected_name))
        finally:
            self.closed = True


@pytest.fixture
def session(tmp_path, registry):
    @asynccontextmanager
    async def opened(config=None, task_id="session-a", resume_path=None):
        events = []
        runner = OfflineRunner(registry)
        host = NativeHost(events.append, tmp_path / "runs", runner=runner)
        command = {"command": "start", "task_id": task_id, "text": "first instruction", "config": {
            "llm_base_url": "https://offline.invalid/v1", "model_name": "fixture",
            "allow_empty_api_key": True, **(config or {}),
        }}
        if resume_path is not None:
            command["resume_path"] = str(resume_path)
        try:
            await host.command(command)
            await until(lambda: runner.ready.is_set() or host.task.done())
            assert runner.ready.is_set(), "fixture runner 未启动；请检查当前生产代码导入/配置错误"
            yield host, runner, events
        finally:
            await host.close()
    return opened


async def send(host, kind, command_id=None, **fields):
    await host.command({"command": kind, "task_id": host.control.task_id,
                        **({"command_id": command_id} if command_id else {}), **fields})


def results(events, command_id):
    return [e for e in events if e["event"] == "control_result" and e.get("command_id") == command_id]


def test_foreground_policy_is_session_scoped_cross_turn_and_revocable(session):
    async def scenario():
        async with session({"foreground_allowed": True}) as (host, runner, events):
            c = host.control
            assert not c.foreground_allowed  # 文件不能预先授予实时会话权限。
            await send(host, "set_policy", "allow", foreground_allowed=True)
            assert runner.policy_calls[-1] == (True, True)
            await send(host, "message", message_id="next", text="second instruction")
            runner.finish_turn.set()
            await until(lambda: len(runner.turns) == 2)
            assert host.control is c and not host.task.done()
            assert c.foreground_allowed and runner.grants.state(1) == "allowed"
            count = sum(e["event"] == "approval" for e in events)
            assert await runner.frontend.choose_approval(approval("next-window", 1)) == "allow"
            assert sum(e["event"] == "approval" for e in events) == count
            await send(host, "set_policy", "revoke", foreground_allowed=False)
            assert not c.foreground_allowed and runner.grants.state(1) == "unset"
            assert results(events, "allow")[-1]["status"] == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("choice", ["allow", "reject", "cancel"])
def test_disabled_business_questions_do_not_disable_typed_approval(session, choice):
    async def scenario():
        async with session({"ask_user_enabled": False}) as (host, runner, events):
            c = host.control
            with pytest.raises(ValueError, match="ASK_USER_DISABLED"):
                await c.ask("business question")
            waiting = asyncio.create_task(runner.frontend.choose_approval(approval()))
            await until(lambda: c.approval_id is not None)
            card = next(e for e in events if e["event"] == "approval")
            assert card["scope"] == "session" and "本次任务" not in card["text"]
            assert not any(e["event"] == "question" for e in events)
            with pytest.raises(ValueError, match="过期"):
                await send(host, "approval_reply", "stale-card", approval_id="old", choice="allow")
            with pytest.raises(ValueError, match="过期"):
                await host.command({"command": "approval_reply", "task_id": "old-session",
                                    "approval_id": c.approval_id, "choice": "allow", "command_id": "stale-session"})
            with pytest.raises(ValueError, match="选项"):
                await send(host, "approval_reply", approval_id=c.approval_id, choice="yes")
            assert not waiting.done() and not c.foreground_allowed
            await send(host, "approval_reply", "reply", approval_id=c.approval_id, choice=choice)
            # Future 已结束，即使卡片清理还没运行，重复批准也必须拒绝。
            with pytest.raises(ValueError, match="过期"):
                await send(host, "approval_reply", "duplicate", approval_id=c.approval_id, choice="allow")
            assert await asyncio.wait_for(waiting, 3) == choice
            assert c.foreground_allowed is (choice == "allow") and not c.ask_user_enabled
            assert c.approval_wait is None and c.approval_id is None
            assert [e["choice"] for e in events if e["event"] == "approval_closed"] == [choice]
            assert not any(e.get("command_id") in {"stale-card", "stale-session", "duplicate"}
                           for e in events if e["event"] == "command_ack")
    asyncio.run(scenario())


def test_revoke_cancels_pending_approval_without_granting_permission(session):
    async def scenario():
        async with session() as (host, runner, events):
            waiting = asyncio.create_task(host.control.choose_approval(approval()))
            await until(lambda: host.control.approval_id is not None)
            await send(host, "set_policy", foreground_allowed=False)
            with pytest.raises(ValueError, match="过期"):
                await send(host, "approval_reply", approval_id="approval-1", choice="allow")
            assert await asyncio.wait_for(waiting, 3) == "cancel"
            assert not host.control.foreground_allowed and not runner.grants.session_allowed
    asyncio.run(scenario())


def test_disabling_ask_cancels_existing_question_but_keeps_session(session):
    async def scenario():
        async with session() as (host, runner, events):
            question = asyncio.create_task(host.control.ask("Which file?"))
            await until(lambda: host.control.question_id is not None)
            old_id = host.control.question_id
            await send(host, "set_policy", ask_user_enabled=False)
            with pytest.raises(asyncio.CancelledError):
                await question
            assert not host.task.done() and not host.control.ended
            assert runner.interrupts == [False] and not host.control.ask_user_enabled
            with pytest.raises(ValueError):
                await send(host, "reply", question_id=old_id, text="stale")
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("foreground_allowed", 1), ("foreground_allowed", "true"),
                                         ("ask_user_enabled", 0), ("ask_user_enabled", None)])
def test_policy_requires_real_booleans(session, field, value):
    async def scenario():
        async with session() as (host, runner, events):
            before = len(runner.policy_calls)
            with pytest.raises(ValueError, match="布尔"):
                await send(host, "set_policy", "invalid", **{field: value})
            assert len(runner.policy_calls) == before
            assert not host.control.foreground_allowed and host.control.ask_user_enabled
            assert not any(e.get("command_id") == "invalid" for e in events)
    asyncio.run(scenario())


def test_queue_fifo_consumes_one_message_per_turn_not_per_step(session):
    async def scenario():
        async with session() as (host, runner, events):
            for name in ("first", "second", "third"):
                await send(host, "message", message_id=name, text=name)
            await host.control.boundary()
            await send(host, "queue_list", "list")
            assert [m["id"] for m in host.control.messages] == ["first", "second", "third"]
            for count in range(2, 5):
                runner.finish_turn.set()
                await until(lambda: len(runner.turns) == count)
                assert len(host.control.messages) == 4 - count
            assert [text for text, _ in runner.turns[1:]] == ["first", "second", "third"]
            assert [e["message_id"] for e in events if e["event"] == "message" and e["state"] == "consumed"] == ["first", "second", "third"]
            await send(host, "message", message_id="first", text="duplicate must not execute")
            assert host.control.messages == []
    asyncio.run(scenario())


def test_interrupt_suspends_old_queue_correction_runs_then_manual_resume(session):
    async def scenario():
        async with session() as (host, runner, events):
            c, task = host.control, host.task
            await send(host, "set_policy", foreground_allowed=True, ask_user_enabled=False)
            for name in ("old-1", "old-2"):
                await send(host, "message", message_id=name, text=name)
            await send(host, "interrupt", "interrupt")
            await until(lambda: c.waiting_for_follow_up)
            assert host.task is task and not task.done() and not c.ended
            assert c.suspended and not c.active_turn and c.foreground_allowed and not c.ask_user_enabled
            assert runner.interrupts == [False] and not runner.closed
            interrupted = next(e for e in events if e["event"] == "interrupted")
            assert interrupted["worker_reset"] is False  # fixture 未重建执行器，不能虚报环境重置。
            assert not any(e["event"] == "finished" for e in events)
            await send(host, "message", message_id="fix", text="correction")
            await until(lambda: len(runner.turns) == 2)
            assert runner.turns[-1][0] == "correction" and c.suspended
            runner.finish_turn.set()
            await until(lambda: c.waiting_for_follow_up)
            assert [m["id"] for m in c.messages] == ["old-1", "old-2"]
            await send(host, "queue_resume", "manual")
            await until(lambda: len(runner.turns) == 3)
            assert runner.turns[-1][0] == "old-1" and not c.suspended
            await send(host, "queue_clear", "clear")
            assert not c.messages and c.message_states["old-2"] == "cancelled"
            assert c.message_states["old-1"] == "consumed"
    asyncio.run(scenario())


def test_clear_queue_only_cancels_pending_items_not_question_or_applied_skill(session):
    async def scenario():
        async with session({"selected_skill": "example"}) as (host, runner, events):
            c = host.control
            await send(host, "message", message_id="pending", text="pending")
            await send(host, "compaction", "compact-pending")
            await send(host, "skill_select", "skill-pending", name="other")
            question = asyncio.create_task(c.ask("current question"))
            await until(lambda: c.question_id is not None)
            await send(host, "queue_clear", "clear")
            assert not c.messages and not c.commands and not question.done()
            assert runner.skills.selected_name == "example" and not runner.compaction_calls
            assert c.message_states["pending"] == "cancelled"
            for command_id in ("compact-pending", "skill-pending"):
                assert [e["status"] for e in results(events, command_id)] == ["cancelled"]
            await send(host, "reply", question_id=c.question_id, text="answer")
            assert await asyncio.wait_for(question, 3) == "answer"
    asyncio.run(scenario())


def test_compaction_callback_runs_only_at_turn_boundary_with_correlated_result(session):
    async def scenario():
        async with session() as (host, runner, events):
            await send(host, "compaction", "compact")
            await host.control.boundary()  # step/pause boundary 不能消费手动控制。
            assert runner.compaction_calls == [] and results(events, "compact") == []
            assert any(e["event"] == "command_ack" and e["command_id"] == "compact" for e in events)
            runner.finish_turn.set()
            await until(lambda: bool(runner.commits))
            assert runner.compaction_calls == runner.commits == ["compact"]
            assert [e["status"] for e in results(events, "compact")] == ["started", "completed"]
            assert results(events, "compact")[-1]["budget_after"] == 40
            assert host.control.current_command_id is None and not host.task.done()
    asyncio.run(scenario())


def test_cancel_running_compaction_preserves_pending_message_and_no_commit(session):
    async def scenario():
        async with session() as (host, runner, events):
            runner.compaction_release.clear()
            await send(host, "compaction", "compact")
            runner.finish_turn.set()
            await asyncio.wait_for(runner.compaction_started.wait(), 3)
            await send(host, "message", message_id="later", text="later")
            host.control.suspended = True
            await send(host, "compaction_cancel", "cancel")
            await until(lambda: any(e["status"] == "cancelled" for e in results(events, "compact")))
            assert not runner.commits and runner.interrupts == [False]
            assert host.control.messages == [{"id": "later", "text": "later"}]
            assert host.control.message_states["later"] == "queued" and host.control.suspended
            assert not host.task.done() and host.control.current_command_id is None
    asyncio.run(scenario())


def test_compaction_callback_failure_is_result_not_runner_retry(session):
    async def scenario():
        async with session() as (host, runner, events):
            runner.compaction_failure = True
            await send(host, "compaction", "broken")
            runner.finish_turn.set()
            await until(lambda: any(e["status"] == "failed" for e in results(events, "broken")))
            assert runner.compaction_calls == ["broken"] and not runner.commits
            assert "callback failed" in results(events, "broken")[-1]["text"]
            assert not host.task.done() and host.control.current_command_id is None
    asyncio.run(scenario())


def test_skill_select_pending_during_turn_applies_before_next_message(session):
    async def scenario():
        async with session({"selected_skill": "example"}) as (host, runner, events):
            await send(host, "skill_select", "select", name="other")
            pending = [e for e in events if e["event"] == "skill_state"][-1]
            assert pending["name"] == "other" and pending["pending"] is True
            assert runner.skills.selected_name == "example" and runner.skill_calls == ["example"]
            await send(host, "message", message_id="next", text="next instruction")
            runner.finish_turn.set()
            await until(lambda: len(runner.turns) == 2)
            assert runner.turns == [("first instruction", "example"), ("next instruction", "other")]
            applied = [e for e in events if e["event"] == "skill_state"][-1]
            assert not applied["pending"] and applied["digest"] == runner.skills.records()[0]["digest"]
            assert [e["status"] for e in results(events, "select")] == ["completed"]
            await send(host, "skill_select", "unset", name=None)
            await send(host, "message", message_id="last", text="ordinary message")
            runner.finish_turn.set()
            await until(lambda: len(runner.turns) == 3)
            assert runner.turns[-1] == ("ordinary message", None)
            assert runner.skills.system_slot() == ""
    asyncio.run(scenario())


def test_skill_callback_failure_preserves_selection_and_next_turn(session):
    async def scenario():
        async with session({"selected_skill": "example"}) as (host, runner, events):
            runner.skill_failure = True
            await send(host, "skill_select", "broken", name="other")
            await send(host, "message", message_id="next", text="next")
            runner.finish_turn.set()
            await until(lambda: len(runner.turns) == 2)
            assert [e["status"] for e in results(events, "broken")] == ["failed"]
            assert "callback failed" in results(events, "broken")[-1]["text"]
            assert runner.turns[-1] == ("next", "example") and runner.skill_calls == ["example", "other"]
            assert host.control.config["selected_skill"] == "example" and not host.task.done()
            with pytest.raises(ValueError):
                await send(host, "skill_select", "unknown", name="missing")
            assert not host.control.commands
            assert not any(e.get("command_id") == "unknown" for e in events)
    asyncio.run(scenario())


@pytest.mark.parametrize("callback_fails", [False, True])
def test_task_control_skill_dispatch_at_follow_up_boundary(registry, callback_fails):
    """直接测控制分派；上面的 Host 测试仍保留真实命名选择预检失败。"""
    async def scenario():
        events = []
        c = TaskControl("session", events.append)
        runner = OfflineRunner(registry)
        runner.control, runner.frontend = c, NativeFrontend(c)
        c.bind_runtime(policy=runner.policy, skill=runner.select, compaction=runner.compact)
        runner.select("example")
        runner.skill_failure = callback_fails
        c.commands.append({"command": "skill_select", "command_id": "select", "name": "other"})
        c.message("next", "ordinary next instruction")
        await c.boundary()
        assert runner.skill_calls == ["example"] and runner.skills.selected_name == "example"
        c.complete_turn("success", "done")
        text = await asyncio.wait_for(c.follow_up(), 3)
        c.begin_turn(text)
        assert text == "ordinary next instruction" and c.turn_index == 1
        expected = "example" if callback_fails else "other"
        assert runner.skills.selected_name == c.config["selected_skill"] == expected
        assert runner.skills.system_slot().count(f"UNIQUE_{expected}_BODY") == 1
        assert runner.skill_calls == ["example", "other"]
        assert [e["status"] for e in results(events, "select")] == ["failed" if callback_fails else "completed"]
        assert c.current_command_id is None and not c.commands and not c.messages
        assert c.message_states["next"] == "consumed" and not c.ended
    asyncio.run(scenario())


def test_new_host_and_history_resume_do_not_inherit_foreground_authorization(session, tmp_path):
    async def scenario():
        async with session() as (host, runner, events):
            await send(host, "set_policy", foreground_allowed=True)
            path = tmp_path / "runs" / "saved"
            path.mkdir(parents=True)
            (path / "traj.json").write_text(json.dumps({
                "config": {"ui_api": "mai-ui"}, "tasks": [{"task": "saved", "status": "success"}],
                "foreground_allowed": True,
            }), encoding="utf-8")
        for resume in (None, path):
            async with session(task_id="new-session", resume_path=resume) as (host, runner, events):
                assert not host.control.foreground_allowed and not runner.grants.session_allowed
                assert runner.policy_calls == [(False, True)]
    asyncio.run(scenario())


def test_native_frontend_semantic_sequence_attempt_and_payload_are_structured():
    events = []
    c = TaskControl("session", events.append)
    c.config = {"llm_protocol": "anthropic", "reasoning_effort": "high"}
    frontend = NativeFrontend(c)
    frontend.emit(AgentEvent("session", payload={"session_id": "runtime-session", "model": "effective"}))
    frontend.emit(AgentEvent("phase", "planning", step=0))
    for attempt in ("attempt-a", "attempt-b"):
        frontend.emit(AgentEvent("model_status", "API wait", step=2, payload={
            "type": "model_status", "attempt_id": attempt, "attempt": 1 if attempt.endswith("a") else 2,
            "stage": "waiting", "max_attempts": 3, "timeout_seconds": 60,
        }))
        for kind in ("code_reset", "intent_delta", "intent_end", "reasoning_delta", "reasoning_end", "code_start", "code_delta", "code_end"):
            payload = {"type": kind, "block_index": 1, "start": kind in {"reasoning_delta", "intent_delta"}}
            frontend.emit(AgentEvent(kind, "中文片段", step=2, payload=payload))
            assert events[-1]["attempt_id"] == attempt and events[-1]["event"] == kind
            assert events[-1]["text"] == "中文片段" and events[-1]["step"] == 2
            assert "type" not in events[-1] and payload["type"] == kind
    c.current_command_id = "summary"
    frontend.emit(AgentEvent("control_result", "result", payload={"status": "completed"}))
    assert events[-1]["command_id"] == "summary"
    c.begin_turn("next")
    frontend.emit(AgentEvent("foreground_consent"))
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["task_id"] == "session" for e in events)
    assert events[-1]["turn_index"] == 1 and events[-1]["event"] == "policy"
    assert events[0]["session_id"] == "runtime-session" and events[0]["protocol"] == "anthropic"
    assert events[1]["event"] == "progress" and events[1]["step"] == 1


def test_native_frontend_aborted_cancels_waits_preserves_session_and_queue():
    async def scenario():
        events = []
        c = TaskControl("session", events.append)
        c.foreground_allowed = True
        c.message("old", "keep me")
        question = asyncio.create_task(c.ask("question"))
        await until(lambda: c.answer is not None)
        # 独立等待的审批 future，模拟门禁与问题同时尚未完成的清理边界。
        c.foreground_allowed = False
        gate = asyncio.create_task(c.choose_approval(approval()))
        await until(lambda: c.approval_wait is not None)
        c.pause()
        NativeFrontend(c).emit(AgentEvent("aborted", "中止"))
        with pytest.raises(asyncio.CancelledError):
            await question
        assert await asyncio.wait_for(gate, 3) == "cancel"
        assert not c.ended and c.suspended and c.waiting_for_follow_up and not c.active_turn
        assert c.resume_event.is_set() and c.messages == [{"id": "old", "text": "keep me"}]
        assert any(e["event"] == "queue_state" and e["suspended"] for e in events)
        assert not any(e["event"] == "finished" for e in events)
        c.end()
        assert await NativeFrontend(c).read(AgentPrompt("task")) is None
    asyncio.run(scenario())


@pytest.fixture
def context_manager(registry):
    from mac_agent import ContextManager
    selected = SkillContext(registry)
    manager = ContextManager(
        task="initial ordinary request", coordinate_range=1000, img_keep_n=0, img_interval=10,
        history_turns=1, micro_compaction=False, micro_trigger_steps=60, micro_keep_turns=25,
        micro_interval=35, todo_diff_snapshot=True, max_image_dimension=1920, jpeg_quality=85,
        skill_context=selected,
    )
    return manager, selected


def request_messages(manager):
    return manager.build_messages(current_feedback="", current_screenshot=None, current_user_response="")


def test_system_skill_slot_is_once_per_request_persistent_and_only_user_replaceable(context_manager):
    manager, selected = context_manager
    assert "UNIQUE_example_BODY" not in json.dumps(request_messages(manager))
    selected.activate("example")  # 空槽允许模型首次自动选择。
    base_prompt = manager.system_prompt
    for turn in range(3):
        if turn:
            manager.set_task(f"ordinary follow-up {turn}")
        for _ in range(2):  # 重建请求不得将 slot 追加到可变 system_prompt 或 user 历史。
            messages = request_messages(manager)
            systems = [m for m in messages if m["role"] == "system"]
            assert len(systems) == 1 and systems[0]["content"].count("UNIQUE_example_BODY") == 1
            assert json.dumps(messages).count("UNIQUE_example_BODY") == 1
            assert all("UNIQUE_example_BODY" not in json.dumps(m) for m in messages if m["role"] != "system")
        assert selected.selection_source == "model" and manager.system_prompt == base_prompt
    with pytest.raises(ValueError, match="单槽"):
        selected.activate("other")
    selected.select("other")
    rendered = json.dumps(request_messages(manager))
    assert rendered.count("UNIQUE_other_BODY") == 1 and "UNIQUE_example_BODY" not in rendered
    selected.select(None)
    assert "UNIQUE_other_BODY" not in json.dumps(request_messages(manager))


def test_ask_policy_is_once_per_request_dynamic_and_not_business_authorization(context_manager):
    manager, selected = context_manager
    selected.activate("example")
    base_prompt = manager.system_prompt
    for enabled in (False, False, True, False):
        manager.ask_user_enabled = enabled
        manager.set_task("ordinary request, not permission")
        messages = request_messages(manager)
        system = messages[0]["content"]
        assert system.count("## ask_user 已关闭") == (0 if enabled else 1)
        if not enabled:
            assert "禁止调用 ask_user" in system and "不借前台审批提问" in system
        assert system.count("UNIQUE_example_BODY") == 1
        assert "不授予权限" in system and "不覆盖用户请求" in system
        assert "ordinary request, not permission" not in system
        assert manager.system_prompt == base_prompt


def test_foreground_policy_projects_once_without_granting_business_permissions(context_manager):
    manager, selected = context_manager
    baseline = manager.system_prompt
    selected.activate("example")
    for state, text in [("allowed", "本 session 已允许前台接管"), ("denied", "本 session 前台接管已拒绝"), ("unset", "本 session 尚未允许前台接管")]:
        manager.foreground_consent = state
        system = request_messages(manager)[0]["content"]
        assert system.count("## 当前前台授权状态（宿主实时提供）") == 1
        assert text in system
        assert "target app is no longer frontmost 不代表授权失效" in system
        assert "业务副作用" in system and "业务信息缺失" in system
        assert system.count("UNIQUE_example_BODY") == 1
        assert manager.system_prompt == baseline


def test_empty_intent_start_and_multiblock_delta_flush_are_forwarded():
    async def scenario():
        events = []
        control = TaskControl("session", events.append)
        frontend = NativeFrontend(control)
        for index, text in [(1, "打开"), (2, "验证")]:
            payload = {"start": True, "block_index": index, "attempt_id": "attempt", "turn_index": 0}
            frontend.emit(AgentEvent("intent_delta", "", step=4, payload=payload))
            assert events[-1]["event"] == "intent_delta" and events[-1]["start"] is True
            frontend.emit(AgentEvent("intent_delta", text, step=4, payload={**payload, "start": False}))
            frontend.emit(AgentEvent("intent_delta", "会议室", step=4, payload={**payload, "start": False}))
            frontend.emit(AgentEvent("intent_end", step=4, payload={"block_index": index, "attempt_id": "attempt"}))
            assert events[-1]["event"] == "intent_end"
            assert events[-2]["text"] == text + "会议室"
            assert events[-2]["block_index"] == index
        frontend.close()
    asyncio.run(scenario())


def test_latest_revoke_wins_over_completed_but_unresumed_approval(session):
    async def scenario():
        async with session() as (host, runner, events):
            waiting = asyncio.create_task(host.control.choose_approval(approval()))
            await until(lambda: host.control.approval_wait is not None)
            await send(host, "approval_reply", "approve", approval_id="approval-1", choice="allow")
            await send(host, "set_policy", "revoke", foreground_allowed=False)
            assert await waiting == "cancel"
            assert not host.control.foreground_allowed
            assert not runner.grants.session_allowed
    asyncio.run(scenario())


def test_ask_switch_does_not_cancel_independent_foreground_card(session):
    async def scenario():
        async with session() as (host, runner, events):
            waiting = asyncio.create_task(host.control.choose_approval(approval()))
            await until(lambda: host.control.approval_wait is not None)
            await send(host, "set_policy", "ask-off", ask_user_enabled=False)
            assert not waiting.done()
            await send(host, "approval_reply", "approve", approval_id="approval-1", choice="allow")
            assert await waiting == "allow"
    asyncio.run(scenario())


def test_boundary_controls_respect_message_order_and_interrupt_suspension(session):
    async def scenario():
        async with session() as (host, runner, events):
            c = host.control
            await send(host, "message", message_id="first", text="first ordinary message")
            await send(host, "skill_select", "skill-after", name="example")
            await send(host, "interrupt", "interrupt")
            await until(lambda: c.suspended)
            await send(host, "message", message_id="correction-1", text="first correction")
            await send(host, "message", message_id="correction-2", text="second correction")
            await until(lambda: len(runner.turns) >= 2)
            assert runner.turns[1][0] == "first correction"
            assert not runner.skill_calls
            runner.finish_turn.set()
            await until(lambda: len(runner.turns) >= 3)
            assert runner.turns[2][0] == "second correction"
            assert not runner.skill_calls
            runner.finish_turn.set()
            await until(lambda: not c.active_turn)
            await send(host, "queue_resume", "resume")
            await until(lambda: len(runner.turns) >= 4)
            assert runner.turns[3][0] == "first ordinary message"
            assert not runner.skill_calls  # 后到的控制仍未越过该消息轮次。
            runner.finish_turn.set()
            await until(lambda: runner.skills.selected_name == "example")
    asyncio.run(scenario())


def test_queued_compaction_cancel_does_not_interrupt_active_turn(session):
    async def scenario():
        async with session() as (host, runner, events):
            await send(host, "compaction", "later")
            await send(host, "compaction_cancel", "cancel-later")
            assert host.control.active_turn and not runner.interrupts
            assert not host.control.commands
            assert results(events, "later")[-1]["status"] == "cancelled"
    asyncio.run(scenario())
