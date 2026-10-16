# runtime — agent loop & host (macOS real-device)

The layer that runs a GCUA on a user's own machine. It executes the model's emitted
Python code on a **persistent interpreter** (variables / imports / cwd survive turns),
assembles each next model input (history window, retained screenshots, execution
feedback), and owns everything human-facing: confirmations, `ask_user`, and an interrupt
that cancels the in-flight step and immediately returns to reading the next instruction.

| File | Role |
|---|---|
| `mac_agent.py` | agent loop, context assembly, turn boundaries, confirmation/abort flow, trajectory recording; holds `MAI_UI_SYSTEM_PROMPT` |
| `agent_events.py` | presentation-neutral agent events, input prompts, and the pipe-compatible plain terminal frontend |
| `terminal_tui.py` | Rich + prompt-toolkit visual terminal, local display controls, input history, and Ctrl-C turn abort |
| `code_runtime.py` | persistent-interpreter worker (RPC over a fifo), execution lifecycle and isolation from the host process; it is not a security sandbox |
| `console_server.py` | session/console plumbing shared by CLI and the demo studio |
| `demo_hub.py` / `demo_hud.py` | demo studio web UI (screen preview + HUD + conversation) and the canvas self-composite recorder |
| `sdk_backend.py`, `code_executor.py` | bridges the loop to the Device SDK |
| `llm_compaction.py`, `model_stream.py`, `model_metrics.py` | context compaction, streaming, usage metrics |
| `start_demo.sh` | one-click demo studio launcher |
| `tests/` | unit tests (`pytest packages/harness/runtime/tests -q`) |

See the [Quick Start](../../../QUICKSTART.md) to run it. Runtime and provider errors
are surfaced as typed failures, with task-specific recovery guidance added to the
model context when applicable.

The standalone [OpenGCUA macOS app](../../macos/README.md) uses
`native_host.py` to drive this same agent loop through private process pipes.
Its pause/reply/cancel lifecycle does not depend on the demo studio web server.
