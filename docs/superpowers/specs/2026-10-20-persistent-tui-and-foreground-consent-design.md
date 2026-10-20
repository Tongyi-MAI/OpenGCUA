# Persistent terminal input and task-level foreground consent

Date: 2026-10-20

This design supersedes the terminal-input and GUI-consent sections of
`2026-10-20-interrupt-and-gui-consent-design.md`. Its point-reference and AX
decisions are unaffected.

## Goal

The interactive OpenGCUA terminal should behave like a continuous conversation:
agent output streams in a scrollable area above an input box that remains usable
throughout planning and execution. Ctrl-C interrupts only the current agent
turn, then the user can correct the same task and retain its context. A message
submitted while the agent is running waits until that turn completes normally.

Before the agent first takes foreground focus, mouse, or keyboard control in a
task, the terminal must show an explicit Allow/Reject choice. The choice warns
that the agent may take over the user's foreground input repeatedly until the
task ends. Authorization is for the task, not one action or one window. The
agent must not solicit the same permission in chat before the host presents the
choice.

## Scope and boundaries

This changes the interactive `--ui tui` frontend, its connection to the agent
loop, the harness SDK-action consent gate, and the permission guidance in the
agent prompt. `--ui plain` remains line-oriented; interactive plain mode uses
an explicit textual Yes/No prompt, and noninteractive mode denies a permission
request.
Demo Hub is not redesigned. The consent gate covers macOS SDK actions routed
through the harness, not arbitrary model-authored Python, Bash, AppleScript, or
other tools that bypass the SDK. It does not prevent websites or applications
from raising their own windows. Those limitations must not be represented as
OS-wide protection.

## Terminal architecture

Use one persistent `prompt_toolkit` application to own terminal input and
rendering for the full session. It has a scrollable transcript, a small status
line, and a fixed input area. Agent execution runs as a separate asyncio task;
semantic `AgentEvent` values are delivered to the UI through a serialized event
queue. Only the UI task paints the terminal. This replaces the current pattern
of Rich writes into normal scrollback and a `PromptSession` that exists only
when `frontend.read()` is called. Keep the presentation-neutral events and
the plain frontend. Preserve existing intent, reasoning, and code streaming
controls; hidden reasoning/code stay hidden by default.

The frontend's input routing distinguishes ordinary task messages, answers to
an agent question, and a foreground-permission decision. An approval decision
is a typed UI result associated with the active request ID, never a string
parsed from chat text. A queued task message cannot answer a question or
approve a permission request. If a question arrives while the user has a draft,
the draft and queued messages are retained while the input area switches to an
answer context. An approval choice is presented separately from the text box.

## Input and interruption rules

While a turn is active, Enter adds the current message to a visible in-memory
FIFO queue and clears the draft. Queued messages are delivered, in order, only
after the current turn completes normally and the agent loop requests the next
task message. They do not interrupt the current turn. The queue is not
persisted across process exit; exiting with pending messages must warn rather
than silently claim they were sent.

Ctrl-C is handled by the persistent UI in every phase, including model waits,
worker execution, ordinary questions, and permission choices. On the first
press during an active turn it requests cancellation, displays `interrupting`,
and stops scheduling new model or SDK actions. The input area stays available;
the next instruction is not dispatched until the old worker is confirmed
stopped. A second Ctrl-C during interruption force-stops the owned worker or
process group. Already dispatched external actions may have happened and are
not rolled back. When idle, Ctrl-C only clears the current draft. Ctrl-D and
`/exit` end the session.

After an interruption, the same task identity, model context, and existing
foreground grant remain. Queued messages are retained but are **not**
auto-dispatched after an abort: the queue becomes suspended. `/queue` shows its
contents, `/queue send` resumes FIFO delivery, and `/queue clear` discards it.
A newly typed correction can be sent immediately while the old queue remains
suspended. A normal task completion clears the task's grant before a queued
message starts the next task. An ordinary `ask_user` pause does not complete a
task and does not consume queued messages. The status line distinguishes
running, waiting for input, waiting for approval, interrupting, queued message
count, and whether the task currently has foreground consent.

## Foreground consent policy

The host, not prompt text, decides whether a macOS SDK action requires
foreground consent. The gate triggers when an action requests
`delivery="foreground"` or explicitly activates a window; `win.activate()`
counts even if its wire action has no delivery field. A background
`vision.click`, background AX action, DOM operation, and observation do not
trigger this consent. This rule replaces the current unconditional
`point_click` trigger. A driver that refuses background delivery may return an
error, after which an explicit foreground attempt goes through the gate.

Consent state is `unset`, `allowed`, or `denied` for the current task. On the
first foreground attempt in `unset`, the SDK action pauses before dispatch and
the host presents a two-option choice. The text identifies the task and target
App/window and says: allowing means the agent may repeatedly activate windows
and use the user's foreground mouse and keyboard until this task ends, so the
user should avoid simultaneous desktop input. Reject is selected by default;
arrow/Tab selects an option and Enter commits it. Only an Allow selection on
the current request ID moves the task to `allowed` and releases the pending
action. Ctrl-C, EOF, UI failure, missing answer, or Reject never grants access.

In `allowed`, later foreground actions in the same task do not prompt again,
including actions aimed at another window. Every action must still carry or
resolve an exact target, and the provider must verify the intended foreground
target immediately before dispatch. Missing or mismatched targets fail closed;
task-level consent does not authorize clicking whichever application happens
to be under the user's pointer. In `denied`, later foreground attempts are
blocked without another choice and return a distinct permission-denied result
to the agent. No automatic retry may cause a prompt loop.

After denial, an explicit host-mediated `request_foreground_permission(reason)`
entry point may present the same choice again. It is the sole way for the agent
to re-request permission within that task. The call does not execute a GUI
action, cannot approve itself, and remains denied until the user explicitly
selects Allow. The denial result tells the agent to use background/AX routes or
stop, and names this re-request entry point if foreground work is genuinely
necessary. The system prompt removes instructions to call `ask_user` before
foreground work; it continues to prefer background operations and explains
that the host enforces foreground consent. Significant irreversible actions
still require their own confirmation.

Consent survives ordinary questions and Ctrl-C corrections within the same
task. It is cleared on task termination, explicit `/new`, and session exit.
The UI shows the current grant state while the task continues. A permission
decision and its scope are recorded without treating model prose as evidence
of user approval.

## Failure handling and verification

Rendering failures must not retry agent actions or turn an absent decision
into approval. If the TUI cannot own an interactive terminal, auto mode falls
back to plain mode; an explicitly requested TUI reports a clear startup error.
Cancellation must settle or force-stop the active worker before accepting a
correction. If an in-flight native action has an unknown outcome, the next
agent step must reobserve rather than blindly repeat it.

Automated tests must include a real pseudo-terminal exercise, not only direct
frontend method calls: stream output while the input remains editable; queue
multiple messages; verify normal FIFO delivery; verify question and approval
prompts cannot consume queued chat; press Ctrl-C during planning, execution,
question, and approval; press it twice to force-stop a resistant worker; and
confirm the same task can accept a correction without an orphaned worker.
Permission tests must prove background `vision.click` does not prompt,
foreground input and `activate` do, Reject prevents dispatch, repeat attempts
after Reject do not prompt, explicit re-request does prompt, Allow is reused
across windows within the task, and task end clears it. Also test missing or
stale request IDs, no terminal/EOF, focus mismatch, and a UI exception as
fail-closed cases. Manual verification uses a harmless local test window; it
must not modify live user data.
