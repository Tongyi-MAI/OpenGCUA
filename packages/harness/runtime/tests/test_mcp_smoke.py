import json
from pathlib import Path
import sys

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_server.py"


def _config(tmp_path, *, allowed=("read_value",), env_names=()):
    path = tmp_path / "local.json"
    path.write_text(json.dumps({"mcp_servers": {"fixture": {
        "transport": "stdio", "command": sys.executable, "args": [str(FIXTURE)],
        "env_from_host": list(env_names), "read_only_tools": list(allowed),
        "timeout_seconds": 2,
    }}}), encoding="utf-8")
    return path


def _invoke(config, capsys, *, tool="read_value", args='{"value":"abc"}', environ=None):
    from mcp_smoke import main

    exit_code = main([
        "--config", str(config), "--server", "fixture", "--tool", tool,
        "--args-json", args,
    ], environ=environ if environ is not None else {})
    output = capsys.readouterr()
    return exit_code, json.loads(output.out), output.err


def test_smoke_real_stdio_success_only_prints_safe_metadata(tmp_path, capsys):
    config = _config(tmp_path)
    code, result, stderr = _invoke(config, capsys)
    assert code == 0
    assert result == {"category": "OK", "server": "fixture", "tool": "read_value",
                      "schema_type": "object", "argument_names": ["value"],
                      "required_arguments": ["value"], "truncated": False}
    assert "value:abc" not in str(result) + stderr


def test_smoke_missing_credentials_does_not_echo_secret(tmp_path, capsys):
    config = _config(tmp_path, env_names=("MCP_FIXTURE_SECRET",))
    code, result, stderr = _invoke(config, capsys)
    assert code == 2
    assert result["category"] == "CONFIG_ERROR"
    assert "MCP_FIXTURE_SECRET" in result["hint"]
    assert stderr == ""


def test_smoke_server_permission_error_omits_body(tmp_path, capsys):
    config = _config(tmp_path, allowed=("failing_read",))
    code, result, stderr = _invoke(config, capsys, tool="failing_read", args="{}")
    assert code == 2
    assert result["category"] == "MCP_TOOL_ERROR"
    assert "fixture failure" not in str(result) + stderr


def test_smoke_denies_nonallowlisted_write_before_process_start(tmp_path, capsys):
    config = _config(tmp_path, env_names=("MCP_FIXTURE_MARKER", "MCP_FIXTURE_WRITE_MARKER"))
    env = {"MCP_FIXTURE_MARKER": str(tmp_path / "started"),
           "MCP_FIXTURE_WRITE_MARKER": str(tmp_path / "wrote")}
    code, result, _stderr = _invoke(
        config, capsys, tool="write_value", args='{"value":"bad"}', environ=env,
    )
    assert code == 2
    assert result["category"] == "MCP_TOOL_DENIED"
    assert not (tmp_path / "started").exists()
    assert not (tmp_path / "wrote").exists()


@pytest.mark.parametrize("value", ['{"value":', '[1,2]', '{"value":"a","value":"b"}'])
def test_smoke_rejects_bad_arguments_without_echoing_them(tmp_path, capsys, value):
    config = _config(tmp_path)
    code, result, stderr = _invoke(config, capsys, args=value)
    assert code == 2
    assert result["category"] == "INPUT_ERROR"
    assert value not in str(result) + stderr
