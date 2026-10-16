"""Model lifecycle visibility without calling a provider or controlling the desktop."""

import asyncio
import io
import json
import shutil
import subprocess

import pytest

from agent_events import AgentEvent, PlainFrontend, model_status_event
import demo_hub
import model_stream


def test_timeout_is_identified_and_retry_status_matches_actual_attempts(monkeypatch):
    events = []
    client = model_stream.ModelClient(base_url="https://example.test/v1", api_key="test",
                                      model_name="test", timeout=30, retries=1,
                                      on_event=events.append)
    client.client = object()
    calls = 0

    async def request(payload, headers, preview):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError()
        # A partial tag is already model output, even before an intent can be parsed.
        preview.feed("<int")
        preview.feed("ent>观察</intent><code>pass</code>")
        return "<intent>观察</intent><code>pass</code>", "", {}, "stop"

    monkeypatch.setattr(client, "_request", request)
    asyncio.run(client.complete([{"role": "user", "content": "test"}], max_tokens=128))
    statuses = [model_status_event(e, 4) for e in events]
    statuses = [s for s in statuses if s]
    assert [s["stage"] for s in statuses] == ["waiting", "retrying", "waiting", "generating", "validating"]
    assert statuses[0]["timeout_seconds"] == 30
    assert statuses[2]["attempt"] == statuses[2]["max_attempts"] == 2
    assert "超时" in statuses[1]["text"] and "自动重试" in statuses[1]["text"]
    assert sum(e["type"] == "response_started" for e in events) == 1


@pytest.mark.parametrize("retrying", [True, False])
def test_parse_failure_never_claims_retry_when_budget_exhausted(retrying):
    status = model_status_event({"type": "response_rejected", "retrying": retrying}, 3)
    assert status["stage"] == ("retrying" if retrying else "error")
    assert ("自动重试" in status["text"]) is retrying


def test_wall_clock_timeout_reaches_frontend_as_terminal_error(monkeypatch):
    events = []
    client = model_stream.ModelClient(base_url="https://example.test/v1", api_key="test",
                                      model_name="test", timeout=0.01, retries=0,
                                      on_event=events.append)
    client.client = object()

    async def request(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(model_stream.AgentError):
        asyncio.run(client.complete([{"role": "user", "content": "test"}], max_tokens=128))
    failure = next(e for e in events if e["type"] == "request_aborted")
    frame = model_status_event(failure, 0)
    assert frame["stage"] == "error"
    assert "超时" in frame["text"] and "自动重试" not in frame["text"]


def test_lifecycle_frame_survives_unterminated_reasoning_and_hub_snapshot(tmp_path):
    output = io.StringIO()
    frontend = PlainFrontend(lambda _: None, interactive=False, stdout=output, hub_events=True)
    frontend.emit(AgentEvent("reasoning_delta", "still thinking", step=2))
    frame = model_status_event({"type": "request_aborted", "reason": "timeout", "retrying": False}, 2)
    frontend.emit(AgentEvent("model_status", frame["text"], step=2, payload=frame))
    hub = demo_hub.Hub({})
    for line in output.getvalue().splitlines():
        hub.session._push_line(line)
    assert hub.session.model_request == frame
    path = tmp_path / "traj.json"
    path.write_text(json.dumps({"status": "running", "steps": [{"step": 2}]}))
    hub.session.run_dir = str(tmp_path)
    hub.session.status = "exited"
    state = hub.state()
    assert state["model_request"] == frame
    assert state["phase"] == "attention"


def test_browser_wait_timer_failure_notice_and_terminal_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the browser simulation")
    script = demo_hub.HUB_HTML.split("let currentModelRequest =", 1)[1].split("const liveIntent =", 1)[0]
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const box = {hidden:true, textContent:'', dataset:{}};
const notices = [];
const context = {$:() => box, Date, Number, Math, setInterval:() => {},
  bubble:(kind, text) => notices.push(text)};
vm.createContext(context);
vm.runInContext('let currentModelRequest =' + fs.readFileSync(0,'utf8'), context);
const call = expr => vm.runInContext(expr, context);
assert.match(call("modelStatusLabel({stage:'waiting',text:'正在等待 API 响应',at_ms:1000,attempt:2,max_attempts:4,timeout_seconds:600},'running',36000)"), /35 秒.*2\/4.*600 秒/);
assert.match(call("modelStatusLabel({stage:'waiting',text:'等待',at_ms:1000},'running',36000)"), /暂未收到/);
assert.match(call("modelStatusLabel({stage:'waiting'},'exited',36000)"), /Agent 已结束/);
call("modelSessionStatus = 'running'; handleModelStatus({stage:'retrying',text:'API 请求超时，将自动重试'})");
assert.equal(notices.length, 1);
assert.equal(box.dataset.stage, 'retrying');
call("handleModelStatus({stage:'retrying',text:'API 请求超时，将自动重试'},false)");
assert.equal(notices.length, 1); // polling must not duplicate the notice
call("handleModelStatus({stage:'ready',text:'通过校验'})");
assert.equal(box.hidden, true);
"""
    result = subprocess.run([node, "-e", harness], input=script, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
