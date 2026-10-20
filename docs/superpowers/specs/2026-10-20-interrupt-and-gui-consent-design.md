# Interrupts, GUI consent, and observation validity

Date: 2026-10-20

## Goal and observed failure

An OpenGCUA CLI user must be able to interrupt an active turn with Ctrl-C and immediately regain an input prompt, without the old worker continuing to operate the desktop concurrently. An agent must not start visual clicking or take the foreground mouse, keyboard, or focus without one explicit, comprehensible approval for the current task and target window. That approval permits a sequence of operations; normal `ask_user` pauses do not revoke it. Screenshot-derived points must no longer expire merely because 120 seconds elapsed.

The 2026-10-20 DingTalk run showed an unapproved `input.text(..., delivery="foreground")`, window activation, and repeated foreground `vision.click` calls. The current system prompt already instructs the agent to ask, so prompt wording alone is not an enforcement mechanism. The run also reused a screenshot after a long user pause and misread the point's `STALE_TARGET` as a lost window binding. Current TUI cancellation can ignore a second Ctrl-C and wait without a deadline for cancellation cleanup.

## Scope and non-goals

This design covers the interactive CLI/TUI, the harness worker lifecycle, SDK calls made by the harness agent, screenshot point validity in both unified and legacy SDK paths, and the macOS AX observation status. It does not make arbitrary model-authored Python or Bash a sandbox: code that imports native input libraries or invokes external automation tools can bypass an SDK-only policy. We must document that limitation rather than claim OS-level protection. It does not change the independent CuaDriver daemon or user applications.

## Decisions and alternatives

The selected design uses a host-owned task/window grant enforced before SDK dispatch. Prompt-only approval is rejected because the observed agent ignored the existing instruction. A driver-wide OS input policy would cover more bypasses, but requires changes to CuaDriver outside this repository and could block legitimate independent clients. SDK enforcement is an immediate, testable improvement with the stated arbitrary-code limitation.

There is no wall-clock point expiry in the selected design. The retained safeguards are observation ownership, scope/window identity, snapshot invalidation, and geometry checks. An old screenshot can still become semantically wrong if an application changes without a new observation; the agent must observe again after any user pause and before a visual action based on a pre-pause image.

## Ctrl-C behavior

During planning, execution, compaction, or recovery, the first Ctrl-C immediately changes the CLI state to `interrupting`, prints that already dispatched actions cannot be rolled back, and requests cancellation of the active operation. New model steps stop being scheduled. Cancellation has a two-second grace period; if the model call or worker does not settle, the harness terminates only the active worker/process group, records the action outcome as unknown, and invalidates its Python/SDK handles. The user input prompt becomes available as soon as the old worker is confirmed stopped, rather than waiting for the normal step timeout. A second Ctrl-C during interruption skips the grace period and starts force-stop instead of being silently ignored. If Ctrl-C arrives while the prompt is already open, it clears that input without cancelling the entire session. EOF or `/exit` still exits.

No follow-up instruction is dispatched while an old worker can still act. Conversation history survives interruption; a hard worker stop loses worker-local Python state and is reported plainly. An in-flight native action may already have completed before cancellation reaches it, so the next step must verify external state instead of replaying it.

## Task-scoped GUI approval

The harness owns an approval record containing the current task identity, app identity, exact window identity, and `allow_vision_action`/`allow_foreground_action` flags, both false initially. Before dispatching **any** `vision.click`, including background delivery, or any foreground keyboard/mouse action or activation, it checks the record. If missing, the action is not dispatched; the worker sends a dedicated approval-request event to the host and waits for its answer on the existing control channel. The host asks the user once for permission to use visual clicks and, if necessary, real foreground input for the named app/window and stated purpose. Only an explicit affirmative answer sets both flags for that task/window, after which the pending action may resume. Rejection, missing answer, or Ctrl-C leaves them false and returns a clear denial to the agent. Approval is a host event, not an agent `ask_user` call.

A granted sequence may contain many visual clicks and foreground actions without repeat prompts. Ordinary `ask_user` does not revoke the grant. `terminate` revokes it. A new task, `/new`, session end, or Ctrl-C-aborted turn also revokes it: these are new authority boundaries and must not inherit the previous task's grant. A different app/window needs its own request. Independently of approval, foreground actions must verify the intended target is still the foreground target immediately before dispatch; if the user changes focus, the action fails closed and the agent pauses rather than clicking the user's current app. If CuaDriver cannot provide a reliable pre-dispatch check, foreground pointer/keyboard delivery remains denied for that route. Background AX reads/actions remain available without approval.

The prompt will describe the same rule as guidance, but runtime enforcement is authoritative. The permission message must distinguish a visual target from a real mouse/keyboard takeover; it must not describe all `vision.click` calls as moving the physical cursor.

## Point-reference validity

Remove `max_age_ms` from public point APIs, Python validation, Rust point storage/validation, legacy runtime validation, internal callers, API stubs, docs, and tests. This is a deliberate public API change; callers using that keyword must be updated. Time elapsed alone no longer yields `STALE_TARGET`. Existing scope, capture ownership, window/geometry, and snapshot checks continue to reject mismatches. After a user pause, the harness invalidates prior screenshot point references and tells the agent to refresh its observation before visual action; this is a task boundary check, not a wall-clock expiry. Error feedback must distinguish an invalid screenshot reference from a stale window handle and never recommend rebind for point-only invalidation.

## DingTalk AX findings and correction

Direct CuaDriver `get_window_state` on the current DingTalk window returned structured AX elements with a `搜索` menu item but no editable search field. The SDK adapter copies each returned element without a role filter, so there is no evidence it dropped that search field. The direct result had `elements_complete: false`; therefore we cannot distinguish DingTalk's AX exposure from CuaDriver's native walk limits from this trace alone. The agent's own filter excluded `AXTextArea`, which can hide other editable controls. Separately, the adapter initializes `truncated` to `tree`, marking every tree observation partial regardless of the native result. Correct this status from native completeness/truncation metadata, and teach the agent to inspect `AXTextArea`, `AXTextField`, `AXSearchField`, and `AXComboBox` before deciding no editable node exists. Do not fabricate a node or silently upgrade to foreground if the search control remains absent.

## Verification

Automated tests must cover: Ctrl-C during model planning, a responsive worker, and a cancellation-resistant worker; repeated Ctrl-C; prompt-time Ctrl-C; no new task dispatch before worker stop; no zombie child process; one approval for multiple visual/foreground actions; approval surviving `ask_user`; revocation on `terminate`, `/new`, Ctrl-C, session end, or window change; rejection before any native action; focus-change refusal; background AX operations without approval; point use after 120 seconds without an age failure; point invalidation by scope/snapshot/geometry; and truthful AX completeness plus `AXTextArea` discovery.

Manual CLI verification should run a harmless task that loops in the worker, interrupt it, confirm the input prompt returns promptly and the old process is gone, then try a scoped visual action against a test window and confirm approval happens once. No real DingTalk messages or documents are modified during verification.
