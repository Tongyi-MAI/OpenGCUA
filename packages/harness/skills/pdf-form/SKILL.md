---
name: pdf-form
description: Use when a native PDF form in macOS Preview has unnamed accessibility fields or field edits cannot be verified reliably.
---

# Native PDF form workflow

Use this for a PDF form opened in macOS Preview, especially when AX fields lack useful names. It does not authorize foreground control or changing document contents beyond the user's request.

1. Confirm the correct PDF and Preview window. Observe the window's AX tree and a screenshot. Use the visible labels, AX bounds, and read-only form metadata to map fields. Do not treat `editable=True` as proof that AXValue can be written.
2. Prefer `ui.ax.set_text(node, value)` only when the actual field supports it. Read back the field and inspect the visible PDF result. If Preview rejects AXValue or AXFocus, do not repeat the same call indefinitely.
3. If GUI filling is necessary, obtain the harness's foreground permission before activating the target window. Capture a fresh image of that same window. Click the intended field with a same-window node/image pair when reliable; otherwise use a point derived from that image, not hand-converted screen coordinates.
4. Confirm the editing focus before `ui.input.text(value, delivery="foreground", timeout_ms=15000)`. A background AX click receipt does not prove foreground keyboard focus. To replace an existing value, use Cmd+A only after confirming focus within that field.
5. Re-observe after each small group of fields, after page changes, and after saving. Verify the resulting PDF values or render, not just the return code, printed variables, or AX node text. If the PDF cannot be verified, report that limitation instead of claiming completion.

Stop and ask the user if the field mapping is ambiguous, the application needs foreground control that has not been approved, or the required data is missing.
