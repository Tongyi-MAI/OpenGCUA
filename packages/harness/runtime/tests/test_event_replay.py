"""D01 离线事件回放：同一 EventClock 的 flat v1 信封分发到 TUI、Plain→Hub、Native 三条链路。

不启动 prompt_toolkit 全屏应用、Hub 子进程、Swift App、桌面、模型或任何网络服务；
只比较共享身份（event_id / source sequence / stream_id）与各自的有界投影。
Native 侧只覆盖 Python NativeFrontend/TaskControl 发出的 NDJSON，Swift 去重不在本文件验证范围。
"""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import demo_hub
from agent_events import HUB_EVENT_PREFIX, AgentEvent, PlainFrontend
from event_protocol import ACTION_LABELS, EventProjection
from native_host import NativeFrontend, TaskControl
from terminal_tui import RichTerminalFrontend

NATIVE_TASK_ID = "native-task-a"
ACTION_A, ACTION_B = "action-a", "action-b"
CELL_A, CELL_B = "cell-request-1", "cell-request-2"


def test_structured_hub_events_keep_sigint_abort_eligibility():
    from event_protocol import EventClock
    callbacks = []
    frontend = PlainFrontend(_no_input, interactive=False, stdout=io.StringIO(), hub_events=True)
    frontend.bind_abort(callbacks.append)
    clock = EventClock("abort")
    frontend.emit(AgentEvent.from_wire(clock.make("phase", text="planning", phase="planning", turn_id="turn")))
    frontend.handle_sigint()
    assert callbacks == [False]
    frontend.emit(AgentEvent.from_wire(clock.make("answer", text="done", status="success", turn_id="turn")))
    frontend.handle_sigint()
    assert callbacks == [False], "完成后 SIGINT 不得误中止下一轮"


def test_structured_manual_compaction_keeps_correlated_terminal():
    control = TaskControl("native", lambda _: None)
    emitted = []
    control.send = emitted.append
    frontend = NativeFrontend(control)
    control.current_command_id = "compact-command"
    wire = control.event_clock.make("control_result", text="no history", status="insufficient_history")
    frontend.emit(AgentEvent.from_wire(wire))
    assert emitted[-1]["command_id"] == "compact-command"
    assert emitted[-1]["purpose"] == "compaction" and emitted[-1]["manual"]
    assert emitted[-1]["event_id"] == wire["event_id"]


async def _no_input(_prompt):
    raise AssertionError("回放不应读取输入")


