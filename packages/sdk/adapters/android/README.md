# Android adapter

`agent/` contains the resident Java UiAutomation bridge. `host/` is the Rust
`mu-broker` executable: explicit ADB selection, an OS writer lease, bridge startup,
request forwarding, native error translation, and conservative failure handling.
`fixture/` is the owned demo application used for native effect verification.
Build products go to the ignored `out/` directory.

From the repository root:

```sh
cargo build -p mu-adapter-android --bin mu-broker
.venv/bin/python scripts/build_android.py
.venv/bin/python scripts/verify_unified_android.py --serial YOUR_OWNED_TEST_DEVICE
```

The common subprocess transport is in `crates/client`; PyO3 is in
`crates/bindings-python`. Neither belongs in this adapter. Android system keys,
active-window coverage, and UTF-16 bridge offsets remain adapter-specific parts
of the preview protocol. See [the API contract](../../api/contract/python-android-v0.2.json).

## Restricted executor on a rooted benchmark device

ADB can remain root for setup and evaluation while only the JAR and its children
run as Android shell (UID/GID 2000). Build the native pre-ART launcher:

```sh
python scripts/build_android_launcher.py --ndk "$ANDROID_NDK_HOME" --abi arm64-v8a
export MAI_UI_ANDROID_EXECUTION_PROFILE=shell
export MAI_UI_ANDROID_LAUNCHER="$PWD/adapters/android/out/mu-android-shell-arm64-v8a"
```

The Rust broker pushes the launcher and JAR, then the launcher drops root IDs and
capabilities and sets inherited `no_new_privs` before executing `app_process`.
The JAR verifies its credentials before accepting requests. Startup fails when
the profile cannot be established; it never falls back to root. `inherit` remains
the default outside explicitly configured benchmark deployments.

`run-as` and setuid `su` cannot gain privileges from this process tree, including
when invoked indirectly by a shell script. APK flags and maintenance ADB are not
changed. Command failures return normal shell results; callers using `check=True`
receive ordinary SDK errors and may continue in the next cell.

This restricts process credentials, not filesystem visibility or network access.
SELinux remains as configured by the device, and shell still has broad debugging
authority. A host Python worker with independent root ADB access can bypass this
boundary; isolating that worker and privileged controller endpoints is separate
work. This profile is not advertised as a complete agent sandbox.

On an owned, idle root test device, `scripts/verify_android_shell_profile.py`
checks denied escalation/private reads, AX and visual effects, shared-file I/O,
and continued root maintenance access using the dedicated demo APK.
