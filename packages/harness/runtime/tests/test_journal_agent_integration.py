"""真实 mac_agent.run_agent + 真实 CodeExecutor/ActionJournal/code_runtime worker 的接线集成测试。

替换边界（与 test_action_journal_executor 相同层级）：
- 模型：plan_code / build_model_client 为离线 fake，不发真实 API；
- 桌面：CuaDesktop / CuaRpcServer 为空 fake，不触碰 TCC/CUA；
- SDK：worker 进程内 mai_ui 为 fake Session/Device/Core（来自 FAKE_WORKER），
  但 code_runtime 的 journal 握手、CodeExecutor 的 ACK/发布、ActionJournal 的落盘全部是生产代码。
不替换 run_agent 内部的 journal 创建、executor 构造参数或 action_event_handler。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
import types
from pathlib import Path

import pytest

from action_journal import ActionJournal
from agent_events import AgentEvent, AgentPrompt, ForegroundApprovalPrompt
from event_protocol import validate_event
from test_action_journal_executor import FAKE_WORKER

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP_ANCHOR = "return dict(mu=mu, device=device, asyncio=asyncio,"
_EXIT_ANCHOR = "raise SystemExit(rt.main(sys.argv))"
# run_agent 在 session_reset 后会请求 reset_task；真实 worker 在那里重建 SDK Session。
# fake SDK 只替换“重建 Session”这一步，仍由生产 _instrument_sdk_session 绑定 journal 闸门。
_RESET_FIXTURE = '''
Device.ui = None

async def _fixture_reset_sdk_session():
    session = Session()
    attached = Device(session)
    session._devices[attached.id] = attached
    rt._MAI_SESSION = session
    rt._instrument_sdk_session(session)
    return session, attached
rt._reset_sdk_session = _fixture_reset_sdk_session
'''


def _worker_source() -> str:
    # 复用真实 worker fixture，只额外暴露生产 terminate，使 run_agent 能正常结束任务。
    assert _BOOTSTRAP_ANCHOR in FAKE_WORKER and _EXIT_ANCHOR in FAKE_WORKER, \
        "FAKE_WORKER 结构变化，需同步本 fixture"
    source = FAKE_WORKER.replace(
        _BOOTSTRAP_ANCHOR, _BOOTSTRAP_ANCHOR + " terminate=rt.terminate, ask_user=rt.ask_user,", 1)
    source = source.replace(_EXIT_ANCHOR, _RESET_FIXTURE + _EXIT_ANCHOR, 1)
    return "import sys\nsys.path.insert(0, " + repr(str(RUNTIME_ROOT)) + ")\n" + source


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


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        import code_executor
        import llm_entry
        import mac_agent
        from model_stream import CodeAction

        self.tmp_path = tmp_path
        self.runs_dir = tmp_path / "runs"
        self.runs_dir.mkdir()
        self.mac_agent = mac_agent
        self.plans: list[str] = []
        self.model_inputs: list = []
        self.executors: list = []
        self.fsyncs: list[tuple] = []
        worker = tmp_path / "fake_worker.py"
        worker.write_text(_worker_source())
        harness = self

        monkeypatch.setitem(sys.modules, "mai_ui", types.SimpleNamespace(Runtime=object))

        class Desktop:
            generation = 0
            permission_status = {"accessibility": True, "screen_recording": True}
            def __init__(self, **_kwargs): pass
            async def start(self): pass
            async def close(self): pass
            # session_reset 恢复路径会等待驱动空闲；fake 桌面没有在途调用。
            async def wait_for_idle(self): pass
            def invalidate_references(self): self.generation += 1

        class RpcServer:
            def __init__(self, _desktop, _socket): self.calls = []
            async def start(self): pass
            async def close(self): pass

        class Client:
            on_event = on_attempt = on_parse_event = on_compaction_event = None
            token_estimator = context_limit = None
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): pass
            async def complete(self, *_args, **_kwargs):
                raise AssertionError("本测试不触发压缩")

        class WorkerExecutor(code_executor.CodeExecutor):
            """生产 CodeExecutor；仅把 runtime_path 指向 fake SDK worker，并只读记录 run 结果。"""
            def __init__(self, **kwargs):
                self.fixture_kwargs = dict(kwargs)
                super().__init__(**{**kwargs, "runtime_path": worker})
                journal = kwargs.get("action_journal")
                self.fixture_journal_fd = getattr(journal, "_fd", None)
                self.fixture_runs: list[dict] = []
                harness.executors.append(self)

            async def run(self, code, todo_items):
                result = await super().run(code, todo_items)
                self.fixture_runs.append({"code": code, "result": result,
                                          "context": dict(self.action_context)})
                return result

        async def plan(_client, messages, **_kwargs):
            self.model_inputs.append(messages)
            code = self.plans.pop(0)
            action = CodeAction(intent="fixture", code=code)
            return action, action.as_response(), "", {}

        real_fsync = os.fsync

        def recording_fsync(fd):
            real_fsync(fd)
            try:
                inode = os.fstat(fd).st_ino
            except OSError:
                inode = None
            executor = self.executors[-1] if self.executors else None
            if executor is not None and executor.action_journal is not None:
                path = Path(executor.action_journal.path)
                lines = path.read_text().splitlines() if path.exists() else []
                stage = json.loads(lines[-1])["journal_stage"] if lines else None
                self.fsyncs.append((inode, len(self.native_calls(executor.work_dir)), stage,
                                    len(lines)))

        monkeypatch.setattr(mac_agent, "CuaDesktop", Desktop)
        monkeypatch.setattr(mac_agent, "CuaRpcServer", RpcServer)
        monkeypatch.setattr(mac_agent, "CodeExecutor", WorkerExecutor)
        monkeypatch.setattr(mac_agent, "plan_code", plan)
        monkeypatch.setattr(llm_entry, "build_model_client", lambda *_args, **_kwargs: Client())
        # 只包装记录，不改变 fsync 行为；用于确认 journal fd 在派发前已落盘。
        monkeypatch.setattr(os, "fsync", recording_fsync)

    @staticmethod
    def native_calls(work_dir):
        path = Path(work_dir) / "native_calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run(self, message, plans, *extra):
        self.plans[:] = plans
        frontend = RecordingFrontend()
        args = self.mac_agent.build_parser().parse_args([
            message, *extra, "--allow-empty-api-key", "--countdown", "0", "--disable-multi-task",
            "--startup-observation", "none", "--runs-dir", str(self.runs_dir),
        ])
        result = asyncio.run(self.mac_agent.run_agent(args, frontend=frontend))
        assert not self.plans, f"剩余未消费计划：{self.plans}"
        return result, frontend.events

    def run_dir(self):
        return next(self.runs_dir.glob("*/traj.json")).parent


FIRST_PLANS = [
    "await device.act('act-ok', text='提交')",
    "await device.act('act-fail', failure='TIMEOUT')",
    "terminate(status='success', answer='已提交，结果待核实')",
]


def _records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def _first_run(harness):
    result, events = harness.run("提交表单", list(FIRST_PLANS))
    assert result == 0
    executor = harness.executors[-1]
    return executor, events


def test_run_agent_injects_journal_context_and_publishes_durable_action_events(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    executor, events = _first_run(harness)
    run_dir = harness.run_dir()
    journal = executor.fixture_kwargs["action_journal"]

    # 1) run_agent 默认注入 ActionJournal（run 目录下 actions.jsonl）与 action_event_handler。
    assert isinstance(journal, ActionJournal) and executor.action_journal is journal
    assert Path(journal.path) == run_dir / "actions.jsonl"
    assert callable(executor.action_event_handler)
    assert [run["code"] for run in executor.fixture_runs] == FIRST_PLANS
    session_id = journal.session_id
    for index, run in enumerate(executor.fixture_runs):
        context = run["context"]
        assert context["session_id"] == session_id and context["step"] == index
        assert context["task_id"] == f"{session_id}/task/0"
        assert context["step_id"] == f"{session_id}/step/{index}"
        assert type(context["policy_revision"]) is int and context["turn_id"]
    assert executor.fixture_runs[0]["result"].returncode == 0
    assert executor.fixture_runs[1]["result"].returncode != 0

    # 2) 真实 worker 两次 native 调用；JSONL 序号/摘要链连续，fsync 早于 native 派发。
    calls = harness.native_calls(executor.work_dir)
    assert [call["action_id"] for call in calls] == ["act-ok", "act-fail"]
    records = _records(journal.path)
    assert [(r["action_id"], r["journal_stage"]) for r in records] == [
        ("act-ok", "intent_recorded"), ("act-ok", "dispatching"), ("act-ok", "receipt_received"),
        ("act-fail", "intent_recorded"), ("act-fail", "dispatching"), ("act-fail", "outcome_unknown"),
    ]
    assert [r["record_sequence"] for r in records] == list(range(1, 7))
    assert all(b["previous_digest"] == a["record_digest"] for a, b in zip(records, records[1:]))
    assert all(r["verification"] == "unverified" and r["session_id"] == session_id for r in records)
    cells = {r["action_id"]: r["cell_request_id"] for r in records}
    assert cells["act-ok"] != cells["act-fail"]
    assert all(r["cell_request_id"] not in {"act-ok", "act-fail"} for r in records)
    assert records[-1]["error"]["code"] == "TIMEOUT"
    journal_inode = Path(journal.path).stat().st_ino
    synced = [(calls_seen, stage, count) for inode, calls_seen, stage, count in harness.fsyncs
              if inode == journal_inode]
    for count in range(1, 7):
        assert any(seen_count == count for _calls, _stage, seen_count in synced), count
    # dispatching 落盘时对应 native 调用尚未发生。
    dispatch_syncs = [calls_seen for calls_seen, stage, count in synced if stage == "dispatching"]
    assert dispatch_syncs[:1] == [0] and dispatch_syncs[-1] == 1

    # 3) 父进程 present 出的所有事件都是 v1 信封；身份唯一、源序号单调。
    wires = [event.to_wire() for event in events]
    for wire in wires:
        validate_event(wire)
    assert len({w["event_id"] for w in wires}) == len(wires)
    sequences = [w["sequence"] for w in wires]
    assert sequences == sorted(sequences)
    assert len({w["stream_id"] for w in wires}) == 1 and {w["session_id"] for w in wires} == {session_id}

    # 4) 每条 durable 记录都有对应 action_status，带 V1 id、action_id≠cell_request_id，且不伪 verified。
    status = [w for w in wires if w["kind"] == "action_status"]
    durable = [w for w in status if w.get("record_sequence") is not None]
    assert [(w["action_id"], w["stage"], w["record_sequence"]) for w in durable] == [
        (r["action_id"], r["journal_stage"], r["record_sequence"]) for r in records]
    for w in status:
        assert w["cell_request_id"] == cells[w["action_id"]] != w["action_id"]
        assert w["verification"] == "unverified" and w["stage"] != "verified"
        assert w["task_id"] == f"{session_id}/task/0" and w["turn_id"] and w["step_id"]
        assert w["sdk_session_id"] == records[0]["sdk_session_id"]
    assert durable[-1]["effect"] == "unknown"

    # 5) 父进程 writer 投影与 journal 对账。
    trajectory = json.loads((run_dir / "traj.json").read_text())
    assert trajectory["action_journal"]["path"] == "actions.jsonl"
    assert trajectory["action_journal"]["last_sequence"] == 6
    assert {a["action_id"]: (a["journal_stage"], a["record_sequence"], a["verification"])
            for a in trajectory["actions"]} == {
        "act-ok": ("receipt_received", 3, "unverified"), "act-fail": ("outcome_unknown", 6, "unverified")}

    # 6) run 结束后 journal 句柄释放。
    assert journal._fd is None
    fd = executor.fixture_journal_fd
    assert isinstance(fd, int)
    try:
        still_journal = os.fstat(fd).st_ino == journal_inode
    except OSError:
        still_journal = False
    assert not still_journal


def _resume(harness):
    run_dir = harness.run_dir()
    before_bytes = (run_dir / "actions.jsonl").read_bytes()
    before_inputs = len(harness.model_inputs)
    # session_reset 时 stdout 不保证回传，SDK session id 由 cell 写入 work_dir 旁证。
    plans = ["import pathlib, code_runtime as _rt\n"
             "pathlib.Path(_rt._CONFIG['work_dir'], 'sdk_session.txt').write_text(mu.id)\n"
             "await device.act('act-ok', text='提交')",
             "terminate(status='success', answer='已核实')"]
    result, events = harness.run("新的请求：核实上次提交", list(plans), "--resume", str(run_dir))
    return result, events, before_bytes, before_inputs, plans


def test_resume_reports_recovery_to_ui_and_blocks_old_action_in_new_sdk_session(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    first, _ = _first_run(harness)
    old_records = _records(first.fixture_kwargs["action_journal"].path)
    old_calls = harness.native_calls(first.work_dir)
    result, events, before_bytes, _before_inputs, plans = _resume(harness)
    assert result == 0
    run_dir = harness.run_dir()
    resumed = harness.executors[-1]
    assert resumed is not first

    # UI：recovery_report 是 v1 事件，早于新一轮规划；只报告、不宣称 verified。
    wires = [event.to_wire() for event in events]
    reports = [w for w in wires if w["kind"] == "recovery_report"]
    assert len(reports) == 1
    validate_event(reports[0])
    states = {a["action_id"]: a for a in reports[0]["actions"]}
    assert {aid: a["status"] for aid, a in states.items()} == {
        "act-ok": "receipt_unverified", "act-fail": "outcome_unknown"}
    assert all(a["verification"] == "unverified" for a in states.values())
    first_planning = next(i for i, w in enumerate(wires) if w["kind"] == "phase")
    assert wires.index(reports[0]) < first_planning
    trajectory = json.loads((run_dir / "traj.json").read_text())
    assert {a["action_id"]: a["status"] for a in trajectory["action_recovery"]} == {
        "act-ok": "receipt_unverified", "act-fail": "outcome_unknown"}

    # 新消息不会让旧代码自动重跑：恢复后只执行新计划。
    assert [run["code"] for run in resumed.fixture_runs] == plans
    assert not any(run["code"] in FIRST_PLANS for run in resumed.fixture_runs)

    # 新 SDK session 再用旧 action_id：journal 阻止，native 未派发，磁盘记录不变。
    blocked = resumed.fixture_runs[0]["result"]
    new_sdk = (Path(resumed.work_dir) / "sdk_session.txt").read_text()
    assert new_sdk and new_sdk != old_records[0]["sdk_session_id"]
    assert blocked.returncode != 0 and "ACTION_REPLAY_BLOCKED" in blocked.stderr
    assert harness.native_calls(resumed.work_dir) == old_calls
    assert (run_dir / "actions.jsonl").read_bytes() == before_bytes
    assert not [w for w in wires if w["kind"] == "action_status"]
    assert resumed.fixture_kwargs["action_journal"]._fd is None


def test_resume_recovery_report_reaches_model_before_first_plan(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    _first_run(harness)
    first_inputs = json.dumps(harness.model_inputs, ensure_ascii=False, default=str)
    result, events, _bytes, before_inputs, _plans = _resume(harness)
    assert result == 0
    report = next(event for event in events if event.kind == "recovery_report")
    markers = [report.text, "receipt_unverified"]
    # 前置条件：这些标记不是旧历史里本来就有的文本。
    assert not any(marker and marker in first_inputs for marker in markers)
    resumed_first = json.dumps(harness.model_inputs[before_inputs], ensure_ascii=False, default=str)
    assert any(marker and marker in resumed_first for marker in markers), \
        "恢复对账只发给 UI/轨迹，未进入恢复后的第一条模型输入"


def test_intent_fsync_failure_through_run_agent_dispatches_nothing(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)

    class IntentFsyncFails(ActionJournal):
        """生产 ActionJournal；仅在 intent 写入时让 fsync 失败（同 test_action_journal_executor）。"""
        def transition(self, event, context):
            if event.get("journal_stage") != "intent_recorded":
                return super().transition(event, context)
            original = os.fsync
            def broken(_fd):
                raise OSError("模拟落盘失败")
            os.fsync = broken
            try:
                return super().transition(event, context)
            finally:
                os.fsync = original

    monkeypatch.setattr(harness.mac_agent, "ActionJournal", IntentFsyncFails)
    result, events = harness.run("提交表单", [
        "await device.act('act-ok', text='提交')",
        "await device.act('act-next')",
        "terminate(status='failure', answer='无法安全派发')",
    ])
    # 模型以 failure 结束任务，run_agent 返回非 0；这里只确认它是正常结束而非异常。
    assert result == 1
    assert [e.text for e in events if e.kind == "finished"] == ["failure"]
    assert not [e for e in events if e.kind == "error"]
    executor = harness.executors[-1]
    first, second = executor.fixture_runs[0]["result"], executor.fixture_runs[1]["result"]
    assert first.session_reset and "JOURNAL_UNAVAILABLE" in first.stderr
    assert second.returncode != 0 and "JOURNAL_UNAVAILABLE" in second.stderr
    assert harness.native_calls(executor.work_dir) == []
    assert not [e for e in events if e.kind == "action_status"]
    journal = executor.fixture_kwargs["action_journal"]
    assert journal._fd is None
    reopened = ActionJournal(journal.path, journal.session_id)
    try:
        assert {r["action_id"]: r["recovery_state"] for r in reopened.recover()} == {"act-ok": "not_dispatched"}
    finally:
        reopened.close()


def test_new_modules_are_declared_as_py_modules():
    config = tomllib.loads((RUNTIME_ROOT / "pyproject.toml").read_text())
    modules = config["tool"]["setuptools"]["py-modules"]
    for name in ("event_protocol", "action_journal"):
        assert name in modules and (RUNTIME_ROOT / f"{name}.py").is_file()
