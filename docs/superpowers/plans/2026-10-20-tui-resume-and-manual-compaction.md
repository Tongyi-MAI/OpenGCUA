# TUI Resume and Manual Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `/resume` and `/compaction` to the persistent TUI without replaying old tasks, crossing session queues, or replacing context on a failed summary.

**Architecture:** Route typed terminal control requests through the existing input boundary, never through model chat. A persistent TUI supervisor closes one agent runtime before opening a selected saved run and waits for a new message; manual compaction invokes the shared summary engine at a safe input boundary with an explicit trigger distinct from server-limit recovery.

**Tech Stack:** Python 3.11+ (3.12 recommended), asyncio, prompt_toolkit, pytest, existing trajectory JSON and `llm_compaction.py`.

**Spec:** `docs/superpowers/specs/2026-10-20-tui-resume-and-manual-compaction-design.md`

## Global Constraints

- Keep one TUI application alive; `/resume` selection is read-only until a new message starts a new task. Never replay the saved task automatically.
- Close the previous executor, RPC bridge, desktop provider, model client, and writer before another run starts. Python variables, SDK objects, UI handles, and foreground consent do not transfer.
- Commands entered during a turn wait in input order for a safe task boundary; abort-suspended queues remain suspended. Neither command answers `ask_user` or grants GUI permission.
- A successful manual summary is used even with automatic `llm_compaction` disabled; that setting remains disabled for automatic triggers. Manual failure/cancel retains the old summary, cutoff, micro cutoff, and raw turns.
- Keep existing `--resume` without a new task compatible, and keep `--ui plain` line-oriented. Do not redesign Demo Hub.

## Review Focus

- A run path contains spaces or a symlink to an incompatible trajectory: preflight must validate the resolved target and not close the current run; test in Task 2.
- `/resume` is entered while an agent question or GUI permission is pending: it must not answer or approve either prompt; test in Task 3.
- A queued message follows `/resume`: it must remain visible but suspended after the switch, never auto-send to the selected run; test in Task 3.
- Auto compaction is disabled when `/compaction` succeeds: the resulting summary must still appear in the next model input and survive resume; test in Tasks 4–5.
- Ctrl-C cancels a manual model request after micro-compaction advanced: all old compaction state must be restored and no queued task auto-sent; test in Task 5.

## File responsibilities

- `packages/harness/runtime/terminal_interaction.py`: ordered task/control queue with abort suspension; no UI rendering.
- `packages/harness/runtime/terminal_tui.py`: slash completion, recent-run selector, visible command/compaction status, keyboard actions.
- `packages/harness/runtime/resume_catalog.py` (new): read-only recent-run discovery and target preflight; owns the existing UI-API compatibility check after it moves out of `mac_agent.py`, with the old import name re-exported there.
- `packages/harness/runtime/agent_events.py`: typed `TerminalCommand` input contract shared with the agent loop.
- `packages/harness/runtime/mac_agent.py`: safe control boundary, run teardown/rebuild supervisor, new-task resume branch, and manual-summary persistence.
- `packages/harness/runtime/llm_compaction.py`: explicit manual trigger and atomic state semantics; context construction in `mac_agent.py` uses any valid committed summary independently of the auto-trigger flag.
- Tests under `packages/harness/runtime/tests/`; README and QUICKSTART explain both commands and limitations.

---

### Task 1: Typed task/control queue

**Files:** Modify `packages/harness/runtime/agent_events.py`, `terminal_interaction.py`; test `packages/harness/runtime/tests/test_terminal_interaction.py`.

**Interfaces:** `TerminalCommand(kind: Literal["resume", "compaction"], target: str | None = None)` is a frozen dataclass. `TerminalInteraction.submit_control(command: TerminalCommand) -> Literal["send", "queued"]`, `.next_item() -> str | TerminalCommand | None`, and `.queued_items() -> tuple[str | TerminalCommand, ...]` preserve one FIFO; existing `.submit()` and `/queue` behavior remain compatible. Task 1 does not expose slash commands in the UI yet, so the shipped TUI behavior remains unchanged until Task 3.