class Fanout:
    """生产链路的离线组合：TUI 直接消费；Plain 输出 hub 行再交给 HubSession；Native 经 TaskControl 发送。"""

    def __init__(self):
        self.native_sent: list[dict] = []
        self.control = TaskControl(NATIVE_TASK_ID, self.native_sent.append)
        # 与 run_agent(host_control=...) 相同：共享 control.event_clock，canonical task_id 来自 harness。
        self.clock = self.control.event_clock
        self.clock.session_id = "harness-session"
        self.task_id = f"{self.clock.session_id}/task/0"
        self.control.event_context = {"task_id": self.task_id}
        self.native = NativeFrontend(self.control)
        self.tui = RichTerminalFrontend()
        # guard：不进入全屏应用；_events 为 None 时 emit 同步走 _consume。
        self.tui.start = lambda: None
        assert self.tui._events is None and self.tui._pending_read is None
        self.plain_out = io.StringIO()
        self.plain = PlainFrontend(_no_input, interactive=False, stdout=self.plain_out,
                                   stderr=io.StringIO(), hub_events=True)
        self.hub = demo_hub.Hub({})
        assert self.hub.session.proc is None
        self.delivered: list[dict] = []
        self.named: dict[str, dict] = {}

    def turn(self, index):
        return f"{self.clock.stream_id}/turn/{index}"

    def make(self, kind, turn, text="", step=None, **fields):
        return self.clock.make(kind, text=text, step=step, task_id=self.task_id,
                               turn_id=self.turn(turn), origin="harness",
                               policy_revision=self.control.policy_revision, **fields)

    def plain_lines(self, start):
        return [line for line in self.plain_out.getvalue()[start:].splitlines() if line]

    def deliver(self, wire, name=None):
        if name:
            self.named[name] = copy.deepcopy(wire)
        self.delivered.append(copy.deepcopy(wire))
        event = AgentEvent.from_wire(copy.deepcopy(wire))
        self.tui.emit(event)
        start = len(self.plain_out.getvalue())
        self.plain.emit(event)
        for line in self.plain_lines(start):
            # 结构化事件只经 hub 行传输，不再并行输出旧语义文本。
            assert line.startswith(HUB_EVENT_PREFIX), line
            self.hub.session._push_line(line)
        self.native.emit(event)

    def action(self, turn, action_id, stage, *, cell, record_sequence=None,
               execution="unknown", effect="unknown", step=0):
        return self.make("action_status", turn, ACTION_LABELS[stage], step=step,
                         action_id=action_id, stage=stage, sdk_session_id="sdk-session-1",
                         cell_request_id=cell, operation="action", record_sequence=record_sequence,
                         resource_id="resource-1", observation_id="obs-1",
                         execution=execution, effect=effect, verification="unverified")

    # —— 脚本分段：每段结束后测试检查投影 ——
    def turn_one_actions(self):
        self.deliver(self.make("session", 0, model="fixture-model", mode="code"), "session")
        self.deliver(self.make("turn_started", 0), "t1_start")
        self.deliver(self.make("phase", 0, "planning", step=0, phase="planning"), "t1_planning")
        self.deliver(self.make("model_status", 0, "waiting", step=0, stage="waiting", attempt_id="attempt-1"))
        self.deliver(self.make("model_status", 0, "generating", step=0, stage="generating",
                               attempt_id="attempt-1"), "model_generating")
        self.deliver(self.make("phase", 0, "execution", step=0, phase="execution"), "t1_execution")
        record = 0
        for stage in ("intent_recorded", "waiting_resource", "dispatching", "running", "receipt_received"):
            durable = stage not in {"waiting_resource", "running"}
            record += durable
            done = stage == "receipt_received"
            self.deliver(self.action(0, ACTION_A, stage, cell=CELL_A,
                                     record_sequence=record if durable else None,
                                     execution="completed" if done else "unknown",
                                     effect="applied" if done else "unknown"), f"A:{stage}")
        for stage in ("intent_recorded", "dispatching", "outcome_unknown"):
            record += 1
            self.deliver(self.action(0, ACTION_B, stage, cell=CELL_B, record_sequence=record, step=1),
                         f"B:{stage}")

    def turn_one_finish(self):
        # 模型声明成功；动作 B 的 effect 仍 unknown，任何前端都不得把它投影为 verified。
        self.deliver(self.make("answer", 0, "已提交表单", step=1, status="success"), "t1_answer")
        self.deliver(self.make("turn_finished", 0, "已提交表单", state="completed", path=""), "t1_finished")

    def turn_two_abort(self):
        self.control.turn_index = 1
        self.deliver(self.make("turn_started", 1), "t2_start")
        self.deliver(self.make("phase", 1, "planning", step=2, phase="planning"), "t2_planning")
        self.deliver(self.make("aborted", 1, "用户中止；已经发出的操作不会回滚", step=2), "t2_aborted")

    def turn_three_start(self):
        self.control.turn_index = 2
        self.deliver(self.make("turn_started", 2), "t3_start")
        self.deliver(self.make("phase", 2, "planning", step=3, phase="planning"), "t3_planning")

    def run_all(self):
        self.turn_one_actions()
        self.turn_one_finish()
        self.turn_two_abort()
        self.turn_three_start()
        return self


def _hub_state(f):
    return f.hub.session.event_view()["state"]


def _hub_backlog_v1(f):
    return [event for event in f.hub.session.backlog if "schema_version" in event]


def _native_by_id(f):
    return {event["event_id"]: event for event in f.native_sent}


def _projection_digest(projection):
    return {
        "session_id": projection.session_id, "turn_id": projection.turn_id, "phase": projection.phase,
        "terminal": (projection.terminal or {}).get("event_id"),
        "actions": {aid: (item["event_id"], item["stage"]) for aid, item in projection.actions.items()},
    }


@pytest.fixture()
def fanout():
    return Fanout()


