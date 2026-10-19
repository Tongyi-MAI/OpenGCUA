# macOS provider

Rust provider for the local Mac, using **unmodified Cua Driver 0.36.0** over a
persistent MCP stdio connection to its signed app daemon. Python adds lifecycle
and public handles; native operations and host services stay in Rust.

- `src/setup.rs`: explicit download, digest/signature checks, installation and lease.
- `src/provider.rs`: MCP initialization, errors, endpoint/app/window identity, routing.
- `src/provider/observation.rs`: Cua projection to shared AX and multi-image values.
- `src/provider/actions.rs`: exact target validation and private native translations.
- `fixture/`: owned AppKit, Chromium and deterministic MCP fixtures.
- `cua-driver.lock.json`: pinned release, artifact digest and signing identity.

Shared AX rendering, delivery histories, ownership and action ledger remain in
`crates/core` and `crates/runtime`. Local files/processes live in
`crates/adapter/src/host.rs` for reuse by other native desktop providers.

See the [real-device quick start](../../../../QUICKSTART.md) and
[upstream notices](../../python/mai_ui/THIRD_PARTY_NOTICES.md).
