---
name: dingtalk-workflow
description: Use when a macOS agent must find DingTalk contacts, groups, or messages, send a message, or book a meeting room for the user through the signed-in DingTalk app (or an already-working DWS user profile).
---

# DingTalk workflow

This Skill is low-priority guidance. It is not a DingTalk connection, a login, a foreground grant, or permission to send, book, or change anything. The global harness rules (exact window binding, foreground gate, coordinates, irreversible-action approval) still apply and win over this text.

## Status

| Item | Value |
| --- | --- |
| Target | DingTalk desktop app on macOS, the user's own signed-in session |
| App versions | Untested. No DingTalk version has been checked against this Skill. Report the version only if the app inventory or a visible About screen shows it. |
| Validation status | `offline-contract`: text and loader contract tests only. Not `real-GUI-verified`. |
| Sources | The current OpenGCUA SDK contract (`mu.help("native")`, `"ax"`, `"vision"`, `"input"`, `"coordinates"`) and the known failures listed below. No DingTalk window was inspected to write it. |

Because nothing here was verified against real DingTalk UI, this Skill names no buttons, menus, field labels, or layouts. Find every control in the current observation, by its meaning, in the language the app is showing.

## Choose a route

1. **The user's instruction comes first.** If the user says GUI-only, "let me see it," or names a route, use only that route. Do not swap in a CLI or API, even when it would be faster.
2. **Signed-in DingTalk GUI (default).** Bind the exact DingTalk app and window, observe, act, and verify, as described below.
3. **DWS, only if it already works.** Use it only if `shutil.which("dws")` finds it, the user has not asked for GUI-only, and an existing profile is plainly the requesting user in the right organization. Before calling any command, check its usage with `dws --help` or `dws <service> --help`, because subcommands and flags vary by installed version. A read-only status that returns a `reason` is inconclusive, not proof of logout. Pass the chosen profile explicitly on each call and never change the default. Use JSON output where supported and a finite subprocess timeout.
4. **No route available.** Tell the user what is missing (no window, not signed in, wrong organization, no DWS) and ask how to continue.

Never install, log in, authorize, or configure DingTalk, DWS, or any connector on the user's behalf, and never say a route is installed or connected unless you checked it in this run. MCP is not a route in this Skill: do not look for, install, or connect a DingTalk MCP server as a substitute. A robot or app credential never stands in for "send as me."

## Prerequisites: identity before action

- **Window.** Run `apps = await device.apps.list()`, select DingTalk by its real `id` (not a guessed display name), then `windows = await app.windows.list()`. Select the window by exact id after checking its title and role. If several DingTalk windows exist and none is clearly the main or target window, ask. Never default to the first app or window in a list.
- **Account and organization.** Read the signed-in name and current organization only if the observation shows them. If the task depends on a specific organization, several organizations seem present, or the identity is not visible, ask the user rather than switching organizations yourself.
- **Secrets.** Do not open or read tokens, QR or login codes, passwords, keychain items, cookies, or DingTalk's local databases, caches, or logs. Login, CAPTCHA, and security prompts go to the user.
- **Privacy.** Read only the chats or records the task needs. Do not copy whole contact lists, unrelated messages, or private content into the context, logs, or final answer.

## Observe, filter, refuse ambiguity

Choose targets by meaning in the current observation, never by a fixed index, remembered node id, or hard-coded coordinate. A sketch of the pattern (it is not a DingTalk API):

```python
ax = await ui.ax.observe()
EDITABLE = {"AXTextArea", "AXTextField", "AXSearchField", "AXComboBox"}

def label(node):
    return " ".join(str(v) for v in (node.name, node.text) if v)

def find(nodes, roles, wanted):
    """Return every match; the caller decides what to do with zero or many."""
    return [n for n in nodes if n.role in roles and wanted(label(n).casefold())]

matches = find(ax.tree.nodes, EDITABLE, lambda s: "<search meaning seen in this UI>" in s)
if len(matches) != 1:
    ...  # Inspect the same-window screenshot, narrow by parent_id/bounds, or ask. Never take the first match.
```

- Before treating a missing control as absent, check `ax.tree.truncated` and each component's `ax_complete`. If observation is incomplete, an empty match list proves nothing.
- Static text (for example `AXStaticText`) is a label, not an input. A placeholder or caption near a field does not make it editable.
- If AX is not enough, use `vision = await ui.vision.observe()` from the same window. Click with `ui.vision.click(node, image=image)` when a node exists. Build a point from that same fresh image only when no reliable node exists. Never reuse points or nodes from an earlier task, another window, or another layout.
- After anything that changes the layout (search results, opening a chat, a dialog, scrolling, resizing), observe again before choosing the next target.

## Recipe: find a person or group

1. Find the search input in the bound window: an editable node whose label or visible context means "search" in the current UI. If there are zero or several such inputs, look at the same-window screenshot, and ask if it is still unclear.
2. Enter the query (the name the user gave). `ui.ax.set_text(node, query)` writes AXValue in the background. The value may read back correctly while DingTalk ignores it, so no results appear. Treat it as working only if a fresh observation shows results that match. If it does not, use element-level `ui.input.text(query, target=node)`, or the visible route: `win.activate()` (host gate), click the field, confirm focus, then `ui.input.text(query, delivery="foreground")`.
3. Observe again and list every candidate whose visible name matches. Distinguish them only by what the UI shows, such as organization, department, member count, or the type of person or group.
4. If there are zero candidates, or more than one plausible candidate, stop. Ask the user, showing only the minimal distinguishing details. Never pick the top hit or the first result.
5. Open the chosen candidate and observe again. Confirm that the visible chat or profile title matches the target, and that you are still in the same bound window.