def test_action_status_identity_is_shared_by_tui_hub_and_native(fanout):
    f = fanout.run_all()
    actions = [wire for wire in f.delivered if wire["kind"] == "action_status"]
    assert len(actions) == 8
    hub = {event["event_id"]: event for event in _hub_backlog_v1(f)}
    native = _native_by_id(f)
    for wire in actions:
        for target in (hub[wire["event_id"]], native[wire["event_id"]]):
            # 源序号、流和来源原样保留；Hub 的 delivery seq 是另一套游标。
            for key in ("sequence", "stream_id", "session_id", "origin", "kind", "action_id", "stage",
                        "cell_request_id", "sdk_session_id", "verification", "task_id", "turn_id"):
                assert target[key] == wire[key], (key, target, wire)
        assert type(hub[wire["event_id"]]["seq"]) is int
        assert native[wire["event_id"]]["native_task_id"] == NATIVE_TASK_ID
        assert wire["action_id"] != wire["cell_request_id"]
    # 三条链路对每个动作最后阶段指向同一个源事件。
    final = {ACTION_A: f.named["A:receipt_received"], ACTION_B: f.named["B:outcome_unknown"]}
    tui_actions = f.tui.event_projection.actions
    plain_actions = f.plain.event_projection.actions
    hub_actions = {item["action_id"]: item for item in _hub_state_after_turn_one(f)["actions"]}
    native_last = {}
    for event in f.native_sent:
        if event["kind"] == "action_status":
            native_last[event["action_id"]] = event
    for aid, wire in final.items():
        assert tui_actions[aid]["event_id"] == plain_actions[aid]["event_id"] == wire["event_id"]
        assert hub_actions[aid]["event_id"] == wire["event_id"]
        assert hub_actions[aid]["sequence"] == wire["sequence"]
        assert native_last[aid]["event_id"] == wire["event_id"]
    # Native 原生顺序与源顺序一致。
    assert [e["event_id"] for e in f.native_sent if e["kind"] == "action_status"] == [w["event_id"] for w in actions]


def _hub_state_after_turn_one(f):
    # Hub 在新 turn 清空 actions；重新回放到 T1 结束以取 T1 的动作摘要。
    session = demo_hub.Hub({}).session
    for wire in f.delivered:
        session.push(copy.deepcopy(wire))
        if wire["event_id"] == f.named["t1_finished"]["event_id"]:
            break
    return session.event_view()["state"]


def test_native_aliases_keep_source_kind_and_canonical_task_id(fanout):
    f = fanout.run_all()
    native = _native_by_id(f)
    for name, alias in (("session", "session_info"), ("t1_planning", "progress"),
                        ("t2_aborted", "interrupted"), ("A:dispatching", "action_status"),
                        ("t1_finished", "turn_finished")):
        wire = f.named[name]
        sent = native[wire["event_id"]]
        assert sent["event"] == alias and sent["kind"] == wire["kind"], sent
        assert sent["task_id"] == f.task_id != NATIVE_TASK_ID
        assert sent["native_task_id"] == NATIVE_TASK_ID
        assert sent["origin"] == "harness" and sent["schema_version"] == 1
    sequences = [event["sequence"] for event in f.native_sent]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    assert {event["stream_id"] for event in f.native_sent} == {f.clock.stream_id}
    # 原生侧自身事件（如中止后的 queue_state）共享同一时钟，不与源序号冲突。
    own = [event for event in f.native_sent if event["origin"] == "native_host"]
    assert own and all(event["event_id"] not in {w["event_id"] for w in f.delivered} for event in own)


