# Qwen-Intelligence SDK — 🟡 preview

One programming vocabulary across **computer, browser, and mobile**. A GUI agent has one interface;
a GCUA has four. A model that must learn a different vocabulary per device has learned three things
where one would do.

## Install

The Python distribution is **`qwen-intelligence-sdk`**. Both
`import qwen_intelligence` and `import mai_ui` work. The distribution version is
`0.1.0`; the underlying `mai_ui` API reports `0.3.0`.

From the repository root, with [uv](https://docs.astral.sh/uv/) installed:

```bash
uv venv --python 3.12
uv pip install ./packages/sdk
.venv/bin/mu info
```

Source installs require **CPython 3.11–3.14, Rust/Cargo 1.89+ and a C toolchain**
(Xcode Command Line Tools on macOS, a C compiler/linker on Linux). `uv` installs
Maturin automatically and builds the native extension. Use `uv pip install -e
./packages/sdk` for an editable development install; native edits still require rebuilding.
Windows host builds are not supported yet because the native provider code uses
Unix APIs. Device support and SDK host compatibility are separate: controlling
macOS still requires macOS 14+ and its driver/permissions.

For the fastest install, pass a downloaded wheel or a hosted wheel URL directly
to `uv pip install`. **Wheel installs do not require Rust or a checkout**:

```bash
uv pip install /path/to/qwen_intelligence_sdk-0.1.0-<python>-<abi>-<platform>.whl
```

Select the wheel matching your Python version, OS and architecture. The installer
checks these tags automatically.

The SDK package does not install the agent harness, CuaDriver, browser engines,
or Android tools. See [the real-device quick start](../../QUICKSTART.md) for setup and
permissions. Browser setup (`mu setup browser`) requires Node.js 22+; Linux desktop
setup (`mu setup linux`) requires the system dependencies described in
[the Linux adapter](adapters/linux/README.md). The legacy REPL and MobileWorld
import shims require the separate `mai-ui-repl` and `mai-ui-mobileworld` packages.

Android's optional broker and Java agent are built separately from `packages/sdk/` in a
repository checkout (the Python sdist only builds the SDK extension):

```bash
cargo build -p mu-adapter-android --locked
python scripts/build_android.py
export MAI_UI_BROKER="$PWD/target/debug/mu-broker"
export MAI_UI_AGENT_JAR="$PWD/adapters/android/out/agent.jar"
```

The Java build requires JDK/Android SDK tools; see [Android setup](adapters/android/README.md).
Set the absolute paths above for installed wheels as well as source installs.

## Build and distribute

From the repository root:

```bash
# Builds an sdist and then a wheel from that sdist, using the locked Rust dependencies.
uv build packages/sdk --python 3.12 --out-dir dist
uv pip install dist/*.whl
.venv/bin/python -I packages/sdk/tests/smoke_install.py
```

`packages/sdk/` is self-contained: the sdist includes Rust crates, Python namespaces,
contracts, and the browser/Linux worker assets embedded by Rust. No precompiled
extension is tracked. The browser worker's generated JavaScript is tracked so
Python builds do not require Node.js; regenerate it with `npm ci` and `npm run
build` in `packages/sdk/adapters/browser-playwright/` after TypeScript changes.

[The SDK wheel workflow](../../.github/workflows/sdk-wheel.yml) builds macOS arm64
and Linux x86_64 wheels for CPython 3.11–3.14. Linux builds use manylinux containers.
Each wheel is installed in a fresh environment and checked for imports, native
runtime lifecycle, bundled resources, and `mu info`. A separate job builds an
sdist and verifies it can build a wheel outside the checkout.

The workflow runs on SDK changes and manual dispatch and uploads downloadable
artifacts. The [release workflow](../../.github/workflows/release.yml) also calls it
on version tags, publishing the wheels and source distribution alongside the
macOS DMG in GitHub Releases.
It does not publish to PyPI. GitHub Actions is optional for local installation
and builds. The SDK is licensed under Apache-2.0; see `packages/sdk/LICENSE`.

## Native source layout

The SDK build uses `packages/sdk/adapters/macos` as the canonical native macOS provider.
Make native macOS changes in that directory.

## Design principles

1. **One vocabulary across devices.** Devices, applications, windows, and browser tabs are discovered
   and bound to names; the same code addresses a phone and a Mac.
2. **Two perception modalities, kept separate.** Accessibility-tree observation and visual observation
   are distinct namespaces — an element index and a screen coordinate cannot be confused, because they
   are grounded on different observations.
3. **Observation must be delivered.** An observation is not visible to the model until explicitly
   delivered; element indices are valid only relative to the last delivered observation. The model
   can never refer by index to a tree it captured but has not seen.
4. **Stabilization and staleness.** The SDK waits for the interface to settle before observing;
   a reference to a changed screen is rejected, not guessed.
5. **Cross-device operation.** Concurrent observation of multiple devices; file transfer between them.

## Platform coverage (this preview)

| Platform | Status |
|---|---|
| Python API | 🟡 preview |
| macOS adapter | 🟡 preview |
| Android adapter | 🟡 preview |
| Browser adapter | 🟡 preview |
| Linux adapter | 🟡 preview |
| Windows adapter | ⚪ planned (v1.0) |

## Layout

```
packages/sdk/
├── pyproject.toml      Python metadata and Maturin configuration
├── Cargo.toml         self-contained Rust workspace
├── Cargo.lock         locked native dependencies
├── crates/            core, runtime, protocol, host and Python bindings
├── adapters/          macOS provider, browser/Linux workers, Android sources
├── api/contract/      contracts embedded by Rust
├── python/
│   ├── mai_ui/         implementation
│   └── qwen_intelligence/  public alias namespace
├── tests/             installed-wheel smoke check
└── examples/          cross-device example programs
```

> The SDK is model-agnostic by design — running another model through it is itself a contribution.
