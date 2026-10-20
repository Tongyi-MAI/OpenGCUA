from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from code_executor import CodeExecutor
import code_runtime


def test_grant_is_scoped_to_task_not_window():
    from gui_approval import GuiGrantStore
    grants = GuiGrantStore()
    assert grants.state(4) == "unset"
    grants.decide(4, allow=True)
    assert grants.state(4) == "allowed"
    assert grants.state(5) == "unset"
    grants.clear()
    assert grants.state(4) == "unset"


def test_one_allow_grants_all_windows_in_same_task():
    from gui_approval import request_gui_approval

    async def scenario():
        grants = GuiGrantStore()
        questions = []
        async def choose(prompt):
            questions.append(prompt)
            return "allow"

        first = {"app_id": "app:a", "window_id": "window:7", "needs_vision": True}
        first["request_id"] = "r1"
        assert await request_gui_approval(grants, 4, first, choose)
        assert await request_gui_approval(grants, 4, first, choose)
        assert len(questions) == 1
        assert questions[0].app_id == "app:a" and questions[0].window_id == "window:7"
        assert "键鼠" in questions[0].text
        assert await request_gui_approval(
            grants, 4, {**first, "window_id": "window:8"}, choose,
        )
        assert len(questions) == 1
        assert await request_gui_approval(grants, 5, first, choose)
        assert len(questions) == 2

    from gui_approval import GuiGrantStore
    asyncio.run(scenario())


def test_reject_sticks_until_explicit_retry_and_cancel_does_not_change_state():
    from gui_approval import GuiGrantStore, request_gui_approval

    async def scenario():
        grants = GuiGrantStore()
        calls = []
        async def deny(prompt):
            calls.append(prompt)
            return "reject"
        assert not await request_gui_approval(
            grants, 1, {"request_id": "r1", "app_id": "app:a", "window_id": "window:7"}, deny,
        )
        assert grants.state(1) == "denied"
        assert not await request_gui_approval(
            grants, 1, {"request_id": "r2", "app_id": "app:a", "window_id": "window:8"}, deny,
        )
        assert len(calls) == 1
        async def cancel(prompt):
            calls.append(prompt)
            return "cancel"
        assert not await request_gui_approval(grants, 1, {
            "request_id": "r3", "app_id": "app:a", "window_id": "window:7",
            "explicit_retry": True, "reason": "Need to click PDF field",
        }, cancel)
        assert grants.state(1) == "denied"
        assert len(calls) == 2
        assert not await request_gui_approval(grants, 2, {
            "request_id": "r4", "app_id": "app:a", "window_id": "",
        }, cancel)
        assert len(calls) == 2
        assert not await request_gui_approval(grants, 2, {
            "request_id": "", "app_id": "app:a", "window_id": "window:7",
        }, cancel)
        assert len(calls) == 2

    asyncio.run(scenario())


def test_retry_only_after_reject_and_requires_new_allow():
    from gui_approval import GuiGrantStore, request_gui_approval

    async def scenario():
        grants = GuiGrantStore()
        calls = []

        async def choose(prompt):
            calls.append(prompt)
            return "allow"

        event = {"request_id": "r1", "app_id": "app:a", "window_id": "window:7"}
        assert not await request_gui_approval(grants, 3, {**event, "explicit_retry": True}, choose)
        assert calls == []
        grants.decide(3, allow=False)
        assert await request_gui_approval(grants, 3, {**event, "explicit_retry": True}, choose)
        assert grants.state(3) == "allowed"
        assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("approved,wrong_id,expected", [
    (True, False, True), (False, False, False), (True, True, False),
])
def test_worker_rejects_denied_or_mismatched_approval_ack(monkeypatch, approved, wrong_id, expected):
    import socket
    worker, host = socket.socketpair()
    worker.settimeout(0.1)
    monkeypatch.setattr(code_runtime, "_CONTROL_SOCKET", worker)
    seen = []

    def emit(event):
        seen.append(event)
        host.sendall((json.dumps({
            "kind": "gui_approval_ack",
            "request_id": "wrong" if wrong_id else event["request_id"],
            "approved": approved,
        }) + "\n").encode())

    monkeypatch.setattr(code_runtime, "_emit", emit)
    try:
        result = asyncio.run(code_runtime._request_gui_approval(
            "app:a", "window:7",
        ))
        assert result is expected
        assert seen[0]["type"] == "gui_approval_request"
        assert seen[0]["app_id"] == "app:a"
        assert seen[0]["window_id"] == "window:7"
    finally:
        worker.close()
        host.close()


