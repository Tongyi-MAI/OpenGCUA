from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from code_executor import CodeExecutor
import code_runtime


def test_grant_is_scoped_to_task_app_and_exact_window():
    from gui_approval import GuiGrantStore
    grants = GuiGrantStore()
    assert not grants.allowed(4, "app:a", "window:7")
    grants.grant(4, "app:a", "window:7")
    assert grants.allowed(4, "app:a", "window:7")
    assert not grants.allowed(5, "app:a", "window:7")
    assert not grants.allowed(4, "app:b", "window:7")
    assert not grants.allowed(4, "app:a", "window:8")
    grants.clear()
    assert not grants.allowed(4, "app:a", "window:7")


def test_one_explicit_yes_grants_repeated_actions_but_not_another_window():
    from gui_approval import request_gui_approval

    async def scenario():
        grants = GuiGrantStore()
        questions = []
        async def ask(question):
            questions.append(question)
            return "yes"

        first = {"app_id": "app:a", "window_id": "window:7", "needs_vision": True}
        assert await request_gui_approval(grants, 4, first, ask)
        assert await request_gui_approval(grants, 4, first, ask)
        assert len(questions) == 1
        assert "app:a" in questions[0] and "window:7" in questions[0]
        assert "前台" in questions[0] and "视觉" in questions[0]
        assert await request_gui_approval(
            grants, 4, {**first, "window_id": "window:8"}, ask,
        )
        assert len(questions) == 2

    from gui_approval import GuiGrantStore
    asyncio.run(scenario())


def test_denial_missing_scope_and_cancel_never_grant():
    from gui_approval import GuiGrantStore, request_gui_approval

    async def scenario():
        grants = GuiGrantStore()
        calls = []
        async def deny(question):
            calls.append(question)
            return "maybe"
        assert not await request_gui_approval(
            grants, 1, {"app_id": "app:a", "window_id": "window:7"}, deny,
        )
        assert not grants.allowed(1, "app:a", "window:7")
        assert not await request_gui_approval(
            grants, 1, {"app_id": "app:a", "window_id": ""}, deny,
        )
        assert len(calls) == 1

        async def cancelled(_question):
            raise asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await request_gui_approval(
                grants, 1, {"app_id": "app:a", "window_id": "window:7"}, cancelled,
            )
        assert not grants.allowed(1, "app:a", "window:7")

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
            "app:a", "window:7", needs_vision=True, needs_foreground=False,
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
