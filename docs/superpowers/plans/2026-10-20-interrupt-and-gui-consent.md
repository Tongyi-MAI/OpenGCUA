# Interrupt and GUI Consent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ctrl-C safely interrupt one turn while preserving task context and GUI approval, require one scoped approval before visual or foreground SDK actions, remove point time expiry, and correct AX completeness guidance.

**Architecture:** The CLI owns a cancellable turn and a task/window grant store. The worker pauses before protected SDK dispatch and asks the host over its existing control socket for a decision; the host renders the approval request and acknowledges it. Point references keep identity/geometry checks but no wall-clock limit, and user-interaction boundaries invalidate prior points without revoking approval.

**Tech Stack:** Python 3.11+, asyncio, prompt_toolkit, pytest, Rust/Cargo SDK runtime and macOS adapter.

**Spec:** `docs/superpowers/specs/2026-10-20-interrupt-and-gui-consent-design.md`

## Global Constraints

- First Ctrl-C stops the current turn and returns to input after at most a two-second cooperative grace period plus worker teardown; second Ctrl-C skips the grace period.
- Ctrl-C preserves task identity, conversation history, and existing GUI approval. Plain follow-up text corrects the same task; explicit `/new`, `terminate`, or session end revokes approval.
- Approval covers all SDK `vision.click` calls, including background delivery, plus foreground pointer/keyboard delivery and window activation. One approval permits a sequence on the same task/app/window; `ask_user` does not revoke it.
- No protected SDK action is dispatched while approval is pending or denied. Background AX reads/actions remain usable without approval.
- Remove the wall-clock point-age feature from unified and legacy SDK APIs. Keep capture scope, identity, and geometry checks; invalidate old points after `ask_user` and Ctrl-C.
- The agent runs arbitrary Python/Bash: SDK approval is not an OS sandbox. Do not claim otherwise in help or docs.

## Review Focus

- Ctrl-C arrives while a worker is blocked in a native call: the old process must be gone before a correction can execute (Task 1 test).
- Ctrl-C arrives during an unanswered approval prompt: no grant or protected action is created, while any earlier grant remains (Task 2 test).
- An approved action targets a different window or a reused window ID under another app: require a new approval (Task 2 test).
- The user changes foreground focus mid-sequence: refuse the next foreground action without revoking approval or clicking the unrelated app (Task 3 test).
- A point survives beyond 120 seconds but crosses `ask_user` or Ctrl-C: time alone is allowed, the user boundary is not (Task 4 test).

---

### Task 1: Bounded turn interruption and same-task correction

**Files:** Modify `packages/harness/runtime/terminal_tui.py`, `mac_agent.py`, `code_executor.py`; test `packages/harness/runtime/tests/test_terminal_tui.py`, `test_agent_frontend.py`; create `packages/harness/runtime/tests/test_turn_interrupt.py`.

**Interfaces:** `with_abort(coro, *, force_stop: Callable[[], Awaitable[None]] | None = None) -> Any` in `mac_agent.py`; `CodeExecutor.interrupt_active(self, *, force: bool = False) -> None`; TUI abort callback accepts `force: bool` (`False` first Ctrl-C, `True` thereafter).

- [ ] Write tests for first/second Ctrl-C, prompt-time Ctrl-C, planning cancellation, a cancellation-resistant worker, no child left running, and follow-up text retained under the same `task_index` without `begin_task`/`set_task`.
- [ ] Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_terminal_tui.py packages/harness/runtime/tests/test_turn_interrupt.py packages/harness/runtime/tests/test_agent_frontend.py -q`; expect the new tests to fail.
- [ ] Implement bounded cancellation: first signal sets interrupting state and cancels the active model/worker task; after two seconds or second Ctrl-C, force-stop only the owned worker group. Do not return a usable next-turn prompt until worker teardown is confirmed. Preserve the current task/context/grant in `StepAborted`; append plain follow-up as the same-task user event. Make prompt-time Ctrl-C clear input and reprompt.
- [ ] Rerun the three test files; expect pass. Also run a harmless worker-loop PTY smoke test that sends Ctrl-C, enters a correction, and verifies the old PID is gone before the correction starts.
- [ ] Commit the Task 1 code and tests.

### Task 2: Host-owned task/window grant and approval protocol

**Files:** Create `packages/harness/runtime/gui_approval.py`; modify `mac_agent.py`, `code_executor.py`, `code_runtime.py`; test `packages/harness/runtime/tests/test_agent_frontend.py`, `test_overlay_guard.py`; create `packages/harness/runtime/tests/test_gui_approval.py`.

**Interfaces:** `GuiGrantStore.allowed(task_index: int, app_id: str, window_id: str) -> bool`, `.grant(...) -> None`, `.clear() -> None`; `CodeExecutor(..., approval_handler: Callable[[dict], Awaitable[bool]] | None = None)`; worker `gui_approval_request`/host `gui_approval_ack` frames carrying one request ID and the exact app/window scope.

- [ ] Write failing tests: first protected action pauses before dispatch; one affirmative answer authorizes multiple same-window actions; `ask_user` and Ctrl-C preserve the grant; `terminate`, `/new`, session close, or different app/window do not; denial, timeout, malformed ack, or Ctrl-C during a pending request dispatch nothing. Include a reused window ID with a different app ID.
- [ ] Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_gui_approval.py packages/harness/runtime/tests/test_overlay_guard.py packages/harness/runtime/tests/test_agent_frontend.py -q`; expect the new tests to fail.
- [ ] Add the grant store and an approval handler in the host. Extend the worker control channel using the existing overlay request/ack pattern; serialize readers so overlay and approval acks cannot consume each other. The host question names app/window, visual clicking, possible real foreground input, and purpose; only an explicit affirmative answer grants. Default to deny if no interactive answer path exists.
- [ ] Rerun the test files; expect pass. Commit the Task 2 code and tests.

