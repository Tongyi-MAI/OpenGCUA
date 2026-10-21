"""D01 离线事件流回归：不启动 agent、桌面或真实 HTTP 服务。"""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

import console_server
import demo_hub
from action_journal import ActionJournal
from agent_events import AgentEvent, PlainFrontend
from event_protocol import EventClock, validate_event

# mac_agent resume 时 recovery_report 的正文（原样复制，便于对齐生产形状）。
RECOVERY_TEXT = "历史动作对账：已派发且无回执的操作结果未知；有技术回执也未证明业务完成。先核实目标，不自动重放。"


def envelope(kind="phase", sequence=71, **fields):
    return {"schema_version": 1, "event_id": f"source-{sequence}", "sequence": sequence,
            "session_id": "source-session", "task_id": "task-1", "turn_id": "turn-1",
            "step_id": None, "timestamp": "2026-10-21T00:00:00Z", "kind": kind,
            "origin": "harness", "policy_revision": 2, "text": "", "step": None, **fields}


@pytest.fixture()
def session():
    return console_server.Session("delivery-session", "test", {}, "")


def test_source_identity_and_local_delivery_cursor_are_independent(session):
    source = envelope(sequence=908, phase="planning", action_id="a")
    session.push(source)
    session.push({"type": "stdout", "text": "旧日志"})
    first, local = session.backlog
    assert source == envelope(sequence=908, phase="planning", action_id="a")
    assert (first["event_id"], first["sequence"], first["seq"], first["type"]) == (
        "source-908", 908, 1, "phase")
    assert local["schema_version"] == 1 and local["kind"] == "stdout"
    assert local["session_id"] == session.sid and local["seq"] == 2
    assert local["event_id"] != first["event_id"]


def test_slow_consumer_is_bounded_and_terminal_survives_retention(session):
    _, slow = session.subscribe_events()
    session.push(envelope("finished", status="success", text="已完成"))
    for i in range(10000):
        session.push({"type": "stdout", "text": str(i)})
    assert slow.maxsize == 256 and slow.qsize() <= 256
    assert len(session.backlog) == 2000
    frames = []
    while not slow.empty():
        frames.append(slow.get_nowait())
    snapshots = [event for event in frames if event["kind"] == "resync_snapshot"]
    assert snapshots and snapshots[-1]["state"]["terminal"]["text"] == "已完成"
    assert snapshots[-1]["seq"] == snapshots[-1]["cursor"]
    assert snapshots[-1]["view_only"] and not snapshots[-1]["authoritative"]
    initial, live = session.subscribe_events(1)
    assert len(initial) == 1 and initial[0]["type"] == "snapshot"
    assert initial[0]["state"]["terminal"]["event_id"] == "source-71"
    watermark = initial[0]["cursor"]
    session.push({"type": "stdout", "text": "恢复后"})
    assert live.get_nowait()["seq"] == watermark + 1
    session.unsubscribe_events(slow)
    session.unsubscribe_events(live)


def test_projection_actions_and_completed_scopes_are_bounded(session):
    for i in range(100):
        session.push(envelope("action_status", i + 1, action_id=f"action-{i}", status="running"))
    assert len(session.event_view()["state"]["actions"]) == 64
    session.push(envelope("action_status", 101, action_id="action-99", status="receipt_received"))
    assert len(session.event_view()["state"]["actions"]) == 64
    assert session.event_view()["state"]["actions"][-1]["status"] == "receipt_received"
    for i in range(100):
        session.push(envelope("phase", 102 + i, turn_id=f"turn-{i}", phase="planning"))
    assert len(session.event_view()["state"]["completed_turns"]) == 64


def test_projection_preserves_final_intent_and_bounded_summary(session):
    session.push(envelope("intent_start", 1, step=3, block_index=1))
    session.push(envelope("intent_delta", 2, step=3, block_index=1, text="检查"))
    session.push(envelope("intent_delta", 3, step=3, block_index=1, text="表单"))
    session.push(envelope("intent_end", 4, step=3, block_index=1))
    assert session.event_view()["state"]["last_intent"]["text"] == "检查表单"
    session.push(envelope("question", 5, text="q" * 10000, huge_payload=["not summary"] * 100))
    question = session.event_view()["state"]["question"]
    assert len(question["text"]) == 4000 and "huge_payload" not in question
    view = session.event_view()
    view["state"]["question"]["text"] = "外部修改"
    assert session.event_view()["state"]["question"]["text"] != "外部修改"


def test_gui_preview_projection_keeps_legacy_intent_delta(session):
    session.push({"type": "gui_preview", "preview_type": "intent_start", "step": 0})
    session.push({"type": "gui_preview", "preview_type": "intent_delta", "step": 0, "delta": "旧预览"})
    session.push({"type": "gui_preview", "preview_type": "intent_end", "step": 0})
    assert session.event_view()["state"]["last_intent"]["text"] == "旧预览"


def test_terminal_model_request_and_session_global_event_are_not_stale(session):
    session.push(envelope("model_status", 1, attempt_id="a", stage="generating"))
    session.push(envelope("answer", 2, attempt_id="a", text="完成"))
    session.push(envelope("finished", 3, turn_id=None, status="succeeded"))
    session.push({"type": "status", "status": "exited"})
    state = session.event_view()["state"]
    assert state["turn_id"] == "turn-1" and state["attempt_id"] == "a"
    assert state["terminal"]["answer"] == "完成" and state["phase"] == "completed"
    assert state["model_status"] is None


