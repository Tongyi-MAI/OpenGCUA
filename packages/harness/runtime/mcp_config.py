"""Validate opt-in stdio MCP servers without retaining credential values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re


_SERVER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "csh", "tcsh", "env"})
_FIELDS = frozenset({
    "transport", "command", "args", "env_from_host", "read_only_tools", "timeout_seconds"
})


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    name: str
    command: Path
    args: tuple[str, ...]
    env_from_host: tuple[str, ...]
    read_only_tools: frozenset[str]
    timeout_seconds: float


def load_unique_json(raw: str) -> object:
    """Decode config JSON while rejecting duplicate object keys."""
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON config key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique)


def _string_list(value: object, field: str, *, unique: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"MCP {field} must be a list of strings")
    if unique and len(value) != len(set(value)):
        raise ValueError(f"MCP {field} contains duplicate entries")
    return tuple(value)


def parse_mcp_servers(value: object, environ: Mapping[str, str]) -> tuple[McpServerSpec, ...]:
    """Return inert server specifications; secret values remain in ``environ`` only."""
    if value is None or value == {}:
        return ()
    if not isinstance(value, dict):
        raise ValueError("mcp_servers must be an object")
    if len(value) > 16:
        raise ValueError("mcp_servers exceeds the 16-server limit")
    result: list[McpServerSpec] = []
    for name, raw in value.items():
        if not isinstance(name, str) or not _SERVER_NAME.fullmatch(name):
            raise ValueError("invalid MCP server name")
        if not isinstance(raw, dict):
            raise ValueError(f"MCP server {name} must be an object")
        if set(raw) - _FIELDS:
            raise ValueError(f"MCP server {name} has unsupported fields")
        if raw.get("transport") != "stdio":
            raise ValueError(f"MCP server {name} supports only stdio transport")
        command = raw.get("command")
        if not isinstance(command, str) or not command or not Path(command).is_absolute():
            raise ValueError(f"MCP server {name} command must be an absolute executable path")
        executable = Path(command)
        if executable.name in _SHELLS or executable.name == "npx":
            raise ValueError(f"MCP server {name} command cannot launch a shell or npx")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError(f"MCP server {name} executable is missing or not runnable")
        args = _string_list(raw.get("args", []), "args", unique=False)
        env_names = _string_list(raw.get("env_from_host", []), "env_from_host")
        for env_name in env_names:
            if not _ENV_NAME.fullmatch(env_name):
                raise ValueError(f"MCP server {name} has an invalid environment variable name")
            if not environ.get(env_name):
                raise ValueError(f"MCP server {name} requires environment variable {env_name}")
        tools = _string_list(raw.get("read_only_tools", []), "read_only_tools")
        if any(not _TOOL_NAME.fullmatch(tool) for tool in tools):
            raise ValueError(f"MCP server {name} has an invalid read-only tool name")
        timeout = raw.get("timeout_seconds", 30.0)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 1 <= timeout <= 120):
            raise ValueError(f"MCP server {name} timeout_seconds must be between 1 and 120")
        result.append(McpServerSpec(
            name, executable.resolve(), args, env_names, frozenset(tools), float(timeout)
        ))
    return tuple(result)
