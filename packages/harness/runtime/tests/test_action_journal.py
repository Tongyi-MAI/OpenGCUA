from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from action_journal import ActionJournal, ActionJournalError


CONTEXT = {"session_id": "host", "task_id": "task", "turn_id": "turn", "step_id": "step",
           "step": 2, "policy_revision": 7, "cell_request_id": "cell"}


def event(stage="intent_recorded", action_id="a", sdk_session_id="sdk", request=None, **extra):
    return {"type": "action_journal", "journal_stage": stage, "action_id": action_id,
            "sdk_session_id": sdk_session_id, "operation": "action.click",
            "request": {"action_id": action_id, "text": "私有正文"} if request is None else request,
            **extra}


@pytest.mark.parametrize("stage,expected", [
    ("intent_recorded", "not_dispatched"), ("dispatching", "outcome_unknown"),
    ("receipt_received", "receipt_unverified"),
])
def test_three_crash_points_and_continued_sequence(tmp_path, stage, expected):
    path = tmp_path / "actions.jsonl"
    journal = ActionJournal(path, "host")
    assert journal.transition(event(), CONTEXT)["allowed"]
    if stage != "intent_recorded":
        assert journal.transition(event("dispatching"), CONTEXT)["allowed"]
    if stage == "receipt_received":
        assert journal.transition(event(stage, raw_result={"ok": True}, receipt={
            "execution": "completed", "effect": "applied", "resource_id": "r", "based_on": "o",
        }), CONTEXT)["allowed"]
    count = journal.record_sequence
    journal.close()
    reopened = ActionJournal(path, "host")
    assert reopened.recover()[0]["recovery_state"] == expected
    assert reopened.recover()[0]["verification"] == "unverified"
    if stage != "receipt_received":
        assert not reopened.transition(event("dispatching"), CONTEXT)["allowed"]
        assert not reopened.transition(event(), CONTEXT)["allowed"]
    assert reopened.transition(event(action_id="b"), CONTEXT)["record_sequence"] == count + 1
    assert path.stat().st_mode & 0o777 == 0o600
    reopened.close()
    assert path.exists()


def test_duplicate_digest_scope_and_memory_replay(tmp_path):
    journal = ActionJournal(tmp_path / "j", "host")
    journal.transition(event(), CONTEXT)
    assert not journal.transition(event(), CONTEXT)["allowed"]
    journal.transition(event("dispatching"), CONTEXT)
    assert not journal.transition(event(), CONTEXT)["allowed"]
    journal.transition(event("receipt_received", receipt={"execution": "completed", "effect": "applied"}), CONTEXT)
    assert journal.transition(event(), CONTEXT)["memory_replay"]
    assert journal.record_sequence == 3
    for changed in (event(sdk_session_id="sdk2"), event(request={"text": "不同"}),
                    {**event(), "operation": "action.other"}):
        assert journal.transition(changed, CONTEXT)["error"]["code"] == "ACTION_REPLAY_BLOCKED"
    path = journal.path
    journal.close()
    stored = path.read_bytes()
    with pytest.raises(ActionJournalError, match="CORRUPT"):
        ActionJournal(path, "host2")
    assert path.read_bytes() == stored, "不同 host session 不得接管或覆盖原日志"
    reopened = ActionJournal(path, "host")
    assert not reopened.transition(event(sdk_session_id="new"), CONTEXT)["allowed"]
    assert not reopened.transition(event(action_id="new"), {"session_id": "host2"})["allowed"]
    reopened.close()


