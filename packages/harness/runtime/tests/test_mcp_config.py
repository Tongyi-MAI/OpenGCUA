import importlib
import json
from pathlib import Path
import sys

import pytest


def _module():
    return importlib.import_module("mcp_config")


def _server(**changes):
    value = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["/tmp/dingtalk-mcp/index.js"],
        "env_from_host": ["DINGTALK_Client_ID", "DINGTALK_Client_Secret"],
        "read_only_tools": ["getUser", "getCalendarView"],
    }
    value.update(changes)
    return value


def test_mcp_config_is_disabled_when_absent():
    assert _module().parse_mcp_servers(None, {}) == ()
    assert _module().parse_mcp_servers({}, {}) == ()


def test_valid_server_keeps_variable_names_not_secret_values():
    secret = "secret-value-must-not-leak"
    spec, = _module().parse_mcp_servers(
        {"dingtalk": _server()},
        {"DINGTALK_Client_ID": "app-key", "DINGTALK_Client_Secret": secret},
    )
    assert spec.name == "dingtalk"
    assert spec.command == Path(sys.executable).resolve()
    assert spec.args == ("/tmp/dingtalk-mcp/index.js",)
    assert spec.env_from_host == ("DINGTALK_Client_ID", "DINGTALK_Client_Secret")
    assert spec.read_only_tools == frozenset({"getUser", "getCalendarView"})
    assert spec.timeout_seconds == 30.0
    assert secret not in repr(spec)


def test_fixed_arguments_may_repeat_flags():
    spec, = _module().parse_mcp_servers(
        {"fixture": _server(args=["--require", "a", "--require", "b"], env_from_host=[])},
        {},
    )
    assert spec.args == ("--require", "a", "--require", "b")


@pytest.mark.parametrize("changes", [
    {"transport": "http"},
    {"command": "node"},
    {"command": "/bin/sh"},
    {"env": {"DINGTALK_Client_Secret": "inline-secret"}},
    {"timeout_seconds": 0},
    {"timeout_seconds": 121},
    {"read_only_tools": ["bad\nname"]},
])
def test_mcp_config_rejects_unsafe_or_unsupported_server(changes):
    with pytest.raises(ValueError) as exc:
        _module().parse_mcp_servers(
            {"dingtalk": _server(**changes)},
            {"DINGTALK_Client_ID": "app-key", "DINGTALK_Client_Secret": "inline-secret"},
        )
    assert "inline-secret" not in str(exc.value)


def test_missing_forwarded_variable_is_named_without_value():
    with pytest.raises(ValueError, match="DINGTALK_Client_Secret"):
        _module().parse_mcp_servers(
            {"dingtalk": _server()}, {"DINGTALK_Client_ID": "app-key"}
        )


def test_invalid_server_name_and_non_object_are_rejected():
    with pytest.raises(ValueError):
        _module().parse_mcp_servers({"bad name": _server()}, {})
    with pytest.raises(ValueError):
        _module().parse_mcp_servers([_server()], {})


def test_duplicate_server_names_in_json_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        _module().load_unique_json(
            '{"mcp_servers":{"dingtalk":{},"dingtalk":{}}}'
        )


def test_parser_and_native_host_preserve_mcp_servers_config(tmp_path):
    from mac_agent import build_parser
    from native_host import task_arguments

    servers = {"dingtalk": _server(env_from_host=[])}
    args = build_parser().parse_args(["--mcp-servers", json.dumps(servers)])
    assert args.mcp_servers == servers
    native = task_arguments({
        "llm_base_url": "https://example.com/v1",
        "model_name": "test-model",
        "mcp_servers": servers,
    }, tmp_path)
    assert native.mcp_servers == servers