def test_cursor_reconnect_does_not_replay_terminal(session):
    session.push(envelope("finished", status="success"))
    session.push({"type": "stdout", "text": "debug"})
    backlog, subscriber = session.subscribe_events(1)
    assert [event["seq"] for event in backlog] == [2]
    session.push({"type": "stdout", "text": "live"})
    assert subscriber.get_nowait()["seq"] == 3
    session.unsubscribe_events(subscriber)
    backlog, subscriber = session.subscribe_events(3)
    assert backlog == [] and subscriber.empty()
    session.unsubscribe_events(subscriber)


@pytest.mark.parametrize("cursor", [-1, 1, "0", True, 1.5])
def test_invalid_or_future_cursor_is_rejected_before_registration(session, cursor):
    with pytest.raises(ValueError):
        session.subscribe_events(cursor)
    assert not session._subscribers


@pytest.mark.parametrize("raw", ["", "-1", "abc", "1.5", "1%0A", "999", "0&after=1"])
def test_hub_sse_bad_cursor_has_explicit_400(session, monkeypatch, raw):
    monkeypatch.setattr(demo_hub, "HUB", SimpleNamespace(session=session))
    request = StreamFixture(f"/api/events?after={raw}")
    demo_hub.Handler.do_GET(request)
    assert request.code == 400 and request.error["error"]
    assert not session._subscribers and request.wfile.getvalue() == b""


def test_snapshot_registration_has_no_gap_during_response_headers(session, monkeypatch):
    session.push(envelope("finished", status="success"))
    for i in range(2001):
        session.push({"type": "stdout", "text": str(i)})
    monkeypatch.setattr(demo_hub, "HUB", SimpleNamespace(session=session))
    watermark = session.event_view()["cursor"]
    request = StreamFixture("/api/events?after=1", flushes=2,
                            on_headers=lambda: session.push({"type": "stdout", "text": "live"}))
    demo_hub.Handler.do_GET(request)
    snapshot, live = request.events()
    assert snapshot["type"] == "snapshot" and snapshot["cursor"] == watermark
    assert snapshot["state"]["terminal"]["status"] == "success"
    assert live["seq"] == watermark + 1
    assert not session._subscribers


@pytest.mark.parametrize("kind", ["phase", "question", "action_status", "finished", "future_semantic_kind"])
def test_hub_decoder_accepts_flat_v1_without_code_event_restrictions(kind):
    event = envelope(kind, text="语义事件", action_id="a", resource_id="r", observation_id="o")
    line = demo_hub.HUB_EVENT_PREFIX + json.dumps(event)
    assert demo_hub.decode_hub_event(line) == event


@pytest.mark.parametrize("fields", [{"sequence": -1}, {"sequence": 0}, {"sequence": True},
                                     {"step": True}, {"step": -1}, {"schema_version": 2},
                                     {"schema_version": True}, {"schema_version": 1.0},
                                     {"policy_revision": True}, {"event_id": None}, {"task_id": 8},
                                     {"turn_id": []}, {"step_id": 2}, {"text": []},
                                     {"stream_id": None}, {"stream_id": ""}])
def test_hub_decoder_rejects_malformed_v1_without_legacy_rewrap(session, fields):
    wire = envelope(**fields)
    assert not console_server.is_v1_envelope(wire)
    for decode in (validate_event, AgentEvent.from_wire, session.push):
        with pytest.raises(ValueError):
            decode(wire)
    with pytest.raises(ValueError):
        demo_hub.decode_hub_event(demo_hub.HUB_EVENT_PREFIX + json.dumps(wire))
    assert not session.backlog and session.event_view()["cursor"] == 0


@pytest.mark.parametrize("key", ["task_id", "turn_id", "step_id", "text", "step", "sequence",
                                 "policy_revision", "event_id", "session_id", "timestamp", "kind", "origin"])
def test_required_fields_have_one_shared_validator(key):
    wire = envelope()
    wire.pop(key)
    with pytest.raises(ValueError):
        validate_event(wire)
    assert not console_server.is_v1_envelope(wire)


def test_new_answer_and_legacy_console_do_not_duplicate_semantics():
    hub = demo_hub.Hub({})
    session = hub.session
    session._push_line(demo_hub.HUB_EVENT_PREFIX + json.dumps(envelope("phase", phase="planning")))
    session._push_line(demo_hub.HUB_EVENT_PREFIX + json.dumps(envelope("answer", 72, text="完成",
                                                                         status="success")))
    session._push_line("Agent> 完成")
    session._push_line("[Step 001] intent 1（预览）: 旧预览")
    session._push_line("[Step 001] reasoning: 旧思考")
    session.push({"type": "stream", "text": "Agent> 完成"})
    assert [event["kind"] for event in session.backlog] == ["phase", "answer"] + ["stdout"] * 3 + ["stream"]
    assert all(event["legacy_semantics_suppressed"] for event in session.backlog[2:])
    state = hub.state()
    assert state["structured_semantics"] and state["answer"] == "完成"
    assert state["phase"] == "completed" and state["event_cursor"] == 6
    assert state["event_state"]["terminal"]["event_id"] == "source-72"
    assert state["event_view_only"] and not state["event_authoritative"]
    session.push({"type": "state", "status": "running", "steps": 0, "step": 0})
    assert session.event_view()["state"]["phase"] == "completed"


