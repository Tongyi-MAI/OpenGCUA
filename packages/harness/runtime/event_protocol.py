"""所有前端共享的事件信封；传输游标与源序号不可混用。"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import threading
import uuid

SCHEMA_VERSION = 1
ENVELOPE_KEYS = frozenset({
    "schema_version", "event_id", "sequence", "session_id", "task_id", "turn_id", "step_id",
    "timestamp", "kind", "origin", "policy_revision", "stream_id",
})
ACTION_LABELS = {
    "intent_recorded": "动作意图已记录，尚未派发",
    "waiting_resource": "工具正在等待 SDK 资源",
    "dispatching": "正在进入 SDK 派发边界",
    "running": "工具正在执行",
    "receipt_received": "已收到技术回执，业务结果未验证",
    "outcome_unknown": "动作结果未知，请核实后继续",
    "rejected": "动作已拒绝，未应用",
    "cancelled_before_dispatch": "动作在派发前取消",
    "verified": "结果已验证",
    "verification_failed": "结果验证失败",
}


SUCCESS_STATUSES = frozenset({"success", "succeeded", "ok", "done", "completed"})
ABORTED_STATUSES = frozenset({"aborted", "cancelled", "canceled", "interrupted", "stopped"})
TERMINAL_STATUSES = SUCCESS_STATUSES | ABORTED_STATUSES | {"exited", "failed", "error"}
LEGACY_FINISHED_TEXT = TERMINAL_STATUSES | {"failure", "stalled", "max_steps", "timeout"}


def terminal_status(event: dict) -> str | None:
    """只读取显式终态字段；旧 finished 仅兼容状态词，不推断回答正文。"""
    kind = event.get("kind") or event.get("type")
    if kind in {"aborted", "interrupted"}:
        return kind
    for key in ("status", "state"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()[:64]
    text = event.get("text")
    if kind == "finished" and isinstance(text, str) and text.strip().lower() in LEGACY_FINISHED_TEXT:
        return text.strip().lower()
    return None


def terminal_state(status: str | None) -> str:
    """共同展示态，不代表动作的业务验证结论。"""
    if status in SUCCESS_STATUSES:
        return "completed"
    if status in ABORTED_STATUSES:
        return "aborted"
    return "attention"


def validate_event(event: dict) -> dict:
    if (not isinstance(event, dict) or type(event.get("schema_version")) is not int
            or event["schema_version"] != SCHEMA_VERSION):
        raise ValueError("不支持的事件版本")
    for key in ("event_id", "session_id", "kind", "origin", "timestamp"):
        if not isinstance(event.get(key), str) or not event[key]:
            raise ValueError(f"事件缺少 {key}")
    if type(event.get("sequence")) is not int or event["sequence"] < 1:
        raise ValueError("事件序号无效")
    if type(event.get("policy_revision")) is not int or event["policy_revision"] < 0:
        raise ValueError("策略版本无效")
    for key in ("task_id", "turn_id", "step_id"):
        if key not in event or not (event[key] is None or isinstance(event[key], str)):
            raise ValueError(f"事件归属 {key} 无效")
    if not isinstance(event.get("text"), str):
        raise ValueError("事件正文无效")
    if "step" not in event or not (event["step"] is None
                                  or type(event["step"]) is int and event["step"] >= 0):
        raise ValueError("事件步骤无效")
    if "stream_id" in event and (not isinstance(event["stream_id"], str) or not event["stream_id"]):
        raise ValueError("事件流身份无效")
    return event


class EventClock:
    def __init__(self, session_id=None, sequence=0):
        self.session_id = session_id or uuid.uuid4().hex
        self.stream_id = uuid.uuid4().hex
        self.sequence = sequence
        self._lock = threading.Lock()

    def make(self, kind, *, text="", step=None, task_id=None, turn_id=None,
             origin="harness", policy_revision=0, **fields):
        with self._lock:
            self.sequence += 1
            sequence = self.sequence
        fields = {k: v for k, v in fields.items() if k not in ENVELOPE_KEYS}
        event = {
            **fields, "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id, "stream_id": self.stream_id,
            "task_id": task_id, "turn_id": turn_id,
            "step_id": f"{self.session_id}/step/{step}" if step is not None else None,
            "event_id": f"{self.stream_id}:{sequence}", "sequence": sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(), "kind": kind,
            "origin": origin, "policy_revision": policy_revision,
            "text": str(text), "step": step,
        }
        return event


class EventProjection:
    """回放测试和只读诊断使用的有界投影，不决定业务成功或执行资格。"""
    def __init__(self):
        self.session_id = None
        self.turn_id = None
        self.phase = "idle"
        self.terminal = None
        self.actions = OrderedDict()
        self._sequence = OrderedDict()
        self._seen = OrderedDict()
        self._retired_sessions = OrderedDict()
        self._retired_turns = OrderedDict()
        self._retired_attempts = OrderedDict()
        self.attempt_id = None

    def consume(self, event):
        validate_event(event)
        identity = event["event_id"]
        stream = event.get("stream_id") or event["origin"]
        key = (event["session_id"], stream)
        if event["session_id"] in self._retired_sessions:
            return False
        if self.session_id is not None and event["session_id"] != self.session_id:
            if event["kind"] not in {"session", "session_info", "turn_started"}:
                return False
            self._retired_sessions[self.session_id] = True
            while len(self._retired_sessions) > 64:
                self._retired_sessions.popitem(last=False)
        if identity in self._seen or event["sequence"] <= self._sequence.get(key, 0):
            return False
        kind = event["kind"]
        same_session = self.session_id == event["session_id"]
        turn = event.get("turn_id")
        attempt = event.get("attempt_id")
        if (event["session_id"], turn) in self._retired_turns:
            return False
        if (same_session and self.turn_id is not None and turn not in (None, self.turn_id)
                and kind not in {"turn_started", "session", "session_info"}):
            return False
        same_turn = same_session and turn in (None, self.turn_id)
        attempt_key = (event["session_id"], turn or self.turn_id, attempt)
        if same_turn and attempt is not None:
            if attempt_key in self._retired_attempts:
                return False
            if self.attempt_id is not None and attempt != self.attempt_id:
                if kind != "model_status":
                    return False
                self._retired_attempts[(self.session_id, self.turn_id, self.attempt_id)] = True
        if not same_turn:
            if same_session and self.turn_id is not None:
                self._retired_turns[(self.session_id, self.turn_id)] = True
            self.attempt_id = None
        if attempt is not None:
            self.attempt_id = attempt
        while len(self._retired_turns) > 64:
            self._retired_turns.popitem(last=False)
        while len(self._retired_attempts) > 64:
            self._retired_attempts.popitem(last=False)
        self._sequence[key] = event["sequence"]
        while len(self._sequence) > 128:
            self._sequence.popitem(last=False)
        self._seen[identity] = True
        while len(self._seen) > 256:
            self._seen.popitem(last=False)
        self.session_id = event["session_id"]
        kind = event["kind"]
        if kind == "turn_started" or not same_session:
            self.turn_id = turn
            self.terminal = None
        elif kind in {"session", "session_info"} and turn is not None:
            self.turn_id = turn
        if kind == "action_status":
            aid = event.get("action_id")
            if aid:
                self.actions.pop(aid, None)
                self.actions[aid] = dict(event)
                while len(self.actions) > 64:
                    self.actions.popitem(last=False)
            if self.terminal is None:
                self.phase = event.get("stage", "unknown")
        elif kind in {"phase", "progress"} and self.terminal is None:
            self.phase = event.get("phase", event.get("text", "working"))
        elif kind == "model_status" and self.terminal is None:
            self.phase = event.get("stage", "waiting")
        elif kind in {"approval", "question"} and self.terminal is None:
            self.phase = "waiting_authorization" if kind == "approval" else "waiting_user"
        elif kind in {"answer", "turn_finished", "finished", "aborted", "interrupted"}:
            status = terminal_status(event)
            if status is None:
                status = (self.terminal or {}).get("terminal_status")
            self.terminal = {**event, "terminal_status": status, "terminal_state": terminal_state(status)}
            self.phase = terminal_state(status)
        return True
