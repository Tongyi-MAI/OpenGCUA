"""动作私有 JSONL：持久确认先于派发；恢复永不自动重放。"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

from action_feedback import action_feedback


_STAGES = {
    "intent_recorded", "dispatching", "receipt_received", "outcome_unknown",
    "rejected", "cancelled_before_dispatch",
}
_CONTEXT = (
    "session_id", "task_id", "turn_id", "step_id", "step", "policy_revision", "cell_request_id",
)
_SECRET = re.compile(
    r"api.?key|cookie|token|secret|password|passwd|authorization|credential|private.?key|storage.?state",
    re.I,
)


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SECRET.search(key) else _redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class ActionJournalError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ActionJournal:
    def __init__(self, path: Path, session_id: str, on_event=None):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id 必须是非空字符串")
        self.path = Path(path)
        self.session_id = session_id
        self.on_event = on_event
        self._lock = threading.RLock()
        self._fd = None
        self._failed = False
        self._actions = {}
        self._restored_ids = set()
        self.record_sequence = 0
        self._chain = ""
        # 不截断任何既有文件，包括未完成尾行；损坏时选择停止并报告 CORRUPT。
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        created = not self.path.exists()
        try:
            self._fd = os.open(self.path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(self._fd).st_mode):
                raise ActionJournalError("CORRUPT")
            os.fchmod(self._fd, 0o600)
            self._load()
            self._restored_ids.update(self._actions)
            os.fsync(self._fd)
            if created:
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except BaseException:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise

    def _load(self):
        with os.fdopen(os.dup(self._fd), "rb") as stream:
            stream.seek(0)
            for line in stream:
                try:
                    if not line.endswith(b"\n"):
                        raise ValueError("未完成尾行")
                    record = json.loads(line)
                    checksum = record.pop("record_digest")
                    if (record["record_sequence"] != self.record_sequence + 1
                            or record["previous_digest"] != self._chain
                            or checksum != _digest(record)
                            or record.get("session_id") != self.session_id
                            or record.get("schema_version", 1) != 1
                            or record["journal_stage"] not in _STAGES
                            or not record["action_id"] or not record["sdk_session_id"]
                            or not re.fullmatch(r"[0-9a-f]{64}", record["request_digest"])):
                        raise ValueError("损坏记录")
                    previous = self._actions.get(record["action_id"])
                    if previous is None:
                        if record["journal_stage"] != "intent_recorded":
                            raise ValueError("缺少意图")
                    elif (not self._same(previous, record)
                          or not self._valid_stage(previous, record["journal_stage"])):
                        raise ValueError("无效状态转移")
                    record["record_digest"] = checksum
                    self._accept(record)
                except (ValueError, TypeError, KeyError, AttributeError):
                    self._failed = True
                    raise ActionJournalError("CORRUPT") from None

    @staticmethod
    def _same(left, right):
        return (left["sdk_session_id"] == right["sdk_session_id"]
                and left["request_digest"] == right["request_digest"]
                and left["operation"] == right["operation"])

    @staticmethod
    def _valid_stage(previous, stage):
        return stage in {
            "intent_recorded": {"dispatching", "rejected", "cancelled_before_dispatch"},
            "dispatching": {"receipt_received", "outcome_unknown", "rejected", "cancelled_before_dispatch"},
        }.get(previous["journal_stage"], set())

    def _accept(self, record):
        self._actions[record["action_id"]] = record
        self.record_sequence = record["record_sequence"]
        self._chain = record["record_digest"]

    @staticmethod
    def _error(code):
        return {"allowed": False, "error": {"code": code, "message": code}}

    @staticmethod
    def _status(record):
        receipt = record.get("receipt") or {}
        if not isinstance(receipt, dict):
            receipt = {}
        raw_result = record.get("raw_result") or {}
        if not isinstance(raw_result, dict):
            raw_result = {}
        facts = {**{key: raw_result[key] for key in ("execution", "effect") if key in raw_result}, **receipt}
        return {
            **{key: record.get(key) for key in _CONTEXT},
            "kind": "action_status", "stage": record["journal_stage"],
            "action_id": record["action_id"], "sdk_session_id": record["sdk_session_id"],
            "operation": record["operation"], "record_sequence": record["record_sequence"],
            "resource_id": receipt.get("resource_id", record.get("resource_id")),
            "observation_id": receipt.get("based_on", record.get("observation_id")),
            "execution": facts.get("execution", record["journal_stage"] if record["journal_stage"]
                                     in {"rejected", "cancelled_before_dispatch"} else "unknown"),
            "effect": facts.get("effect", "not_applied" if record["journal_stage"]
                                  in {"rejected", "cancelled_before_dispatch"} else "unknown"),
            "verification": "unverified",
            **({"rule_feedback": action_feedback(
                record["operation"], action_id=record["action_id"], sdk_session_id=record["sdk_session_id"],
                stage=record["journal_stage"], receipt=receipt, raw_result=raw_result,
                error=record.get("error"),
                not_dispatched=isinstance(record.get("error"), dict)
                               and record["error"].get("not_dispatched") is True,
            )} if record["journal_stage"] in {
                "receipt_received", "outcome_unknown", "rejected", "cancelled_before_dispatch",
            } else {}),
        }

    def transition(self, event: dict, context: dict) -> dict:
        with self._lock:
            if self._failed or self._fd is None:
                return self._error("JOURNAL_UNAVAILABLE")
            try:
                stage = event["journal_stage"]
                if (event.get("type") != "action_journal" or stage not in _STAGES
                        or not isinstance(event["action_id"], str) or not event["action_id"]
                        or not isinstance(event["sdk_session_id"], str) or not event["sdk_session_id"]
                        or not isinstance(event["operation"], str) or not event["operation"]
                        or context.get("session_id", self.session_id) != self.session_id):
                    return self._error("ACTION_REPLAY_BLOCKED")
                # 摘要只基于完整请求，不持久保存脱敏前的 literal。
                request_digest = _digest(event["request"])
                record = _redact({
                    "schema_version": 1, "timestamp": datetime.now(timezone.utc).isoformat(),
                    **{key: context.get(key) for key in _CONTEXT},
                    "session_id": self.session_id,
                    **{key: event[key] for key in (
                        "journal_stage", "action_id", "sdk_session_id", "operation", "request",
                    )},
                    **{key: event[key] for key in (
                        "raw_result", "receipt", "error", "resource_id", "observation_id",
                    ) if key in event},
                    "request_digest": request_digest, "verification": "unverified",
                })
                previous = self._actions.get(record["action_id"])
                if previous is not None:
                    if not self._same(previous, record):
                        return self._error("ACTION_REPLAY_BLOCKED")
                    if stage == "intent_recorded":
                        if previous["journal_stage"] == "receipt_received":
                            return {"allowed": True, "memory_replay": True,
                                    "record_sequence": previous["record_sequence"]}
                        return self._error("ACTION_REPLAY_BLOCKED")
                    if record["action_id"] in self._restored_ids or not self._valid_stage(previous, stage):
                        # 恢复时只准查看旧记录/同 SDK receipt 的内存幂等，不恢复派发管线。
                        return self._error("ACTION_REPLAY_BLOCKED")
                elif stage != "intent_recorded":
                    return self._error("ACTION_REPLAY_BLOCKED")
                if stage in {"rejected", "cancelled_before_dispatch"}:
                    receipt = event.get("receipt") or {}
                    error = event.get("error") or {}
                    known = (isinstance(receipt, dict) and receipt.get("effect") == "not_applied"
                             and receipt.get("execution") == stage)
                    before = (previous is not None and previous["journal_stage"] == "intent_recorded"
                              and isinstance(error, dict) and error.get("not_dispatched") is True
                              and bool(error.get("code")))
                    if not (known or before):
                        return self._error("ACTION_REPLAY_BLOCKED")
                record.update(record_sequence=self.record_sequence + 1, previous_digest=self._chain)
                record["record_digest"] = _digest(record)
                encoded = _canonical(record) + b"\n"
            except (TypeError, ValueError, KeyError, OverflowError):
                return self._error("ACTION_REPLAY_BLOCKED")
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(self._fd, view)
                    if written <= 0:
                        raise OSError("短写")
                    view = view[written:]
                os.fsync(self._fd)
            except Exception:
                # 写入是否完整都不能再派发；重开后仍须按磁盘记录保守恢复。
                self._failed = True
                return self._error("JOURNAL_UNAVAILABLE")
            self._accept(record)
            status = self._status(record)
            answer = {"allowed": True, "record_sequence": self.record_sequence, "status": status}
            if self.on_event is not None:
                try:
                    outcome = self.on_event(copy.deepcopy(status))
                    if inspect.isawaitable(outcome):
                        if inspect.iscoroutine(outcome):
                            outcome.close()
                        raise TypeError("on_event 必须是同步回调")
                except Exception:
                    # 发布失败不是动作失败，不能因此重试动作。
                    answer["publication_error"] = "ACTION_EVENT_PUBLICATION_FAILED"
            return answer

    def summary(self) -> list[dict]:
        """兼容轨迹只投影小索引，不在每次进度更新复制全部业务请求/大结果。"""
        keys = ("action_id", "sdk_session_id", "journal_stage", "verification", "record_sequence", "step", "operation")
        with self._lock:
            return [{key: record.get(key) for key in keys} for record in self._actions.values()]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(list(self._actions.values()))

    def recover(self) -> list[dict]:
        with self._lock:
            result = self.snapshot()
            for record in result:
                record["recovery_state"] = {
                    "intent_recorded": "not_dispatched", "dispatching": "outcome_unknown",
                    "outcome_unknown": "outcome_unknown", "receipt_received": "receipt_unverified",
                    "rejected": "rejected", "cancelled_before_dispatch": "cancelled_before_dispatch",
                }[record["journal_stage"]]
                receipt = record.get("receipt") or {}
                if (isinstance(receipt, dict) and receipt.get("effect") == "not_applied"
                        and receipt.get("execution") in {"rejected", "cancelled_before_dispatch"}):
                    record["recovery_state"] = receipt["execution"]
            return result

    def close(self):
        with self._lock:
            if self._fd is not None:
                fd, self._fd = self._fd, None
                os.close(fd)
