# For: python -m mai_ui_repl --platform macos --cells examples/macos_cells.py
# ruff: noqa: F704, F821 -- Persistent REPL supplies top-level await, device and mu.
# Open TextEdit first. Captures enter the result only through explicit emit.
app = await device.apps.get("com.apple.TextEdit")
ui = app.ui
await mu.emit(await ui.ax.observe())
await mu.emit(await ui.vision.observe())
