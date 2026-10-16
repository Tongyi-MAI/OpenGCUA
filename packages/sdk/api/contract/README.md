# API contracts

`operations-v0.3.json` is the common public operation, default and capability-name
schema. Run `python scripts/generate_contract.py` after editing it, or `--check`
to verify generated Python/TypeScript declarations and default fixtures. Python
signature checks and native-backed cross-language fixtures guard drift. Rust uses
this schema for public capability mapping and explicit wire validators for admission.

The v0.2 Android/macOS profiles remain historical native capability references;
API 0.3 retains their behavior. `provider-v1.schema.json` describes the base
correlated envelope; `provider-v1.1.schema.json` adds unsolicited lifecycle events,
cancellation and browser artifact transfer. No daemon transport is implemented.

The Linux host entrypoint `Runtime.add_linux` is generated from the same operation schema. Its X11 provider implements desktop-scoped capabilities; app/window and native Wayland operations are explicitly unsupported. See `docs/linux.md` for supported units and input constraints.