def test_projection_ignores_old_turn_and_old_attempt_events(session):
    session.push(envelope("model_status", 1, stage="waiting", attempt_id="a"))
    session.push(envelope("model_status", 2, stage="generating", attempt_id="b"))
    session.push(envelope("model_status", 3, stage="error", attempt_id="a"))
    assert session.event_view()["state"]["model_status"]["stage"] == "generating"
    session.push(envelope("phase", 4, turn_id="turn-2", phase="planning"))
    session.push(envelope("answer", 5, text="旧回答"))
    state = session.event_view()["state"]
    assert state["turn_id"] == "turn-2" and state["terminal"] is None


def test_capture_and_registration_are_atomic_under_concurrent_publish(session):
    _, first = session.subscribe_events()
    barrier = threading.Barrier(3)

    def publish(offset):
        barrier.wait()
        for i in range(64):
            session.push({"type": "stdout", "text": str(offset + i)})

    threads = [threading.Thread(target=publish, args=(offset,)) for offset in (0, 64)]
    for thread in threads:
        thread.start()
    barrier.wait()
    backlog, second = session.subscribe_events()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert [first.get_nowait()["seq"] for _ in range(128)] == list(range(1, 129))
    combined = backlog + [second.get_nowait() for _ in range(second.qsize())]
    assert [event["seq"] for event in combined] == list(range(1, 129))
    session.unsubscribe_events(first)
    session.unsubscribe_events(second)


class StreamFixture:
    def __init__(self, path, headers=None, *, on_headers=None, flushes=1):
        self.path = path
        self.headers = headers or {}
        self.code = None
        self.response_headers = {}
        self.error = None
        self.wfile = io.BytesIO()
        self.on_headers = on_headers
        self.flushes = flushes
        self.wfile.flush = self.flush

    def send_response(self, code):
        self.code = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        if self.on_headers:
            self.on_headers()

    def _json(self, payload, code=200):
        self.code, self.error = code, payload

    def flush(self):
        self.flushes -= 1
        if self.flushes <= 0:
            raise BrokenPipeError("离线客户端已断开")

    def events(self):
        lines = self.wfile.getvalue().decode().splitlines()
        events = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
        ids = [int(line[4:]) for line in lines if line.startswith("id: ")]
        assert ids == [event["seq"] for event in events]
        return events


@pytest.mark.parametrize("handler", ["hub", "console"])
def test_sse_routes_use_delivery_ids_and_resume_after_header(session, monkeypatch, handler):
    session.push(envelope("action_status", 501, action_id="a", status="outcome_unknown"))
    session.push(envelope("action_status", 509, action_id="a", status="verification_failed"))
    if handler == "hub":
        monkeypatch.setattr(demo_hub, "HUB", SimpleNamespace(session=session))
        path, route = "/api/events?after=0", demo_hub.Handler.do_GET
    else:
        monkeypatch.setattr(console_server.MANAGER, "sessions", {session.sid: session})
        path, route = f"/api/sessions/{session.sid}/stream?after=0", console_server.Handler.do_GET
    request = StreamFixture(path, {"Last-Event-ID": "1"})
    route(request)
    assert request.code == 200
    assert request.response_headers["Content-Type"] == "text/event-stream"
    assert [(event["seq"], event["sequence"], event["status"]) for event in request.events()] == [
        (2, 509, "verification_failed")]
    assert not session._subscribers


NODE_PRELUDE = r"""
const assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
const elements = new Map(), bubbles = [], rendered = [];
const element = id => {
  if (!elements.has(id)) elements.set(id, {textContent:'', innerHTML:'', className:'', hidden:false, style:{}, dataset:{}});
  return elements.get(id);
};
let locked = false, lockCalls = 0, fetchCalls = 0;
const context = {$:element, window:{__guiOnly:false},
  bubble:(kind,text) => bubbles.push({kind,text}), liveEnd() {}, liveStream() {},
  renderStep:(...args) => rendered.push(args), renderModelStatus() {},
  handleModelStatus(ev) { context.lastModel = ev; }, setSendLock(value) { locked = value; lockCalls++; },
  fetch() { fetchCalls++; return Promise.reject(new Error('离线：不允许发请求')); },
  esc:s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;')};
vm.createContext(context);
vm.runInContext(`let lastSeq=-1,lastAsk='',lastAnswer='',guiReasoningText='',hudState=null;
  let currentModelRequest=null,modelSessionStatus='running'; const chatLog=[];
  const stepCards=new Map();
  const PH={thinking:'THINKING',executing:'EXECUTING',completed:'COMPLETED',attention:'ATTENTION',aborted:'ABORTED'};
` + fs.readFileSync(0,'utf8'), context);
const run = text => vm.runInContext(text,context);
"""


def _run_node_fixture(script, fixture_path=None):
    """从 HUB_HTML 抽取真实语义函数与 SSE 处理器，在 node:vm 里离线运行。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node.js 运行离线前端语义回归")
    html = demo_hub.HUB_HTML
    helpers = "const liveIntent =" + html.split("const liveIntent =", 1)[1].split("// 页面加载时", 1)[0]
    clear = "function clearView() {" + html.split("function clearView() {", 1)[1].split("let sendLocked", 1)[0]
    handler = "const processEvent = " + html.split("es.onmessage = ", 1)[1].split("\n\nasync function poll()", 1)[0]
    env = {"PATH": os.environ.get("PATH", "")}
    if fixture_path is not None:
        env["MAI_FIXTURE"] = str(fixture_path)
    result = subprocess.run([node, "-e", NODE_PRELUDE + script], input=helpers + clear + handler,
                            capture_output=True, text=True, timeout=20, env=env)
    assert result.returncode == 0, result.stderr


def test_browser_semantics_snapshot_late_events_and_bounded_history():
    script = r"""
