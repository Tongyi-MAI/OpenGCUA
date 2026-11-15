# Native Windows preview

The Windows implementation uses OpenGCUA's existing persistent Python agent,
SDK and Rust authority. A Windows provider translates requests to an owned CUA
Driver process. Public app/window, accessibility, vision and action interfaces
remain shared with the other platforms.

This is a development preview. Native builds, offline contracts and a controlled
Windows test are separate from the maintainer's required **model-driven Office
workflow**. Do not treat a successful build or a model's final answer as proof
that a saved document is correct.

## Install

Use native 64-bit Windows and CPython 3.11–3.14. Source builds require Rust 1.89+
and the Visual Studio C++ build tools. A matching prebuilt wheel avoids these
compiler requirements. Windows on ARM and 32-bit Python are not covered by the
pinned driver package in this preview.

Browser setup additionally needs **Node.js 22 or newer with npm**. A bundled
Node-only runtime is insufficient. `MAI_UI_NODE` can select an absolute
`node.exe`; `MAI_UI_NPM_CLI` can select npm's absolute `npm-cli.js` when npm is
installed separately. Setup invokes the JavaScript entry point directly on
Windows, without constructing a batch-shell command.

From the repository root:

```powershell
uv venv --python 3.12
uv pip install --python .venv/Scripts/python.exe ./packages/sdk ./packages/harness/runtime
.venv/Scripts/mu.exe setup windows
.venv/Scripts/mu.exe doctor windows
.venv/Scripts/mu.exe setup browser --engine chromium
```

`setup windows` explicitly downloads official CUA Driver **0.38.0**, verifies the
release archive's SHA256, and installs the executable and its UIA helper under
`%LOCALAPPDATA%/mai-ui/windows/0.38.0`. Imports and normal runs never download a
driver. An existing incompatible installation is preserved unless `--replace`
is supplied. Publication replaces the complete installation directory and
restores the previous one if publication fails.

For offline setup, download
[the pinned Windows archive](https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.38.0/cua-driver-rs-0.38.0-windows-x86_64-binary.zip)
and use `mu setup windows --archive C:/absolute/path/to/archive.zip`.
The expected SHA256 is
`5edaa74d0afa1b55a9668dbc4f32736507bd2bd4d75b18ca1dc8881bb12402b5`.
`MAI_UI_WINDOWS_HOME` selects an absolute installation directory.
`MAI_UI_WINDOWS_DRIVER` or `Runtime.add_windows(driver_path=...)` selects an
existing absolute executable path. Driver-version mismatches fail explicitly.

## Start the agent

Copy `packages/harness/configs/windows_example.json` to the ignored
`packages/harness/configs/local.json`. Set a model you can access, its base URL,
and the matching protocol. `openai` means Chat Completions-compatible;
`openai-thinking` uses Responses; `anthropic` uses Messages. Credentials can
come from the corresponding environment variable. Keep credentials and run
artifacts out of commits.

```powershell
.venv/Scripts/mai-ui-windows-agent.exe --config packages/harness/configs/local.json
```

The example intentionally contains model/endpoint placeholders. The agent
refuses these before creating a desktop session or making a model request.
Default Windows browser sessions are isolated and headless. GUI controls use
background delivery; unsupported operations return a specific refusal instead
of moving the user's mouse. The persistent interpreter exposes `powershell()`
on Windows. `bash()` remains Unix-only.

## Use the SDK directly

```python
import asyncio
from qwen_intelligence import Runtime

async def main():
    async with Runtime() as runtime:
        resource = await runtime.add_windows(name="local-windows")
        async with runtime.session() as session:
            windows = await session.register(resource.id, name="windows")
            await session.emit(await windows.windows.list())

asyncio.run(main())
```

Bind an observed app or window before observing controls or acting. Captured
nodes belong to that exact scope and snapshot. The adapter uses process
creation times, observed window lifetimes, and snapshot tokens to reject stale
targets. It retains the SDK's action IDs, permissions, receipts and unknown
outcome handling. It does not add OS isolation; generated programs run with the
user's ordinary privileges.

Current desktop support includes observations, structured primary clicks,
text replacement on supported edit/document controls, native expand, exact
window-image clicks, and limited targetless text/key input. Read
[the Windows provider contract](packages/sdk/adapters/windows/README.md) for
precise restrictions. Launch/activation, foreground fallback, scrolling,
drag, modifier chords and several service namespaces remain unsupported.
Open the intended applications manually for this preview. Do not substitute
foreground automation or hidden Office COM edits when reporting a background
GUI result.

## Verification

Offline SDK tests and installed-wheel smoke checks:

```powershell
uv pip install --python .venv/Scripts/python.exe pytest
.venv/Scripts/python.exe -m pytest packages/sdk/tests -q
.venv/Scripts/python.exe -I packages/sdk/tests/smoke_install.py
```

The opt-in live test creates its own small Win32 window. It binds through the
actual SDK, replaces an edit value and invokes a button through the real driver,
independently reads the fixture's state, captures that exact window and samples
foreground-window/mouse stability. It closes only its own fixture and processes.
Run it in a dedicated interactive test session without moving the pointer:

```powershell
$env:MAI_UI_RUN_WINDOWS_LIVE = "1"
.venv/Scripts/python.exe -m pytest packages/sdk/tests/test_windows_live.py -q
```

The separate browser test uses the SDK's managed Chromium and a local form,
verifies submitted rows from the DOM, and checks session cleanup:

```powershell
$env:MAI_UI_RUN_BROWSER_LIVE = "1"
.venv/Scripts/python.exe -m pytest packages/sdk/tests/test_windows_browser_live.py -q
```

Both live tests are deterministic integration tests. Neither substitutes for a
model-driven Office task. See the [validation record](packages/sdk/adapters/windows/VALIDATION.md)
for the executed checks and their limits.

The harness's `tests/test_windows_host.py` covers persistent cells, native
PowerShell, interruptions, process-tree cleanup and private Windows storage.
The Windows CI jobs build the native workspace and wheel and run offline SDK
tests; they do not claim Office or desktop-interaction coverage.

## Office acceptance still required

The upstream milestone is defined in
[the maintainer's guidance](https://github.com/Tongyi-MAI/OpenGCUA/issues/5#issuecomment-6408027919)
and [the existing Windows design](https://github.com/Tongyi-MAI/OpenGCUA/issues/6).
It requires a model-driven browser-to-Excel-to-Word/PowerPoint task, background
control throughout, action-delivery statistics, independently verified saved
files, and the Windows/Office/driver/DPI versions. The controlled fixture above
does **not** establish that milestone. Document creation and saving must be
tested separately; precreating files is not silently equivalent to passing it.

Use a dedicated Windows test session, a licensed desktop Office installation,
and an explicitly configured model. Record failures and unsupported paths as
such. Redact secrets and personal content from any trajectory before sharing it.
