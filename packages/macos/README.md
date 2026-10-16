# OpenGCUA for macOS

Native macOS menu bar assistant. SwiftUI renders
all screens; AppKit owns the menu item, anchored popover and nonactivating
floating panel. No WebView, Electron, browser, HTTP listener or separately
installed Python/uv is required to run the app.

Current target: **Apple Silicon, macOS 14+**. The build is a usable development
app; public distribution still requires Developer ID signing, notarization and
clean-machine permission acceptance.

## Use

1. Open `OpenGCUA-macOS-arm64.dmg`, drag **OpenGCUA** into **Applications**,
   then eject the disk image and open OpenGCUA from Applications. For source
   development, open `packages/macos/build/OpenGCUA.app` directly.
   First launch opens a three-step setup assistant: welcome, model connection,
   then Mac permissions. **继续** on the model step validates and saves the
   credentials before permission setup, so a system-requested restart retains
   them. **开始使用** requires credentials (or an explicit keyless configuration),
   Accessibility, and Screen Recording. This checks local readiness, not model
   API connectivity. **稍后设置** retains the current step and previously saved
   settings; unsaved field edits are discarded when the assistant is reopened.
   Unfinished setup resumes next launch.
   After setup, it starts quietly in the menu bar with no Dock icon or independent main window.
   Click the OpenGCUA logo to open the task popover; click outside to dismiss it.
   The menu popover is transient: deactivation, a Space change, or becoming fully
   hidden closes it without recreating a floating panel or stopping a task.
   **Control–Option–Space (⌃⌥Space)** globally shows the task popover or hides
   the currently visible popover/floating panel. Hiding ends voice capture and
   playback while the computer task continues. The shortcut appears in Settings
   and the menu-bar tooltip. It uses macOS hot-key registration without Input
   Monitoring access; if another app owns it, Settings reports the conflict.
2. Settings opens in its own native window, with **通用**, **模型**, **权限**
   and **语音** tabs. Edits stay in a draft until **保存**; **取消** discards them.
   In **模型**, choose the model endpoint, model name, and OpenAI-compatible or
   Anthropic Messages protocol, then
   save its API key. The default is DashScope / `qwen3.8-max`. Keys are stored in
   macOS Keychain, separately for each endpoint. Alternatively, click **选择 JSON
   配置…** and choose your private `packages/harness/configs/local.json`; the app reads it
   before every new task. No CLI environment credential is imported.
3. Enter a task and press **Enter**, or click **开始任务**. **Shift+Enter** inserts
   a newline; **⌘Enter** also sends. Settings' **Enter 键发送** is enabled by
   default, including for existing preferences. Turning it off uses **Enter**
   for newlines and **⌘Enter** to send. These shortcuts also apply to the small
   window's task updates and question replies; confirming an input-method
   candidate never sends. The main card collapses into a
   draggable floating panel. Click the menu bar logo to return to the app.
4. **暂停** requests a pause at the next agent boundary: already executing code
   finishes first. The UI shows **正在暂停** until the boundary is reached.
   **停止** cancels the agent and drains/kills its owned code worker; it does not
   undo previously completed actions.
5. Questions from `ask_user` expand the panel in place, with a free-text reply.
   There is no generic “approve everything” button. Completion, failure and stop
   results appear directly in the panel as selectable, scrollable text. The panel
   grows to show the result without taking focus or reopening a dismissed panel.
6. **任务记录** opens a resizable window with a task sidebar and selectable results. **查看运行记录** reveals the corresponding run.
   Dismissing the popover keeps the app in the menu bar; ⌘Q exits and cleans up
   its host, worker and private driver daemon.

The popover footer’s **小窗** button also opens it before starting a task.

### Existing Generalist installations

OpenGCUA retains the bundle identifier `app.generalist.mac`, Keychain service
`app.generalist.model-api`, and `~/Library/Application Support/Generalist/` storage
path for compatibility. Existing preferences, model keys and task history remain
available. Quit Generalist before installing and opening OpenGCUA, then remove
the old app bundle to avoid keeping two launch entries. macOS may still ask to
confirm permissions after moving or replacing a signed app.

