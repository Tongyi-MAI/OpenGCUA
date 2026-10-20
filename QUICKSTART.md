# Setup, configuration, and troubleshooting

The root [README](README.md#quick-start) contains the shortest supported path from a clean clone
to a running macOS agent. This guide explains the installation choices, every runtime setting,
permissions, alternate launch modes, and common errors. Follow the README first; return here when
you need to customize or diagnose the harness.

## What is where

| Path | What it is |
|---|---|
| `packages/harness/runtime/` | The agent loop & host: `mac_agent.py` (loop, context, confirm/abort), `code_runtime.py` (persistent interpreter worker), `demo_hub.py` / `demo_hud.py` (demo studio + recorder), `console_server.py`, tests |
| `packages/sdk/adapters/macos/` | Native macOS provider (Rust) above the OS accessibility / screen-capture APIs: durable element identities, versioned snapshots, background delivery, typed errors |
| `packages/harness/configs/` | Runnable configurations (`macos_demo.json`) |
| `packages/sdk/python/mai_ui/` | The SDK implementation (device-agnostic `unified/` + native extension) |
| `packages/sdk/python/qwen_intelligence/` | Public alias namespace over `mai_ui` (frozen import path) |
| `packages/sdk/examples/` | Example programs (macos / browser / linux / android) |
| `packages/harness/examples/osworld/` | Standalone screenshot-action and code-action agents for OSWorld integration |

> The importable package keeps the internal name `mai_ui` because the shipped native
> extension (`mai_ui/_native*.so`) is built against that module path. `qwen_intelligence`
> re-exports it for the frozen public path.

## Prerequisites

- macOS 14+ and **CPython 3.11–3.14** (examples below use 3.12).
- [uv](https://docs.astral.sh/uv/), Rust/Cargo 1.89+ and Xcode Command Line Tools
  for source installs. Installing a prebuilt SDK wheel does not require Rust.
- **CuaDriver.app**, installed with `.venv/bin/mu setup macos`; its daemon is bundled
  and starts with the app (there is no separate `cua-driver serve` command).
- macOS **Accessibility** and **Screen Recording** granted to CuaDriver.app
  (System Settings → Privacy & Security). Without these the driver returns
  `PERMISSION_DENIED` for observation/input.
- If you explicitly use `--startup-observation desktop`, also grant **Screen Recording**
  to the terminal/IDE that launches Python. That optional frame uses the parent macOS
  `screencapture` command; the default `auto` path leaves capture ownership with CuaDriver.
- For the GUI-only baseline, grant the launching terminal or IDE (for example Terminal,
  Visual Studio Code, or Qoder) **Accessibility** and **Screen Recording**. The optional
  `--verify-input-preflight` TextEdit diagnostic additionally requires **Automation**
  access to System Events and TextEdit. Quit and reopen the launcher after changing permissions.
- An LLM endpoint, model name, and API key configured in the gitignored
  `packages/harness/configs/local.json` (see Step 3).

## Setup

### 1) Install the SDK and harness runtime

```bash
git clone <this-repo> OpenGCUA && cd OpenGCUA

# Source installs compile the native extension and require Rust/Cargo + Xcode CLT.
# Install Rust first if needed: brew install rustup && rustup default stable
# Homebrew rustup is keg-only, so expose its binaries for this shell:
export PATH="$(brew --prefix rustup)/bin:$PATH"

# Virtualenv at repo root (packages/harness/runtime/start_demo.sh expects ./.venv); Python 3.12 recommended.
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ./packages/sdk -e ./packages/harness/runtime
uv pip install pytest                 # development/test extra

# sanity-check: SDK imports and the native extension loads
python -c "import mai_ui; from mai_ui import Runtime; import qwen_intelligence; print('sdk ok')"
```

### 2) Install CuaDriver and grant macOS permissions

```bash
# Install CuaDriver (a macOS .app, NOT a brew formula).
# The app bundle includes the daemon; there is no separate daemon package or `serve` command.
.venv/bin/mu setup macos        # downloads & installs /Applications/CuaDriver.app

# CuaDriver.app launches its bundled daemon automatically.
# If `mu doctor macos` cannot find it, open CuaDriver from /Applications once.
```

If a different CuaDriver version is already installed, stop running agent sessions and use
`.venv/bin/mu setup macos --replace` to install this checkout's pinned version.

Then grant macOS permissions to **CuaDriver.app** (required, else `PERMISSION_DENIED`):
**System Settings → Privacy & Security → Accessibility** and **→ Screen Recording** → enable
CuaDriver. Then verify:

```bash
.venv/bin/mu doctor macos       # all capabilities should show "ready"
```

> **Permissions are granted per *responsible process*, not globally.** macOS TCC records the
> Accessibility / Screen Recording grant against the binary that is *responsible* for a GUI
> call. The daemon bundled in `CuaDriver.app` is its own responsible process, but a
> per-session driver child spawned under your agent may be attributed to **the Python
> / terminal that runs the agent**. So if you switch venvs, Python binaries, or terminals
> (e.g. moving between repos), the new responsible binary is *not* granted and every GUI
> call suddenly returns `PERMISSION_DENIED` even though it worked minutes earlier.
> Note macOS usually **cannot** add a bare `python3.12` binary to *Screen Recording*, so
> "grant the python" is often impossible. The reliable fixes are: (a) run the harness with
> an **already-granted interpreter** (e.g. another venv's `python` that previously worked),
> so attribution stays granted; or (b) quit and reopen `CuaDriver.app` so calls route through
> its granted bundled daemon; or (c) grant the *terminal/IDE app* you launch from, if macOS offers it.

### 3) Configure the model and credentials

```bash
cp packages/harness/configs/macos_demo.json packages/harness/configs/local.json
```

Edit `local.json` (gitignored, never committed). The base URL should normally end at
`/v1`; the client appends `/chat/completions` for `openai`, `/responses` for
`openai-thinking`, or `/messages` for `anthropic`.

For a GPT reasoning model on the official OpenAI Responses API:

```json
{
  "llm_protocol": "openai-thinking",
  "model_name": "YOUR_GPT_REASONING_MODEL",
  "llm_base_url": "https://api.openai.com/v1",
  "api_key": "sk-..."
}
```

For DashScope's OpenAI-compatible endpoint:

```json
{
  "llm_protocol": "openai",
  "model_name": "qwen3.8-flash",
  "llm_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "api_key": "sk-..."
}
```

For the official Anthropic Messages API:

```json
{
  "llm_protocol": "anthropic",
  "model_name": "YOUR_CLAUDE_MODEL",
  "llm_base_url": "https://api.anthropic.com/v1",
  "api_key": "sk-ant-...",
  "anthropic_thinking": {"type": "adaptive"}
}
```

Use `openai` for Chat Completions-compatible endpoints (including Qwen, vLLM, Ollama,
and LiteLLM); their readable `reasoning_content`, when present, follows the existing
history path. Use `openai-thinking` only for GPT reasoning endpoints that support the
Responses API. It requests stateless encrypted reasoning and replays complete response
output items in later turns; it does not expose or paste `encrypted_content` as text.
The two protocols are separate, so existing `openai` configurations are unchanged.
Use `anthropic` for native/compatible Anthropic Messages endpoints. Claude thinking
uses native `thinking` blocks plus an opaque `signature`, not OpenAI's
`reasoning_content` field. The agent preserves the blocks (including
`redacted_thinking`) in their original order and replays them in later requests;
it does not paste the thinking summary into the assistant text as a substitute.
Adaptive thinking may produce no thinking block on a simple step. On older Claude
models that require manual extended thinking, use
`"anthropic_thinking": {"type": "enabled", "budget_tokens": 10000}` instead and
keep `max_tokens` above that budget. The default `null` leaves the provider's
thinking behavior unchanged. Choose the form supported by your Claude model;
an unsupported form returns HTTP 400. `reasoning_effort` currently has no effect
on Anthropic requests. Signatures are retained in the harness's local resume
state; GUI-only diagnostics show counts and fingerprints, not the signature text.
Treat run directories and any opt-in exact model-request recording as sensitive local data.
Check the [OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
and [Claude thinking guide](https://platform.claude.com/docs/en/build-with-claude/thinking)
for model-specific support and current API behavior.

## Configuration

Configuration is auto-loaded (no need to pass `--config`). Priority (first hit wins):

1. env `MAC_AGENT_CONFIG=<path>`
2. `packages/harness/configs/local.json` — your local config (gitignored)
3. `packages/harness/configs/macos_demo.json` — shipped defaults

CLI flags always override file values. The two agents share the endpoint client and token-based
compaction implementation, but some history and image settings are agent-specific. Unknown keys
are ignored, so spelling and the agent column below matter. Sampling knobs `temperature` and
`top_p` were intentionally removed.

### Model and request settings

These keys work in `local.json` for both `mac_agent` and the GUI-only baseline unless noted.
`macos_demo.json` records the shipped defaults, including the complete context and image policy.
Copy it to `local.json`, then change only the values required by your provider or workload.

| Key | Default | What it controls | When to change it |
|---|---:|---|---|
| `llm_protocol` | `openai` | Request/response wire format. `openai` uses `/chat/completions`; `openai-thinking` uses `/responses` and replays encrypted reasoning items; `anthropic` uses `/messages`. | Use `openai-thinking` only if your GPT endpoint supports Responses; keep `openai` for Qwen and other Chat Completions-compatible models. |
| `model_name` | `qwen3.8-flash` | Model identifier sent to the provider. | Replace it with a model ID accepted by your endpoint. |
| `llm_base_url` | DashScope compatible `/v1` URL | Provider or gateway base URL. The client appends the protocol endpoint. | Use `https://api.openai.com/v1` for OpenAI or `https://api.anthropic.com/v1` for Anthropic. |
| `api_key` | empty | Authentication credential. | Put a real key only in gitignored `local.json`, or leave it empty and use `ANTHROPIC_API_KEY`, `DASHSCOPE_API_KEY`, `OPENAI_API_KEY`, or `API_KEY`. Never commit it. |
| `reasoning_effort` | `low` | Provider-side reasoning budget when the endpoint supports it. | Use `none` for plain vLLM or another endpoint that rejects this extension. Anthropic requests ignore this key; use `anthropic_thinking` for Claude. |
| `anthropic_thinking` | `null` | Optional native Anthropic `thinking` object (`adaptive` or legacy `enabled` with `budget_tokens`). When present, signed thinking blocks are replayed in subsequent turns. | Set only with `llm_protocol: anthropic` and a Claude model that supports that thinking mode. `null` preserves the provider default. |
| `max_tokens` | `32768` | Maximum output tokens requested for one agent action. A truncated action may be retried with a larger temporary budget, up to four times this value. | Lower it to reduce latency/cost. It must be positive and smaller than `compaction_trigger_tokens` for `mac_agent`. This is not the model's total context size. |
| `api_timeout` | `600.0` | Wall-clock timeout, in seconds, for one model request. | Raise it for slow local or reasoning models; lower it to fail faster. |
| `api_retries` | `3` | Transport/provider retries after request failures. | Raise cautiously for flaky endpoints; retries can repeat cost-bearing requests. |
| `parse_retries` | `2` | Extra attempts after a response arrives but does not match the required agent action format. | Raise only if the model often returns malformed action markup. `mac_agent` only; the GUI-only CLI has its own default. |

### Context and image settings (`mac_agent`)

One history **turn** is one completed observation → model reasoning/action → execution-result cycle.
It is not a chat message count, a task limit, or a token limit.

| Key | Default | What it controls | Important interaction |
|---|---:|---|---|
| `llm_compaction` | `true` | Enables lossy LLM-generated summaries of old turns. Compaction occurs only when the estimated next request exceeds the token threshold. | When enabled, `history_turns` does **not** truncate normal history. |
| `compaction_trigger_tokens` | `200000` | Soft total-context budget used to decide when to summarize older turns. It includes estimated input plus the reserved action output. | Keep it within the selected model's real context window and greater than `max_tokens`. |
| `compaction_max_tokens` | `100000` | Maximum output budget for the summary request itself. | Must be positive and smaller than `compaction_trigger_tokens`; lower it if summaries are unnecessarily large or expensive. |
| `compaction_image_tokens` | `1600` | Estimated token cost assigned to each retained image when checking the context budget. | Adjust only when the provider's image accounting differs substantially. |
| `compaction_retries` | `3` | Extra attempts to obtain a structurally valid history summary. | These are summary-format retries, separate from `api_retries`. |
| `history_turns` | `100` | Maximum number of raw completed turns included when `llm_compaction` is `false`. The current observation is still included. | With the default `llm_compaction: true`, this is a fallback setting and normally has no effect. |
| `reasoning_history_chars` | `0` | Per-turn character budget when replaying old model reasoning. `0` keeps it in full; positive values retain a head/tail excerpt. Full reasoning remains in `traj.json`. | Set a positive limit when reasoning text, rather than screenshots, dominates context. |
| `img_keep_n` | `5` | Number of recent image-bearing turns retained after each image-retention trim. | Increase for visually dependent tasks; decrease to reduce request size. At least the latest image is retained. |
| `img_interval` | `10` | Extra image-bearing turns allowed before another trim occurs. It controls trim cadence, not “keep every Nth image.” | A larger value keeps more images between trims and uses more context. |
| `max_image_dimension` | `1920` | Maximum width or height used when encoding screenshots for the model; aspect ratio is preserved. | Reduce for lower bandwidth/token use; increase only if fine visual detail is necessary and supported. |
| `jpeg_quality` | `85` | JPEG quality for model-input screenshots. | Lower it to save bandwidth; raise it if compression artifacts hide UI details. |

The same defaults are written explicitly in `macos_demo.json`. To override them, edit the desired
fields in the **same top-level JSON object** in `packages/harness/configs/local.json`—do not paste a second
JSON object after the existing one. The default context policy is:

```json
{
  "max_tokens": 32768,
  "llm_compaction": true,
  "compaction_trigger_tokens": 200000,
  "compaction_max_tokens": 100000,
  "compaction_image_tokens": 1600,
  "compaction_retries": 3,
  "history_turns": 100,
  "reasoning_history_chars": 0,
  "img_keep_n": 5,
  "img_interval": 10
}
```

If you explicitly disable compaction, `history_turns` becomes the raw-history limit:

```json
{
  "llm_compaction": false,
  "history_turns": 30
}
```

### Driver and observation settings (`mac_agent`)

| Key | Default | What it controls | When to change it |
|---|---:|---|---|
| `driver_timeout` | `30.0` | Timeout, in seconds, for one CuaDriver call. | Raise it for unusually slow app launches or observations. It does not control model requests. |
| `coordinate_range` | `1000` | Normalized coordinate scale exposed to the model (`0..1000` by default). | Normally leave unchanged; prompts and coordinate conversion must agree with it. |
| `startup_observation` | `none` | Initial visual observation policy: `none` defers observation to agent code; `desktop` captures the full desktop; `auto` follows the selected UI backend. | Use `desktop` only when the model needs an immediate overview and the launching process has Screen Recording permission. |
| `background_mode` | `true` | Prefer DOM actions in an isolated, headed browser and AX actions in native apps without taking over your mouse or keyboard. On macOS, Chromium launches without activation; agent-created tabs stay in the background. Recognized GET search forms targeting `_blank` stay in their existing tab, while other recognized `_blank` links/forms require foreground consent. | Set `false` (or pass `--disable-background-mode`) for the foreground-oriented workflow. Script-created pop-ups, system dialogs, and explicit foreground actions can still take focus; demo mode deliberately shows foreground actions. |

When an SDK action requests foreground focus or real mouse/keyboard input, the host pauses
before dispatch and presents one task-level Allow/Reject choice. Allow covers further SDK
foreground operations in that task, including a different window; target and focus checks still
apply to each operation. Reject blocks repeat attempts without prompting again. The agent can
explicitly re-request via `request_foreground_permission(win, reason)` if foreground work is
genuinely necessary. This is not a host-level gate for arbitrary Python/Bash code.

The GUI-only baseline uses `history` (default `5`) for its screenshot window rather than
`history_turns`. Its token threshold is named `compact_trigger_tokens`. Run either script with
`--help` for its remaining execution-only flags; execution-only flags are not configuration keys.

## Run the agent

### Interactive (recommended): a persistent conversational session

```bash
.venv/bin/python packages/harness/runtime/mac_agent.py        # no task arg -> interactive REPL
```

On a normal interactive terminal this opens the visual CLI. Redirected stdin/stdout and
`TERM=dumb` automatically use the original plain-text interface. Override the selection with:

```bash
.venv/bin/python packages/harness/runtime/mac_agent.py --ui tui     # require an interactive visual terminal
.venv/bin/python packages/harness/runtime/mac_agent.py --ui plain   # stable line output for scripts and logs
```

Each submitted message becomes a new task in the **same persistent session**: interpreter state,
bindings, TODOs, and history carry across turns. The visual interface uses normal terminal
scrollback rather than an alternate screen, so completed output remains searchable and copyable.

| Input | Behavior |
|---|---|
| Enter | Submit the current message. |
| Alt/Option+Enter | Insert a newline. Bracketed multiline paste remains one logical message. |
| While the agent runs, Enter | Queue a message for the next normal turn; the fixed input area stays editable. |
| `/queue`, `/queue send`, `/queue clear` | Inspect, resume, or discard queued messages. An abort suspends the queue; a new correction can still be sent immediately. |
| `/help` | Show terminal commands and shortcuts. |
| `/reasoning on\|off` | Show or hide reasoning emitted after the command. It is hidden by default. |
| `/code on\|off` | Show or hide generated code emitted after the command. It is hidden by default. |
| `/clear` | Clear visible terminal output without clearing the agent session or trajectory. |
| `/new <message>` | Start a fresh task boundary and invalidate old UI observations while keeping the healthy interpreter session. |
| `/exit`, `/quit`, or Ctrl-D | End the session. |
| Ctrl-C during an active step | Abort this turn but keep its task context and foreground consent. A second Ctrl-C force-stops a resistant worker. Already-issued actions are not rolled back. |
| Foreground consent choice | When an SDK action requires foreground focus, mouse, or keyboard, use arrows/Tab to select Allow or Reject and Enter to confirm. Reject is selected by default; permission lasts only for this task. Background AX/visual clicks do not trigger it. |

`SIGUSR1` remains the programmatic turn-abort signal and is exposed as **Abort Turn** in the demo
studio. After an abort, a correction continues the same task and the model is told to verify
the current application state before continuing. The permission gate covers only SDK actions
through this harness, not arbitrary model-authored Python/Bash or behavior initiated by an App or website.

### One-shot / scripted: pass the first task as an argument

```bash
.venv/bin/python packages/harness/runtime/mac_agent.py "Open Chrome, go to example.com, and tell me the page title"
```

### GUI-only baseline (pure pyautogui, no SDK)

```bash
.venv/bin/python packages/harness/runtime/gui_agent.py "Describe the frontmost window, then terminate"
```

A deliberately simple comparison baseline: raw `pyautogui` actions + `computer.wait/terminate`,
0-1000 normalized coordinates only, same config chain and same LLM entry as mac_agent.
Unlike `mac_agent`, GUI-only actions run under the terminal or IDE that launched the process:
that app needs Accessibility for synthetic keyboard/mouse events and Screen Recording for
screenshots. TextEdit is not opened during normal startup. To explicitly test the clipboard
input path, pass `--verify-input-preflight`; this optional diagnostic also requires Automation
access to System Events and TextEdit.
In the demo studio it is selectable via the **Agent** dropdown (`our harness | gui-only`)
for side-by-side efficiency recordings.
Sessions are **interactive**: when stdin is a pipe (the demo hub) or `--interactive` is
given, the process stays alive between messages and carries turns/summary context across
tasks; a plain tty run keeps the old one-shot semantics. If the SDK browser endpoint is
unavailable on your host, `code_runtime` automatically falls back to the interface-compatible
local Playwright shim (`packages/harness/runtime/web_shim.py`) so `web` tasks never silently degrade
to driving the user's own Chrome.

### Web demo studio: interactive + recordable

See the next section (`start_demo.sh`).

The agent plans in natural-language intent and emits guarded Python code; it escalates
actions AX(background) → in-window vision coordinates → foreground real input only when a
lower tier is refused or proven ineffective, so it disturbs your foreground session as
little as possible. A CLI launched from the repository root writes trajectories to
`runs/<timestamp>_.../`. The demo hub launches its child from `packages/harness/runtime/`, so demo
trajectories land in `packages/harness/runtime/runs/<timestamp>_.../`.

## Run the demo studio (recommended for demos & recording)

```bash
bash packages/harness/runtime/start_demo.sh        # serves http://127.0.0.1:8800
```

Then in a browser:
1. **Select Screen** — pick the *application window* the agent will operate on (not the whole
   monitor, to avoid nesting the studio inside its own preview).
2. Type a task and send. The middle column shows phase / step / intent / thinking / code;
   the right column is the conversation.
3. **Record** — records a canvas self-composite (screen + HUD + conversation) via
   `canvas.captureStream(60)`; it does not throttle like tab capture. The download is a
   fragmented MP4: play in Chrome/VLC, or remux for QuickTime with
   `ffmpeg -i in.mp4 -c copy -movflags +faststart out.mp4`.
4. Scroll the conversation to review history (it will not yank you back to the bottom
   while you read); **Abort Turn** interrupts the in-flight step and immediately accepts
   your next instruction.

## Run the tests

```bash
.venv/bin/python -m pytest packages/harness/runtime/tests -q
```

## Troubleshooting

Start with the exact error text; similarly named failures can have different owners.

| Error or symptom | What it means | What to do |
|---|---|---|
| `VERSION_MISMATCH` from `mu setup macos` or `mu doctor macos` | The installed CuaDriver.app differs from this checkout's pinned version. Setup preserves an existing installation by default. | Stop any running agent sessions, then run `.venv/bin/mu setup macos --replace` and `.venv/bin/mu doctor macos`. Recheck macOS permissions if the replacement is not ready. |
| `PERMISSION_DENIED ... tool_invocation_failed` on capture, window listing, or input | macOS denied Accessibility or Screen Recording to the process responsible for this call. The responsible process can change after switching terminals, interpreters, or driver processes. | Enable **CuaDriver.app** under both Privacy & Security permission pages, quit and reopen it, and run `.venv/bin/mu doctor macos`. If the failure remains, grant the launching terminal/IDE when macOS offers it or use the previously granted interpreter. |
| `DEVICE_OFFLINE` from a failed Mac GUI call | The Mac SDK endpoint or its connection became unavailable. This code alone does not identify whether the driver exited, restarted, or was fenced earlier. | Send the next turn and let the host rebuild the endpoint. Treat earlier window/UI handles as invalid. If it repeats, inspect the failed call's diagnostic fields (`endpoint_offline_before`, `last_gui_success_age_seconds`, `keepalive_error`) and the preceding native error. |
| Browser startup reports `Browser worker is missing Playwright or its background-browser module; run mu setup browser` followed by `local playwright shim active` | The optional Node-based SDK browser worker is missing its npm dependency or is an older cached build; the local browser fallback succeeded. This does **not** mean the Mac GUI endpoint is offline. Older builds may instead print `ERR_MODULE_NOT_FOUND: Cannot find package 'playwright'`. | Browser-only work can continue through the fallback. To enable the native SDK browser, run `.venv/bin/mu setup browser --engine chromium` and restart the harness. |
| `No such file or directory` or `node: command not found` while starting the browser SDK | Node.js is missing from the environment used to launch the harness. | Install Node.js, verify `node --version` in the same terminal, and restart the harness. Do not troubleshoot this as a device-offline condition. |
| `ImportError: ... _native ...` | The SDK extension is absent or does not match the active Python version/architecture. | Activate the intended venv and run `uv pip install --reinstall ./packages/sdk`, or install a matching wheel. Source builds support CPython 3.11–3.14. |
| Model request times out | `api_timeout` was exceeded; it is unrelated to GUI-driver timeouts. | Raise `api_timeout` in `local.json`, or check provider latency and connectivity. |
| Anthropic says a thinking signature is invalid after history compaction | Claude rejected a signed block because an earlier message or screenshot changed. The client retries that request once with thinking blocks removed and keeps the text history; the lost reasoning continuity is reported in the request attempt. | If this happens often, reduce history rewriting/compaction or use a Claude model and context policy compatible with your workload. Do not edit a signature or its thinking block manually. |
| A GUI action reports an operation timeout | `driver_timeout` was exceeded. The failure is returned to the agent so it can observe again, retry a safer method, or ask for help. | Check whether an app dialog or permission prompt is blocking progress. Raise `driver_timeout` only for consistently slow operations. |
| Context estimate exceeds `compaction_trigger_tokens` | The conversation outgrew the configured soft context budget. | Keep `llm_compaction` enabled and set the trigger within the model's actual context window. A compaction summary is lossy and does not resume an interrupted action automatically. |
| Recording shows only the first frame in QuickTime | The browser produced a fragmented MP4 that QuickTime does not index correctly. | Play it in Chrome/VLC, or run `ffmpeg -i in.mp4 -c copy -movflags +faststart out.mp4`. |
| `ModuleNotFoundError: rich` or `prompt_toolkit` | The harness runtime was installed before the visual CLI dependencies were added, or the wrong venv is active. | Activate the repository venv and run `uv pip install -e ./packages/harness/runtime`. |
| `--ui tui requires interactive stdin and stdout` | Visual mode was forced while input or output is redirected. | Run it in a real terminal, omit `--ui` to use automatic selection, or use `--ui plain` for the pipeline. |
| Visual output is garbled or the terminal reports `TERM=dumb` | The terminal does not advertise the capabilities needed for Rich/prompt-toolkit rendering. | Use `--ui plain`, or correct the terminal's `TERM` configuration before retrying visual mode. |

### Optional GUI-only input diagnostic

- `Not authorized to send Apple events ... (-1743)` from `--verify-input-preflight`:
  open **System Settings → Privacy & Security → Automation**, expand the launching terminal
  or IDE, and enable System Events and TextEdit. Quit and reopen the launcher.
- `GUI text preflight mismatch ... actual=''`: Automation works, but the synthetic click or
  Command-V did not reach TextEdit. Enable the launcher under **Accessibility**, then restart it.

### The agent repeats `terminate()` after a permission failure

`terminate()` still travels through the active worker. If the worker cannot initialize because
macOS denied the underlying driver, the model may retry the same otherwise-correct answer until
stall detection stops the run. Fix the permission failure above, restart CuaDriver and the agent,
then retry the task; changing the greeting or asking the model to terminate differently does not
repair the worker.

## See also

- `packages/harness/README.md` — the five-layer architecture and per-layer responsibilities.
- `README.md` — project mission, open-stack status, and roadmap.