let delivery=0, source=800;
const event = (kind,fields={}) => ({schema_version:1,event_id:'e'+(++source),sequence:source,
  seq:++delivery,session_id:'s',turn_id:'t1',attempt_id:'a',origin:'harness',text:'',step:3,kind,...fields});
const emit = ev => { context.frame={data:JSON.stringify(ev),lastEventId:String(ev.seq)}; run('processEvent(frame)'); };
emit(event('phase',{phase:'planning',type:'wrong_alias',cursor:999999}));
assert.equal(element('phase').textContent,'THINKING'); assert.equal(locked,true);
assert.equal(run('lastSeq'),1);
for (const [status,label] of Object.entries({waiting_resource:'等待资源',dispatching:'正在派发',
    running:'动作运行中',receipt_received:'未验证',outcome_unknown:'结果未知',verification_failed:'验证失败'})) {
  emit(event('action_status',{action_id:'action',status}));
  assert.ok(element('actionstatus').textContent.includes(label));
  assert.notEqual(element('phase').textContent,'COMPLETED'); assert.equal(locked,true);
}
emit(event('code_start',{block_index:1})); emit(event('code_delta',{block_index:1,text:'old draft'}));
emit(event('model_status',{attempt_id:'b',stage:'generating'}));
assert.equal(run('liveCode.active'),false);
const before = context.lastModel;
emit(event('model_status',{attempt_id:'a',stage:'error'}));
assert.equal(context.lastModel,before);
emit(event('question',{attempt_id:'b',text:'确认吗？'})); assert.equal(locked,false);
emit(event('answer',{attempt_id:'b',text:'完成'}));
emit(event('finished',{attempt_id:'b',status:'success',answer:'完成'}));
emit({type:'stdout',origin:'hub_compat',seq:++delivery,text:'Agent> 完成'});
assert.equal(bubbles.filter(b => b.kind==='ag').length,1);
emit(event('reasoning_delta',{attempt_id:'b',text:'迟到思考'}));
assert.notEqual(element('think').textContent,'迟到思考');
emit(event('phase',{turn_id:'t2',phase:'planning'}));
emit(event('answer',{turn_id:'t1',attempt_id:null,text:'旧轮回答'}));
assert.equal(run('semanticView.scope.turn_id'),'t2');
assert.equal(bubbles.filter(b => b.kind==='ag').length,1);
emit(event('intent_delta',{turn_id:'t2',text:'新意图',block_index:1}));
emit(event('code_start',{turn_id:'t2',block_index:1}));
emit(event('code_delta',{turn_id:'t2',block_index:1,text:'stale draft'}));
const watermark=delivery+50;
const state={session_id:'s',turn_id:'t2',attempt_id:'a',source_sequence:source,
  phase:'receipt_received',step:3,last_intent:{step:3,text:'快照意图'},model_status:null,
  question:null,terminal:null,actions:[{action_id:'action',status:'receipt_received'}],
  completed_turns:[{session_id:'s',turn_id:'t1',attempt_id:'b',turn_closed:true}]};
emit({kind:'resync_snapshot',type:'wrong',cursor:watermark,seq:watermark,state});
assert.equal(run('lastSeq'),watermark); assert.equal(run('liveCode.active'),false);
assert.equal(element('intents').textContent,'快照意图');
assert.ok(element('actionstatus').textContent.includes('未验证'));
delivery=watermark;
emit(event('action_status',{turn_id:'t2',action_id:'action',status:'verification_failed'}));
assert.ok(element('actionstatus').textContent.includes('验证失败'));
const last=run('lastSeq');
emit(event('phase',{turn_id:'t2',phase:'execution',seq:last,sequence:source+10000}));
assert.equal(element('phase').textContent,'验证失败');
for (let i=0;i<150;i++) {
  emit(event('phase',{turn_id:'turn'+i,phase:'planning'}));
  emit(event('answer',{turn_id:'turn'+i,text:'answer'+i}));
}
assert.ok(run('semanticView.completedTurns.size')<=64);
assert.ok(run('semanticView.answers.size')<=64);
assert.ok(run('semanticView.actions.size')<=64);
assert.ok(run('lastSeq')<run('semanticView.sourceSequence'));
const terminalState={...state,turn_id:'turn149',source_sequence:source,phase:'completed',
  terminal:{kind:'finished',event_id:'last-terminal',status:'success',answer:'answer149'}};
