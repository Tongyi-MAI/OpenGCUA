# Read-only MCP Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `mac_agent` an opt-in, host-managed, read-only MCP client and verify it against a local server and, when credentials exist, DingTalk.

**Architecture:** A validated server registry lives in the host. The host owns stdio MCP clients and forwards only named environment variables to each server; a narrow `mcp` proxy in the code worker communicates through the existing fenced worker control channel. Tool schemas and results are fetched on demand and bounded before reaching model-authored code.

**Tech Stack:** Python 3.12, official `mcp==2.3.0` Python SDK, asyncio, pytest, existing macOS app requirements lock.

**Spec:** `docs/superpowers/specs/2026-10-20-mcp-readonly-integration-design.md`

## Global Constraints

- `mcp_servers` is optional; absent/empty means no server and no changed default behavior.
- First release supports only stdio and exact `read_only_tools`; every other `tools/call` fails before dispatch.
- No inline credentials, shell command strings, `npx -y`, automatic installation/login, or arbitrary resources/prompts/sampling.
- The worker must not inherit credential variables forwarded to MCP servers. Do not log credentials or raw DingTalk contact data.
- All connection/call deadlines are finite; cancellation is effective; never map MCP failure to CuaDriver `DEVICE_OFFLINE`.
- Default call timeout is 30 seconds (accepted range 1–120); cap a server catalog at 64 tools, each schema at 16 KiB, a returned result at 64 KiB, and a surfaced stderr/error excerpt at 4 KiB.
- App/robot DingTalk identity is distinct from the signed-in user's identity. Live testing performs no writes.

## Review Focus

1. Missing or malformed environment variables: config validation names the variable without disclosing its value (Task 1 test).
2. Paginated or changed tool catalogs: listing is complete; a changed schema invalidates a previously permitted call (Task 2 tests).
3. Server exits during a call: surface an MCP-specific error and do not automatically replay it (Task 2 test).
4. Ctrl-C while a call is pending: the worker returns to the composer and no MCP subprocess lingers after run shutdown (Tasks 2–3 tests).
5. Oversized or sensitive output/stderr: bound the response and redact credentials before display or trajectory write (Tasks 2 and 4 tests).

---

### Task 1: Validated opt-in configuration

**Files:** Create `packages/harness/runtime/mcp_config.py`; modify `packages/harness/runtime/mac_agent.py`, `packages/harness/runtime/native_host.py`, `packages/harness/runtime/pyproject.toml`, `packages/macos/requirements.lock`; test `packages/harness/runtime/tests/test_mcp_config.py`.

**Interfaces:** Produce `McpServerSpec(name: str, command: Path, args: tuple[str, ...], env_from_host: tuple[str, ...], read_only_tools: frozenset[str], timeout_seconds: float)` and `parse_mcp_servers(value: object, environ: Mapping[str, str]) -> tuple[McpServerSpec, ...]`. Specs contain variable names, never resolved secret values. Add the `mcp_servers` parser/config destination accepted by both plain/TUI and native host.

- [ ] Write tests for empty config, one valid absolute-command spec, duplicate/invalid names, inline env, relative/shell command, missing variable, timeout bounds, and native-host config forwarding. Assert no secret value appears in errors or `repr(spec)`.
- [ ] Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_mcp_config.py -q`; expect failures because config support is absent.
- [ ] Implement the signatures above, add `mcp==2.3.0`, and regenerate `packages/macos/requirements.lock` with the documented `uv pip compile` command.
- [ ] Re-run the focused test and `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests packages/macos/tests -q`; expect all green.
- [ ] Commit only Task 1 files with `feat: validate opt-in MCP server config`.

### Task 2: Host-owned stdio broker

**Files:** Create `packages/harness/runtime/mcp_broker.py`, `packages/harness/runtime/tests/fixtures/mcp_server.py`, `packages/harness/runtime/tests/test_mcp_broker.py`.

**Interfaces:** Produce `McpBroker(specs: tuple[McpServerSpec, ...], environ: Mapping[str, str], on_event: Callable[[dict], None])` with async `list_tools(server: str | None = None) -> dict`, `call_tool(server: str, tool: str, arguments: dict) -> dict`, and `close() -> None`. Listing without a server returns configured names without starting subprocesses; listing one server returns its bounded tool metadata. Results include `content`, `structured_content`, `is_error`, and `truncated`; errors use `MCP_NOT_CONFIGURED`, `MCP_TOOL_DENIED`, `MCP_SERVER_UNAVAILABLE`, `MCP_TIMEOUT`, or `MCP_TOOL_ERROR`, never `DEVICE_OFFLINE`. Only exact allowlisted calls dispatch.

- [ ] Write a real local stdio fixture and failing tests for handshake, `tools/list` pagination, one allowed read call, denial before dispatch, schema/argument mismatch, server error/exit, timeout without replay, cancellation, output/stderr caps, and subprocess cleanup. Include a schema change between `list_tools` and `call_tool`.
- [ ] Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_mcp_broker.py -q`; expect feature-absence failures.
- [ ] Install `mcp==2.3.0` into the test venv, then implement `McpBroker` with the pinned SDK's `Client(StdioServerParameters(...))`, bounded catalog/result conversion, lazy connection, and async context cleanup. Resolve secret values only when spawning that server; pass only a small runtime base environment plus listed variables.
- [ ] Re-run focused and full Python suites; expect all green and no leaked secret fixture string in captured output.
- [ ] Commit Task 2 files with `feat: add read-only MCP host broker`.