def test_secret_redaction_digest_uses_full_request_and_safe_publication(tmp_path):
    published = []
    journal = ActionJournal(tmp_path / "j", "host", published.append)
    request = {"text": "私有正文", "APIKey": "secret1", "headers": {"Authorization": "secret2"},
               "nested": [{"cookies": [{"value": "secret3"}], "access_token": "secret4"}]}
    journal.transition(event(request=request), CONTEXT)
    journal.transition(event("dispatching", request=request), CONTEXT)
    journal.transition(event("receipt_received", request=request,
                             raw_result={"token": "secret5"}, receipt={"execution": "completed", "effect": "applied"}), CONTEXT)
    raw = journal.path.read_text()
    assert all(f"secret{i}" not in raw for i in range(1, 6))
    assert journal.snapshot()[0]["request"]["text"] == "私有正文"
    assert not journal.transition(event(request={**request, "APIKey": "changed"}), CONTEXT)["allowed"]
    assert "私有正文" not in json.dumps(published, ensure_ascii=False)
    assert published[-1]["execution"] == "completed"
    assert published[-1]["effect"] == "applied"
    assert published[-1]["policy_revision"] == 7
    journal.close()


@pytest.mark.parametrize("failure", ["fsync", "write"])
def test_io_failure_poison_and_never_allow_dispatch(tmp_path, monkeypatch, failure):
    journal = ActionJournal(tmp_path / "j", "host")
    def fail(*args):
        raise OSError("不应泄漏的诊断")
    monkeypatch.setattr(os, failure, fail)
    answer = journal.transition(event(), CONTEXT)
    assert answer["error"]["code"] == "JOURNAL_UNAVAILABLE"
    assert "诊断" not in str(answer)
    assert not journal.transition(event("dispatching"), CONTEXT)["allowed"]
    assert not journal.transition(event(action_id="b"), CONTEXT)["allowed"]
    journal.close()


@pytest.mark.parametrize("damage", ["middle_missing", "middle_invalid", "partial_tail", "checksum"])
def test_corrupt_is_fail_closed_and_never_overwrites(tmp_path, damage):
    path = tmp_path / "j"
    journal = ActionJournal(path, "host")
    journal.transition(event(), CONTEXT)
    journal.transition(event("dispatching"), CONTEXT)
    journal.transition(event("receipt_received"), CONTEXT)
    journal.close()
    lines = path.read_bytes().splitlines(keepends=True)
    if damage == "middle_missing":
        del lines[1]
    elif damage == "middle_invalid":
        lines[1] = b"broken\n"
    elif damage == "checksum":
        lines[1] = lines[1].replace(b"dispatching", b"dispatched!")
    else:
        lines.append(b'{"record_sequence":4')
    corrupt = b"".join(lines)
    path.write_bytes(corrupt)
    with pytest.raises(ActionJournalError, match="CORRUPT"):
        ActionJournal(path, "host")
    assert path.read_bytes() == corrupt


@pytest.mark.parametrize("stage", ["rejected", "cancelled_before_dispatch"])
def test_known_not_applied_classification_and_generic_timeout_refused(tmp_path, stage):
    journal = ActionJournal(tmp_path / "j", "host")
    journal.transition(event(), CONTEXT)
    journal.transition(event("dispatching"), CONTEXT)
    assert not journal.transition(event(stage, error={"code": "TIMEOUT"}), CONTEXT)["allowed"]
    assert journal.transition(event(stage, error={"code": "EXACT_NATIVE_CODE"}, receipt={
        "execution": stage, "effect": "not_applied",
    }), CONTEXT)["allowed"]
    assert journal.recover()[0]["recovery_state"] == stage
    journal.close()


def test_receipt_event_preserves_known_native_rejection_and_short_writes(tmp_path, monkeypatch):
    journal = ActionJournal(tmp_path / "j", "host")
    write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: write(fd, data[:17]))
    assert journal.transition(event(), CONTEXT)["allowed"]
    assert journal.transition(event("dispatching"), CONTEXT)["allowed"]
    answer = journal.transition(event("receipt_received", receipt={
        "execution": "rejected", "effect": "not_applied", "error": {"code": "EXACT"},
    }), CONTEXT)
    assert answer["allowed"] and answer["status"]["effect"] == "not_applied"
    assert journal.recover()[0]["recovery_state"] == "rejected"
    journal.close()
    reopened = ActionJournal(tmp_path / "j", "host")
    assert reopened.recover()[0]["recovery_state"] == "rejected"
    reopened.close()


