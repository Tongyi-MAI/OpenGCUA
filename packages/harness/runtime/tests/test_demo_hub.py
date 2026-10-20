"""demo_hub（单会话录制工作台）离线回归：不起真实 agent、不连桌面。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess

import pytest

import console_server
import demo_hub
import demo_hud
import gui_agent
import mac_agent
from agent_events import AgentEvent, PlainFrontend


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = iter(())
        self.terminated = False
        self.signals = []
        self.returncode = None
        self.pid = 4321

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.terminated = True
        self.returncode = -sig

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9


@pytest.fixture()
def hub(monkeypatch, tmp_path):
    hub = demo_hub.Hub({"reasoning_effort": "medium"})
    hub.session.fifo = tmp_path / "hub.in"
    started: list[list[str]] = []
    environments: list[dict[str, str]] = []

    def fake_popen(cmd, **kwargs):
        started.append(cmd)
        environments.append(kwargs.get("env") or {})
        assert "MAI_FOREGROUND_REQUIRES_VISIBLE" not in (kwargs.get("env") or {})
        return _FakeProc()

    monkeypatch.setattr(demo_hub.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(demo_hub.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    hub._test_environments = environments
    return hub, started


def test_sessions_use_process_unique_fifos():
    first = console_server.Session("hub", "a", {}, "")
    second = console_server.Session("hub", "b", {}, "")

    assert first.fifo != second.fifo
    assert str(first.fifo).startswith("/tmp/mai_console_hub_")


def test_session_send_frames_multiline_message_as_one_physical_line():
    session = console_server.Session("hub", "a", {}, "")
    session.proc = _FakeProc()
    read_fd, write_fd = os.pipe()
    session._fifo_fd = write_fd
    try:
        session.send("first line\nsecond line\nthird line")
        os.close(write_fd)
        session._fifo_fd = None
        wire = os.read(read_fd, 4096)
    finally:
        os.close(read_fd)
        if session._fifo_fd is not None:
            os.close(session._fifo_fd)
            session._fifo_fd = None

    assert wire == b'\x1eMAI1 {"text":"first line\\nsecond line\\nthird line"}\n'
    assert wire.count(b"\n") == 1


def test_console_message_readers_restore_multiline_and_keep_legacy_lines(monkeypatch):
    frame = '\x1eMAI1 {"text":"first line\\nsecond line"}'

    async def framed_input(_prompt):
        return frame

    monkeypatch.setattr(mac_agent, "console_input", framed_input)
    assert asyncio.run(mac_agent.read_chat_message()) == "first line\nsecond line"
    assert gui_agent.read_console_message(io.StringIO(frame + "\n")) == "first line\nsecond line"
    assert gui_agent.read_console_message(io.StringIO("legacy input\n")) == "legacy input"


def test_hub_reasoning_cli_does_not_override_config_by_default():
    parser = demo_hub.build_parser()
    assert parser.parse_args([]).reasoning_effort is None
    assert parser.parse_args(["--reasoning-effort", "none"]).reasoning_effort == "none"


def test_hub_reports_background_default_and_transmits_explicit_opt_out():
    default_hub = demo_hub.Hub({})
    assert default_hub.state()["background_mode"] is True
    assert "--enable-background-mode" in default_hub.session._config_args()

    opted_out = demo_hub.Hub({"background_mode": False})
    assert opted_out.state()["background_mode"] is False
    assert "--disable-background-mode" in opted_out.session._config_args()


def test_background_hub_start_does_not_announce_recording_mode(hub):
    session = hub[0].session
    session.submit("任务")
    assert not any(event.get("type") == "space" for event in session.backlog)


def test_events_are_broadcast_to_every_subscriber(hub):
    session = hub[0].session
    backlog_a, subscriber_a = session.subscribe_events()
    backlog_b, subscriber_b = session.subscribe_events()
    assert backlog_a == backlog_b == []

    session.push({"type": "gui_step", "step": 0})
    event_a = subscriber_a.get_nowait()
    event_b = subscriber_b.get_nowait()
    assert event_a == event_b
    assert event_a["type"] == "gui_step" and event_a["step"] == 0

    session.unsubscribe_events(subscriber_a)
    session.unsubscribe_events(subscriber_b)


def test_gui_stdout_emits_realtime_and_terminal_events(hub):
    session = hub[0].session
    session.gui_coalesce = True
    _, subscriber = session.subscribe_events()

    session._push_line("[Turn Start] 1")
    session._push_line("[Step 001] Capturing screenshot...")
    session._push_line('[Reasoning Delta] "正在思考"')
    session._push_line("Answer: 完成")
    session._push_line("[Turn Complete] success")

    events = [subscriber.get_nowait() for _ in range(5)]
    assert [(event["type"], event.get("step"), event.get("status")) for event in events] == [
        ("gui_turn_start", None, None),
        ("gui_step", 1, None),
        ("gui_reasoning_delta", None, None),
        ("answer", None, None),
        ("gui_turn_complete", None, "success"),
    ]
    assert events[0]["round_index"] == 1
    assert events[2]["delta"] == "正在思考"
    assert events[3]["text"] == "完成"
    session.unsubscribe_events(subscriber)


def test_gui_stdout_forwards_structured_intent_preview(hub):
    session = hub[0].session
    session.gui_coalesce = True
    session._push_line('[GUI Preview] {"type":"intent_delta","step":2,"delta":"检查表单"}')
    session._push_line('[GUI Preview] {"type":"intent_end","step":2,"intent":"检查表单字段"}')
    assert [(event["type"], event["step"]) for event in session.backlog] == [
        ("gui_preview", 2), ("gui_preview", 2),
    ]
    assert session.backlog[0]["delta"] == "检查表单"
    assert session.backlog[1]["intent"] == "检查表单字段"


def test_gui_hud_state_exposes_intent_history_and_actual_actions(tmp_path):
    run = tmp_path / "gui_run"
    run.mkdir()
    path = run / "traj.json"
    path.write_text("{}", encoding="utf-8")
    trajectory = {"task": "填写 PDF", "status": "running", "steps": [
        {"step": 0, "intent": "打开文件", "actions": [{"source": "pyautogui.doubleClick(10, 20)", "status": "success"}]},
        {"step": 1, "intent": "填写姓名", "actions": [{"source": "pyautogui.write('Li')", "status": "pending"}]},
        {"step": 2, "reasoning": "下一步"},
    ]}
    state = demo_hud.build_hud_state(trajectory, path)
    assert state["gui_intents"] == [
        {"step": 0, "intent": "打开文件"},
        {"step": 1, "intent": "填写姓名"},
    ]
    assert state["gui_actions_step"] == 1
    assert state["gui_actions"] == [{"source": "pyautogui.write('Li')", "status": "pending"}]


def test_gui_trace_emits_full_intent_as_single_structured_line(capsys, monkeypatch):
    monkeypatch.setenv("GUI_AGENT_HUB_EVENTS", "1")
    gui_agent.print_model_trace("", "<tool_call>pyautogui.click(1, 2)</tool_call>",
                                streamed=True, intent="第一行\n第二行", step=3)
    lines = capsys.readouterr().out.splitlines()
    marker = next(line for line in lines if line.startswith("[GUI Preview] "))
    assert json.loads(marker[len("[GUI Preview] "):]) == {
        "type": "intent_final", "step": 3, "intent": "第一行\n第二行",
    }
    monkeypatch.delenv("GUI_AGENT_HUB_EVENTS")
    gui_agent.print_model_trace("", "<tool_call>pyautogui.click(1, 2)</tool_call>",
                                streamed=True, intent="普通终端", step=4)
    assert "[GUI Preview]" not in capsys.readouterr().out


def test_gui_intent_preview_and_action_panel_render_like_harness_steps():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the browser intent simulation")
    controls = demo_hub.HUB_HTML.split("const liveIntent =", 1)[1].split("// 页面加载时", 1)[0]
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {textContent:'', innerHTML:''});
  return elements.get(id);
};
const rendered = [];
const context = {$:element, window:{__guiOnly:true}, Map, Number, String,
  stepCards:new Map(), renderStep:(...args) => rendered.push(args),
  esc:s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;')};
vm.createContext(context);
vm.runInContext('const liveIntent =' + fs.readFileSync(0, 'utf8'), context);
const call = (name, value) => vm.runInContext(`${name}(${JSON.stringify(value)})`, context);
call('handleGuiPreview', {preview_type:'intent_start',step:2});
call('handleGuiPreview', {preview_type:'intent_delta',step:2,delta:'填写 <'});
call('handleGuiPreview', {preview_type:'intent_delta',step:2,delta:'姓氏'});
assert.equal(rendered.at(-1)[0], 3);
assert.equal(rendered.at(-1)[3], '填写 <姓氏');
assert.match(element('intents').innerHTML, /填写 &lt;姓氏/);
call('handleGuiPreview', {preview_type:'intent_final',step:2,intent:'填写姓氏'});
assert.equal(rendered.at(-1)[2][0].intent, '填写姓氏');
vm.runInContext("renderCodePanel({step:3,gui_actions_step:2,gui_actions:[{source:'pyautogui.write(\\\"Li\\\")',status:'success'}]})", context);
assert.match(element('codebody').innerHTML, /pyautogui.write/);
assert.equal(element('codestep').textContent, 'step 03');
"""
    result = subprocess.run([node, "-e", harness], input=controls,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_hub_turns_code_preview_frames_into_sse_events(hub):
    session = hub[0].session
    output = io.StringIO()
    frontend = PlainFrontend(lambda _prompt: None, interactive=False,
                             stdout=output, hub_events=True)
    frontend.emit(AgentEvent("code_delta", "x = 1\ny = 2", step=4,
                             payload={"block_index": 1}))

    session._push_line(output.getvalue().strip("\n"))

    assert session.backlog[-1]["type"] == "code_delta"
    assert session.backlog[-1]["step"] == 4
    assert session.backlog[-1]["text"] == "x = 1\ny = 2"
    assert session.backlog[-1]["block_index"] == 1


def test_hub_code_panel_streams_draft_until_matching_step_is_committed():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the browser code preview simulation")
    controls = demo_hub.HUB_HTML.split("const liveCode =", 1)[1].split("// 页面加载时", 1)[0]
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) {
    const node = {_text: '', _html: ''};
    Object.defineProperties(node, {
      textContent: {get() { return this._text; }, set(v) { this._text = v; this._html = ''; }},
      innerHTML: {get() { return this._html; }, set(v) { this._html = v; this._text = v; }},
    });
    elements.set(id, node);
  }
  return elements.get(id);
};
const context = { $: element, hudState: null, window: {__guiOnly: false},
  esc: s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;'), Map, String, Number };
vm.createContext(context);
vm.runInContext('const liveCode =' + fs.readFileSync(0, 'utf8'), context);
const call = (name, value) => vm.runInContext(`${name}(${JSON.stringify(value)})`, context);
call('handleCodePreview', {type:'code_start',step:3,block_index:1,text:''});
call('handleCodePreview', {type:'code_delta',step:3,block_index:1,text:'print("a")\n<b>'});
assert.match(element('codebody').textContent, /print\("a"\)\n<b>/);
assert.equal(element('codebody').innerHTML, '');
assert.match(element('codestate').textContent, /生成中/);
call('renderCodePanel', {step:3,blocks_step:2,blocks:[{index:1,code:'old'}]});
assert.match(element('codebody').textContent, /print\("a"\)/);
call('handleCodePreview', {type:'code_end',step:3,block_index:1,text:''});
call('renderCodePanel', {step:3,blocks_step:3,blocks:[{index:1,code:'approved'}]});
assert.match(element('codebody').innerHTML, /approved/);
assert.equal(element('codestate').textContent, '');
call('handleCodePreview', {type:'code_start',step:4,block_index:1,text:''});
call('handleCodePreview', {type:'code_delta',step:4,block_index:1,text:'draft'});
call('handleCodePreview', {type:'code_reset',step:4,text:''});
assert.doesNotMatch(element('codebody').textContent, /draft/);
"""
    result = subprocess.run([node, "-e", harness], input=controls,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_hub_inline_script_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the browser script syntax check")
    script = demo_hub.HUB_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    result = subprocess.run([node, "-e", "new (require('node:vm').Script)(require('node:fs').readFileSync(0, 'utf8'))"],
                            input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_first_message_starts_agent_then_later_messages_are_piped(hub):
    session, started = hub[0].session, hub[1]
    assert session.proc is None and session.status == "starting"
    assert session.submit("  ") == "empty"
    assert session.submit("打开知乎热榜") == "started"
    assert session.proc is not None and session.status == "running"
    assert "打开知乎热榜" in started[0]  # 首条指令即任务 positional
    assert "--reasoning-effort" in started[0]
    # mac agent 不受影响；GUI-only 会单独禁用 CLI 安全倒计时。
    assert "--countdown" not in started[0]
    assert hub[0]._test_environments[0]["MAC_AGENT_HUB_EVENTS"] == "1"
    sent: list[str] = []
    session.send = lambda text: sent.append(text)
    assert session.submit("再看第二条") == "sent"
    assert sent == ["再看第二条"] and len(started) == 1  # 不重复拉起进程


def test_gui_only_starts_without_countdown(hub):
    session, started = hub[0].session, hub[1]
    session.hub_config = {"agent_mode": "gui-only"}

    assert session.submit("你好") == "started"
    assert started[0][1:3] == [str(demo_hub.REPO / "gui_agent.py"), "--countdown"]
    assert started[0][3] == "0"
    assert "MAC_AGENT_HUB_EVENTS" not in hub[0]._test_environments[0]
    assert hub[0]._test_environments[0]["GUI_AGENT_HUB_EVENTS"] == "1"


def test_state_before_start_reports_waiting(hub):
    state = hub[0].state()
    assert state["ok"] is False and "尚未启动" in state["error"]


def test_state_uses_hud_view_model(hub, tmp_path):
    hub_obj = hub[0]
    run = tmp_path / "run"
    (run / "steps").mkdir(parents=True)
    (run / "traj.json").write_text(json.dumps({
        "task": "打开知乎热榜", "status": "running",
        "config": {"model_name": "qwen3.8-max", "reasoning_effort": "medium"},
        "steps": [{"step": 0, "intent": "绑定窗口", "reasoning": "先观察",
                   "code": "ax = await ui.ax.observe()", "metrics": {}}],
    }), encoding="utf-8")
    hub_obj.session.run_dir = str(run)
    state = hub_obj.state()
    assert state["ok"] is True and state["intent"] == "绑定窗口"
    assert state["session_status"] == hub_obj.session.status


def test_phase_completed_when_persistent_session_task_done(hub, tmp_path):
    # 常驻多轮会话：traj.status 仍是 running，但最后一个 task 已 success
    # -> phase 必须是 completed，不能永远 thinking（否则前端看起来像发送后卡死）。
    hub_obj = hub[0]
    run = tmp_path / "run_done"
    (run / "steps").mkdir(parents=True)
    (run / "traj.json").write_text(json.dumps({
        "task": "你好", "status": "running",
        "tasks": [{"task": "你好", "status": "success"}],
        "steps": [{"step": 0, "intent": "回复问候", "code": 'terminate(status="success")', "metrics": {}}],
    }), encoding="utf-8")
    hub_obj.session.run_dir = str(run)
    assert hub_obj.state()["phase"] == "completed"

    # 新一轮任务开始（最后一个 task 尚无终态）-> 回到 thinking，不能误报 completed。
    (run / "traj.json").write_text(json.dumps({
        "conversation": True, "latest_user_message": "查天气", "status": "running",
        "rounds": [
            {"user_message": "你好", "status": "success"},
            {"user_message": "查天气", "status": None},
        ],
        "steps": [{"step": 0, "metrics": {}}, {"step": 1, "intent": "观察", "metrics": {}}],
    }), encoding="utf-8")
    state = hub_obj.state()
    assert state["phase"] == "thinking"
    assert state["task"] == "查天气"


def test_hub_session_skips_space_anchor_and_stop_is_clean(hub):
    session = hub[0].session
    session._create_space_anchor()
    assert any(e["type"] == "space" and e["anchor"] is None for e in session.backlog)
    session.submit("任务")
    session.stop()
    assert session.status == "stopped" and session.proc.terminated
    assert session.proc.signals == [demo_hub.signal.SIGINT]
    assert session._fifo_fd is None


def test_html_shell_has_three_zones_and_recording_controls():
    html = demo_hub.HUB_HTML
    for needle in ("Agent Demo Studio", "getDisplayMedia", "MediaRecorder",
                   "选择屏幕", "● 录制", "中止本轮", "conversation",
                   'id="rightpane"', 'id="cmd"', 'id="think"', 'id="codebody"', 'id="intents"',
                   'id="msgs"', 'id="stepnum"', "stepCard", "renderStep", ".bub.ask", ".bub.ag", "bubble('ask'",
                   "/api/events", "/api/state", "/api/message", "/api/abort"):
        assert needle in html, needle
    # 已按需求下线：不再有 USER TASK / ACTION / TERMINAL 面板
    for gone in ("user task", "action history", "latest batch", "TERMINAL"):
        assert gone not in html, gone


def test_abort_sends_sigusr1_without_killing_session(hub, monkeypatch):
    session = hub[0].session
    assert session.abort() is False  # 未启动时不可中止
    session.submit("任务")
    session.proc.pid = 4321
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(demo_hub.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert session.abort() is True
    assert sent == [(4321, demo_hub.signal.SIGUSR1)]
    assert session.status == "running" and session.proc.terminated is False


def test_submit_restarts_session_when_agent_process_is_gone(hub, monkeypatch):
    server, started = hub
    session = server.session
    assert session.submit("第一条任务") == "started"
    first = session.proc
    # 模拟 agent 被外部 kill：进程已退出，fifo 再无读者
    first.returncode = -15
    delivered: list[str] = []
    monkeypatch.setattr(session, "send", lambda text: delivered.append(text))
    assert session.submit("第二条任务") == "started"
    assert delivered == []                      # 绝不写进没人读的 fifo
    assert session.proc is not first            # 换成了新进程
    assert len(started) == 2 and "第二条任务" in started[1]
    assert any("[新会话]" in (e.get("text") or "") for e in session.backlog)


def test_submit_restarts_when_fifo_write_loses_process_race(hub, monkeypatch):
    server, started = hub
    session = server.session
    assert session.submit("第一条任务") == "started"
    first = session.proc

    def broken_send(_text):
        first.returncode = -15
        raise BrokenPipeError("reader exited")

    monkeypatch.setattr(session, "send", broken_send)
    assert session.submit("第二条任务") == "started"
    assert session.proc is not first
    assert len(started) == 2 and "第二条任务" in started[1]


def test_reset_endpoint_stops_session_and_clears_run_reference(hub):
    server, started = hub
    session = server.session
    session.submit("第一条任务")
    session.run_dir = "runs/old_run"
    first = session.proc
    server.session.stop()
    session._reset_for_restart()
    session.push({"type": "reset", "text": "[新会话] 已结束上一个会话，输入指令开始新的"})
    assert first.terminated is True and session.proc is None
    assert session.run_dir == "" and session.status == "idle"
    assert not server.state()["ok"]                     # HUD 不再读旧轨迹
    assert session.submit("第二条任务") == "started"      # 之后输入即新会话
    assert len(started) == 2


def test_stale_reader_cannot_mark_replacement_session_exited(hub):
    session = hub[0].session
    session.submit("第一条任务")
    old = session.proc
    old_generation = session._generation
    old.returncode = -15
    session.submit("第二条任务")
    replacement = session.proc

    # 旧 stdout 线程比新会话更晚结束时，不得修改新会话状态。
    class _ClosedStdout:
        def fileno(self):
            return -1

    old.stdout = _ClosedStdout()
    session._read_stdout(old, old_generation)
    assert session.proc is replacement
    assert session.status == "running"


def test_html_exposes_session_lifecycle_controls():
    html = demo_hub.HUB_HTML
    for needle in ('id="endsess"', 'id="newsess"', "/api/reset", "clearView",
                   "结束会话", "＋ 新会话"):
        assert needle in html, needle


def test_html_locks_send_while_agent_busy():
    html = demo_hub.HUB_HTML
    for needle in ("sendLocked", "function setSendLock", "if (sendLocked) return",
                   "setSendLock(true)", ".send:disabled", "#cmd:disabled",
                   "if (s.ask_user) setSendLock(false)",
                   "if (s.session_status === 'exited' || s.session_status === 'idle') setSendLock(false)",
                   "if (s.phase === 'completed') setSendLock(false)",
                   "ev.type === 'gui_turn_start'", "ev.type === 'gui_turn_complete'", "ev.type === 'answer'",
                   "guiReasoningText +=", "Number(ev.step) + 1",
                   "body.busy", "执行中…"):
        assert needle in html, needle
    # 关键回归：启动中 session_status 可能为 null，绝不能按 !running 解锁
    assert "if (!running) setSendLock(false)" not in html


def test_recording_composites_canvas_not_tab_capture():
    # 录制改为 canvas 自合成 + captureStream(60)：rAF 每帧重绘、帧率恒定，不受标签页
    # 限流影响（标签页捕获会产出损坏 h264 / 只有首帧）。参考 demo_agent/demo_overlay/studio.js。
    html = demo_hub.HUB_HTML
    for needle in ('id="composite"', 'compCanvas.captureStream(60)', 'startComposite()',
                   'new MediaRecorder(output, options)', 'function drawComposite',
                   'cctx.drawImage', 'currentRecorder.ondataavailable', 'currentRecorder.onstop',
                   'recordError || !chunks.length'):
        assert needle in html, needle
    # 默认录制源是 canvas 自合成；getDisplayMedia 仅两处：pick 预览与「复刻hub」
    # 窗口捕获双模式。preferCurrentTab 只允许出现在解释性注释里，绝不作为选项。
    assert 'preferCurrentTab:' not in html
    assert 'rechub' in html
    assert 'new MediaRecorder(recordStream' not in html
    assert html.count('getDisplayMedia(') == 2
    # MP4 优先，避免 WebM 在 QuickTime 缺时长
    assert html.index('video/mp4;codecs=avc1') < html.index('video/webm;codecs=vp9')


def test_recording_lifecycle_records_canvas_stream_and_downloads():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the browser recording simulation")
    html = demo_hub.HUB_HTML
    controls = html.split("$('pick').onclick =", 1)[1].split("$('abort').onclick =", 1)[0]
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const messages = [], downloads = [], captures = [];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    disabled: false, textContent: '', style: {}, srcObject: null, videoWidth: 1920, videoHeight: 1080,
    classList: {add() {}, remove() {}},
  });
  return elements.get(id);
}
function makeTrack(label, displaySurface) {
  return {label, readyState: 'live', onended: null,
    getSettings() { return {width: 1920, height: 1080, frameRate: 30, displaySurface}; },
    stop() { this.readyState = 'ended'; }};
}
const previewTrack = makeTrack('Agent screen', 'monitor');
const previewStream = {getVideoTracks: () => [previewTrack], getTracks: () => [previewTrack]};
let canvasFps = null;
const canvasStream = {__canvas: true, getVideoTracks: () => [makeTrack('composite', 'canvas')], getTracks: () => []};
const compCanvas = {width: 1920, height: 1080,
  captureStream(fps) { canvasFps = fps; return canvasStream; }, getContext: () => ({})};
let started = false, recorderSource = null;
class Recorder {
  static isTypeSupported(type) { return type === 'video/mp4;codecs=avc1'; }
  constructor(source, options) {
    recorderSource = source;
    assert.equal(options.mimeType, 'video/mp4;codecs=avc1');
    this.mimeType = options.mimeType; this.state = 'inactive';
  }
  start() { this.state = 'recording'; }
  requestData() { if (this.ondataavailable) this.ondataavailable({data: {size: 4}}); }
  stop() { this.state = 'inactive'; queueMicrotask(() => { this.ondataavailable({data: {size: 8}}); this.onstop(); }); }
}
    const context = {
      $: element, bubble: (kind, text) => messages.push(text),
      hasPreview: () => previewTrack.readyState === 'live',
      lockSources: locked => ['pick','layout','phoneselect','phonerefresh','phoneconnect']
        .forEach(id => { element(id).disabled = locked; }),
      navigator: {mediaDevices: {async getDisplayMedia(options) { captures.push(options); return previewStream; }}},
      window: {MediaRecorder: Recorder, studioMediaChanged() {}}, MediaRecorder: Recorder,
  compCanvas, startComposite: () => { started = true; },
  Blob: class {constructor(chunks) {this.size = chunks.reduce((n, c) => n + c.size, 0);}},
  document: {createElement() {return {click() {downloads.push(this.download);}};}},
  URL: {createObjectURL() {return 'blob:test';}, revokeObjectURL() {}},
  setTimeout() {}, setInterval() {return 1;}, clearInterval() {}, Date,
};
vm.runInNewContext('let stream = null, recordStream = null, recorder = null, recStart = 0, recTimer = null;\n' +
  "$('pick').onclick =" + fs.readFileSync(0, 'utf8'), context);
(async () => {
  await element('pick').onclick();
  assert.equal(captures.length, 1);                    // 预览选屏一次
  assert.equal(element('video').srcObject, previewStream);
  assert.equal(element('rectoggle').disabled, false);

  await element('rectoggle').onclick();                // 开始录制
  assert.equal(started, true);                         // 启动了 canvas 合成循环
  assert.equal(canvasFps, 60);                         // captureStream(60)
  assert.equal(recorderSource, canvasStream);          // 录制源是 canvas 流，不是标签页流
  assert.equal(captures.length, 1);                    // 录制不再触发第二次 getDisplayMedia
  assert.equal(element('pick').disabled, true);

  await element('rectoggle').onclick();                // 停止 -> 下载
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(downloads.length, 1);
  assert.match(downloads[0], /^agent-demo-\d+\.mp4$/);
  assert.equal(previewTrack.readyState, 'live');       // 预览流不被停掉
  assert.ok(!messages.some(m => m.includes('录制未保存')));
})().catch(e => {console.error(e); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", harness], input=controls,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