@pytest.mark.parametrize("approved", [True, False])
def test_worker_approval_frame_is_answered_before_cell_continues(tmp_path, approved):
    async def scenario():
        worker = tmp_path / "pipe_worker.py"
        worker.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
            "import code_runtime as rt\n"
            "rt._bootstrap = lambda loop: {}\n"
            "raise SystemExit(rt.main(sys.argv))\n", encoding="utf-8",
        )
        seen = []

        async def approve(event):
            seen.append((event["request_id"], event["app_id"], event["window_id"]))
            return approved

        executor = CodeExecutor(
            runtime_path=worker, work_dir=tmp_path,
            rpc_socket=tmp_path / "unused.sock", timeout=10, rpc_timeout=3,
            coordinate_range=1000, pyautogui_pause=0, enable_bash=False,
            ui_api="legacy-cua", approval_handler=approve,
        )
        try:
            result = await executor.run("""import code_runtime as rt
import json
rt._emit({'type':'gui_approval_request','request_id':'approval-1','app_id':'app:a','window_id':'window:7'})
reply = b''
while not reply.endswith(b'\\n'):
    reply += rt._CONTROL_SOCKET.recv(1)
assert json.loads(reply) == {'kind':'gui_approval_ack','request_id':'approval-1','approved':EXPECTED}
print('continued after approval response')
""".replace("EXPECTED", repr(approved)), [])
            assert result.returncode == 0, result.stderr
            assert seen == [("approval-1", "app:a", "window:7")]
            assert "continued after approval response" in result.stdout
        finally:
            await executor.close()

    asyncio.run(scenario())


def test_pending_gui_approval_does_not_consume_code_timeout_or_auto_deny(tmp_path):
    async def scenario():
        worker = tmp_path / "approval_worker.py"
        worker.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
            "import code_runtime as rt\n"
            "rt._bootstrap = lambda loop: {}\n"
            "raise SystemExit(rt.main(sys.argv))\n", encoding="utf-8",
        )
        asked = asyncio.Event()
        answer = asyncio.Event()

        async def approve(_event):
            asked.set()
            await answer.wait()
            return True

        executor = CodeExecutor(
            runtime_path=worker, work_dir=tmp_path,
            rpc_socket=tmp_path / "unused.sock", timeout=0.5, rpc_timeout=3,
            coordinate_range=1000, pyautogui_pause=0, enable_bash=False,
            ui_api="legacy-cua", approval_handler=approve,
        )
        try:
            running = asyncio.create_task(executor.run("""import code_runtime as rt
import json
rt._emit({'type':'gui_approval_request','request_id':'approval-1','app_id':'app:a','window_id':'window:7'})
reply = b''
while not reply.endswith(b'\\n'):
    reply += rt._CONTROL_SOCKET.recv(1)
assert json.loads(reply)['approved'] is True
print('approved')
""", []))
            await asyncio.wait_for(asked.wait(), 2)
            await asyncio.sleep(0.65)
            assert not running.done(), "an unanswered permission prompt must remain pending"
            answer.set()
            result = await asyncio.wait_for(running, 2)
            assert result.returncode == 0, result.stderr
            assert result.stdout == "approved"
            assert not result.timed_out
        finally:
            answer.set()
            await executor.close()

    asyncio.run(scenario())
