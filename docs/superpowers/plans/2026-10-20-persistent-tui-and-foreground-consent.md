# Persistent TUI and Task-Level Foreground Consent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a usable terminal input box throughout agent work, make Ctrl-C interrupt one turn, and obtain one explicit, task-level choice before foreground SDK input.

**Architecture:** A prompt_toolkit application owns the terminal for the session and exchanges typed input/approval events with the agent loop. A small input-state component handles queued messages and abort suspension; the existing SDK control channel carries foreground requests to a host-owned task consent store. The worker cannot dispatch a protected action until the host has acknowledged a current request ID.

**Tech Stack:** Python 3.11+, asyncio, prompt_toolkit 3, pytest, macOS mai-ui SDK/CuaDriver.

**Spec:** `docs/superpowers/specs/2026-10-20-persistent-tui-and-foreground-consent-design.md`

## Global Constraints

- `--ui tui` has a scrollable transcript above a fixed input box; `--ui plain` stays line-oriented and Demo Hub is not redesigned.
- Enter during an active turn queues messages FIFO; they auto-send only after normal completion. An abort suspends the queue until `/queue send`; `/queue` inspects and `/queue clear` discards it.
- Ctrl-C interrupts the current turn, retains its task/context/grant, and never dispatches a correction until the old worker stops. A second Ctrl-C force-stops the owned worker; already-sent actions do not roll back.
- Consent is triggered by `delivery="foreground"` or window activation, not by background `vision.click`. Allow is task-wide until terminate, `/new`, or session exit; Reject is sticky until an explicit re-request.
- Approval defaults to Reject and only an explicit selection for the current request ID grants it. Model text and queued messages are never consent. Foreground actions still require an exact target and pre-dispatch focus verification.
- The gate protects routed SDK calls, not arbitrary Python/Bash/AppleScript or application-initiated window focus. Do not describe it as OS-wide isolation.

## Review Focus

- A long model stream arrives while the user edits a multiline draft: output must not corrupt the draft or reflow code; test in Task 3.
- An agent asks a question while messages are queued: neither the queue nor the draft may become the answer; test in Task 2 and Task 3.
- A rejected foreground call is repeated in a loop: it must not redisplay consent or dispatch the action; test in Task 1.
- Ctrl-C lands during an unanswered permission choice or a resistant worker: no grant, no orphan, same-task correction; test in Task 4.
- A stale approval acknowledgement or focus change occurs after Allow: no unrelated-window input; test in Task 1 and Task 4.

## File responsibilities

- `packages/harness/runtime/gui_approval.py`: task consent state and choice semantics, independent of UI rendering.
- `packages/harness/runtime/code_runtime.py` and `code_executor.py`: classify foreground calls, carry request/ack IDs, block denied dispatch, expose explicit re-request.
- `packages/harness/runtime/terminal_interaction.py` (new): pure queue/draft/turn state and transition rules.
- `packages/harness/runtime/terminal_tui.py`: persistent prompt_toolkit layout, event rendering, keybindings, and approval choice; no grant-policy decisions.
- `packages/harness/runtime/agent_events.py` and `mac_agent.py`: typed frontend/agent-loop boundary and lifecycle wiring; `pyproject.toml` lists the new module.
- Focused tests live under `packages/harness/runtime/tests/`; `README.md` and `QUICKSTART.md` describe the finished terminal behavior and policy limitations.

---

### Task 1: Foreground-only, task-scoped SDK consent

**Files:** Modify `packages/harness/runtime/gui_approval.py`, `agent_events.py`, `code_runtime.py`, `code_executor.py`, `mac_agent.py`; test `packages/harness/runtime/tests/test_gui_approval.py`, `test_sdk_runtime.py`, `test_overlay_guard.py`.

**Interfaces:** `ConsentChoice = Literal["allow", "reject", "cancel"]` and `ForegroundApprovalPrompt(request_id: str, text: str, task_index: int, app_id: str, window_id: str)` in `agent_events.py`; `GuiGrantStore.state(task_index: int) -> Literal["unset", "allowed", "denied"]`, `.decide(task_index: int, *, allow: bool) -> None`, `.clear() -> None`; `gui_action_scope(device: Any, message: dict[str, Any]) -> tuple[str, str] | None`; `request_gui_approval(grants, task_index, event, choose: Callable[[ForegroundApprovalPrompt], Awaitable[ConsentChoice]]) -> bool`. Worker control frames retain `request_id`, `app_id`, `window_id` and add `explicit_retry: bool` and optional `reason`; host emits `AgentEvent(kind="foreground_consent", payload={"state": state})` after a decision or clear.

