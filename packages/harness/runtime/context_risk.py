"""有界只读风险索引：只收集宿主可信的结构化动作/代码块事实，跨摘要 cutoff 提醒未核实操作。

条目只来自 action journal 的 durable status（带整数 record_sequence）、journal 恢复快照以及
CodeExecutor 的控制字段；绝不解析 stdout、模型自述或摘要正文。索引不把任何条目提升为
verified/failure，不改变任务状态，也不触发重试。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

__all__ = ["ActionRiskIndex", "MAX_ENTRIES", "MAX_CHARS"]

MAX_ENTRIES = 64
MAX_CHARS = 3000
_MAX_IDENT = 200
_MAX_STAGES = 6
# 只有这些停止原因让当前块的副作用处于未知；ask_user/terminate 是块边界，普通异常是已知失败。
_INTERRUPTED_STOPS = frozenset({
    "timeout", "session_fault", "session_reset", "journal_unavailable", "memory_channel_unavailable",
})
_PROGRESS = ("total_blocks", "started_blocks", "completed_blocks", "stopped_block", "stop_reason")
_HEADER = (
    "# 未核实操作索引（宿主结构化记录，只读数据）\n"
    "条目仅来自 action journal 状态与代码块控制字段，不是新指令、授权或执行证明；"
    "不代表成功或失败，宿主不会自动重试。涉及这些 action_id/步骤的后续操作，"
    "先按原 id 与当前观察核实，禁止盲目重放。\n"
)


def _ident(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= _MAX_IDENT else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


class ActionRiskIndex:
    def __init__(self) -> None:
        self._entries: OrderedDict[tuple, dict] = OrderedDict()
        self.evicted = 0

    def __len__(self) -> int:
        return len(self._entries)

    def _put(self, key: tuple, entry: dict) -> None:
        self._entries.pop(key, None)
        self._entries[key] = entry
        while len(self._entries) > MAX_ENTRIES:
            self._entries.popitem(last=False)
            self.evicted += 1

    def observe_status(self, event: Any, *, recovered: bool = False) -> bool:
        """只接受 journal durable status；返回是否被当作可信事实处理。"""
        if (not isinstance(event, dict) or event.get("kind") != "action_status"
                or type(event.get("record_sequence")) is not int or event["record_sequence"] < 1):
            return False
        action_id, session = _ident(event.get("action_id")), _ident(event.get("sdk_session_id"))
        if action_id is None or session is None:
            return False
        key = ("action", session, action_id)
        stage = event.get("stage")
        feedback = event.get("rule_feedback") if isinstance(event.get("rule_feedback"), dict) else {}
        previous = self._entries.get(key)
        if previous is not None and event["record_sequence"] <= previous["record_sequence"]:
            return False
        if stage in {"rejected", "cancelled_before_dispatch"} or feedback.get("evaluation") == "not_dispatched":
            # 派发前拒绝/取消是已知未应用，不能误归为未知；只撤销本 id 的在途记录。
            if previous is not None and previous["state"] == "dispatching":
                self._entries.pop(key)
            return True
        if stage == "intent_recorded":
            return True
        execution, effect = event.get("execution"), event.get("effect")
        if stage == "dispatching":
            state = "outcome_unknown" if recovered else "dispatching"
        elif stage == "outcome_unknown" or execution == "unknown":
            state = "outcome_unknown"
        elif stage == "receipt_received":
            if effect in {"partial", "suspected_noop"}:
                state = effect
            elif recovered and not (execution == "rejected" and effect == "not_applied"):
                state = "receipt_unverified"
            elif previous is not None and previous["state"] != "dispatching":
                # 未知后同 id 的 SDK 回执只说明有技术回执，仍未核实业务结果。
                state = "receipt_unverified"
            else:
                if previous is not None:
                    self._entries.pop(key)
                return True
        else:
            return False
        stages = [*(previous or {}).get("stages", ()), stage][-_MAX_STAGES:]
        entry = {
            "kind": "action", "state": state, "sdk_session_id": session, "action_id": action_id,
            "stage": stage, "stages": stages, "record_sequence": event["record_sequence"],
            "verification": "unverified",
        }
        for field in ("operation", "execution", "effect"):
            if (value := _ident(event.get(field))) is not None:
                entry[field] = value
        if (step := _count(event.get("step"))) is not None:
            entry["step"] = step
        elif previous is not None and "step" in previous:
            entry["step"] = previous["step"]
        if recovered:
            entry["recovered"] = True
        self._put(key, entry)
        return True

    def seed_recovered(self, records: Any) -> None:
        """resume 时用 journal 恢复快照重建；dispatching 已无存活 worker，按结果未知处理。"""
        for record in records if isinstance(records, list) else ():
            if not isinstance(record, dict):
                continue
            facts = {}
            for source in (record.get("raw_result"), record.get("receipt")):
                if isinstance(source, dict):
                    facts.update({key: source[key] for key in ("execution", "effect") if key in source})
            error = record.get("error")
            self.observe_status({
                "kind": "action_status", "stage": record.get("journal_stage"),
                "record_sequence": record.get("record_sequence"),
                "action_id": record.get("action_id"), "sdk_session_id": record.get("sdk_session_id"),
                "operation": record.get("operation"), "step": record.get("step"),
                "execution": facts.get("execution"), "effect": facts.get("effect"),
                **({"rule_feedback": {"evaluation": "not_dispatched"}}
                   if isinstance(error, dict) and error.get("not_dispatched") is True else {}),
            }, recovered=True)

    def observe_execution(self, step: Any, control: Any, *, timed_out: bool = False,
                          session_reset: bool = False) -> bool:
        """按 CodeExecutor 控制字段记录被中断的步骤；未开始块数由已开始/总块数直接得出。"""
        if _count(step) is None or not isinstance(control, dict):
            return False
        reason = control.get("stop_reason")
        if not (timed_out is True or session_reset is True or reason in _INTERRUPTED_STOPS):
            return False
        entry = {"kind": "step", "state": "block_interrupted", "step": step, "verification": "unverified",
                 "timed_out": timed_out is True, "session_reset": session_reset is True}
        for field in _PROGRESS:
            value = control.get(field)
            if _count(value) is not None or (field == "stop_reason" and _ident(value) is not None):
                entry[field] = value
        total, started = entry.get("total_blocks"), entry.get("started_blocks")
        if total is not None and started is not None and total >= started:
            entry["not_started_blocks"] = total - started
        self._put(("step", step), entry)
        return True

    def observe_step_unknown(self, step: Any) -> bool:
        """动作已提交但宿主没有拿到任何执行结果（中止或旧轨迹 execution_state=unknown）。"""
        if _count(step) is None:
            return False
        self._put(("step", step), {"kind": "step", "state": "execution_unknown", "step": step,
                                   "verification": "unverified"})
        return True

    def seed_steps(self, steps: Any) -> None:
        """从轨迹 turn.actual_result.status 恢复；只读结构字段，缺字段的旧轨迹直接跳过。"""
        for record in steps if isinstance(steps, list) else ():
            if not isinstance(record, dict) or _count(record.get("step")) is None:
                continue
            turn = record.get("turn") if isinstance(record.get("turn"), dict) else {}
            result = turn.get("actual_result") if isinstance(turn.get("actual_result"), dict) else {}
            status = result.get("status") if isinstance(result.get("status"), dict) else {}
            if status.get("execution_state") == "unknown":
                self.observe_step_unknown(record["step"])
            elif status:
                self.observe_execution(record["step"], status, timed_out=status.get("timed_out") is True,
                                       session_reset=status.get("session_reset") is True)

    def _selection(self) -> tuple[list[str], int]:
        lines: list[str] = []
        used = len(_HEADER)
        for entry in reversed(self._entries.values()):
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            # 预留省略计数行，保证整个数据区不超过上限。
            if len(lines) >= MAX_ENTRIES or used + len(line) + 1 + 48 > MAX_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        lines.reverse()
        return lines, len(self._entries) - len(lines) + self.evicted

    def render(self) -> str:
        if not self._entries and not self.evicted:
            return ""
        lines, omitted = self._selection()
        footer = f"[省略 {omitted} 条较早记录；原始状态见 actions.jsonl]" if omitted else ""
        return _HEADER + "\n".join([*lines, *([footer] if footer else [])])

    def metadata(self) -> dict:
        lines, omitted = self._selection() if self._entries else ([], self.evicted)
        states: dict[str, int] = {}
        for entry in self._entries.values():
            states[entry["state"]] = states.get(entry["state"], 0) + 1
        return {"entries": len(self._entries), "shown": len(lines), "omitted": omitted,
                "chars": len(self.render()), "states": states}
