"""真实 run_agent（离线 fake 模型 + fake SDK worker）发布 context_status，并由 TUI/Plain fixture 消费。

替换边界同 test_journal_agent_integration：模型/桌面/SDK 是离线 fake，长期记忆与 runs 都在 tmp；
run_agent 的 prepare_context、事件投影、ActionJournal 与 CodeExecutor 均为生产代码。
"""
from __future__ import annotations

import io
import json

from agent_events import PlainFrontend
from event_protocol import validate_event
from memory_runtime import MemoryService
from model_metrics import estimate_messages_tokens
from terminal_tui import RichTerminalFrontend
from test_journal_agent_integration import FIRST_PLANS, Harness

STATUS_KEYS = {
    "input_tokens", "reserve_tokens", "total_tokens", "context_limit", "heuristic_input_tokens",
    "estimate_source", "components", "total_turns", "visible_turns", "summary_cutoff",
    "image_count", "memory_count", "active_skill", "risk_index",
}


def _run(tmp_path, monkeypatch):
    memory = MemoryService(tmp_path / "memory")
    approved = memory.propose({"title": "钉钉周报流程", "content": "已批准正文：发送前先确认收件人", "app": "钉钉"})
    memory.mutate("approve", approved["memory_id"], 1)
    memory.propose({"title": "钉钉周报候选", "content": "候选正文：不应注入模型"})
    harness = Harness(tmp_path, monkeypatch)
    result, events = harness.run("帮我在钉钉发周报", list(FIRST_PLANS), "--memory-dir", str(tmp_path / "memory"))
    assert result == 0
    return harness, events


def test_run_agent_publishes_context_status_after_planning_and_persists_it(tmp_path, monkeypatch):
    harness, events = _run(tmp_path, monkeypatch)
    wires = [event.to_wire() for event in events]
    for wire in wires:
        validate_event(wire)
    statuses = [(i, w) for i, w in enumerate(wires) if w["kind"] == "context_status"]
    assert [w["step"] for _, w in statuses] == [0, 1, 2]
    for index, wire in statuses:
        planning = max(i for i, w in enumerate(wires[:index])
                       if w["kind"] == "phase" and w.get("phase") == "planning")
        assert wires[planning]["step"] == wire["step"] and wires[planning]["turn_id"] == wire["turn_id"]
        started = [i for i, w in enumerate(wires[:index]) if w["kind"] == "turn_started"]
        assert started and wires[started[-1]]["turn_id"] == wire["turn_id"]
        # 规划请求前已发布；同一步内没有更早的 action_status/answer。
        assert not any(w["kind"] in {"action", "action_status"} and w.get("step") == wire["step"]
                       for w in wires[planning:index])
        assert wire["origin"] == "harness" and wire["schema_version"] == 1
        assert STATUS_KEYS <= set(wire)

    run_dir = harness.run_dir()
    trajectory = json.loads((run_dir / "traj.json").read_text())
    for step, (_, wire) in enumerate(statuses):
        payload = {key: wire[key] for key in STATUS_KEYS}
        assert trajectory["steps"][step]["metrics"]["context_status"] == payload
        disk = json.loads((run_dir / "steps" / f"step_{step}.json").read_text())["messages"]
        # 由已提交的落盘请求重算；无 usage 锚点时 input 即启发式，四项分项精确加和。
        assert payload["heuristic_input_tokens"] == estimate_messages_tokens(disk)
        assert payload["estimate_source"] == "heuristic"
        assert payload["input_tokens"] == payload["heuristic_input_tokens"] == sum(payload["components"].values())
        assert payload["total_tokens"] == payload["input_tokens"] + payload["reserve_tokens"]
        assert payload["context_limit"] == 200000
        assert payload["total_turns"] == payload["visible_turns"] == step
        assert payload["summary_cutoff"] == 0 and payload["image_count"] == 0
        assert payload["memory_count"] == 1, "只计本次实际注入的已批准条目，不计候选或累计 shown"
        assert payload["active_skill"] is None

    # 只有 outcome_unknown 的 act-fail 进入风险索引；之后的规划请求独立带上该数据区。
    assert [w["risk_index"]["entries"] for _, w in statuses] == [0, 0, 1]
    assert statuses[-1][1]["risk_index"]["states"] == {"outcome_unknown": 1}
    inputs = [json.dumps(messages, ensure_ascii=False) for messages in harness.model_inputs]
    assert ["未核实操作索引" in text for text in inputs] == [False, False, True]
    risk = [m["content"] for m in harness.model_inputs[2]
            if isinstance(m["content"], str) and m["content"].startswith("# 未核实操作索引")]
    assert len(risk) == 1
    assert '"action_id":"act-fail"' in risk[0] and '"action_id":"act-ok"' not in risk[0]
    # 状态事件只给 UI/轨迹，不进入模型消息正文。
    for text in inputs:
        assert "context_status" not in text and "heuristic_input_tokens" not in text
        assert "候选正文" not in text