- [ ] **Step 1: Write failing policy and dispatch tests.** Assert background `point_click`/AX calls yield no approval scope; foreground `point_click`, `input_text`, drag, desktop input, and `activate` do. Assert one Allow covers multiple windows in task 4 but not task 5; Reject blocks repeated attempts without another choice; `explicit_retry=True` reopens choice only after Reject; malformed scope/request ID and focus mismatch dispatch nothing.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_gui_approval.py packages/harness/runtime/tests/test_sdk_runtime.py packages/harness/runtime/tests/test_overlay_guard.py -q`; expect the new assertions to fail.
- [ ] **Step 3: Implement the consent model.** Add Task 1's types and task-indexed `GuiGrantStore` states. Call `.decide` only for explicit Allow/Reject; Cancel leaves the prior state unchanged.
- [ ] **Step 4: Implement foreground classification.** Change `code_runtime.gui_action_scope` to return a scope only for `delivery="foreground"` or `activate`; retain exact-target checks before dispatch.
- [ ] **Step 5: Implement the host/worker protocol.** Keep the request/ack channel and request-ID matching. Add `async def request_foreground_permission(window: Any, reason: str) -> bool` to the worker namespace as an explicit retry for a bound App/window; the host refuses a retry unless state is `denied`. Until Task 4, adapt the existing textual frontend response to `ConsentChoice` in `mac_agent`. Emit consent-state events on decision/clear. Return `PERMISSION_DENIED` with background/AX and explicit-retry guidance without dispatching a denied action.
- [ ] **Step 6: Verify green.** Rerun the Step 2 command; expect pass. Run `git diff --check`; expect no whitespace errors.
- [ ] **Step 7: Commit.** Stage only this task's code/tests and commit `feat: gate foreground SDK input by task consent`.

### Task 2: Deterministic input queue and typed choices

**Files:** Create `packages/harness/runtime/terminal_interaction.py`, `packages/harness/runtime/tests/test_terminal_interaction.py`; modify `packages/harness/runtime/agent_events.py`, `pyproject.toml`; test `packages/harness/runtime/tests/test_agent_frontend.py`.

**Interfaces:** `TerminalInteraction.submit(text: str) -> Literal["send", "queued", "ignored"]`, `.begin_turn() -> None`, `.finish_turn(*, normal: bool) -> None`, `.next_message() -> str | None`, `.queued_messages() -> tuple[str, ...]`, `.suspend_queue() -> None`, `.resume_queue() -> None`, `.clear_queue() -> None`; `AgentFrontend.choose_approval(prompt: ForegroundApprovalPrompt) -> Awaitable[ConsentChoice]` consumes Task 1's types, with a textual fallback in `PlainFrontend`.

- [ ] **Step 1: Write failing state tests.** Assert Enter during a turn queues FIFO; normal completion makes the next queued message available; abort suspends the queue; a new correction can send while suspended; queue inspection, resume, and clear have deterministic effects. Assert question/approval contexts never consume queued task messages or a preserved draft, and a malformed/absent choice is `cancel`, never `allow`.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_terminal_interaction.py packages/harness/runtime/tests/test_agent_frontend.py -q`; expect the new tests to fail.
- [ ] **Step 3: Implement `TerminalInteraction`.** Keep queue transitions UI-independent and messages in memory only; a `"send"` result lets the frontend fulfill its pending read with the submitted text, whereas `.next_message()` yields only a ready queued item.
- [ ] **Step 4: Add the typed frontend method.** Extend `AgentFrontend`; keep plain mode sequential and require an explicit textual affirmative. Add `terminal_interaction` to `pyproject.toml` modules.
- [ ] **Step 5: Verify green.** Rerun the Step 2 command; expect pass.
- [ ] **Step 6: Commit.** Stage only this task's code/tests and commit `feat: model terminal input queue and consent choices`.

### Task 3: Persistent TUI rendering and input

**Files:** Modify `packages/harness/runtime/terminal_tui.py`, `packages/harness/runtime/tests/test_terminal_tui.py`; create `packages/harness/runtime/tests/test_terminal_tui_pty.py`.

**Interfaces:** `RichTerminalFrontend.start() -> None` schedules the application on the active loop; `async def RichTerminalFrontend.stop() -> None` exits and joins it; `emit(event: AgentEvent) -> None`, `read(prompt: AgentPrompt) -> Awaitable[str | None]`, and `choose_approval(prompt: ForegroundApprovalPrompt) -> Awaitable[ConsentChoice]` consume Task 2's state. `bind_abort(callback: Callable[[bool], None])` retains the current abort bridge. `build_rich_frontend()` remains the factory used by `mac_agent.main`.