### Configuration files

Manual Settings include **Reasoning effort**, defaulting to `low`, with the
source harness's choices: `none`, `low`, `medium`, `high`, `xhigh`, and `max`.
Existing app preferences also default to `low` when this field is absent.
The selected JSON's `reasoning_effort` overrides this setting; actual support
depends on the configured model service.

Selected JSON files use the source harness's keys, including `llm_base_url`,
`model_name`, `llm_protocol` (`openai` or `anthropic`), `reasoning_effort`, token
and context budgets, retries, history/compaction options, and Background/Demo mode.
File values override manual Settings. Unknown keys and `_` comments are ignored;
invalid values or unreadable files stop the new task with an error. An already
running task retains its starting configuration. Click **使用手动设置** to clear
the selection.

A nonempty file `api_key` takes precedence. Otherwise the app reads the Keychain
entry for the effective `llm_base_url`, avoiding reuse of a key from a different
endpoint. Only the selected file path is saved in app preferences; the file's
credentials and contents are not copied to UserDefaults or Keychain. Protect
the private JSON file as you would for the source harness.

The app controls `runs_dir`, task/resume state, `multi_task`, countdown, terminal
UI, and its private driver/socket. These lifecycle settings in a selected file
are ignored. The native app remains a macOS frontend; studio-specific mobile
and recording options are not applied.
The top-right **×** closes either the task panel or voice bar. Closing the voice
bar also ends capture/playback; an accepted computer task continues in the
background. Click the menu-bar sparkle to return, then **小窗** to reopen the panel.
The floating panel includes text input, a microphone, and Send (⌘Return).
During a task, messages join the next planning round; a new message received
while planning supersedes that plan before it executes. In-flight code finishes
first. Sending does not resume a paused task. While the agent is asking a
question, the same composer sends a question-ID-fenced reply. After completion,
it starts a new task. Queued messages that were not consumed remain as a draft.

Voice is a separate **full-duplex conversation mode** powered by
`qwen3.8-omni-flash-realtime` with the `Tina` voice. Clicking the
microphone replaces the floating task editor with a compact waveform bar.
When a computer task exists, a progress section below the waveform shows the
runtime phase (thinking, operating, checking results), planning step number and
current/recent action intent. Questions and terminal results appear in the same
section without leaving voice mode. Step numbers count planning rounds, not
individual clicks; action intent describes what is being attempted, not proof
that it succeeded.
Input energy and assistant audio animate the waveform. Semantic VAD turns
continuous 16 kHz PCM input into conversation turns; streamed 24 kHz output
plays through the same AVAudioEngine with voice-processing echo cancellation.
Speaking interrupts local playback and cancels the pending response. A context
note tells the model that the previous answer was interrupted; Qwen does not
document a precise heard-audio truncation event. No microphone recordings are written to disk.

The model has exactly two Function Calling tools:

- `queue_task(instruction)`: starts a task while idle, queues an update to the
  running computer agent, or replies to its current question.
- `stop_task()`: requests cancellation of the current computer task while the
  voice conversation stays open.

Only completed structured tool calls can dispatch actions. ASR and spoken model
text are captions, never executable commands or task drafts. Calls are deduplicated
and fenced against task/question changes. Tool outputs wait for the private
runtime's acceptance receipt; an accepted request does not mean execution has
completed. Runtime state is supplied as context without unsolicited progress
speech. Closing voice stops capture and playback without cancelling a computer
task that was already accepted. Reconnecting does not replay tools.

Settings support Beijing/Singapore, the editable model, a region-scoped Keychain
credential, and optional Workspace ID. Empty Workspace ID uses the regional
public DashScope endpoint; explicit IDs select a
dedicated endpoint. The no-recording connection test requires both text and audio
in a completed model response, without playing audio or calling task tools.

