from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import sys

import pytest

from code_executor import CodeExecutor


def _worker(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
        "import code_runtime as rt\n"
        "rt._bootstrap = lambda loop: {'mcp': rt.McpProxy()}\n"
        "raise SystemExit(rt.main(sys.argv))\n",
        encoding="utf-8",
    )
    return worker


def _executor(tmp_path, handler, *, timeout=10):
    return CodeExecutor(
        runtime_path=_worker(tmp_path), work_dir=tmp_path,
        rpc_socket=tmp_path / "unused.sock", timeout=timeout, rpc_timeout=1,
        coordinate_range=1000, pyautogui_pause=0, enable_bash=False,
        ui_api="legacy-cua", mcp_handler=handler,
        mcp_env_names=("MCP_WORKER_SECRET",),
    )


def test_worker_mcp_list_call_round_trip_and_secret_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKER_SECRET", "worker-secret-must-stay-host-side")
    seen = []

    async def handler(event):
        seen.append(event)
        if event["operation"] == "list":
            return {"servers": ["fixture"]}
        return {"content": [{"type": "text", "text": "ok"}],
                "structured_content": None, "is_error": False, "truncated": False}

    async def scenario():
        executor = _executor(tmp_path, handler)
        try:
            first = await executor.run(
                "import asyncio, os\n"
                "print(os.environ.get('MCP_WORKER_SECRET', 'missing'))\n"
                "print(asyncio.run(mcp.list()))",
                [],
            )
            second = await executor.run(
                "import asyncio\n"
                "print(asyncio.run(mcp.call('fixture', 'read_value', {'value': 'abc'})))",
                [],
            )
            assert "missing" in first.stdout
            assert "fixture" in first.stdout
            assert "ok" in second.stdout
            assert "worker-secret-must-stay-host-side" not in first.feedback() + second.feedback()
            assert [event["operation"] for event in seen] == ["list", "call"]
        finally:
            await executor.close()

    asyncio.run(scenario())


def test_worker_can_call_real_local_mcp_server(tmp_path, monkeypatch):
    from mcp_broker import McpBroker
    from mcp_config import McpServerSpec

    fixture = Path(__file__).parent / "fixtures" / "mcp_server.py"
    secret_name = "MCP_WORKER_SECRET"
    monkeypatch.setenv(secret_name, "server-only-secret-324")
    spec = McpServerSpec(
        "fixture", Path(sys.executable), (str(fixture),),
        ("MCP_FIXTURE_SECRET",), frozenset({"read_value"}), 5.0,
    )

    async def scenario():
        broker = McpBroker((spec,), {"MCP_FIXTURE_SECRET": "server-only-secret-324"}, lambda event: None)

        async def handler(event):
            if event["operation"] == "list":
                return await broker.list_tools(event.get("server"))
            return await broker.call_tool(event["server"], event["tool"], event["arguments"])

        executor = _executor(tmp_path, handler)
        try:
            listing = await executor.run(
                "import asyncio\nprint(asyncio.run(mcp.list('fixture')))", [],
            )
            result = await executor.run(
                "import asyncio\nprint(asyncio.run(mcp.call('fixture', 'read_value', {'value': 'hello'})))", [],
            )
            assert "read_value" in listing.stdout
            assert "value:hello" in result.stdout
            assert "server-only-secret-324" not in listing.feedback() + result.feedback()
        finally:
            await executor.close()
            await broker.close()

    asyncio.run(scenario())


def test_worker_mcp_errors_return_without_device_offline(tmp_path):
    async def handler(event):
        raise ValueError("MCP_TOOL_DENIED: not allowlisted")

    async def scenario():
        executor = _executor(tmp_path, handler)
        try:
            result = await executor.run(
                "import asyncio\n"
                "try:\n"
                "    asyncio.run(mcp.call('fixture', 'write_value', {}))\n"
                "except Exception as exc:\n"
                "    print(type(exc).__name__, str(exc))",
                [],
            )
            assert "MCP_TOOL_DENIED" in result.stdout
            assert "DEVICE_OFFLINE" not in result.feedback()
        finally:
            await executor.close()

    asyncio.run(scenario())


def test_worker_pending_call_can_be_interrupted_then_reused(tmp_path):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(event):
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    async def scenario():
        executor = _executor(tmp_path, handler, timeout=120)
        try:
            running = asyncio.create_task(executor.run(
                "import asyncio\nprint(asyncio.run(mcp.list()))", [],
            ))
            await asyncio.wait_for(entered.wait(), 5)
            executor.interrupt_active(force=True)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 4)
            assert cancelled.is_set()
            following = await executor.run("print('next-message-ok')", [])
            assert following.session_reset is True
            retried_after_notice = await executor.run("print('next-message-ok')", [])
            assert "next-message-ok" in retried_after_notice.stdout
        finally:
            await executor.close()

    asyncio.run(scenario())


def test_worker_rejects_background_mcp_call_without_active_turn(tmp_path):
    import code_runtime as runtime

    async def scenario():
        with pytest.raises(RuntimeError, match="活动回复"):
            await runtime.McpProxy().list()

    asyncio.run(scenario())


def test_host_rejects_oversized_mcp_reply(tmp_path):
    async def handler(event):
        return {"content": [{"type": "text", "text": "x" * (100 * 1024)}]}

    async def scenario():
        executor = _executor(tmp_path, handler)
        try:
            result = await executor.run(
                "import asyncio\n"
                "try:\n"
                "    asyncio.run(mcp.list())\n"
                "except Exception as exc:\n"
                "    print(str(exc))", [],
            )
            assert "too large" in result.stdout or "超过" in result.stdout
        finally:
            await executor.close()

    asyncio.run(scenario())


def test_ack_reader_buffers_other_reply_but_applies_caller_limit(monkeypatch):
    import code_runtime as runtime

    receiver, sender = socket.socketpair()
    receiver.settimeout(0.1)
    monkeypatch.setattr(runtime, "_CONTROL_SOCKET", receiver)
    runtime._ACK_BUFFER.clear()
    runtime._ACK_PENDING.clear()
    try:
        first = {"kind": "mcp_reply", "call_id": "first", "ok": True,
                 "result": {"blob": "x" * 5000}}
        second = {"kind": "mcp_reply", "call_id": "second", "ok": True}
        sender.sendall(json.dumps(first).encode() + b"\n" + json.dumps(second).encode() + b"\n")
        assert runtime._recv_host_ack("mcp_reply", "call_id", "second", 1) == second
        assert runtime._recv_host_ack("mcp_reply", "call_id", "first", 1, 4096) is None
    finally:
        receiver.close()
        sender.close()
        runtime._ACK_BUFFER.clear()
        runtime._ACK_PENDING.clear()