def test_final_phase_projection_is_consistent_and_never_verified(fanout):
    f = fanout
    f.turn_one_actions()
    f.turn_one_finish()
    tui, plain, hub = f.tui.event_projection, f.plain.event_projection, _hub_state(f)
    assert tui.phase == plain.phase == "completed"
    assert tui.terminal["event_id"] == f.named["t1_finished"]["event_id"]
    assert hub["phase"] == "completed" and hub["terminal"] is not None
    # answer(status=success)+turn_finished(state=completed) 共用一个终态：与 TUI 同指向最后终态帧，回答只保留一份。
    assert hub["terminal"]["event_id"] == f.named["t1_finished"]["event_id"]
    assert (hub["terminal"]["terminal_status"], hub["terminal"]["terminal_state"]) == ("completed", "completed")
    assert hub["terminal"]["answer"] == "已提交表单" == f.hub.state()["answer"]
    assert hub["turn_id"] == tui.turn_id == f.turn(0)
    stages = {aid: item["stage"] for aid, item in tui.actions.items()}
    assert stages == {ACTION_A: "receipt_received", ACTION_B: "outcome_unknown"}
    assert {item["action_id"]: item["stage"] for item in hub["actions"]} == stages
    assert tui.actions[ACTION_B]["effect"] == "unknown"
    native_turn_end = _native_by_id(f)[f.named["t1_finished"]["event_id"]]
    assert native_turn_end["state"] == "completed"
    _assert_never_verified(f)

    f.turn_two_abort()
    tui, hub = f.tui.event_projection, _hub_state(f)
    aborted = f.named["t2_aborted"]
    assert tui.terminal["event_id"] == hub["terminal"]["event_id"] == aborted["event_id"]
    assert tui.phase == "aborted" and hub["phase"] == "aborted" and f.tui.phase == "waiting"
    assert hub["terminal"]["terminal_state"] == "aborted" and "answer" not in hub["terminal"]
    assert _native_by_id(f)[aborted["event_id"]]["event"] == "interrupted"

    f.turn_three_start()
    tui, hub = f.tui.event_projection, _hub_state(f)
    assert tui.turn_id == hub["turn_id"] == f.turn(2)
    assert tui.phase == hub["phase"] == f.tui.phase == "planning"
    assert tui.terminal is None and hub["terminal"] is None
    native_last_progress = [e for e in f.native_sent if e["event"] == "progress"][-1]
    assert native_last_progress["phase"] == "planning" and native_last_progress["turn_id"] == f.turn(2)
    _assert_never_verified(f)


def test_hub_mid_turn_action_phase_matches_source_stage(fanout):
    """当前 harness 只发 stage；Hub 投影若只读 status 会退化成展示文本，与 TUI/Native 不一致。"""
    f = fanout
    f.turn_one_actions()
    assert f.tui.event_projection.phase == "outcome_unknown"
    assert _hub_state(f)["phase"] == f.tui.event_projection.phase


def test_recovery_report_is_session_level_and_read_only_in_hub(fanout):
    """resume 对账帧经 Plain→Hub：只更新对账摘要，不动 phase/terminal/actions，源 envelope 不重写。"""
    f = fanout
    f.turn_one_actions()
    f.turn_one_finish()
    before = copy.deepcopy(_hub_state(f))
    recovered = [{"action_id": f"old-{i}", "status": status, "operation": "click", "resource_id": None,
                  "execution": "unknown", "effect": "unknown", "verification": "unverified"}
                 for i, status in enumerate(["not_dispatched"] * 70 + ["receipt_unverified"] * 2
                                            + ["outcome_unknown"] * 3)]
    f.deliver(f.make("recovery_report", 0, "历史动作对账", actions=recovered), "recovery")
    hub = _hub_state(f)
    for key in ("phase", "terminal", "actions", "question", "turn_id", "attempt_id", "completed_turns"):
        assert hub[key] == before[key], key
    report = hub["recovery_report"]
    assert (report["total"], report["unknown"], report["unverified"], report["omitted"]) == (75, 3, 2, 11)
    assert [a["status"] for a in report["actions"][:5]] == ["outcome_unknown"] * 3 + ["receipt_unverified"] * 2
    wire = f.named["recovery"]
    stored = [event for event in _hub_backlog_v1(f) if event["event_id"] == wire["event_id"]]
    assert len(stored) == 1 and {k: v for k, v in stored[0].items() if k not in {"seq", "type"}} == wire
    assert report["sequence"] == wire["sequence"] and report["stream_id"] == wire["stream_id"]
    _assert_never_verified(f)
    # 下一轮照常开始，对账仍属于本 session。
    f.turn_two_abort()
    assert _hub_state(f)["phase"] == "aborted" and _hub_state(f)["recovery_report"] == report