emit({type:'snapshot',seq:++delivery,cursor:delivery,state:terminalState});
assert.equal(bubbles.at(-1).text,'answer149'); assert.equal(locked,false);
assert.equal(run('liveCode.blocks.size'),0);
const count=bubbles.length;
emit({type:'snapshot',seq:delivery,cursor:delivery,state:terminalState});
assert.equal(bubbles.length,count);
const failed={...terminalState,phase:'attention',terminal:{kind:'aborted',event_id:'failure',text:'用户取消'}};
emit({kind:'snapshot',seq:++delivery,cursor:delivery,state:failed});
assert.equal(bubbles.at(-1).text,'用户取消'); assert.equal(locked,false);
// 中止类终态优先于过时的 phase 摘要，显示为 aborted 而非 attention/completed。
assert.equal(element('phase').textContent,'ABORTED'); assert.equal(element('phase').className,'pill aborted');
// poll 已提前恢复较新摘要时，旧 snapshot 不得先清空界面。
run(`applyProjection(${JSON.stringify(terminalState)},${delivery+10},false)`);
const phase=element('phase').textContent;
emit({kind:'snapshot',seq:++delivery,cursor:delivery,state:failed});
assert.equal(element('phase').textContent,phase);
"""
    _run_node_fixture(script)


async def _no_input(_prompt):
    raise AssertionError("离线生产链路不应读取输入")


class Producer:
    """mac_agent.present → deliver_event → EventClock.make → PlainFrontend(hub_events) → HubSession._push_line。"""

    def __init__(self, hub=None, session_id="producer-session"):
        self.hub = hub or demo_hub.Hub({})
        self.clock = EventClock(session_id, sequence=900)  # 源序号与 Hub seq 故意错开
        self.out = io.StringIO()
        self.plain = PlainFrontend(_no_input, interactive=False, stdout=self.out,
                                   stderr=io.StringIO(), hub_events=True)
        self.wires: list[dict] = []
        self.current_turn = None

    def present(self, kind, text="", *, turn="turn-0", step=None, payload=None):
        # 与 mac_agent.deliver_event 一致：后续轮次在 phase 前显式发布 turn_started。
        if turn is not None and turn != self.current_turn:
            previous = self.current_turn
            self.current_turn = turn
            if previous is not None and kind != "turn_started":
                self.present("turn_started", turn=turn)
        wire = self.clock.make(kind, text=text, step=step, task_id="task-0", turn_id=turn,
                               origin="harness", policy_revision=0, **(payload or {}))
        self.wires.append(copy.deepcopy(wire))
        start = len(self.out.getvalue())
        self.plain.emit(AgentEvent.from_wire(copy.deepcopy(wire)))
        for line in self.out.getvalue()[start:].splitlines():
            if line:
                assert line.startswith(demo_hub.HUB_EVENT_PREFIX), line
                self.hub.session._push_line(line)
        return wire

    def answer(self, text, status, *, turn="turn-0"):
        # mac_agent: present("answer", task_answer or task_status, payload={"status": task_status})
        return self.present("answer", text or status, turn=turn, step=1, payload={"status": status})

    def finished(self, final_status, answer, *, turn="turn-0", displayed=True, **extra):
        # mac_agent: present("finished", final_status, payload={answer, answer_displayed, trajectory})；
        # extra 模拟主代理即将补上的 status 字段。
        return self.present("finished", final_status, turn=turn, payload={
            "answer": answer, "answer_displayed": displayed, "trajectory": "/offline/run", **extra})


def _recovered_actions(journal):
    # 与 mac_agent resume 时组装 recovery_report payload 的推导逐字一致。
    return [{"action_id": a["action_id"], "status": a["recovery_state"],
             "operation": a["operation"], "resource_id": a.get("resource_id"),
             "execution": (a.get("receipt") or {}).get("execution", "unknown"),
             "effect": (a.get("receipt") or {}).get("effect", "unknown"),
             "verification": "unverified"} for a in journal.recover()]


def _journal_actions(tmp_path):
    """真实 ActionJournal：50 未派发、3 拒绝、10 有回执未验证、15 结果未知（故意排在最后）。"""
    path = tmp_path / "actions.jsonl"
    journal = ActionJournal(path, "journal-session")

    def step(action_id, stage, **extra):
        event = {"type": "action_journal", "journal_stage": stage, "action_id": action_id,
                 "sdk_session_id": "sdk-1", "operation": "click", "request": {"target": action_id},
                 "resource_id": f"res-{action_id}", **extra}
        assert journal.transition(event, {"turn_id": "old-turn"})["allowed"], (action_id, stage)

    for i in range(50):
        step(f"pending-{i:02d}", "intent_recorded")
    for i in range(3):
        step(f"rejected-{i}", "intent_recorded")
        step(f"rejected-{i}", "rejected", receipt={"execution": "rejected", "effect": "not_applied"})
    for i in range(10):
        step(f"receipt-{i}", "intent_recorded")
        step(f"receipt-{i}", "dispatching")
        step(f"receipt-{i}", "receipt_received", receipt={"execution": "completed", "effect": "applied"})
    for i in range(15):
        step(f"unknown-{i:02d}", "intent_recorded")
        step(f"unknown-{i:02d}", "dispatching")
        if i % 2:
            step(f"unknown-{i:02d}", "outcome_unknown")
    journal.close()
    reopened = ActionJournal(path, "journal-session")  # 模拟 --resume 重开
    try:
        return _recovered_actions(reopened)
    finally:
        reopened.close()


def _without_delivery(event):
    return {key: value for key, value in event.items() if key not in {"seq", "type"}}


@pytest.mark.parametrize("script, phase, status, answer", [
    # 生产形态：answer 带 status=failure，不得因为有回答就 completed。
    ([("answer", "表单已提交", "failure"), ("finished", "failure", "表单已提交", {})],
     "attention", "failure", "表单已提交"),
    ([("answer", "完成", "success"), ("finished", "success", "完成", {})], "completed", "success", "完成"),
    # 旧 finished 只在 text 放 final_status、无 status：按状态词映射。
    ([("finished", "success", "", {})], "completed", "success", None),
    ([("answer", "停在第 3 步", "stalled"), ("finished", "stalled", "停在第 3 步", {})],
     "attention", "stalled", "停在第 3 步"),
    ([("answer", "", "max_steps"), ("finished", "max_steps", "", {})], "attention", "max_steps", "max_steps"),
    ([("finished", "error", "RuntimeError: x", {})], "attention", "error", "RuntimeError: x"),
    ([("finished", "cancelled", "Cancelled by user.", {})], "aborted", "cancelled", "Cancelled by user."),
    ([("finished", "interrupted", "Interrupted by user.", {})], "aborted", "interrupted", "Interrupted by user."),
    # 缺状态的 answer：不猜成功。
    ([("answer_nostatus", "看起来完成了")], "attention", None, "看起来完成了"),
    # 主代理新 producer：status 在 fields，优先于 text。
    ([("answer", "完成", "success"), ("finished", "done", "完成", {"status": "failed"})],
     "attention", "failed", "完成"),
    ([("finished", "任意文本", "答案", {"status": "success"})], "completed", "success", "答案"),
    ([("aborted", "用户中止；本轮已停止，上下文保留。")], "aborted", "aborted", None),
    ([("turn_finished", "", "completed")], "completed", "completed", None),
    ([("turn_finished", "", "failed")], "attention", "failed", None),
    ([("turn_finished", "", "stopped")], "aborted", "stopped", None),
])
def test_hub_terminal_state_comes_from_explicit_status_not_answer_presence(script, phase, status, answer):
    p = Producer()
    p.present("phase", "planning", payload={"phase": "planning"})
    for kind, text, *rest in script:
        if kind == "answer":
            p.answer(text, rest[0])
        elif kind == "answer_nostatus":
            p.present("answer", text, step=1)
        elif kind == "finished":
            p.finished(text, rest[0], **rest[1])
        elif kind == "turn_finished":
            p.present("turn_finished", text, payload={"state": rest[0]})
        else:
            p.present(kind, text, step=1)
    session = p.hub.session
    state = session.event_view()["state"]
    terminal = state["terminal"]
    assert state["phase"] == terminal["terminal_state"] == phase
    assert terminal["terminal_status"] == status
    assert (terminal.get("answer") or None) == answer and p.hub.state()["answer"] == answer
    assert terminal["event_id"] == p.wires[-1]["event_id"]
    # 重复投递同一终态：源 sequence 拒绝，投影不变。
    session._push_line(demo_hub.HUB_EVENT_PREFIX + json.dumps(p.wires[-1]))
    assert session.event_view()["state"] == state
    # 源 envelope 原样保留；seq 只是 Hub 投递游标。
    v1 = [event for event in session.backlog if event["origin"] != "hub_compat"]
    assert [_without_delivery(event) for event in v1] == p.wires + [p.wires[-1]]
    assert [event["seq"] for event in session.backlog] == list(range(1, len(session.backlog) + 1))
    assert '"verified"' not in json.dumps([state, p.hub.state()])


def test_answer_then_finished_share_one_terminal_and_keep_first_answer():
    p = Producer()
    p.present("phase", "planning", payload={"phase": "planning"})
    answer = p.answer("第一份回答", "success")
    p.finished("success", "第一份回答")
    # 后续缺状态的终态帧沿用前一状态，不替换已显示的回答。
    p.present("turn_complete", "其它文本", payload={"answer": "第二份回答"})
    state = p.hub.session.event_view()["state"]
    assert state["phase"] == "completed" and state["terminal"]["terminal_status"] == "success"
    assert state["terminal"]["answer"] == "第一份回答" == p.hub.state()["answer"]
    assert answer["status"] == "success" and "status" not in p.wires[2]


def test_recovery_report_projection_is_bounded_read_only_and_session_scoped(tmp_path, monkeypatch):
    actions = _journal_actions(tmp_path)
    assert len(actions) == 78
    assert {a["status"] for a in actions} == {"not_dispatched", "rejected", "receipt_unverified", "outcome_unknown"}
    p = Producer()
    session = p.hub.session
    monkeypatch.setattr(session, "send", lambda *_: pytest.fail("对账报告不得自动投递重试"))
    p.present("phase", "planning", payload={"phase": "planning"})
    p.answer("完成", "success")
    p.finished("success", "完成")
    before = session.event_view()["state"]
    # resume 时 turn 未知：会话级只读帧。
    wire = p.present("recovery_report", RECOVERY_TEXT, turn=None, payload={"actions": actions})
    state = session.event_view()["state"]
    report = state["recovery_report"]
    for key in ("phase", "terminal", "question", "turn_id", "attempt_id", "actions", "completed_turns"):
        assert state[key] == before[key], key
    assert state["source_sequence"] == wire["sequence"]
    assert (report["total"], report["unknown"], report["unverified"], report["not_dispatched"], report["omitted"]) == (
        78, 15, 10, 50, 14)
    assert len(report["actions"]) == 64 == console_server.SUMMARY_LIMIT
    kept = [a["action_id"] for a in report["actions"]]
    assert kept[:15] == [f"unknown-{i:02d}" for i in range(15)]
    assert kept[15:25] == [f"receipt-{i}" for i in range(10)]
    assert all(a["verification"] == "unverified" for a in report["actions"])
    assert report["view_only"] and not report["authoritative"]
    assert report["event_id"] == wire["event_id"] and report["sequence"] == wire["sequence"]
    assert '"verified"' not in json.dumps(state)
    # 源事件不被截断或重写；不派生 question/approval/retry。
    stored = session.backlog[-1]
    assert _without_delivery(stored) == wire and len(stored["actions"]) == 78
    assert stored["seq"] == len(session.backlog) != stored["sequence"]
    assert [event["kind"] for event in session.backlog] == ["phase", "answer", "finished", "recovery_report"]
    # 新 turn 不清对账；终态后的对账也不锁发送。
    p.present("phase", "planning", turn="turn-1", payload={"phase": "planning"})
    assert session.event_view()["state"]["recovery_report"] == report
    assert session.event_view()["state"]["phase"] == "planning"
    # 旧 sequence 的对账帧不覆盖。
    session._push_line(demo_hub.HUB_EVENT_PREFIX + json.dumps({**wire, "actions": []}))
    assert session.event_view()["state"]["recovery_report"] == report
    # 跨 session 即消失。
    Producer(p.hub, "another-session").present("phase", "planning", payload={"phase": "planning"})
    assert session.event_view()["state"]["recovery_report"] is None


@pytest.mark.parametrize("boundary", ["reset", "restart"])
def test_recovery_report_disappears_on_reset_and_restart(tmp_path, boundary):
    p = Producer()
    p.present("recovery_report", RECOVERY_TEXT, turn=None, payload={"actions": _journal_actions(tmp_path)})
    assert p.hub.session.event_view()["state"]["recovery_report"]["unknown"] == 15
    if boundary == "reset":
        p.hub.session.push({"type": "reset", "text": "新会话"})
    else:
        p.hub.session._reset_for_restart()
    assert p.hub.session.event_view()["state"]["recovery_report"] is None


def test_hub_compat_recovery_report_is_not_a_reconciliation_source(session):
    session.push({"type": "recovery_report", "text": "旧日志",
                  "actions": [{"action_id": "x", "status": "outcome_unknown"}]})
    assert session.event_view()["state"]["recovery_report"] is None
    assert session.event_view()["state"]["session_id"] is None


def test_recovery_report_survives_retention_and_slow_consumer_snapshots(tmp_path):
    p = Producer()
    session = p.hub.session
    p.present("recovery_report", RECOVERY_TEXT, turn=None, payload={"actions": _journal_actions(tmp_path)})
    p.present("phase", "planning", payload={"phase": "planning"})
    p.answer("部分完成", "failure")
    _, slow = session.subscribe_events()
    for i in range(2001):
        session.push({"type": "stdout", "text": str(i)})
    frames = []
    while not slow.empty():
        frames.append(slow.get_nowait())
    resync = [event for event in frames if event["kind"] == "resync_snapshot"][-1]
    backlog, live = session.subscribe_events(1)
    assert len(backlog) == 1 and backlog[0]["reason"] == "retention_expired"
    for snapshot in (resync, backlog[0]):
        state = snapshot["state"]
        assert state["recovery_report"]["total"] == 78 and state["recovery_report"]["unknown"] == 15
        assert state["recovery_report"]["unverified"] == 10 and len(state["recovery_report"]["actions"]) == 64
        assert state["phase"] == "attention" and state["terminal"]["terminal_status"] == "failure"
        assert snapshot["seq"] == snapshot["cursor"] and snapshot["view_only"] and not snapshot["authoritative"]
    session.unsubscribe_events(slow)
    session.unsubscribe_events(live)


def test_browser_terminal_states_and_recovery_report_from_producer_frames(tmp_path):
    p = Producer()
    session = p.hub.session
    segments, expected = {}, {}

    def segment(name, build):
        watermark = session.event_view()["cursor"]  # backlog 有界，按投递游标切片
        build()
        segments[name] = copy.deepcopy([event for event in session.backlog if event["seq"] > watermark])
        expected[name] = session.event_view()["state"]["phase"]

    def turn(name, *frames):
        def build():
            p.present("phase", "planning", turn=name, payload={"phase": "planning"})
            for frame in frames:
                frame(name)
        return build

    actions = _journal_actions(tmp_path)
    segment("recovery", lambda: p.present("recovery_report", RECOVERY_TEXT, turn=None, payload={"actions": actions}))
    segment("failure", turn("turn-0", lambda t: p.answer("表单已提交", "failure", turn=t),
                            lambda t: p.finished("failure", "表单已提交", turn=t)))
    segment("success", turn("turn-1", lambda t: p.answer("完成", "success", turn=t),
                            lambda t: p.finished("success", "完成", turn=t)))
    segment("legacy_finished", turn("turn-2", lambda t: p.finished("success", "", turn=t, displayed=False)))
    segment("answer_nostatus", turn("turn-3", lambda t: p.present("answer", "看起来完成了", turn=t)))
    segment("aborted", turn("turn-4", lambda t: p.present("aborted", "用户中止；本轮已停止，上下文保留。", turn=t),
                            lambda t: p.finished("cancelled", "", turn=t, displayed=False)))
    segment("late_recovery", lambda: p.present(
        "recovery_report", RECOVERY_TEXT, turn=None, payload={"actions": actions[:3]}))
    assert expected == {"recovery": None, "failure": "attention", "success": "completed",
                        "legacy_finished": "completed", "answer_nostatus": "attention",
                        "aborted": "aborted", "late_recovery": "aborted"}
    for i in range(2001):
        session.push({"type": "stdout", "text": str(i)})
    backlog, live = session.subscribe_events(1)
    session.unsubscribe_events(live)
    segments["snapshot"] = backlog
    segment("other_session", lambda: Producer(p.hub, "another-session").present(
        "phase", "planning", payload={"phase": "planning"}))
    assert [event["kind"] for event in segments["other_session"]] == ["phase"]
    fixture = tmp_path / "frames.json"
    fixture.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
    script = r"""
