# Windows CUA provider (preview)

This provider connects the existing SDK to an **owned CUA Driver 0.38.0** process
over MCP stdio. It uses native Windows Python; WSL is not the execution host.
The Python SDK, Rust authority, action IDs, permissions, observation store and
receipts remain the same as on other platforms. The small Python worker only
translates the provider protocol and maintains native target references.

## Contract and limits

- Applications bind to a process ID **and creation time**. Windows bind to an
  enumerated HWND, owning process, thread and provider-issued lifetime ID. A
  missing/replaced application or observed closed window cannot revive an old
  binding. An unobserved HWND reuse within the same process/thread is not a
  native lifetime guarantee; accessibility references remain snapshot-bound.
- An accessibility read replaces that window's previous node tokens. Image-only
  reads leave node tokens intact. A coordinate action requires the latest exact
  window capture and consumes its capture ID. Resizing or moving the window
  rejects the old image. No second DPI multiplier is applied.
- At most eight windows are captured per app observation, and eight window
  snapshots/images are retained. Omitted or partial trees are explicitly marked;
  absent metadata such as focus, password state or writability remains unknown.
- All supported actions explicitly request **background** delivery. There is no
  automatic foreground fallback, action replay or restoration of user focus.
  A driver-reported foreground action closes the provider and reports an unknown
  outcome. A successful call with an unverified effect stays unverified.
- Supported actions: single primary AX clicks; exact image clicks; string
  replacement on Edit/Document controls; a reported native expand action;
  targetless text and unmodified keys in the bound window's current editing
  context. App behavior can still reject these operations.
- Unsupported in this preview: application launch/activation, foreground input,
  desktop-wide input/capture, scrolling, drag, clipboard, files/shell services,
  modifier chords and node/point-targeted text or keys. The last two cannot be
  replaced with append-to-value or a click that changes the caret.
- Combo-box selection, slider values and other nontext operations are not
  exposed as string replacement. Browser tasks should use the existing browser
  provider rather than desktop keystrokes.
- Elevated applications, secure desktops and locked interactive desktops are
  rejected. The provider never changes permissions or automates UAC.

## Process ownership

The worker acquires an OS file lease for its Windows session/default desktop.
Only one provider controls that desktop at a time, including across independent
SDK runtimes. It assigns itself to a kill-on-close Windows Job before spawning
the driver, so an abrupt worker exit also terminates the owned descendants. The
driver gets a nested Job for normal shutdown. User applications are not placed
in these Jobs and are never closed by provider cleanup.

MCP replies have bounded frames and correlated IDs. A timeout, cancellation or
lost reply after mutation dispatch returns `ACTION_OUTCOME_UNKNOWN` and fences
the driver. There is no reconnect/replay of an ambiguous action. Establish a new
runtime and observe the application again before proceeding.

## Verification

Run the offline contracts without a driver, Office installation or desktop:

```powershell
python -m pytest packages/sdk/tests/test_windows_provider.py -q
```

These cover target/snapshot isolation, nullable driver metadata, incomplete
trees, capture geometry, semantic refusals, unknown outcomes, real subprocess
transport failure and Windows Job cleanup. They are distinct from an actual
model-driven Office/browser task. A full end-to-end background Office workflow
must be demonstrated and independently checked before claiming that acceptance
milestone.

The driver protocol is pinned to the official
[CUA Driver 0.38.0 release](https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.38.0).
The upstream architectural requirements and remaining Office constraints are in
[OpenGCUA #5](https://github.com/Tongyi-MAI/OpenGCUA/issues/5#issuecomment-6408027919)
and [#6](https://github.com/Tongyi-MAI/OpenGCUA/issues/6).