### Task 3: Fenced worker proxy and cancellation

**Files:** Modify `packages/harness/runtime/code_executor.py`, `packages/harness/runtime/code_runtime.py`; create `packages/harness/runtime/tests/test_mcp_worker.py`.

**Interfaces:** Add `mcp_handler` to `CodeExecutor`, backed by Task 2's broker. Expose async worker methods `mcp.list(server: str | None = None)` and `mcp.call(server: str, tool: str, arguments: dict)`. Use `mcp_request`/`mcp_reply` control frames with request IDs, bounded reply size, and the existing active-request fence; no broker secrets enter `_config()`.

- [ ] Write failing persistent-worker tests: list/call round trip, unknown/denied call, no forwarded secret in worker `os.environ`, stale/background request rejection, pending-call Ctrl-C and subsequent new message, oversized reply handling.
- [ ] Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_mcp_worker.py -q`; expect feature-absence failures.
- [ ] Implement proxy/handler framing. Replace the 4 KiB one-byte acknowledgment reader only where necessary with a bounded buffered reply path; preserve GUI approval and overlay acknowledgments. On cancellation, cancel the host call and close the affected server session.
- [ ] Re-run focused and full Python suites; expect all green.
- [ ] Commit Task 3 files with `feat: expose fenced MCP tools to agent worker`.

### Task 4: Agent lifecycle, visibility, and packaging

**Files:** Modify `packages/harness/runtime/mac_agent.py`, `packages/harness/runtime/agent_events.py`, `packages/harness/runtime/terminal_tui.py`, `packages/harness/runtime/demo_hub.py`, `packages/macos/Sources/AppModel.swift`, `packages/harness/runtime/tests/test_agent_frontend.py`, `packages/harness/runtime/tests/test_terminal_tui.py`, `packages/harness/runtime/tests/test_demo_hub.py`, `packages/harness/runtime/tests/test_native_host.py`, `README.md`, `QUICKSTART.md`, `packages/harness/configs/macos_demo.json`; update the bundled `dingtalk-workflow` Skill only if observed API names require it.

**Interfaces:** `run_agent` creates one `McpBroker` from Task 1 specs, passes its handler to Task 3, and closes it in `finally`. Add concise `mcp_connecting`, `mcp_ready`, `mcp_call_started`, `mcp_call_failed` events without secrets or result bodies. Add a short system-prompt instruction for `mcp.list`/`mcp.call` and retain lazy Skill loading.

- [ ] Write failing tests for disabled-by-default behavior, lifecycle cleanup on success/error/interrupt, native config parity, redacted event rendering in TUI/demo/native consumers, and catalog-on-demand (no full schemas in every model request).
- [ ] Run the affected frontend/native tests; expect feature-absence failures.
- [ ] Implement lifecycle/events/prompt and document manual pinned-server setup, env references, exact allowlist, `/skills` usage, and read-only scope. Keep config example disabled and credential-free.
- [ ] Run full Python tests, source/app package-copy tests, and the existing Swift contract/build check; expect all green. Inspect docs for commands that would auto-install or leak secrets.
- [ ] Commit Task 4 files with `feat: integrate MCP broker into mac agent`.

### Task 5: Opt-in DingTalk read-only smoke check

**Files:** Create `packages/harness/runtime/mcp_smoke.py`, `packages/harness/runtime/tests/test_mcp_smoke.py`; update `QUICKSTART.md` with the command and outcome meanings.

**Interfaces:** A CLI entry `python packages/harness/runtime/mcp_smoke.py --config <path> --server dingtalk --tool <exact-read-tool> --args-json <json>` lists the schema and calls only a configured allowlisted read tool. It prints tool name, success/error category, and redacted metadata—not returned contact details or credentials.

- [ ] Write failing tests using the Task 2 fixture for success, missing credentials, permission error, non-allowlisted tool, and no write dispatch.
- [ ] Run the focused smoke test; expect feature-absence failures.
- [ ] Implement the command and document a credential-free local fixture invocation plus the opt-in official DingTalk invocation.
- [ ] Run focused/full Python suites. If locally provisioned DingTalk app credentials and enterprise scopes exist, run one real read-only call and record sanitized evidence; otherwise explicitly record `LIVE_NOT_RUN` and the missing prerequisite. Never send or mutate DingTalk data.
- [ ] Commit Task 5 files with `test: add opt-in DingTalk MCP smoke check`.

## Final verification

- [ ] Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests packages/macos/tests -q` after the last change; report counts and any failures.
- [ ] Run `git diff --check`, inspect the full branch diff against `f32a626`, and check config/docs/trajectory for credential fixture strings.
- [ ] Distinguish local MCP fixture success from live DingTalk success in the handoff; report the exact live read-only tool and result category or `LIVE_NOT_RUN`.
