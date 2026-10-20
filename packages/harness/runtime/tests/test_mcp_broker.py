import asyncio
import os
from pathlib import Path
import sys

import pytest

from mcp_config import McpServerSpec


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_server.py"
SECRET = "credential-that-must-not-escape-81742"


def _spec(*, allowed=("read_value",), timeout=2.0):
    return McpServerSpec(
        "fixture", Path(sys.executable), (str(FIXTURE),),
        ("MCP_FIXTURE_SECRET", "MCP_FIXTURE_MARKER", "MCP_FIXTURE_WRITE_MARKER",
         "MCP_FIXTURE_SCHEMA_MARKER", "MCP_FIXTURE_PAGED", "MCP_FIXTURE_CALL_MARKER",
         "MCP_FIXTURE_SPAM_STDERR"),
        frozenset(allowed), timeout,
    )


def _environ(tmp_path):
    return {
        "MCP_FIXTURE_SECRET": SECRET,
        "MCP_FIXTURE_MARKER": str(tmp_path / "started"),
        "MCP_FIXTURE_WRITE_MARKER": str(tmp_path / "wrote"),
        "MCP_FIXTURE_SCHEMA_MARKER": str(tmp_path / "schema-changed"),
        "MCP_FIXTURE_PAGED": "0",
        "MCP_FIXTURE_CALL_MARKER": str(tmp_path / "slow-calls"),
        "MCP_FIXTURE_SPAM_STDERR": "0",
    }


def _run(coro):
    return asyncio.run(coro)


def test_listing_names_is_lazy(tmp_path):
    from mcp_broker import McpBroker

    env = _environ(tmp_path)

    async def scenario():
        broker = McpBroker((_spec(),), env, lambda event: None)
        try:
            listing = await broker.list_tools()
            assert listing["servers"] == ["fixture"]
            assert not (tmp_path / "started").exists()
        finally:
            await broker.close()

    _run(scenario())


def test_real_stdio_handshake_listing_and_allowed_call(tmp_path):
    from mcp_broker import McpBroker

    events = []

    async def scenario():
        broker = McpBroker((_spec(),), _environ(tmp_path), events.append)
        try:
            listing = await broker.list_tools("fixture")
            tools = {tool["name"]: tool for tool in listing["tools"]}
            assert "read_value" in tools
            assert tools["read_value"]["allowed"] is True
            assert tools["write_value"]["allowed"] is False
            result = await broker.call_tool("fixture", "read_value", {"value": "abc"})
            assert result["is_error"] is False
            assert "value:abc" in str(result["content"])
            assert result["truncated"] is False
        finally:
            await broker.close()

    _run(scenario())
    assert (tmp_path / "started").exists()
    assert SECRET not in str(events)


def test_paginated_catalog_is_complete(tmp_path):
    from mcp_broker import McpBroker

    env = _environ(tmp_path)
    env["MCP_FIXTURE_PAGED"] = "1"

    async def scenario():
        broker = McpBroker((_spec(),), env, lambda event: None)
        try:
            listing = await broker.list_tools("fixture")
            assert {tool["name"] for tool in listing["tools"]} >= {
                "read_value", "read_secret", "large_read", "exit_read"
            }
        finally:
            await broker.close()

    _run(scenario())


def test_changed_schema_rejects_call_until_relisted(tmp_path):
    from mcp_broker import McpBroker, McpBrokerError

    async def scenario():
        broker = McpBroker((_spec(),), _environ(tmp_path), lambda event: None)
        try:
            await broker.list_tools("fixture")
            (tmp_path / "schema-changed").write_text("yes", encoding="utf-8")
            with pytest.raises(McpBrokerError) as exc:
                await broker.call_tool("fixture", "read_value", {"value": "abc"})
            assert exc.value.code == "MCP_TOOL_ERROR"
            assert "schema changed" in str(exc.value)
        finally:
            await broker.close()

    _run(scenario())


