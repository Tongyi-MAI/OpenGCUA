# TUI session resume and manual LLM compaction

Date: 2026-10-20

## Goal and user contract

Add `/resume` and `/compaction` to the persistent terminal UI. `/resume` lists recent saved runs for selection, and `/resume <run directory>` targets one directly. Selection restores conversation context and TODO state but does not execute the saved task. The UI shows what will be restored, then waits for a **new user message** before starting an agent turn. Python variables, SDK objects, and UI/window handles are not restored. `/compaction` explicitly attempts one LLM history summary without waiting for the automatic token threshold. Neither command is model-authored task text.

Both commands are accepted while an agent turn is running but execute only at a safe turn boundary. The TUI visibly marks them queued. They do not interrupt a model request, code cell, or foreground action. The existing Ctrl-C behavior remains the way to interrupt a turn.

## Approach

Keep one `RichTerminalFrontend` and its terminal application alive. At a resume boundary, finish the current `run_agent` lifecycle normally, closing its executor, RPC bridge, desktop provider, model client, and writer before starting another run. Reuse the existing `--resume` trajectory loader for the selected directory. Do not transplant a `ContextManager` into a live executor or spawn a replacement terminal process. The interactive supervisor needs a typed handoff result distinguishing session exit from resume selection; ordinary task messages and control commands must not be confused.

For a TUI resume, the supervisor waits for a new message **before** invoking the resumed `run_agent`. That message starts a new task in the selected trajectory; the restored task is never replayed automatically. Extend the non-host resume path to honor this supplied new task, while preserving existing `--resume` without a task for users who intentionally resume the saved task from the command line. The selected run directory is reused and appended to, as with `--resume` today. Each new task gets fresh Python/SDK state and fresh foreground-consent state.

## Command routing and queue

Parse slash commands in the TUI before ordinary messages enter the model-facing queue. Represent pending task messages and pending control commands as distinct typed items in one FIFO so submission order remains visible and deterministic. A command entered during a turn is labelled queued. At the next normal task-input boundary, process earlier queued items first. A queued `/resume` opens the selector only when reached; a queued `/compaction` runs only when reached. An abort keeps the existing queue suspended; `/queue send` resumes it and `/queue clear` explicitly discards it.

Selection of a different run does not silently carry later, unsent messages into that run. Those messages remain visible and suspended as old-session drafts/queue items until the user explicitly sends or clears them. The UI must not silently delete them on switch or program exit. A pending command is not a user answer to `ask_user` or a GUI-permission choice.

## Resume selection and validation

`/resume` shows up to ten most recently modified valid `traj.json` runs under the configured `runs_dir`, with time, last task, final status, and a short path. The current active run is excluded from the list. The selection occupies the composer region; Up/Down moves, Enter selects, and Esc or Ctrl-C cancels the selector without aborting an already completed turn. `/resume <run directory>` skips the list. Paths with spaces are parsed as the entire remainder of the command. Show the resolved full path before committing the switch.

Preflight a selected run before shutting down the current one: require a readable trajectory, supported UI API/schema, a nonempty task history, and a usable run directory. Reject missing, incompatible, corrupt, or current-active targets with a clear message, leaving the current session available. Revalidate when loading; if the target changes or fails between preflight and load, keep the TUI open, explain the failure, and offer the previous run as a resume target rather than claiming the switch succeeded. A history selection is read-only; no trajectory is appended and no old task starts until the new message is submitted.

If discovery finds no valid runs, say so in the composer and leave the current session unchanged. Esc or Ctrl-C while the selector is open cancels only that selection. An explicit target is not added to the recent list merely by preflight.

After selection, display the source path/session ID, recent task and status, restored turn/TODO counts, and the warning that Python memory and prior UI handles do not survive. The transcript may show a concise recent-history preview, but the model's actual context comes from the validated trajectory, not from rendered TUI text. The input area remains ready for a new message.

## Explicit compaction

Add a distinct manual trigger to the existing compaction function; do not reuse `force=True`, which currently denotes recovery from a server context-limit error. The manual trigger skips the automatic token-threshold check and requests one summary over the eligible completed history prefix. It retains the current rules: at least two uncompacted completed turns, bounded whole-record input, valid four-section XML summary, source digest, and strict reduction of the next input. “Unconditional” means **attempt regardless of threshold**, not guaranteed success or summarizing a single incomplete turn.

The manual command works even when automatic `llm_compaction` is disabled. That setting continues to disable automatic triggering, but a successfully committed manual summary is used for subsequent model inputs and survives trajectory resume. This requires separating “automatic trigger enabled” from “a valid summary is active” in context construction. Do not silently change the user's automatic setting. No agent action request or GUI action is triggered by `/compaction` itself.

Show a distinct `Summarizing` state with elapsed time and cancellation affordance. On success, persist the summary, cutoff, digest, revision, token estimates, and attempt metrics through the existing trajectory writer, then report the new revision and number of summarized turns. On insufficient history, model/transport error, malformed summary, no strict reduction, or cancellation, report the reason and retain the prior summary, cutoff, micro-compaction boundary, and original turn records. Never represent a failed attempt as completed compaction.

Ctrl-C during manual summarization cancels that summary request and returns to the same conversation input; it does not exit the session or silently send the next queued task. If a model request cannot be cancelled immediately, show `Stopping` until it settles. The summary attempt never grants foreground permission.

## Failure handling and tests

Keep the TUI responsive during listing, model summarization, and runtime teardown. A queued command must not be misrouted as an agent instruction. Resume failures must not replay an old task, inherit a foreground grant, or reuse stale Python/UI handles. An explicit path can point outside `runs_dir`, but only the user-entered path is used; list discovery remains confined to the configured root.

Tests cover recent-run ordering and invalid entries, paths with spaces, selector navigation/cancel on a narrow PTY, queued commands and ordinary messages in FIFO order, abort-suspended queues, unsent-message retention across switches, and the no-auto-replay resume boundary. Integration tests verify a new message restores context/TODO but not Python/UI state, that the previous runtime is closed before another starts, and that old CLI `--resume` behavior remains compatible. Manual-compaction tests prove below-threshold invocation, operation with auto-trigger disabled, unchanged state on every failure/cancel path, and successful persistence plus reload of the digest-bound summary. README and QUICKSTART document both commands and the resume limitations.