- [ ] **Step 1: Write failing tests.** Assert `"task A"`, `TerminalCommand("compaction")`, `"task B"` are returned in that order; an abort suspends all three; resuming releases them in order and clearing removes them without changing the active draft.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest -q packages/harness/runtime/tests/test_terminal_interaction.py`; expect new tests to fail for missing typed queue operations.
- [ ] **Step 3: Implement the interfaces.** Keep existing string-only `next_message()` compatibility, but never let it consume a control item; Task 3 will use `next_item()` at task boundaries. Do not parse slash commands or change frontend return types yet.
- [ ] **Step 4: Verify green and commit.** Rerun Step 2; expect all pass. Run `git diff --check`; stage only Task 1 files and commit `feat: queue typed terminal control commands`.

### Task 2: Resume catalog and selector

**Files:** Create `packages/harness/runtime/resume_catalog.py`, `packages/harness/runtime/tests/test_resume_catalog.py`; modify `terminal_tui.py`, `mac_agent.py`, `packages/harness/runtime/pyproject.toml`; test `test_terminal_tui.py`, `test_terminal_tui_pty.py`.

**Interfaces:** `ResumeCandidate(path: Path, session_id: str, last_task: str, status: str, updated_at: str, turns: int, todos: int)`; `inspect_resume_run(path: Path, *, ui_api: str) -> ResumeCandidate` raises `ResumeError`; `recent_resume_runs(root: Path, *, current_run: Path | None, ui_api: str, limit: int = 10) -> tuple[ResumeCandidate, ...]`. `RichTerminalFrontend.choose_resume(candidates: tuple[ResumeCandidate, ...]) -> Awaitable[ResumeCandidate | None]` handles Up/Down, Enter, Esc/Ctrl-C.

- [ ] **Step 1: Write failing tests.** Build literal `traj.json` fixtures: newest-first top ten, current run excluded, malformed/unsupported entries skipped, empty list reported, explicit path outside the discovery root (including spaces) accepted, symlink to incompatible target rejected. In a 30-column PTY, verify selector remains visible, arrows change selection, Esc and Ctrl-C cancel, and no task is sent.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest -q packages/harness/runtime/tests/test_resume_catalog.py packages/harness/runtime/tests/test_terminal_tui_pty.py`; expect missing catalog/selector tests to fail.
- [ ] **Step 3: Implement read-only catalog.** Move `UI_API_MARKERS` and `validate_trajectory_ui_api` to the catalog module and re-export them from `mac_agent.py`; add `inspect_resume_run` and `recent_resume_runs` without writer construction.
- [ ] **Step 4: Implement selector.** Show time/task/status and full path confirmation in the composer; selector input never appends `resume_history` or starts an agent.
- [ ] **Step 5: Verify green and commit.** Rerun Step 2; expect pass. Run `git diff --check`; stage only Task 2 files and commit `feat: select validated TUI resume runs`.

### Task 3: Runtime resume handoff

**Files:** Modify `packages/harness/runtime/mac_agent.py`, `agent_events.py`, `terminal_interaction.py`, `terminal_tui.py`; test `packages/harness/runtime/tests/test_agent_frontend.py`, `test_terminal_interaction.py`, `test_terminal_tui.py`, `test_terminal_tui_pty.py`.

**Interfaces:** `ResumeHandoff(path: Path)` is a typed result from `run_agent` only after a TUI control request reaches a task boundary; ordinary `run_agent` results remain integer exit codes. `AgentFrontend.read(AgentPrompt(kind="task"))` may return Task 1's `TerminalCommand` only for TUI task-input boundaries; question/retry/approval remain text or typed choices. `run_interactive_agent(args, frontend)` keeps one TUI alive, preflights before teardown, waits for `AgentPrompt(kind="task")` after selection, then calls `run_agent` with the selected `resume` path and new `task` message. `--resume` without `task` keeps legacy CLI behavior; with `task`, it begins a fresh task boundary in restored history.

