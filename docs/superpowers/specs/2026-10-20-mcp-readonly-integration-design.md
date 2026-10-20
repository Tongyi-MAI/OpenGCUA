# Read-only MCP integration for the macOS harness

Date: 2026-10-20

## Goal and acceptance boundary

Let the existing `mac_agent` use explicitly configured MCP servers, beginning with read-only tools from the official DingTalk MCP server. The connection must work through the same harness in the TUI, demo hub, and native host. The first release must not send messages, edit DingTalk data, or execute arbitrary MCP tools. No MCP server is enabled by default.

Acceptance has two distinct levels: (1) an automated local MCP server proves discovery, tool calls, errors, timeout, cancellation, and cleanup without DingTalk credentials; (2) an opt-in, read-only DingTalk call proves the actual third-party server and enterprise permissions. The second level can only pass when a user has separately provisioned app credentials and the relevant DingTalk scopes. A successful `tools/list` alone is not evidence that DingTalk accepted an API call.

## Alternatives considered

1. Connect to MCP directly from model-authored worker code. This is small but would place server credentials and subprocess lifecycle in a worker that can already execute arbitrary Python/Bash, and it would make interruption and frontend status harder to manage.
2. Reuse the Rust `rmcp` connection inside the macOS CuaDriver adapter. That adapter pins a signed driver and its private protocol; adding unrelated business services would couple two independent failure and trust domains.
3. **Chosen: a host-owned MCP broker.** The worker receives a narrow `mcp` proxy. The host owns configured connections, tool policy, timeouts, redaction, and cleanup. The existing CuaDriver MCP transport remains unchanged.

## Configuration and trust boundary

Add an optional top-level `mcp_servers` object to the existing local configuration. Each server has a unique name, `transport: "stdio"`, an absolute executable path, a fixed argument vector, names of environment variables to forward only to that server, an exact `read_only_tools` allowlist, and a bounded call timeout. Reject shell command strings, relative executables, inline secret values, duplicate names, malformed tool names, and unsupported transports. An absent or empty object preserves current behavior. Do not run `npx -y`, install dependencies, or log in automatically; the user prepares a pinned DingTalk server package and credentials outside the repository.

The host resolves forwarded environment variables when starting a server. These values never enter model prompts, worker configuration frames, MCP result logs, or trajectory JSON. The code worker must not inherit the specifically forwarded variables from the host environment. This reduces new credential exposure but does not turn the existing Python/Bash worker into a security sandbox; unrelated secrets accessible to that worker remain outside this project.

Tool descriptions and results are untrusted server data. They cannot change the system prompt or authorization rules. An exact allowlist controls `tools/call`; it is not inferred from a tool name or server-supplied annotation. A configured tool is only used after its current schema is retrieved. Unknown servers/tools, changed schemas, malformed arguments, and calls outside the allowlist fail before dispatch. The allowlist is a host policy, not a guarantee that a malicious server's implementation is genuinely read-only. The initial DingTalk configuration should therefore use only documented query tools from the official server and narrow DingTalk app scopes.

## Runtime API and lifecycle

Expose `mcp.list()` and `mcp.call(server, tool, arguments)` in the persistent worker. `list` returns configured server names and bounded, current tool metadata including input schema and whether a call is allowed. `call` sends one JSON argument object to an allowed tool and returns a bounded result with text/structured content, `is_error`, and a truncation indicator. Other MCP capabilities (resources, prompts, elicitation, server-side sampling) are out of scope. The model receives only a short static instruction to discover relevant tools on demand; do not paste full tool catalogs into every model request.

The worker proxy checks the active request token and uses the existing worker-to-host control channel for request/reply. The broker starts a server lazily, negotiates MCP, and can reuse the connection across code cells in one Agent run. Closing the Agent closes all servers. Ctrl-C cancels the active call and leaves the conversation usable. A call timeout returns a specific MCP timeout error; it must not be disguised as `DEVICE_OFFLINE` or silently retried. Server exit or schema change invalidates that connection and requires fresh discovery, not replay of the previous call. Each call has a finite size and duration budget; stderr is bounded and sanitized before user-visible diagnostics. Frontends receive concise connecting, ready, call-in-progress, and error events without secrets or raw contact data.

The read-only `mcp` proxy is separate from `skills`: a Skill can explain a workflow but cannot create a connection or expand its allowlist. The bundled `dingtalk-workflow` Skill must continue to distinguish app/robot identity from a signed-in user. Official DingTalk MCP app credentials do not imply that a tool acts as the user's personal account.

## User-facing setup and verification

Document the opt-in config shape in README and QUICKSTART, including how to install a pinned official server package manually, set `DINGTALK_Client_ID` and `DINGTALK_Client_Secret` in the launching environment, enable only required profiles/scopes, discover tool names, and then set the exact read-only allowlist. Never tell users to paste secrets into tracked JSON, chat, or issue reports. The app must report an absent executable, missing variable, server startup failure, authorization failure, and missing enterprise scope distinctly where the server provides enough detail.

Offline tests use a real local stdio MCP fixture and cover startup/handshake, pagination of `tools/list`, allowed call, denial before dispatch, invalid input, server error, timeout, Ctrl-C cancellation, server exit/restart, output caps, credential forwarding without worker exposure, and clean shutdown. Source and bundled-app configuration paths must exercise the same broker. The opt-in live check uses only one documented DingTalk query with an app account that has the needed scope, records the tool name and success/error category, and redacts IDs and returned contact details. It must not send a message or create/update data. If credentials or enterprise authorization are absent, report that live verification was not performed rather than calling `tools/list` an end-to-end success.