- [ ] **Step 1: Write failing UI/PTY tests.** In a pseudo-terminal, stream `intent_delta` and `code_delta` while typing a multiline draft; assert a fixed input region remains usable, model newlines are preserved, and hidden reasoning/code stay hidden by default. Use arrow/Tab then Enter to Allow, plain Enter to Reject, and Ctrl-C to cancel; assert ordinary chat text and queued messages cannot approve. Test consent-state status, terminal resize, small terminal, EOF with queued-message warning, and payload-redaction behavior.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_terminal_tui.py packages/harness/runtime/tests/test_terminal_tui_pty.py -q`; expect the new tests to fail.
- [ ] **Step 3: Build the full-session layout.** Replace the transient `PromptSession` with one prompt_toolkit `Application`: scrollable transcript, status line, fixed input, and a separate Allow/Reject choice, with Reject initially selected.
- [ ] **Step 4: Route stream events.** Paint only from the UI task via a serialized redraw queue, preserving model line breaks, intent/code streaming, hidden-by-default reasoning/code, and text-only handling of untrusted payloads.
- [ ] **Step 5: Bind input and lifecycle keys.** Use Task 2's state for Enter and `/queue` commands; bind Ctrl-C in every UI phase with first soft-abort/second force-stop semantics. Preserve `/reasoning`, `/code`, `/help`, `/exit`, and the pending-message exit warning.
- [ ] **Step 6: Verify green.** Rerun the Step 2 command; expect pass. Inspect a harmless PTY transcript to confirm no output fragment is inserted into the draft.
- [ ] **Step 7: Commit.** Stage only this task's code/tests and commit `feat: keep terminal input available during agent turns`.

### Task 4: Agent-loop integration, prompt, and end-to-end verification

**Files:** Modify `packages/harness/runtime/mac_agent.py`, `packages/harness/runtime/tests/test_agent_frontend.py`, `test_gui_approval.py`, `test_terminal_tui_pty.py`, `README.md`, `QUICKSTART.md`.

**Interfaces:** `async def run_interactive_agent(args, frontend: RichTerminalFrontend) -> int` starts the UI before `run_agent`, stops it in `finally`, and preserves the existing `run_agent` API. `mac_agent.approve_gui` passes `ForegroundApprovalPrompt` to `frontend.choose_approval`; `request_gui_approval` remains the only grant decision-maker.

- [ ] **Step 1: Write failing integration tests.** Exercise real PTY Ctrl-C during planning, worker execution, ordinary `ask_user`, and approval; verify a resistant child is stopped before a correction runs in the same task. Verify normal completion drains queued messages FIFO, abort suspends them, `/queue send` resumes, and task completion clears consent. Verify a rejected foreground action is not dispatched, does not reprompt on repeat, and explicit `await request_foreground_permission(win, reason)` is the only re-request path.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_agent_frontend.py packages/harness/runtime/tests/test_gui_approval.py packages/harness/runtime/tests/test_terminal_tui_pty.py -q`; expect the new tests to fail.
- [ ] **Step 3: Wire the agent lifecycle.** Start/stop the TUI around `run_agent`, move TUI cleanup out of the old inner close path, route approval to the typed choice, and make `StepAborted` return to the same session without consuming queued messages or clearing a grant.
- [ ] **Step 4: Align prompt and docs.** Remove the system-prompt instruction to pre-ask via `ask_user`; retain background-first guidance and host-gate explanation. Update README/QUICKSTART keybindings, task-level consent wording, background-click behavior, and SDK-only limitation.
- [ ] **Step 5: Verify green and broader regressions.** Rerun the Step 2 command, then run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests -q`; expect pass. Run `git diff --check`; expect no whitespace errors.
- [ ] **Step 6: Commit.** Stage only this task's code/tests/docs and commit `feat: integrate persistent TUI and foreground consent`.

### Final verification and handoff

- [ ] Run the complete harness suite and a harmless local pseudo-terminal smoke test, including both Ctrl-C presses and one Allow/Reject cycle. Do not operate real DingTalk or user documents.
- [ ] Confirm `--ui plain` still works for redirected I/O and the new interactive TUI fails clearly when forced without a terminal.
- [ ] Review the implementation against the spec, verify no orphan worker or unexpected foreground action, and report the SDK-only protection limit and any remaining browser/App focus behavior.