### Task 3: SDK dispatch gate and target-safe foreground delivery

**Files:** Modify `packages/harness/runtime/code_runtime.py`, `mac_agent.py`, `packages/sdk/adapters/macos/src/provider/actions.rs`; test `packages/harness/runtime/tests/test_sdk_runtime.py`, `packages/sdk/adapters/macos/src/provider.rs` (Rust module tests).

**Interfaces:** `gui_action_scope(device, message) -> tuple[str, str, bool, bool] | None` in `code_runtime.py`, returning `(app_id, window_id, needs_vision, needs_foreground)`; Task 2's request/ack protocol is called before `session._request` dispatch.

- [ ] Write failing tests covering `point_click` for both delivery modes; `activate`, foreground `input_text`/`press_key`/drag/move; background AX `click`/`set_text` allowed without approval; missing exact target refused; changed focus or mismatched app/window refused before pointer/keyboard delivery.
- [ ] Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_sdk_runtime.py -q` and `cargo test --manifest-path packages/sdk/Cargo.toml -p mu-adapter-macos`; expect new tests to fail.
- [ ] Classify protected calls at the worker's `session._request` wrapper and request Task 2 approval before the overlay/native call. For macOS foreground actions, require exact pid/window targeting and native verification of the intended target at dispatch; if the available driver route cannot verify it, return `INPUT_REFUSED` rather than operating on the current foreground app. Keep the grant on focus refusal. Align the system prompt/help with runtime behavior and state the arbitrary-code bypass limit.
- [ ] Rerun both commands; expect pass. Commit Task 3.

### Task 4: Remove time-based point expiry; invalidate on user boundaries

**Files:** Modify `packages/sdk/crates/runtime/src/observations.rs`, `packages/sdk/crates/host/src/lib.rs`, `packages/sdk/crates/bindings-python/src/lib.rs`, `packages/sdk/python/mai_ui/unified/_capture.py`, `observations.py`, `ui.py`, `api.pyi`, `packages/sdk/python/mai_ui/runtime.py`, `packages/harness/runtime/mac_agent.py`, `code_executor.py`, `code_runtime.py`; test Rust observation tests and `packages/harness/runtime/tests/test_sdk_runtime.py`.

**Interfaces:** `ObservationStore::invalidate_points(scope: Option<&str>)`; host state operation `invalidate_points`; `Session.invalidate_points()` in unified Python; `CodeExecutor.invalidate_points_next_request()` for the next worker request after `ask_user` or Ctrl-C.

- [ ] Add failing Rust/Python tests using an injected clock rather than a 120-second sleep: elapsed time alone leaves a point usable in the same scope; wrong scope/window/geometry still fails; `ask_user`/Ctrl-C invalidates prior points but not task context or GUI grant; legacy API has no age comparison. Update signature tests to reject the removed `max_age_ms` keyword.
- [ ] Run `cargo test --manifest-path packages/sdk/Cargo.toml -p mu-runtime` and `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_sdk_runtime.py -q`; expect the new tests to fail.
- [ ] Remove point age fields/arguments/checks in Rust, bindings and both Python APIs; update internal swipe/drag callers, stubs and help. Add explicit point invalidation at the user boundaries and feedback that says to reobserve a point, not rebind a healthy window.
- [ ] Rerun both commands and SDK Python import checks; expect pass. Commit Task 4.

### Task 5: Truthful macOS AX status and better editable-node discovery

**Files:** Modify `packages/sdk/adapters/macos/src/provider/observation.rs`, `packages/harness/runtime/mac_agent.py`; test `packages/sdk/adapters/macos/src/provider.rs` (Rust module tests), `packages/harness/runtime/tests/test_sdk_runtime.py`.

**Interfaces:** Preserve native AX row roles/labels; derive observation completeness from `elements_complete` and `truncated`, not the requested `tree` flag. The prompt explicitly includes `AXTextArea`, `AXTextField`, `AXSearchField`, and `AXComboBox`.

- [ ] Add failing tests for complete versus incomplete native trees, unchanged element rows, and the prompt's four editable roles without implying DingTalk must expose a search field.
- [ ] Run `cargo test --manifest-path packages/sdk/Cargo.toml -p mu-adapter-macos` and `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_sdk_runtime.py -q`; expect the new tests to fail.
- [ ] Correct `truncated`/partial status and update prompt guidance. Do not synthesize missing DingTalk nodes or use foreground as an automatic response to missing AX search input.
- [ ] Rerun both commands; expect pass. Commit Task 5.

### Final verification

- [ ] Run all harness runtime tests, `cargo test --manifest-path packages/sdk/Cargo.toml -p mu-runtime -p mu-adapter-macos`, and `git diff --check`.
- [ ] Repeat the harmless CLI Ctrl-C and consent smoke tests, verify no orphan worker or unexpected foreground input, and inspect the resulting trajectory for preserved task context and grant scope. Do not send DingTalk messages during verification.
- [ ] Review the whole branch against the spec, report any remaining SDK-only enforcement limitation, and hand off the commits.