def _assert_never_verified(f):
    projections = (f.tui.event_projection, f.plain.event_projection)
    for projection in projections:
        assert projection.phase not in {"verified"}
        assert all(item["verification"] == "unverified" and item["stage"] != "verified"
                   for item in projection.actions.values())
    hub = _hub_state(f)
    assert hub["phase"] != "verified"
    assert all(item.get("stage") != "verified" and item.get("status") != "verified" for item in hub["actions"])
    for event in f.native_sent:
        assert event.get("verification") in (None, "unverified")
        assert event.get("stage") != "verified" and event.get("state") != "verified"


def test_unknown_version_duplicate_and_old_turn_are_rejected(fanout):
    f = fanout.run_all()
    tui_before = (f.tui.transcript, _projection_digest(f.tui.event_projection))
    plain_before = _projection_digest(f.plain.event_projection)
    hub_before = copy.deepcopy(_hub_state(f))
    hub_backlog = len(f.hub.session.backlog)
    sent_before = len(f.native_sent)

    # 1) 未知 schema_version：解码即拒绝，三条链路都不改变状态、Native 不发送。
    future = {**f.make("phase", 2, "execution", step=3, phase="execution"), "schema_version": 2}
    with pytest.raises(ValueError):
        AgentEvent.from_wire(copy.deepcopy(future))
    raw = AgentEvent(future["kind"], future["text"], future["step"], future)
    for frontend in (f.tui, f.plain, f.native):
        try:
            frontend.emit(raw)
        except ValueError:
            pass  # run_agent 经 emit_safely 隔离该异常；关键是没有状态变化。
    f.hub.session._push_line(HUB_EVENT_PREFIX + json.dumps(future))
    assert (f.tui.transcript, _projection_digest(f.tui.event_projection)) == tui_before
    assert _projection_digest(f.plain.event_projection) == plain_before
    assert len(f.hub.session.backlog) == hub_backlog and _hub_state(f) == hub_before
    assert len(f.native_sent) == sent_before
    plain_output = f.plain_out.getvalue()

    # 2) 重复投递同一源事件：TUI/Plain 投影拒绝，Plain 不再转发给 Hub；Hub 直接收到也不改投影。
    duplicate = f.named["A:receipt_received"]
    f.tui.emit(AgentEvent.from_wire(copy.deepcopy(duplicate)))
    f.plain.emit(AgentEvent.from_wire(copy.deepcopy(duplicate)))
    f.hub.session._push_line(HUB_EVENT_PREFIX + json.dumps(duplicate))
    assert (f.tui.transcript, _projection_digest(f.tui.event_projection)) == tui_before
    assert _projection_digest(f.plain.event_projection) == plain_before
    assert f.plain_out.getvalue() == plain_output
    assert _hub_state(f) == hub_before

    # 3) 新轮开始后到达的旧轮 delta：同样被拒绝。
    stale_intent = f.make("intent_delta", 1, "旧轮迟到意图", step=2, block_index=1)
    stale_code = f.make("code_delta", 0, "旧轮迟到代码", step=1, block_index=1)
    for stale in (stale_intent, stale_code):
        f.tui.emit(AgentEvent.from_wire(copy.deepcopy(stale)))
        f.plain.emit(AgentEvent.from_wire(copy.deepcopy(stale)))
        f.hub.session._push_line(HUB_EVENT_PREFIX + json.dumps(stale))
    assert (f.tui.transcript, _projection_digest(f.tui.event_projection)) == tui_before
    assert _projection_digest(f.plain.event_projection) == plain_before
    assert f.plain_out.getvalue() == plain_output
    assert _hub_state(f)["last_intent"] is None and _hub_state(f)["turn_id"] == f.turn(2)
    assert _hub_state(f)["phase"] == hub_before["phase"]

    # Native Python 侧是传输层：若转发重复/旧轮帧，必须保留原 event_id/sequence/turn_id 供 Swift 防旧。
    for event_id, wire in ((duplicate["event_id"], duplicate),
                           (stale_intent["event_id"], stale_intent), (stale_code["event_id"], stale_code)):
        f.native.emit(AgentEvent.from_wire(copy.deepcopy(wire)))
        f.native.flush()
        forwarded = [event for event in f.native_sent[sent_before:] if event["event_id"] == event_id]
        for event in forwarded:
            assert (event["sequence"], event["turn_id"], event["kind"]) == (
                wire["sequence"], wire["turn_id"], wire["kind"])