API contracts: [Qwen voice guide](https://help.aliyun.com/en/model-studio/realtime),
[WebSocket connection](https://help.aliyun.com/en/model-studio/client-events),
[server events](https://help.aliyun.com/en/model-studio/server-events).

## Architecture and data

```text
OpenGCUA.app (SwiftUI + AppKit)
  └─ bundled Python → native_host.py (private NDJSON stdin/stdout)
       ├─ mac_agent.run_agent → configured model provider
       ├─ persistent code worker → bundled mai_ui Rust SDK
       └─ signed CuaDriver 0.28.1 → private socket → local macOS
```

The SDK validates CuaDriver's pinned version and upstream signing identity. The
bundle includes the original signed helper and preserves any existing
`/Applications/CuaDriver.app`. The app uses its own socket and retains the SDK's exclusive local-device lease.
The helper runs in embedded mode: **OpenGCUA owns the Accessibility and Screen
Recording grants**, not CuaDriver. Existing grants to a separately installed
CuaDriver do not carry over. Click **打开拖拽授权引导** in Settings, or open
**Mac 控制权限…** from the app menu, for a floating permission guide.
Drag its OpenGCUA icon into each System Settings permission
list and enable the switch, or use **在 Finder 中显示** to reveal the running bundle.
The guide refreshes grant status automatically and does not access the API key.
Before sending any model request, both the native UI and host preflight check the
actual grants. Missing or unknown permission state blocks task execution.
“运行组件就绪” means the task host is connected; it is not a permission claim.

Task commands are fenced by task ID; replies also carry a question ID. Pause is
acknowledged only at a real boundary. Terminal results come from `traj.json`.
The host cancels on stdin EOF, including a GUI crash. There is no unauthenticated
localhost control API.

Preferences contain only endpoint/model/appearance settings. Keys are passed to
the host through its private pipe, never command-line arguments. Native runs do
not fall back to CLI environment/config keys. The model sees relevant task and
observation content during execution, as explained in Settings.

History and execution artifacts live under
`~/Library/Application Support/Generalist/` (private user directory). The UI
retains up to 100 recent entries; run directories are retained until the user
removes them. They can contain task text, observations and screenshots. Failed or
interrupted tasks are not automatically replayed.

## Build and package

Build prerequisites: Apple Silicon Mac, Xcode command-line tools, Rust 1.89+ and
uv. They are **not** runtime prerequisites for end users.

```sh
uv run --no-project --python 3.12 python packages/macos/scripts/build.py
# Subsequent UI/host-only iteration on an existing bundle:
uv run --no-project --python 3.12 python packages/macos/scripts/build.py --skip-runtime
bash packages/macos/scripts/package.sh
```

Packaging produces `packages/macos/build/OpenGCUA-macOS-arm64.dmg` and its SHA-256
file. The disk image includes the app and an Applications shortcut for installation.

Quit a running OpenGCUA before replacing its bundle. Full builds include a
managed CPython copy, a fresh SDK wheel, locked/hash-checked Python dependencies,
and a digest/signature-checked CuaDriver archive. The builder copies source files
explicitly; it never copies local configs, API keys, runs or recordings.
`Resources/OpenGCUALogo.png` is the shared brand source. The build generates
the app icon from it, and the menu bar uses its alpha mask as a system-tinted
template. In-app brand marks use the generated app icon.

`--skip-runtime` retains existing Python packages, SDK and companion; use a full
build after SDK, dependency, Python or driver-lock changes.

### Keep permissions across development builds

An ad-hoc signature (`--identity -`) is tied to that exact binary. Recompiling
can invalidate Keychain and privacy grants even when the bundle ID/path stay
the same. Use one stable certificate and signing channel for repeated builds:

```sh
security find-identity -v -p codesigning
uv run --no-project --python 3.12 python packages/macos/scripts/build.py --skip-runtime \
  --identity 'Apple Development: Your Name (TEAMID)' --require-stable-signing
```

Install the certificate **and its private key** via Xcode's Apple account /
Manage Certificates, or your team's existing signing setup. A single available
Apple application identity is selected automatically; multiple identities need
an explicit choice. After successful verification, its fingerprint is pinned in
the ignored `build/signing-identity.json`. Later builds reuse it. If it disappears,
the build fails instead of silently changing identity. `SIGNING_IDENTITY` or
`--identity` explicitly overrides the pin. `--require-stable-signing` fails before
building when no certificate is available; ordinary development/CI can still
use ad-hoc signing with a warning.

Switching from ad-hoc to a certificate, or between development and distribution
signing, can require one fresh grant. Keep the app at a consistent path and retain
the same signing channel afterwards. Screen Recording may also have OS-managed
reminders; stable signing does not override those policies. This setup does not
reset TCC, broaden Keychain ACLs, or change code-signing requirements.
See Apple's [code signing requirements](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements).

To refresh Python dependency pins deliberately:

```sh
uv pip compile packages/harness/runtime/pyproject.toml --python-version 3.12 \
  --python-platform aarch64-apple-darwin --generate-hashes \
  --output-file packages/macos/requirements.lock
```

The [native macOS workflow](../../.github/workflows/macos-app.yml) builds, tests,
checks relocation and uploads a development DMG. The
[release workflow](../../.github/workflows/release.yml) calls it on version tags
and publishes the DMG alongside Python SDK downloads in GitHub Releases. These workflows do not use
signing secrets. `macos-15` is an Apple Silicon runner per
[GitHub's runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

### Public distribution

```sh
SIGNING_IDENTITY='Developer ID Application: Your Organization (TEAMID)' \
  uv run --no-project --python 3.12 python packages/macos/scripts/build.py
NOTARY_PROFILE='your-existing-keychain-profile' bash packages/macos/scripts/package.sh
```

Signing works from nested Python libraries outward with hardened runtime. The
upstream helper retains its own signature; never deep-resign it. The packaging
script optionally submits to Apple, waits for acceptance, staples and assesses
the app, then creates the DMG and SHA-256 file. No credentials are stored in the
repository.

Before public release, complete these outstanding items:

- Use your organization’s Developer ID certificate and notarization account;
  development signing is not a notarized public release.
- Validate first-run Accessibility/Screen Recording, duplex microphone/playback and interruption, real model tasks
  and cancellation on clean supported macOS installations. Existing permissions
  on a development Mac are not evidence of clean-install onboarding.

## Native interface

The UI uses native grouped forms, tabs, sidebar navigation, SF Symbols, semantic system
colors and popover materials. The brief setup assistant defers voice setup until
needed. Design references: Apple's [macOS guidance](https://developer.apple.com/design/human-interface-guidelines/designing-for-macos),
[onboarding](https://developer.apple.com/design/human-interface-guidelines/onboarding),
[settings](https://developer.apple.com/design/human-interface-guidelines/settings),
[materials](https://developer.apple.com/design/human-interface-guidelines/materials),
and [sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars).
The legacy local dictation implementation uses Apple's
[on-device recognition](https://developer.apple.com/documentation/speech/sfspeechrecognizer/supportsondevicerecognition)
capability check.

### Microphone selection

The voice bar's **输入音源** menu and Settings both offer **系统默认** and each
available microphone. The selection is saved by persistent Core Audio device UID
for OpenGCUA only; the app never changes macOS's global default input. Switching
sources reconnects the voice conversation while the computer task keeps running.
Default-device changes are followed during voice mode; a missing explicitly
selected device stops capture with a source-selection error instead of silently
recording another microphone.

The voice bar now separates microphone level from assistant playback. A dBFS input
meter and signal label show whether PCM frames arrive and contain sound. Waveform
scaling uses decibels to make quiet speech visible. A five-second no-frame watchdog
reports a stalled source rather than showing an indefinite listening state.
