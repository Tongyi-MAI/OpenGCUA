# Cua managed-input candidate

`managed-input-v1.patch` applies to `trycua/cua` commit `d8028a7943087ee258dc1b4d19dc12a7cd27669c` (Cua driver 0.28.1). It adds a foreground CoreGraphics executor with scoped ownership, retained keys/buttons, dragged mouse movement, wheel deltas, bounded drag with initial hold, and fallible session-end cleanup. Composite input preserves previously held inputs. Failures attempt release and fence the input session.

Apply the patch in an isolated checkout, then run:

```sh
git apply /absolute/path/to/managed-input-v1.patch
cargo check --manifest-path libs/cua-driver/rust/Cargo.toml -p platform-macos --lib
```

The SDK negotiates the `managed_input` tool only when the reviewed driver lock has `managed_input_protocol: 1`, and requires its schema marker `x-mai-ui-input-version: 1`. The current lock intentionally lacks that flag. This historical source patch targets 0.28.1 and is not applied to the pinned 0.36.0 release. Keep the flag disabled until a compatible signed extension is reviewed and validated.

Before enabling it, build and sign a versioned driver artifact, update the archive hash/source commit/version under the existing signature checks, and validate the AppKit fixture's `input_events`, `pointer_pressed`, and independent text/counter state. Cover held modifiers across typing/clicks, multi-call drag, diagonal scroll, image scaling, target foreground checks, detach/session/disconnect cleanup, and failures. The candidate currently targets the primary display and requires foreground delivery. Its posted-event receipts establish dispatch, not application effects.