def test_event_projection_replay_matches_live_tui_and_rejects_second_pass(fanout):
    f = fanout.run_all()
    live = _projection_digest(f.tui.event_projection)
    assert live == _projection_digest(f.plain.event_projection)
    for source in (f.delivered, _hub_backlog_v1(f), f.native_sent):
        replay = EventProjection()
        for event in source:
            replay.consume(copy.deepcopy(event))
        assert _projection_digest(replay) == live
        # 同一序列再放一次：全部视为重复，不改变投影。
        assert not any(replay.consume(copy.deepcopy(event)) for event in source)
        assert _projection_digest(replay) == live


@pytest.mark.parametrize("text,status,phase,native_state", [
    ("success", None, "completed", "completed"),
    ("完成文本", "success", "completed", "completed"),
    ("success", "failed", "attention", "failed"),
    ("cancelled", None, "aborted", "stopped"),
    ("完成文本", None, "attention", "failed"),
])
def test_terminal_status_is_normalized_across_frontends(fanout, text, status, phase, native_state):
    f = fanout
    f.deliver(f.make("turn_started", 0))
    fields = {} if status is None else {"status": status}
    f.deliver(f.make("finished", 0, text, **fields), "terminal")
    assert f.tui.event_projection.phase == f.plain.event_projection.phase == phase
    assert f.native.event_projection.phase == _hub_state(f)["phase"] == phase
    assert _native_by_id(f)[f.named["terminal"]["event_id"]]["state"] == native_state


def test_native_direct_turn_marker_keeps_following_agent_phase():
    sent = []
    control = TaskControl("native", sent.append)
    frontend = NativeFrontend(control)
    control.begin_turn("第一轮")
    turn = f"{control.event_clock.stream_id}/turn/{control.turn_index}"
    frontend.emit(AgentEvent.from_wire(control.event_clock.make("phase", turn_id=turn, phase="planning")))
    control.emit("turn_finished", state="completed")
    control.begin_turn("第二轮")
    new_turn = f"{control.event_clock.stream_id}/turn/{control.turn_index}"
    frontend.emit(AgentEvent.from_wire(control.event_clock.make("phase", turn_id=new_turn, phase="planning")))
    assert sent[-1]["event"] == "progress" and sent[-1]["turn_id"] == new_turn
    assert frontend.event_projection.terminal is None and frontend.event_projection.phase == "planning"
    stale = control.event_clock.make("approval", turn_id=turn, approval_id="old")
    count = len(sent)
    frontend.emit(AgentEvent.from_wire(stale))
    assert len(sent) == count, "旧轮审批不能进入新轮原生状态"


def test_late_attempt_and_retired_turn_cannot_reopen_frontends(fanout):
    f = fanout
    f.deliver(f.make("turn_started", 0))
    f.deliver(f.make("model_status", 0, stage="waiting", attempt_id="old"))
    f.deliver(f.make("model_status", 0, stage="generating", attempt_id="new"))
    for kind in ("intent_delta", "code_delta", "approval", "model_status"):
        wire = f.make(kind, 0, "迟到", attempt_id="old", stage="error", block_index=1)
        before = (_projection_digest(f.tui.event_projection), len(f.native_sent), copy.deepcopy(_hub_state(f)))
        f.deliver(wire)
        assert (_projection_digest(f.tui.event_projection), len(f.native_sent), _hub_state(f)) == before
    f.deliver(f.make("turn_started", 1))
    before = (_projection_digest(f.tui.event_projection), len(f.native_sent), copy.deepcopy(_hub_state(f)))
    f.deliver(f.make("turn_started", 0))
    assert (_projection_digest(f.tui.event_projection), len(f.native_sent), _hub_state(f)) == before