def test_resume_from_baseline_shaped_trajectory_rebuilds_risk_index_without_replay(tmp_path, monkeypatch):
    harness, _events = _run(tmp_path, monkeypatch)
    run_dir = harness.run_dir()
    # 模拟基线轨迹：没有 context_status / risk_index 元数据，索引只能从 journal 与步骤状态重建。
    trajectory = json.loads((run_dir / "traj.json").read_text())
    for record in trajectory["steps"]:
        record.get("metrics", {}).pop("context_status", None)
    (run_dir / "traj.json").write_text(json.dumps(trajectory, ensure_ascii=False))
    journal_bytes = (run_dir / "actions.jsonl").read_bytes()
    before = len(harness.model_inputs)
    result, events = harness.run("新的请求：先核实上次提交", ["terminate(status='success', answer='待核实')"],
                                 "--resume", str(run_dir), "--memory-dir", str(tmp_path / "memory"))
    assert result == 0
    status = [event.payload for event in events if event.kind == "context_status"]
    assert len(status) == 1 and status[0]["risk_index"]["states"] == {"receipt_unverified": 1, "outcome_unknown": 1}
    assert status[0]["total_turns"] == 3
    first = harness.model_inputs[before]
    risk = [m["content"] for m in first
            if isinstance(m["content"], str) and m["content"].startswith("# 未核实操作索引")]
    assert len(risk) == 1 and '"action_id":"act-fail"' in risk[0] and '"recovered":true' in risk[0]
    # 只提醒不重放：journal 未新增记录，旧代码没有再执行。
    assert (run_dir / "actions.jsonl").read_bytes() == journal_bytes
    assert [run["code"] for run in harness.executors[-1].fixture_runs] == [
        "terminate(status='success', answer='待核实')"]


def test_tui_status_and_plain_frontend_consume_real_run_events(tmp_path, monkeypatch):
    _harness, events = _run(tmp_path, monkeypatch)
    last = [event for event in events if event.kind == "context_status"][-1].payload

    ui = RichTerminalFrontend()
    assert "no planning request yet" in ui._context_status_text()
    for event in events:
        ui.emit(event)
    assert ui.context_status == {key: last[key] for key in STATUS_KEYS}
    before_queue = ui.queued_messages()
    ui._input.text = "/status"
    ui._submit_enter()
    transcript = ui._transcript
    status = transcript[transcript.rindex("Context: "):]
    total = last["total_tokens"]
    assert f"Context: {total:,} / 200,000 tokens ({total / 200000:.1%}, local estimate)" in status
    assert f"heuristic {last['heuristic_input_tokens']:,}: text {last['components']['text']:,}" in status
    assert "turns 2/2 kept · no summary · images 0 · memory 1 · skill: -" in status
    assert "unverified actions 1" in status
    # 本地命令：不排队、不当作任务发送。
    assert ui.queued_messages() == before_queue and "/status" not in "".join(before_queue)

    stdout, stderr = io.StringIO(), io.StringIO()

    async def never(_prompt):
        raise AssertionError("不应读取输入")

    plain = PlainFrontend(never, interactive=False, stdout=stdout, stderr=stderr)
    for event in events:
        plain.emit(event)
    assert "context_status" not in stdout.getvalue() + stderr.getvalue()
    assert "heuristic" not in stdout.getvalue()


def test_manual_model_retry_refreshes_forgotten_memory_before_request(tmp_path, monkeypatch):
    import asyncio
    import copy
    import mac_agent
    from model_stream import AgentError, CodeAction
    from test_memory_agent import Frontend, install_fakes, seed
    service, approved_id, _ = seed(tmp_path)
    model_inputs = []
    install_fakes(monkeypatch, model_inputs)
    async def plan(client, messages, **kwargs):
        model_inputs.append(copy.deepcopy(messages))
        if len(model_inputs) == 1:
            raise AgentError("离线模型失败")
        action = CodeAction(intent="reply", code="terminate(status='success', answer='ok')")
        return action, action.as_response(), "", {}
    monkeypatch.setattr(mac_agent, "plan_code", plan)
    def forget_and_retry(frontend):
        assert service.mutate("forget", approved_id, 2)[0]["ok"]
        return "r"
    frontend = Frontend([forget_and_retry])
    args = mac_agent.build_parser().parse_args(["帮我写钉钉周报", "--allow-empty-api-key", "--countdown", "0",
        "--startup-observation", "none", "--runs-dir", str(tmp_path / "runs"), "--memory-dir", str(tmp_path / "memory")])
    assert asyncio.run(mac_agent.run_agent(args, frontend=frontend)) == 0
    assert len(model_inputs) == 2
    assert "已批准正文" in json.dumps(model_inputs[0], ensure_ascii=False)
    assert "已批准正文" not in json.dumps(model_inputs[1], ensure_ascii=False)
    assert approved_id in json.dumps(model_inputs[1], ensure_ascii=False)
    assert "已失效" in json.dumps(model_inputs[1], ensure_ascii=False)


def test_tui_status_formats_calibrated_summary_and_omissions_without_extra_fields():
    from agent_events import AgentEvent

    ui = RichTerminalFrontend()
    ui.emit(AgentEvent("context_status", payload={
        "input_tokens": 150_000, "reserve_tokens": 4096, "total_tokens": 154_096, "context_limit": 200_000,
        "heuristic_input_tokens": 120_000, "estimate_source": "usage_delta",
        "components": {"text": 100_000, "images": 16_000, "opaque_reasoning": 3_000, "overhead": 1_000},
        "total_turns": 40, "visible_turns": 12, "summary_cutoff": 28, "image_count": 10, "memory_count": 2,
        "active_skill": "pdf-form", "risk_index": {"entries": 64, "omitted": 7}, "messages": ["secret"],
    }))
    assert "messages" not in ui.context_status
    text = ui._context_status_text()
    assert "154,096 / 200,000 tokens (77.0%, calibrated from last usage) · input 150,000 + reserve 4,096" in text
    assert "heuristic 120,000: text 100,000 · images 16,000 · opaque reasoning 3,000 · overhead 1,000" in text
    assert ("turns 12/40 kept · summary covers first 28 · images 10 · memory 2 · skill: pdf-form"
            " · unverified actions 64 (7 omitted)") in text
    ui.emit(AgentEvent("context_status", payload={"total_tokens": "x", "context_limit": 0}))
    assert "? / 0 tokens (?, local estimate)" in ui._context_status_text()
