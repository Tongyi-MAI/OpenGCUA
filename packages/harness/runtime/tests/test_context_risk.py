"""持久风险索引：只采信 journal/执行控制的结构化事实，跨严格摘要 cutoff 保留且不改变验证状态。"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from action_journal import ActionJournal
from context_risk import MAX_CHARS, MAX_ENTRIES, ActionRiskIndex
from llm_compaction import CompactionConfig, CompactionState, maybe_compact, records_digest, restore_state


SUMMARY = "<summary><constraints>c</constraints><progress>p</progress><findings>f</findings><state>s</state></summary>"


def status(stage, action_id="act-1", *, session="sdk-1", seq=1, **extra):
    return {"kind": "action_status", "stage": stage, "action_id": action_id, "sdk_session_id": session,
            "record_sequence": seq, "operation": "click", "verification": "unverified", **extra}


def entries(index):
    return {key: dict(value) for key, value in index._entries.items()}


def journal_event(stage, action_id, **extra):
    return {"type": "action_journal", "journal_stage": stage, "action_id": action_id,
            "sdk_session_id": "sdk-1", "operation": "type_text", "request": {"text": "x"}, **extra}


def test_real_journal_statuses_drive_index_and_never_promote_verification(tmp_path):
    index = ActionRiskIndex()
    journal = ActionJournal(tmp_path / "actions.jsonl", "sess", on_event=index.observe_status)
    context = {"session_id": "sess", "step": 3}
    try:
        for stage in ("intent_recorded", "dispatching"):
            assert journal.transition(journal_event(stage, "ok"), context)["allowed"]
        assert entries(index)[("action", "sdk-1", "ok")]["state"] == "dispatching"
        assert journal.transition(journal_event("receipt_received", "ok",
                                                receipt={"execution": "completed", "effect": "applied"}),
                                  context)["allowed"]
        # 正常技术回执即移除在途条目；没有结构化未知事实就不制造提醒。
        assert ("action", "sdk-1", "ok") not in entries(index)

        for stage in ("intent_recorded", "dispatching"):
            journal.transition(journal_event(stage, "lost"), context)
        journal.transition(journal_event("outcome_unknown", "lost", error={"code": "TIMEOUT"}), context)
        lost = entries(index)[("action", "sdk-1", "lost")]
        assert lost["state"] == "outcome_unknown" and lost["stages"] == ["dispatching", "outcome_unknown"]
        assert lost["verification"] == "unverified" and lost["step"] == 3 and lost["record_sequence"] == 6

        # 宿主门禁拒绝（not_dispatched）是已知未应用，不能误归为未知。
        journal.transition(journal_event("intent_recorded", "gated"), context)
        journal.transition(journal_event("rejected", "gated", error={"code": "FOREGROUND_DENIED",
                                                                     "not_dispatched": True}), context)
        assert ("action", "sdk-1", "gated") not in entries(index)
        for stage in ("intent_recorded", "dispatching"):
            journal.transition(journal_event(stage, "denied"), context)
        journal.transition(journal_event("rejected", "denied", receipt={"execution": "rejected",
                                                                        "effect": "not_applied"}), context)
        assert ("action", "sdk-1", "denied") not in entries(index)
        assert set(entries(index)) == {("action", "sdk-1", "lost")}
    finally:
        journal.close()

    # 恢复：journal 快照里残留的 dispatching 已无存活 worker，只能是未知。
    reopened = ActionJournal(tmp_path / "actions.jsonl", "sess")
    try:
        records = reopened.recover()
    finally:
        reopened.close()
    restored = ActionRiskIndex()
    restored.seed_recovered(records)
    assert {key: value["state"] for key, value in entries(restored).items()} == {
        ("action", "sdk-1", "lost"): "outcome_unknown",
        ("action", "sdk-1", "ok"): "receipt_unverified"}
    assert entries(restored)[("action", "sdk-1", "lost")]["recovered"] is True
    assert all(value["verification"] == "unverified" for value in entries(restored).values())


def test_recovered_dispatching_without_receipt_is_unknown(tmp_path):
    journal = ActionJournal(tmp_path / "actions.jsonl", "sess")
    try:
        for stage in ("intent_recorded", "dispatching"):
            journal.transition(journal_event(stage, "inflight"), {"session_id": "sess", "step": 1})
    finally:
        journal.close()
    reopened = ActionJournal(tmp_path / "actions.jsonl", "sess")
    try:
        index = ActionRiskIndex()
        index.seed_recovered(reopened.recover())
    finally:
        reopened.close()
    entry = entries(index)[("action", "sdk-1", "inflight")]
    assert entry["state"] == "outcome_unknown" and entry["stage"] == "dispatching" and entry["recovered"]


def test_later_receipt_for_same_id_stays_unverified_and_partial_is_kept():
    index = ActionRiskIndex()
    index.observe_status(status("dispatching", seq=2, step=4))
    index.observe_status(status("outcome_unknown", seq=3, execution="unknown", effect="unknown"))
    index.observe_status(status("receipt_received", seq=9, execution="completed", effect="applied"))
    entry = entries(index)[("action", "sdk-1", "act-1")]
    assert entry["state"] == "receipt_unverified" and entry["verification"] == "unverified"
    assert entry["stages"] == ["dispatching", "outcome_unknown", "receipt_received"]
    assert entry["step"] == 4 and entry["record_sequence"] == 9

    index.observe_status(status("dispatching", "act-2", seq=10))
    index.observe_status(status("receipt_received", "act-2", seq=11, execution="completed", effect="partial"))
    assert entries(index)[("action", "sdk-1", "act-2")]["state"] == "partial"
    # receipt 缺 execution 时 journal 投影为 unknown，保守保留。
    index.observe_status(status("dispatching", "act-3", seq=12))
    index.observe_status(status("receipt_received", "act-3", seq=13, execution="unknown"))
    assert entries(index)[("action", "sdk-1", "act-3")]["state"] == "outcome_unknown"
    assert "verified" not in {value["verification"] for value in entries(index).values()} - {"unverified"}


def test_suspected_noop_and_late_delta_do_not_disappear_from_context():
    index = ActionRiskIndex()
    index.observe_status(status("dispatching", seq=1))
    index.observe_status(status("receipt_received", seq=2, execution="completed", effect="suspected_noop"))
    assert entries(index)[("action", "sdk-1", "act-1")]["state"] == "suspected_noop"
    before = entries(index)
    assert not index.observe_status(status("dispatching", seq=1))
    assert entries(index) == before


def test_rejection_does_not_erase_a_prior_unknown_and_scope_is_per_sdk_session():
    index = ActionRiskIndex()
    index.observe_status(status("outcome_unknown", seq=1, execution="unknown"))
    index.observe_status(status("rejected", seq=2, execution="rejected", effect="not_applied"))
    assert entries(index)[("action", "sdk-1", "act-1")]["state"] == "outcome_unknown"
    index.observe_status(status("outcome_unknown", session="sdk-2", seq=3))
    assert set(entries(index)) == {("action", "sdk-1", "act-1"), ("action", "sdk-2", "act-1")}


@pytest.mark.parametrize("event", [
    {"kind": "action_status", "stage": "outcome_unknown", "action_id": "a", "sdk_session_id": "s"},
    status("outcome_unknown", seq=True),
    status("outcome_unknown", seq="7"),
    {**status("outcome_unknown"), "kind": "stdout"},
    status("outcome_unknown", action_id=""),
    status("outcome_unknown", action_id="x" * 201),
    status("outcome_unknown", session=None),
    status("verified"),
    "outcome_unknown act-1",
])
def test_untrusted_or_progress_only_events_are_ignored(event):
    index = ActionRiskIndex()
    assert index.observe_status(event) is False
    assert len(index) == 0 and index.render() == ""


def test_interrupted_blocks_only_from_execution_control_fields():
    index = ActionRiskIndex()
    control = {"total_blocks": 3, "started_blocks": 2, "completed_blocks": 1, "stopped_block": 2,
               "stop_reason": "timeout"}
    assert index.observe_execution(5, control, timed_out=True)
    entry = entries(index)[("step", 5)]
    assert entry == {"kind": "step", "state": "block_interrupted", "step": 5, "verification": "unverified",
                     "timed_out": True, "session_reset": False, **control, "not_started_blocks": 1}
    # 已知边界与普通失败不是未知副作用；stdout 自述不会被读取。
    for reason in ("terminate", "ask_user", "control_exit", "phase", "ask_user_disabled", None):
        assert not index.observe_execution(6, {"stop_reason": reason, "stdout": "可能已经提交"})
    assert index.observe_execution(7, {"stop_reason": "session_fault", "total_blocks": 1, "started_blocks": 1})
    assert index.observe_execution(8, {}, session_reset=True)
    assert index.observe_step_unknown(9)
    assert {key: value["state"] for key, value in entries(index).items()} == {
        ("step", 5): "block_interrupted", ("step", 7): "block_interrupted",
        ("step", 8): "block_interrupted", ("step", 9): "execution_unknown"}


def test_seed_steps_accepts_old_trajectories_with_missing_fields():
    steps = [
        {"step": 0},
        {"step": 1, "turn": None},
        {"step": 2, "turn": {"actual_result": {"status": "unknown"}}},
        {"step": 3, "turn": {"actual_result": {"status": {"returncode": 1, "timed_out": False}}}},
        {"step": 4, "turn": {"actual_result": {"status": {"execution_state": "unknown"}}}},
        {"step": 5, "turn": {"actual_result": {"status": {"returncode": -9, "timed_out": True,
                                                          "total_blocks": 2, "started_blocks": 1,
                                                          "stop_reason": "timeout"}}}},
        {"step": "6"}, "garbage",
    ]
    index = ActionRiskIndex()
    index.seed_steps(steps)
    assert {key: value["state"] for key, value in entries(index).items()} == {
        ("step", 4): "execution_unknown", ("step", 5): "block_interrupted"}
    assert entries(index)[("step", 5)]["not_started_blocks"] == 1
    index.seed_steps(None)
    index.seed_recovered(None)
    assert len(index) == 2


def test_entry_and_char_bounds_report_omissions():
    index = ActionRiskIndex()
    for number in range(100):
        index.observe_status(status("outcome_unknown", f"act-{number:03d}-" + "x" * 150, seq=number + 1,
                                    execution="unknown", effect="unknown"))
    rendered = index.render()
    meta = index.metadata()
    assert len(index) == MAX_ENTRIES and index.evicted == 100 - MAX_ENTRIES
    assert len(rendered) <= MAX_CHARS and meta["chars"] == len(rendered)
    assert meta["entries"] == MAX_ENTRIES and meta["shown"] < MAX_ENTRIES
    assert meta["omitted"] == 100 - meta["shown"]
    assert f"[省略 {meta['omitted']} 条较早记录" in rendered
    # 保留最新条目，按时间顺序输出。
    assert "act-099-" in rendered and "act-000-" not in rendered
    lines = [json.loads(line) for line in rendered.splitlines() if line.startswith("{")]
    assert len(lines) == meta["shown"]
    assert [line["record_sequence"] for line in lines] == sorted(line["record_sequence"] for line in lines)
    assert meta["states"] == {"outcome_unknown": MAX_ENTRIES}


def _manager(*, enabled=True):
    from mac_agent import ContextManager

    return ContextManager(
        task="提交表单", coordinate_range=1000, img_keep_n=0, img_interval=10,
        history_turns=1, micro_compaction=False, micro_trigger_steps=5,
        micro_keep_turns=1, micro_interval=5, todo_diff_snapshot=False,
        max_image_dimension=1200, jpeg_quality=80,
        compaction_config=CompactionConfig(enabled=enabled, trigger_tokens=200000, max_tokens=256, retries=0),
    )


OPAQUE = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "gAAAA-opaque", "summary": []}]
THINKING = [{"type": "thinking", "thinking": "t", "signature": "sig-raw"}]
CURRENT = dict(current_feedback="new observation", current_screenshot=None,
               current_user_response="最新答复：不要重复提交")


class SummaryClient:
    on_compaction_event = None
    last_attempt_id = "summary"

    def __init__(self):
        self.requests = []

    async def complete(self, messages, **_kwargs):
        self.requests.append(copy.deepcopy(messages))
        return SUMMARY, "", {}


def _filled_manager():
    from mac_agent import Turn

    manager = _manager()
    for step in range(6):
        manager.append(Turn(step, "", None, "act", f"await device.act('act-{step}')", f"output {step} " + "y " * 400,
                            "", task="提交表单", actual_result={"status": {"returncode": 0}, "feedback": "ok"},
                            response_output=copy.deepcopy(OPAQUE) if step == 5 else None,
                            anthropic_content=copy.deepcopy(THINKING) if step == 5 else None))
    manager.risk_index.observe_status(status("outcome_unknown", "act-0", seq=3, step=0, execution="unknown"))
    manager.memory_block = "# 长期参考（已批准）\n- 偏好 A"
    return manager


def test_strict_summary_keeps_old_unknown_latest_user_and_opaque_history():
    manager = _filled_manager()
    records_before = manager.compaction_records(0, 6)
    client = SummaryClient()
    result = asyncio.run(maybe_compact(manager, client, current=CURRENT, action_max_tokens=128, trigger="manual"))
    assert result["status"] == "completed", result
    cutoff = manager.compaction_state.cutoff
    assert cutoff >= 1 and result["coverage"]["source_turns"] == [0, cutoff]
    assert result["coverage"]["kept_turns"] == 6 - cutoff
    assert result["coverage"]["omitted"] == {"image_items": 0, "non_source_fields": 0}

    # 摘要输入只含原始轮记录：风险索引与记忆数据区都不进摘要，避免被复述成授权或事实。
    request = json.dumps(client.requests, ensure_ascii=False)
    assert "未核实操作索引" not in request and "长期参考" not in request
    # 索引不进入 turns，严格 digest 与旧轨迹恢复不受影响。
    assert manager.compaction_records(0, 6) == records_before
    state = restore_state(manager.compaction_state.to_dict(), records_before)
    assert state.source_digest == records_digest(records_before[:cutoff])

    messages = manager.build_messages(**CURRENT)
    text = json.dumps(messages, ensure_ascii=False)
    assert "output 0 " not in text, "前提：act-0 所在轮已被摘要覆盖"
    risk = [m for m in messages if isinstance(m["content"], str) and m["content"].startswith("# 未核实操作索引")]
    assert len(risk) == 1 and risk[0]["role"] == "user"
    assert '"action_id":"act-0"' in risk[0]["content"] and '"state":"outcome_unknown"' in risk[0]["content"]
    assert "verified" not in risk[0]["content"].replace("unverified", "")
    # 独立数据区位于摘要之后、当前观察之前；最新用户原文完整保留在最后。
    summary_at = next(i for i, m in enumerate(messages) if "# Previous summary" in str(m["content"]))
    assert summary_at < messages.index(risk[0])
    assert messages[-1] == {"role": "user", "content": "最新答复：不要重复提交"}
    # 保留轮的 provider 不透明回放原样传回。
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    assert assistant["response_output"] == OPAQUE and assistant["anthropic_content"] == THINKING
    # 遗忘记忆后下一次请求立即不再出现；风险索引独立存在。
    manager.memory_block = ""
    again = json.dumps(manager.build_messages(**CURRENT), ensure_ascii=False)
    assert "长期参考" not in again and "未核实操作索引" in again


def test_risk_block_absent_when_index_empty_and_history_start_matches_build():
    from mac_agent import Turn

    manager = _manager(enabled=False)
    for step in range(3):
        manager.append(Turn(step, "", None, "act", "print(1)", f"out {step}", "", task="提交表单"))
    assert manager.history_start() == 2
    text = json.dumps(manager.build_messages(**CURRENT), ensure_ascii=False)
    assert "未核实操作索引" not in text and "out 1" not in text and "out 2" in text
    manager.compaction_state = CompactionState(SUMMARY, 1, 1, records_digest(manager.compaction_records(0, 1)))
    assert manager.history_start() == 1


def test_context_risk_is_declared_as_py_module():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    modules = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"]["py-modules"]
    assert "context_risk" in modules and (root / "context_risk.py").is_file()