def test_denied_call_never_reaches_server(tmp_path):
    from mcp_broker import McpBroker, McpBrokerError

    async def scenario():
        broker = McpBroker((_spec(),), _environ(tmp_path), lambda event: None)
        try:
            with pytest.raises(McpBrokerError) as exc:
                await broker.call_tool("fixture", "write_value", {"value": "bad"})
            assert exc.value.code == "MCP_TOOL_DENIED"
            assert not (tmp_path / "wrote").exists()
            assert not (tmp_path / "started").exists()
        finally:
            await broker.close()

    _run(scenario())


def test_invalid_arguments_fail_before_dispatch(tmp_path):
    from mcp_broker import McpBroker, McpBrokerError

    async def scenario():
        broker = McpBroker((_spec(),), _environ(tmp_path), lambda event: None)
        try:
            await broker.list_tools("fixture")
            with pytest.raises(McpBrokerError) as exc:
                await broker.call_tool("fixture", "read_value", {"unexpected": "x"})
            assert exc.value.code == "MCP_TOOL_ERROR"
        finally:
            await broker.close()

    _run(scenario())


def test_server_tool_error_and_oversize_result(tmp_path):
    from mcp_broker import McpBroker

    async def scenario():
        broker = McpBroker((_spec(allowed=("failing_read", "large_read")),), _environ(tmp_path), lambda event: None)
        try:
            failure = await broker.call_tool("fixture", "failing_read", {})
            assert failure["is_error"] is True
            large = await broker.call_tool("fixture", "large_read", {})
            assert large["truncated"] is True
            assert len(str(large).encode()) <= 68 * 1024
        finally:
            await broker.close()

    _run(scenario())


def test_forwarded_secret_is_redacted_from_result(tmp_path):
    from mcp_broker import McpBroker

    async def scenario():
        broker = McpBroker((_spec(allowed=("read_secret",)),), _environ(tmp_path), lambda event: None)
        try:
            result = await broker.call_tool("fixture", "read_secret", {})
            assert SECRET not in str(result)
        finally:
            await broker.close()

    _run(scenario())


def test_timeout_does_not_replay_call(tmp_path):
    from mcp_broker import McpBroker, McpBrokerError

    async def scenario():
        broker = McpBroker((_spec(allowed=("slow_read",), timeout=1.0),), _environ(tmp_path), lambda event: None)
        try:
            with pytest.raises(McpBrokerError) as exc:
                await broker.call_tool("fixture", "slow_read", {})
            assert exc.value.code == "MCP_TIMEOUT"
        finally:
            await broker.close()

    _run(scenario())
    assert (tmp_path / "slow-calls").read_text() == "1"


def test_server_exit_is_mcp_unavailable_not_device_offline(tmp_path):
    from mcp_broker import McpBroker, McpBrokerError

    async def scenario():
        broker = McpBroker((_spec(allowed=("exit_read",)),), _environ(tmp_path), lambda event: None)
        try:
            with pytest.raises(McpBrokerError) as exc:
                await broker.call_tool("fixture", "exit_read", {})
            assert exc.value.code == "MCP_SERVER_UNAVAILABLE"
            assert "DEVICE_OFFLINE" not in str(exc.value)
        finally:
            await broker.close()

    _run(scenario())


def test_server_stderr_is_bounded_and_never_exposes_credential(tmp_path):
    from mcp_broker import McpBroker, McpBrokerError

    env = _environ(tmp_path)
    env["MCP_FIXTURE_SPAM_STDERR"] = "1"

    async def scenario():
        broker = McpBroker((_spec(allowed=("exit_read",)),), env, lambda event: None)
        try:
            with pytest.raises(McpBrokerError) as exc:
                await broker.call_tool("fixture", "exit_read", {})
            assert SECRET not in str(exc.value)
            assert len(str(exc.value).encode()) <= 4300
        finally:
            await broker.close()

    _run(scenario())


def test_cancellation_and_close_cleanup_server(tmp_path):
    from mcp_broker import McpBroker

    async def scenario():
        broker = McpBroker((_spec(allowed=("slow_read",)),), _environ(tmp_path), lambda event: None)
        task = asyncio.create_task(broker.call_tool("fixture", "slow_read", {}))
        for _ in range(100):
            if (tmp_path / "started").exists():
                break
            await asyncio.sleep(0.05)
        assert (tmp_path / "started").exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await broker.close()

    _run(scenario())
    pid = int((tmp_path / "started").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
