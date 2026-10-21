# DingTalk meeting rooms: verify before acting

Status: `offline-contract`, not `real-GUI-verified`. No DingTalk window was inspected to write this, so the location of room booking is unknown. It might be inside a calendar or event form, a separate workbench app, or something an organization turns off. So might the required fields and any approval or conflict flow. This file has no button names, menu paths, mini-app names, or field labels. Use only what the current observation shows.

## 1. Collect the request before touching the UI

Confirm you have each of these. Ask once for whatever is missing, and do not fill it in with a guess:

- Date, start and end time, and the timezone if it could be unclear
- Building, floor, area, or a specific room, if the user cares
- Number of attendees, and equipment needs only if the user mentioned them
- Subject or title, and whether others should be invited (resolve each person with the "find a person or group" recipe in `SKILL.md`)
- Which account and organization should own the booking

## 2. Find the entry by observation

1. Bind the exact DingTalk window, observe AX, and look at a same-window screenshot if AX is sparse or `ax.tree.truncated` is true.
2. Look for visible navigation whose meaning relates to calendar, meetings, or rooms in the current UI language. List every plausible entry you see.
3. If there is no plausible entry, or several, ask the user where they normally book. Do not open workbench apps one after another, or search the web for a menu path, and then treat the result as fact.
4. After you open an entry, observe again. Treat what you found as evidence for this task only, not as a fixed path for later tasks.

## 3. Map the form from what is visible

- List the fields the observation actually shows, with their visible labels, and any required markers you can see. Do not assume a field exists because another calendar product has it.
- Room availability is only what the UI shows right now. If availability is not displayed, or seems incomplete, say so. Do not claim a room is free.
- Fill one small group at a time. After each group, observe again and read back the values. An echoed `ui.ax.set_text` value is not proof the form accepted it, so look for the visible field value or the picker's selected state.
- Pickers (date, time, room) may open popovers or separate windows. If you see `element_outside_target_window`, bind that window explicitly.

## 4. Review before submitting (always)

Booking reserves a shared resource and may notify others, so it needs explicit approval. Before the submit control, `ask_user` with:

- The room exactly as displayed, the date and time with timezone, and the subject
- Invitees as displayed, and the owning account or organization
- Anything left at a default you did not choose, and anything you could not verify

Submit only after a clear yes for this exact summary. A foreground grant or an earlier booking approval does not count.

## 5. Submit once, verify, never double-book

1. Click the submit control the observation shows, by meaning and not by position, with a fresh `action_id`. Make one attempt.
2. Observe again. Success means a visible confirmation, or the booking showing in the calendar or room view with the same room, time, and subject. If an approval step is shown, report "pending approval," not "booked."
3. If the outcome is unknown (a timeout, `ACTION_OUTCOME_UNKNOWN`, or a feedback line marked `outcome_unknown`), run `mu.action_status(aid)` and observe the calendar or room view. Never submit again while a duplicate booking is possible. If it is still unclear, report "outcome unknown" and ask the user.

## Still to be verified on a real client

Record observations here only after a real-GUI review. Until then, treat each item as unknown:

- Where room booking starts in the tested version and organization
- Whether a room is chosen inside an event form or in a separate flow
- Required fields, and how DingTalk marks them
- Conflict, approval, and cancellation behavior