def test_atomic_thread_sequence_and_callback_failure_does_not_retry(tmp_path):
    def broken(_event):
        raise RuntimeError("发布失败")
    journal = ActionJournal(tmp_path / "j", "host", broken)
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda i: journal.transition(event(action_id=str(i)), CONTEXT), range(32)))
    assert all(answer["allowed"] and answer["publication_error"] for answer in answers)
    assert sorted(answer["record_sequence"] for answer in answers) == list(range(1, 33))
    snapshot = journal.snapshot()
    snapshot[0]["request"]["text"] = "不能改账本"
    assert journal.snapshot()[0]["request"]["text"] == "私有正文"
    summary = journal.summary()
    assert len(summary) == 32 and summary[0]["verification"] == "unverified"
    assert all("request" not in item and "raw_result" not in item for item in summary)
    summary[0]["action_id"] = "不能改账本"
    assert journal.summary()[0]["action_id"] != "不能改账本"
    journal.close()
    assert not journal.transition(event(action_id="after-close"), CONTEXT)["allowed"]


# Files.write(action_id="w1") 的真实 SDK wire：ID 在 command_id 字段，native status 以同值为键。
FILE_WRITE = {"op": "file", "operation": "write", "path": "/a", "data": "aGk=", "overwrite": False,
              "timeout_ms": 5000, "command_id": "w1"}


def file_event(stage="intent_recorded", request=FILE_WRITE, **extra):
    return {**event(stage, action_id="w1", request=request, **extra), "operation": "file.write"}


def test_command_id_wire_keeps_sdk_field_digest_and_native_receipt(tmp_path):
    from action_journal import _digest
    journal = ActionJournal(tmp_path / "j", "host")
    assert journal.transition(file_event(), CONTEXT)["allowed"]
    record = journal.snapshot()[0]
    assert record["action_id"] == "w1" and record["request"] == FILE_WRITE
    assert "action_id" not in record["request"] and record["request_digest"] == _digest(FILE_WRITE)
    # 同一 ID 但改用 action_id 字段是不同请求，不能当作同一动作。
    renamed = {**{k: v for k, v in FILE_WRITE.items() if k != "command_id"}, "action_id": "w1"}
    for changed in (file_event(request=renamed), file_event("dispatching", request=renamed),
                    file_event("dispatching", request={**FILE_WRITE, "data": "eA=="})):
        assert journal.transition(changed, CONTEXT)["error"]["code"] == "ACTION_REPLAY_BLOCKED"
    assert journal.transition(file_event("dispatching"), CONTEXT)["allowed"]
    # Session.action_status(command_id) 的真实回执：bridge 未报告 effect 时保持 unknown。
    receipt = {"action_id": "w1", "attachment_id": "1-1", "duration_ms": 0, "effect": "unknown",
               "execution": "completed", "provider": "fake.bridge", "resource_id": "fake",
               "sequence": 1, "session_id": "sdk"}
    answer = journal.transition(file_event("receipt_received", raw_result={"ok": True}, receipt=receipt), CONTEXT)
    assert answer["status"]["execution"] == "completed" and answer["status"]["effect"] == "unknown"
    assert answer["status"]["operation"] == "file.write"
    assert journal.transition(file_event(), CONTEXT)["memory_replay"]
    journal.close()
    reopened = ActionJournal(tmp_path / "j", "host")
    assert reopened.recover()[0]["recovery_state"] == "receipt_unverified"
    # 重启后新 SDK session 没有该 command_id 的 native 去重状态，只能阻断。
    blocked = reopened.transition({**file_event(), "sdk_session_id": "new"}, CONTEXT)
    assert blocked["error"]["code"] == "ACTION_REPLAY_BLOCKED" and reopened.record_sequence == 3
    reopened.close()
