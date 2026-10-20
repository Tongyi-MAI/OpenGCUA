"""Opt-in MCP read-only smoke check; never print tool result bodies."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
from collections.abc import Mapping, Sequence

from mcp_broker import McpBroker, McpBrokerError
from mcp_config import load_unique_json, parse_mcp_servers


_SAFE_ARGUMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_MISSING_ENV = re.compile(r"requires environment variable ([A-Za-z_][A-Za-z0-9_]*)\Z")


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


async def _check(specs, environ: Mapping[str, str], server: str, tool: str,
                 arguments: dict) -> dict:
    broker = McpBroker(specs, environ, lambda _event: None)
    try:
        listing = await broker.list_tools(server)
        descriptor = next((item for item in listing["tools"] if item["name"] == tool), None)
        if descriptor is None:
            raise McpBrokerError("MCP_TOOL_ERROR", "tool is not advertised")
        schema = descriptor["input_schema"]
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        if not isinstance(properties, dict):
            properties = {}
        if not isinstance(required, list):
            required = []
        argument_names = sorted(name for name in properties
                                if isinstance(name, str) and _SAFE_ARGUMENT.fullmatch(name))
        required_arguments = sorted(name for name in required
                                    if isinstance(name, str) and _SAFE_ARGUMENT.fullmatch(name))
        result = await broker.call_tool(server, tool, arguments)
        schema_kind = schema.get("type") if isinstance(schema, dict) else None
        return {
            "category": "MCP_TOOL_ERROR" if result["is_error"] else "OK",
            "server": server, "tool": tool,
            "schema_type": schema_kind if schema_kind in ("object", "array") else "other",
            "argument_names": argument_names, "required_arguments": required_arguments,
            "truncated": result["truncated"],
        }
    finally:
        await broker.close()


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in read-only MCP smoke check")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--args-json", default="{}")
    args = parser.parse_args(argv)
    host_env = os.environ if environ is None else environ
    try:
        arguments = load_unique_json(args.args_json)
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except (ValueError, json.JSONDecodeError):
        _print({"category": "INPUT_ERROR", "hint": "--args-json must be one JSON object with unique keys"})
        return 2
    try:
        raw = load_unique_json(args.config.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config must be an object")
        specs = parse_mcp_servers(raw.get("mcp_servers"), host_env)
    except (OSError, UnicodeError, ValueError) as exc:
        match = _MISSING_ENV.search(str(exc))
        hint = f"Set {match.group(1)} in the launching environment" if match else "Check the MCP config file"
        _print({"category": "CONFIG_ERROR", "hint": hint})
        return 2
    spec = next((item for item in specs if item.name == args.server), None)
    if spec is None:
        _print({"category": "MCP_NOT_CONFIGURED", "server": args.server})
        return 2
    if args.tool not in spec.read_only_tools:
        _print({"category": "MCP_TOOL_DENIED", "server": spec.name, "tool": args.tool})
        return 2
    try:
        result = asyncio.run(_check(specs, host_env, args.server, args.tool, arguments))
    except McpBrokerError as exc:
        _print({"category": exc.code, "server": spec.name, "tool": args.tool})
        return 2
    _print(result)
    return 0 if result["category"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
