"""Host-owned, allowlisted MCP stdio sessions for the code agent.

No server is started until a catalog or permitted call is requested. Each server
session has one owning task so the SDK's async context is always closed by the
same task that opened it, including after cancellation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import json
import os
import threading
from typing import Any

from mcp_config import McpServerSpec


MAX_TOOLS = 64
MAX_SCHEMA_BYTES = 16 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_ERROR_BYTES = 4 * 1024
MAX_PAGES = 100


class McpBrokerError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class _BoundedStderr:
    def __init__(self):
        read_fd, write_fd = os.pipe()
        self.writer = os.fdopen(write_fd, "w", encoding="utf-8")
        self._read_fd = read_fd
        self._data = bytearray()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        try:
            while chunk := os.read(self._read_fd, 4096):
                if len(self._data) < MAX_ERROR_BYTES:
                    self._data.extend(chunk[:MAX_ERROR_BYTES - len(self._data)])
        except OSError:
            pass
        finally:
            os.close(self._read_fd)

    def close(self) -> None:
        self.writer.close()
        self._thread.join(timeout=1)

    @property
    def text(self) -> str:
        return self._data.decode("utf-8", errors="replace")


class _ServerActor:
    def __init__(self, broker: McpBroker, spec: McpServerSpec):
        self.broker = broker
        self.spec = spec
        self.queue: asyncio.Queue[tuple[str, dict, asyncio.Future] | None] = asyncio.Queue()
        self.stderr = _BoundedStderr()
        self.task = asyncio.create_task(self._run(), name=f"mcp:{spec.name}")

    async def request(self, kind: str, data: dict) -> dict:
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((kind, data, future))
        try:
            async with asyncio.timeout(self.spec.timeout_seconds):
                return await future
        except TimeoutError as exc:
            self.task.cancel()
            raise McpBrokerError("MCP_TIMEOUT", f"{self.spec.name} request timed out") from exc
        except asyncio.CancelledError:
            self.task.cancel()
            raise

    async def close(self) -> None:
        if not self.task.done():
            self.queue.put_nowait(None)
            try:
                async with asyncio.timeout(6):
                    await self.task
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        else:
            await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self) -> None:
        self.broker._event("mcp_connecting", server=self.spec.name)
        pending: asyncio.Future | None = None
        terminal_error = McpBrokerError("MCP_SERVER_UNAVAILABLE", "server session closed")
        try:
            from mcp import Client
            from mcp.client.stdio import StdioServerParameters, stdio_client

            env = {key: self.broker._environ[key] for key in self.spec.env_from_host}
            params = StdioServerParameters(
                command=str(self.spec.command), args=list(self.spec.args), env=env,
            )
            async with Client(stdio_client(params, errlog=self.stderr.writer), cache=None) as client:
                self.broker._event("mcp_ready", server=self.spec.name)
                while True:
                    item = await self.queue.get()
                    if item is None:
                        break
                    kind, data, pending = item
                    if pending.cancelled():
                        continue
                    try:
                        if kind == "list":
                            result = await self.broker._list_on_client(self.spec, client)
                        else:
                            result = await self.broker._call_on_client(self.spec, client, **data)
                    except McpBrokerError as exc:
                        if not pending.done():
                            pending.set_exception(exc)
                        self.broker._event("mcp_call_failed", server=self.spec.name, code=exc.code)
                    except Exception as exc:
                        error = self.broker._server_error(self.spec, exc, self.stderr.text)
                        if not pending.done():
                            pending.set_exception(error)
                        self.broker._event("mcp_call_failed", server=self.spec.name, code=error.code)
                        break
                    else:
                        if not pending.done():
                            pending.set_result(result)
                        if kind == "call" and result.get("is_error"):
                            self.broker._event("mcp_call_failed", server=self.spec.name,
                                               code="MCP_TOOL_ERROR")
                    pending = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = self.broker._server_error(self.spec, exc, self.stderr.text)
            terminal_error = error
            if pending is not None and not pending.done():
                pending.set_exception(error)
            self.broker._event("mcp_call_failed", server=self.spec.name, code=error.code)
        finally:
            self.stderr.close()
            if pending is not None and not pending.done():
                pending.set_exception(terminal_error)
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item is not None and not item[2].done():
                    item[2].set_exception(terminal_error)


class McpBroker:
    def __init__(
        self, specs: tuple[McpServerSpec, ...], environ: Mapping[str, str],
        on_event: Callable[[dict], None],
    ) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._environ = environ
        self._on_event = on_event
        self._actors: dict[str, _ServerActor] = {}
        self._catalog: dict[str, dict[str, dict]] = {}
        self._closed = False

    def _event(self, kind: str, **fields: str) -> None:
        try:
            self._on_event({"type": kind, **fields})
        except Exception:
            pass

    def _redact(self, value: str) -> str:
        for spec in self._specs.values():
            for name in spec.env_from_host:
                secret = self._environ.get(name)
                if secret:
                    value = value.replace(secret, "[REDACTED]")
        return value

    def _redact_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, list):
            return [self._redact_obj(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(key): self._redact_obj(item) for key, item in value.items()}
        return value

    def _server_error(self, spec: McpServerSpec, exc: Exception, stderr: str) -> McpBrokerError:
        detail = f"{spec.name} server failed ({type(exc).__name__})"
        # Server stderr is untrusted and may contain contact data or partial
        # credential fragments. Drain it with a 4 KiB cap, but never surface it.
        if stderr:
            detail += "; server stderr suppressed"
        return McpBrokerError("MCP_SERVER_UNAVAILABLE", detail)

    def _spec(self, server: str) -> McpServerSpec:
        if self._closed or server not in self._specs:
            raise McpBrokerError("MCP_NOT_CONFIGURED", f"server {server!r} is not configured")
        return self._specs[server]

    def _actor(self, spec: McpServerSpec) -> _ServerActor:
        actor = self._actors.get(spec.name)
        if actor is None or actor.task.done():
            actor = _ServerActor(self, spec)
            self._actors[spec.name] = actor
        return actor

    async def list_tools(self, server: str | None = None) -> dict:
        if server is None:
            if self._closed:
                raise McpBrokerError("MCP_NOT_CONFIGURED", "broker is closed")
            return {"servers": sorted(self._specs)}
        spec = self._spec(server)
        return await self._actor(spec).request("list", {})

    async def call_tool(self, server: str, tool: str, arguments: dict) -> dict:
        spec = self._spec(server)
        if tool not in spec.read_only_tools:
            raise McpBrokerError("MCP_TOOL_DENIED", f"tool {tool!r} is not allowlisted")
        if not isinstance(arguments, dict):
            raise McpBrokerError("MCP_TOOL_ERROR", "tool arguments must be a JSON object")
        try:
            raw = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            raise McpBrokerError("MCP_TOOL_ERROR", "tool arguments must be JSON") from exc
        if len(raw) > MAX_RESULT_BYTES:
            raise McpBrokerError("MCP_TOOL_ERROR", "tool arguments are too large")
        self._event("mcp_call_started", server=server, tool=tool)
        return await self._actor(spec).request("call", {"tool": tool, "arguments": arguments})

    async def _read_catalog(self, spec: McpServerSpec, client: Any) -> dict[str, dict]:
        catalog: dict[str, dict] = {}
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            page = await client.list_tools(cursor=cursor, cache_mode="refresh")
            for tool in page.tools:
                schema = tool.input_schema
                if len(json.dumps(schema, ensure_ascii=False).encode()) > MAX_SCHEMA_BYTES:
                    raise McpBrokerError("MCP_TOOL_ERROR", "tool schema exceeds size limit")
                if tool.name in catalog:
                    raise McpBrokerError("MCP_TOOL_ERROR", "duplicate tool in catalog")
                catalog[tool.name] = {
                    "name": tool.name,
                    "description": self._redact(tool.description or "")[:4096],
                    "input_schema": schema,
                    "allowed": tool.name in spec.read_only_tools,
                }
                if len(catalog) > MAX_TOOLS:
                    raise McpBrokerError("MCP_TOOL_ERROR", "tool catalog exceeds 64 tools")
            cursor = page.next_cursor
            if cursor is None:
                return catalog
            if cursor in seen:
                raise McpBrokerError("MCP_TOOL_ERROR", "tool pagination cursor repeated")
            seen.add(cursor)
        raise McpBrokerError("MCP_TOOL_ERROR", "tool pagination exceeds page limit")

    async def _list_on_client(self, spec: McpServerSpec, client: Any) -> dict:
        catalog = await self._read_catalog(spec, client)
        self._catalog[spec.name] = catalog
        return {"server": spec.name, "tools": self._redact_obj(list(catalog.values()))}

    async def _call_on_client(self, spec: McpServerSpec, client: Any, tool: str, arguments: dict) -> dict:
        from jsonschema import Draft202012Validator, ValidationError, SchemaError
        from referencing import Registry
        from referencing.exceptions import Unresolvable

        current = await self._read_catalog(spec, client)
        descriptor = current.get(tool)
        if descriptor is None:
            raise McpBrokerError("MCP_TOOL_ERROR", f"tool {tool!r} is not advertised")
        previous = self._catalog.get(spec.name, {}).get(tool)
        if previous is not None and previous["input_schema"] != descriptor["input_schema"]:
            raise McpBrokerError("MCP_TOOL_ERROR", "tool schema changed; list tools again")
        schema = descriptor["input_schema"]

        def reject_external_refs(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                        if not isinstance(child, str) or not child.startswith("#"):
                            raise McpBrokerError("MCP_TOOL_ERROR", "external tool schema references are not allowed")
                    reject_external_refs(child)
            elif isinstance(value, list):
                for child in value:
                    reject_external_refs(child)

        # MCP servers supply untrusted schemas. Never let validation fetch a URI.
        reject_external_refs(schema)

        def deny_remote(_uri: str):
            raise McpBrokerError("MCP_TOOL_ERROR", "external tool schema references are not allowed")

        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema, registry=Registry(retrieve=deny_remote)).validate(arguments)
        except (ValidationError, SchemaError, Unresolvable) as exc:
            raise McpBrokerError("MCP_TOOL_ERROR", f"invalid tool arguments: {self._redact(str(exc))[:512]}") from exc
        # Use the low-level session call: Client.call_tool may automatically replay
        # a call after HEADER_MISMATCH, which this broker must never do.
        result = await client.session.call_tool(tool, arguments)
        payload = {
            "content": [part.model_dump(mode="json", by_alias=True) for part in result.content],
            "structured_content": result.structured_content,
            "is_error": bool(result.is_error),
            "truncated": False,
        }
        payload = self._redact_obj(payload)
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded.encode()) > MAX_RESULT_BYTES:
            return {
                "content": [{"type": "text", "text": "[MCP result truncated at 64 KiB]"}],
                "structured_content": None, "is_error": bool(result.is_error), "truncated": True,
            }
        return json.loads(encoded)

    async def close(self) -> None:
        self._closed = True
        actors = list(self._actors.values())
        self._actors.clear()
        await asyncio.gather(*(actor.close() for actor in actors), return_exceptions=True)