const fixture = JSON.parse(fs.readFileSync(process.env.MAI_FIXTURE,'utf8'));
const play = name => { for (const ev of fixture[name]) {
  context.frame={data:JSON.stringify(ev),lastEventId:String(ev.seq)}; run('processEvent(frame)'); } };
const ag = () => bubbles.filter(b => b.kind==='ag').map(b => b.text);
const sys = () => bubbles.filter(b => b.kind==='sys').map(b => b.text);
const box = element('recoveryreport');
// 只读对账：不动 phase/terminal，不解锁/锁定发送，不发请求，不冒气泡。
play('recovery');
assert.equal(box.hidden,false);
assert.ok(box.textContent.startsWith('历史动作对账（只读'));
assert.ok(box.textContent.includes('共 78 项 · 结果未知 15 · 有回执未验证 10'));
assert.ok(box.textContent.includes('另有 14 项未列出'));
assert.equal(run('semanticView.recovery.actions.length'),64);
assert.equal(run('JSON.stringify(semanticView.recovery.actions.slice(0,15).map(a => a.status))'),
  JSON.stringify(Array(15).fill('outcome_unknown')));
assert.equal(element('phase').textContent,''); assert.equal(run('semanticView.terminal'),null);
assert.equal(run('semanticView.phase'),null);
assert.equal(lockCalls,0); assert.equal(fetchCalls,0); assert.equal(bubbles.length,0);
assert.ok(!box.textContent.includes('已验证'));
// answer(status=failure)+finished(failure)：attention，回答只一次。
play('failure');
assert.equal(element('phase').textContent,'ATTENTION'); assert.equal(locked,false);
assert.ok(element('conn').textContent.startsWith('需要注意'));
assert.deepEqual(ag(),['表单已提交']); assert.equal(box.hidden,false);
play('success');
assert.equal(element('phase').textContent,'COMPLETED'); assert.deepEqual(ag(),['表单已提交','完成']);
play('legacy_finished');
assert.equal(element('phase').textContent,'COMPLETED');
play('answer_nostatus');
assert.equal(element('phase').textContent,'ATTENTION'); assert.equal(ag().at(-1),'看起来完成了');
assert.equal(sys().length,0);
play('aborted');
assert.equal(element('phase').textContent,'ABORTED'); assert.equal(element('phase').className,'pill aborted');
assert.ok(element('conn').textContent.startsWith('本轮已中止')); assert.equal(locked,false);
assert.deepEqual(sys(),['用户中止；本轮已停止，上下文保留。']);
assert.equal(ag().length,3);
// 终态后的对账帧只更新报告。
const locks=lockCalls, count=bubbles.length;
play('late_recovery');
assert.ok(box.textContent.includes('共 3 项')); assert.equal(element('phase').textContent,'ABORTED');
assert.equal(lockCalls,locks); assert.equal(bubbles.length,count);
// retention 快照：服务端有界摘要恢复对账和终态。
const answers=ag().length;
play('snapshot');
assert.equal(box.hidden,false); assert.ok(box.textContent.includes('共 3 项 · 结果未知 0'));
assert.equal(element('phase').textContent,'ABORTED'); assert.equal(ag().length,answers);
assert.equal(fetchCalls,0);
// 跨 session：对账消失。
play('other_session');
assert.equal(box.hidden,true); assert.equal(run('semanticView.recovery'),null);
"""
    _run_node_fixture(script, fixture)


def test_browser_snapshot_restores_bounded_recovery_counts_and_reset_clears(tmp_path):
    p = Producer()
    session = p.hub.session
    p.present("recovery_report", RECOVERY_TEXT, turn=None, payload={"actions": _journal_actions(tmp_path)})
    for i in range(2001):
        session.push({"type": "stdout", "text": str(i)})
    backlog, live = session.subscribe_events(1)
    session.unsubscribe_events(live)
    session.push({"type": "reset", "text": "新会话"})
    fixture = tmp_path / "frames.json"
    fixture.write_text(json.dumps({"snapshot": backlog, "reset": session.backlog[-1:]}), encoding="utf-8")
    script = r"""
const fixture = JSON.parse(fs.readFileSync(process.env.MAI_FIXTURE,'utf8'));
const play = name => { for (const ev of fixture[name]) {
  context.frame={data:JSON.stringify(ev),lastEventId:String(ev.seq)}; run('processEvent(frame)'); } };
const box = element('recoveryreport');
play('snapshot');
// 快照只带 64 条，计数仍来自服务端总计。
assert.ok(box.textContent.includes('共 78 项 · 结果未知 15 · 有回执未验证 10'));
assert.ok(box.textContent.includes('另有 14 项未列出'));
assert.equal(run('semanticView.recovery.actions.length'),64);
assert.equal(element('phase').textContent,'READY'); assert.equal(fetchCalls,0);
play('reset');
assert.equal(box.hidden,true); assert.equal(run('semanticView.recovery'),null);
"""
    _run_node_fixture(script, fixture)