To find a message, open the confirmed chat (or use the in-app search the observation shows), narrow by the date or keywords the user gave, and report the sender, time, and only the matching context.

## Recipe: send a message

Sending is irreversible. A foreground grant, a loaded Skill, an earlier approval for a different message, or a remembered preference is not approval to send.

1. **Resolve.** Complete "find a person or group" first. The open chat must be the confirmed target.
2. **Compose.** Identify the message composer: an editable node inside the open chat area, distinct from the search input. Prefer element-level `ui.input.text(text, target=node, action_id=aid)`, or the visible route (activate, click the composer, confirm focus, then `ui.input.text(text, delivery="foreground", action_id=aid)`). If you try `ui.ax.set_text`, an echoed AXValue does not prove DingTalk accepted the draft.
   - Do not type line breaks as keystrokes. Whether Return sends or inserts a new line has not been verified in DingTalk, so it could send a partial message. For multi-line text, ask the user how to proceed, or send it as one line with their consent.
3. **Check the target and the draft.** Observe again. Confirm the chat title is the resolved target, the sending identity and organization are what the user asked for, and the composer holds exactly the intended text with nothing extra.
4. **Authorize.** Send only if, in this task, the user explicitly asked to send this exact text to this exact recipient as this identity. If you drafted or changed the text, resolved an ambiguous recipient yourself, or any of these facts changed, first `ask_user` with the recipient, identity, and full text, then wait.
5. **Send once.** Use the send control the observation shows (by meaning, not by position), or the send key only if the observation or the user confirms it. Give it a fresh `action_id` and make one attempt.
6. **Verify.** Observe the same chat again. Success means a new message from the user's own identity in that chat that contains the intended text, and an empty or cleared composer. If you cannot see all three, report the result as unverified, not as success.

**If the outcome is unknown, never resend.** That applies to a timeout, `ACTION_OUTCOME_UNKNOWN`, or a feedback line marked `outcome_unknown`. First run `await mu.emit(await mu.action_status(aid))` and observe the chat. If the message landed, report it. If it is still unclear, report "outcome unknown" and ask the user. Do not retry with a new `action_id`.

## Recipe: meeting rooms

Room booking has no verified entry point, path, or field list. Before acting, read [meeting-room.md](meeting-room.md) with `skills.read("dingtalk-workflow", "meeting-room.md")`. Do not invent buttons, menus, mini-app names, or field names.

## Automatic action feedback

The host may add lines under `[自动动作事实反馈…]`. They restate SDK receipts and observation completeness only. They always say `verification: unverified` and `condition: not_checked`. Do not try to "upgrade" them. Do your own business verification by observing.

| `evaluation` | What to do |
| --- | --- |
| `receipt_unverified` | The call ended. A technical receipt is not business success, so observe the postcondition. |
| `outcome_unknown` | It may have taken effect. Check `mu.action_status` with the same id and observe. Never resend or resubmit. |
| `not_dispatched` | It did not run. You may correct the cause and retry once. |
| `not_applied` / `suspected_noop` / `partial` | Do not repeat the same call. Observe, then change method (for example, from AX write to element-level or visible typing). |
| `execution_failed` | Read the error code and handling notes. The business state is still unknown, so observe before acting. |
| `observation_unavailable` | The observation is incomplete. It cannot prove a control is missing or unique. |

## Known limits and failures

- `ui.ax.set_text` writes AXValue in the background. The value can read back while DingTalk's own state (search results, draft, send button) is unchanged. Verify the visible effect.
- "AXValue is not settable" means this node cannot be written through AXValue; it does not prove the node is a static label or cannot accept real keyboard input. A static text node is not an input. `editable_source=role_hint` is only a role-based hint, never native proof of `value_settable`; `value_settable_source=unknown` requires a supported alternative or fresh evidence.
- Focus loss is not loss of permission. `INPUT_REFUSED` alone can indicate several delivery refusals; only a diagnostic such as "target app is no longer frontmost" establishes that the window lost focus. It does not revoke the session's foreground grant. Activate the exact bound window again, observe, and recheck the target chat and draft before continuing. Do not replay the last click or send, and do not ask for takeover again if it is already allowed.
- `same_pid_keyboard_ambiguity`: the process has several top-level windows, so background keyboard input cannot prove which window it reaches. Use `target=node` or the visible route.
- `element_outside_target_window`: the control belongs to another window or popover. Bind that window explicitly instead of forcing the action.
- Layout, labels, and language vary by version, organization settings, and window size. Nothing here pins a version.
- No DingTalk-specific helper exists in the harness or SDK. Use only the documented `apps`, `windows`, `ui.ax`, `ui.vision`, `ui.input`, and `mu` calls.

## Failure recovery

- If the same step makes no progress twice, or the same error appears twice, stop. Recheck the window, identity, and target, then switch to a supported method or ask the user.
- `STALE_TARGET`, a window closed or recreated, or a session reset: list and bind again, observe again, and resolve the chat again. Do not reuse old nodes, images, or points.
- After a partial failure, resume from the failed step. Do not replay the whole sequence. Anything already sent or submitted stays done.

## What must not become permanent

A foreground grant, a send or booking approval, "send without asking," node ids or indexes, window ids, coordinates, and screenshots are evidence for the current task only. Do not write them into this Skill, memory, or later tasks as standing permission or fixed targets. A stable workflow preference such as "prefer the GUI" may be proposed through `memory.propose` as a candidate, but only user confirmation makes it reusable; it still grants no permission and yields to the latest request.

## Reporting

Say which route you used and what you verified by observation (target title, sender identity, message visible, composer cleared, booking visible). List anything left unverified or unknown, and the DingTalk version if you saw it. Use `terminate(status="success")` only when the business result has been observed.