def test_projection_retired_session_cannot_rebind_or_grow_without_bound():
    from event_protocol import EventClock
    projection = EventProjection()
    old = EventClock("old")
    projection.consume(old.make("session_info"))
    current = EventClock("current")
    projection.consume(current.make("session_info"))
    assert not projection.consume(old.make("session_info"))
    assert projection.session_id == "current"
    for index in range(300):
        clock = EventClock(f"s-{index}")
        projection.consume(clock.make("session_info"))
        projection.consume(clock.make("turn_started", turn_id=f"t-{index}"))
    assert len(projection._sequence) <= 128 and len(projection._seen) <= 256
    assert len(projection._retired_sessions) <= 64 and len(projection._retired_turns) <= 64


def test_same_recorded_events_reach_real_swift_app_model(fanout, tmp_path):
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("同源 Swift AppModel 回放需要 macOS Swift 编译器；未通过不能视为原生验收")
    f = fanout
    segments = []

    def segment(build, expected, actions=None):
        offset = len(f.native_sent)
        build()
        phase = f.tui.event_projection.phase
        assert phase == _hub_state(f)["phase"] == f.native.event_projection.phase
        normalized = phase if phase in {"completed", "attention", "aborted"} else "running"
        assert normalized == expected
        segments.append({"events": copy.deepcopy(f.native_sent[offset:]), "expected_phase": expected,
                         "turn_id": f.tui.event_projection.turn_id, "actions": actions or {}})

    def first():
        f.turn_one_actions()
        f.turn_one_finish()
    segment(first, "completed", {ACTION_A: "receipt_received", ACTION_B: "outcome_unknown"})
    segment(f.turn_two_abort, "aborted")
    segment(f.turn_three_start, "running")
    segment(lambda: f.deliver(f.make("finished", 2, "failure", status="failure")), "attention")
    # 重放迟到审批/delta，直接给 Swift 也不能更改新轮终态。
    stale = [f.make("approval", 0, "旧审批", approval_id="late"),
             f.make("intent_delta", 1, "旧意图", block_index=1)]
    segments.append({"events": [{**wire, "event": wire["kind"], "native_task_id": NATIVE_TASK_ID,
                                 "turn_index": 0} for wire in stale],
                     "expected_phase": "attention", "turn_id": f.turn(2), "actions": {}})
    fixture = tmp_path / "recorded.json"
    fixture.write_text(json.dumps({"native_task_id": NATIVE_TASK_ID, "segments": segments},
                                  ensure_ascii=False), encoding="utf-8")
    macos = Path(__file__).resolve().parents[3] / "macos"
    sources = [macos / "Sources" / f"{name}.swift" for name in (
        "Localization", "AudioInputDevices", "VoiceTaskTools", "Settings", "SpeechInput",
        "QwenSpeechInput", "AppModel", "SessionControls")]
    binary = tmp_path / "recorded-swift-contracts"
    compiled = subprocess.run([shutil.which("swiftc"), "-swift-version", "5", *map(str, sources),
                               str(macos / "tests" / "VoiceRouterContracts.swift"), "-o", str(binary)],
                              capture_output=True, text=True, timeout=120)
    assert compiled.returncode == 0, compiled.stderr
    replay = subprocess.run([str(binary), str(fixture)], capture_output=True, text=True, timeout=20)
    if replay.returncode == -9:
        # 某些 macOS 测试环境的 AMFI 拒绝临时 ad-hoc 可执行文件；不更改系统策略。
        # 生产源已经完整编译，回放改用同源单文件 JIT，逻辑断言失败仍然直接失败。
        combined = tmp_path / "Replay.swift"
        combined.write_text("\n".join(path.read_text() for path in [*sources, macos / "tests" / "VoiceRouterContracts.swift"]))
        sdk = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True, text=True, check=True).stdout.strip()
        replay = subprocess.run([shutil.which("swiftc"), "-frontend", "-interpret", "-parse-as-library",
                                 "-swift-version", "5", "-sdk", sdk, "-target", "arm64-apple-macosx14.0",
                                 *(f"-l/System/Library/Frameworks/{name}.framework/{name}" for name in
                                   ("SwiftUI", "AppKit", "Speech", "AVFoundation", "Security", "Carbon")),
                                 str(combined), "--", str(fixture)], capture_output=True, text=True, timeout=60)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert "同源录制回放通过" in replay.stdout