- [ ] **Step 1: Write failing integration tests.** Assert slash completion includes both commands and `/resume <path with spaces>` preserves the full path. Assert commands typed during `question` or approval cannot answer those prompts. Select a saved run, verify no write to its `traj.json` before the new message, then assert new task/context/TODO append without executing the old task. Verify previous runtime closes before new startup, task consent is unset, old Python/UI handles are not represented as live, queued post-switch messages stay suspended, and incompatible or changed-between-preflight-and-load targets leave TUI input available. Add a regression for legacy `--resume` without `task`.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest -q packages/harness/runtime/tests/test_agent_frontend.py packages/harness/runtime/tests/test_terminal_tui.py`; expect handoff/new-task assertions to fail.
- [ ] **Step 3: Wire typed task-input boundaries.** Parse exact slash names and the full `/resume` remainder in the TUI, return `TerminalCommand` only from task reads, and render queued/blocked status without consuming question or approval answers.
- [ ] **Step 4: Add agent handoff.** Preflight before teardown, return `ResumeHandoff` after normal teardown, and make a supplied `--resume` task create a new task boundary while no-task CLI resume remains unchanged.
- [ ] **Step 5: Keep the TUI across runs.** Loop in `run_interactive_agent`, wait for a new message after selection, revalidate at load, render restored metadata, and keep input available with the previous run identified if load fails.
- [ ] **Step 6: Verify green and commit.** Rerun Step 2 plus `test_terminal_tui_pty.py`; expect pass. Run `git diff --check`; stage only Task 3 files and commit `feat: resume saved context from persistent TUI`.

### Task 4: Manual compaction policy and atomicity

**Files:** Modify `packages/harness/runtime/llm_compaction.py`, `mac_agent.py`; create `packages/harness/runtime/tests/test_manual_compaction.py`.

**Interfaces:** Extend `maybe_compact(context, client, *, current: dict, action_max_tokens: int, trigger: Literal["auto", "server_limit", "manual"] = "auto") -> dict | None`; migrate existing `force` call sites to `trigger="server_limit"` without changing that recovery behavior. Add `ContextManager` summary-active handling so a valid committed manual summary is used even when `compaction_config.enabled` is false; auto triggering remains disabled.

- [ ] **Step 1: Write failing tests.** A below-threshold context with at least two eligible turns calls the summary model once for `trigger="manual"`; auto does not. Auto-disabled manual success appears in the next built model input and restores with digest; fewer than two turns reports insufficient history without a model call. Transport failure, invalid XML, no strict token reduction, and cancellation leave prior summary/cutoffs/raw turns unchanged. Assert server-limit trigger retains its existing behavior.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest -q packages/harness/runtime/tests/test_manual_compaction.py`; expect manual-trigger tests to fail.
- [ ] **Step 3: Implement the manual trigger.** Keep whole-record payload, digest validation, and strict reduction; use `trigger="manual"` to bypass only the threshold and auto-enable checks.
- [ ] **Step 4: Make failure atomic.** Do not commit a candidate summary on failure; restore the old micro cutoff on failed/cancelled manual attempts.
- [ ] **Step 5: Use committed summaries independently of automatic triggering.** Update context rendering and resume restoration so an auto-disabled session still uses a validated manual summary without enabling automatic compaction.
- [ ] **Step 6: Verify green and commit.** Rerun Step 2 plus `test_gui_input_safety.py`; expect pass. Run `git diff --check`; stage only Task 4 files and commit `feat: allow explicit atomic LLM compaction`.

### Task 5: Manual command lifecycle, docs, and full verification

**Files:** Modify `packages/harness/runtime/mac_agent.py`, `terminal_tui.py`, `README.md`, `QUICKSTART.md`; test `packages/harness/runtime/tests/test_agent_frontend.py`, `test_terminal_tui_pty.py`, `test_manual_compaction.py`.

**Interfaces:** At a `TerminalCommand(kind="compaction")` task boundary, `run_agent` calls Task 4's manual trigger using the existing model client and writer, emits `phase="compaction"`, persists a completed attempt in `traj.json`, and returns to the same task-input boundary without sending a model action. Ctrl-C cancels only this summary request; the TUI remains open and the queue does not auto-drain after cancellation.

- [ ] **Step 1: Write failing integration tests.** In a fake model/client run, `/compaction` below threshold emits visible Summarizing state and persists revision/cutoff/digest/attempt metrics; no action code runs. Test queued-after-turn execution, no task auto-dispatch on Ctrl-C, prior state retained on failure/cancel, and a later `/resume` reloads the successful summary.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest -q packages/harness/runtime/tests/test_agent_frontend.py packages/harness/runtime/tests/test_terminal_tui_pty.py packages/harness/runtime/tests/test_manual_compaction.py`; expect new lifecycle assertions to fail.
- [ ] **Step 3: Add the safe-boundary command handler.** Invoke one manual summary using the existing model client, cancel it on Ctrl-C without draining the queue, and persist only a completed summary through the existing writer.
- [ ] **Step 4: Render outcomes and document behavior.** Show completed/failed/insufficient-history status without claiming success on error; document both commands, queue semantics, no-auto-replay, and non-restored Python/UI state in README/QUICKSTART.
- [ ] **Step 5: Verify green and commit.** Rerun Step 2; expect pass. Run `PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest -q` and `git diff --check`; expect no failures or whitespace errors. Stage only Task 5 files and commit `feat: expose resume and compaction in terminal UI`.

## Final handoff

- [ ] In a harmless PTY smoke test, navigate `/resume`, cancel it, select a fixture run, wait for a new message, and verify the old task did not execute. Trigger `/compaction` below threshold and confirm the status/result are visible.
- [ ] Review both commands against the spec, inspect final diff and trajectory state, and report any limitation rather than inferring success from UI text alone.
