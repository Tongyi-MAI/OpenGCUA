# Windows preview validation

Source baseline: OpenGCUA `55c33aea792b48b9c1330a514bfcc8f6d3ecc704`.
Validation date: 15 November 2026.

## Executed locally

The checks below ran on native Windows x86-64 with CPython 3.12.10. The SDK was
built with Rust 1.100.0 and the MSVC toolchain. The driver was the official
checksum-verified CUA Driver 0.38.0 binary. The CI configuration retains the
project's Rust 1.89 minimum; that compiler version was not run locally.

- Rust workspace: **39 passed** using `cargo test --workspace --locked --offline`.
- SDK offline suite: **51 passed, 2 skipped**. The skips are opt-in desktop and
  browser tests, which were run separately.
- Selected harness regressions: **284 passed, 3 skipped** across Windows host,
  journal, journal executor, interruption, SDK-runtime contract and memory-store
  modules. Skips concern unavailable symlink privileges and two POSIX permission
  cases; Windows ACL failures have their own coverage.
- Installed Windows wheel: isolated `python -I` smoke check passed, including
  imports, native runtime/session creation, packaged resources and `mu info`.
- Driver installer: fresh offline installation and replacement from the pinned
  archive passed; wrong checksums, unexpected archive members and publication
  failure rollback have regression coverage.
- Example agent configuration: placeholders were refused before desktop/model
  execution, as intended.
- New Python files: static checks and formatting passed. Changed-file whitespace
  checks passed. Independent source review led to fixes for cancelled operations,
  stale process events, refusal receipts, nullable metadata, blocked writes and
  installation replacement.

## Controlled real Windows test

`packages/sdk/tests/test_windows_live.py` passed against the installed native
wheel and pinned driver after the review fixes. The fixture is a disposable
Win32 window containing a standard edit, button and counter.

The test enumerated and bound the fixture through the SDK, replaced the edit
text through background accessibility input, invoked the button, and read the
fixture's own text/counter for independent verification. It also captured the
exact bound window, decoded the SDK image, and clicked through the image's
coordinate mapping. A stale accessibility reference was rejected before input.
Foreground window and physical
pointer state were sampled every 5 ms; the test observed no changes. Sampling
does not establish that an arbitrarily shorter transient is impossible.

This proves the tested paths for these standard controls. It does not establish
compatibility with arbitrary apps or a model's ability to complete a task.

## Controlled real browser test

`packages/sdk/tests/test_windows_browser_live.py` passed with portable Node
24.22.1, the SDK's pinned Playwright 1.63.0, and managed Chromium 153.0.8010.12.
`mu setup browser --engine chromium` completed on native Windows. The test
matched the harness's default isolated headless browser options and used only
a local data URL.

The SDK filled and submitted a form twice, read the resulting table back from
the DOM, checked cleared form fields, captured accessibility and PNG output,
and verified explicit tab closure and session cleanup. The two action receipts
reported execution completed with unknown effect; independent DOM read-back
provided the result evidence. No account, external website or model was used.

## Still unverified

- A model-driven browser/Excel/Word or PowerPoint workflow, including creation,
  saving, delivery statistics and independent saved-file verification.
- macOS and Linux regression execution; existing behavior was preserved in
  source and relevant contract fixtures, but those operating systems were not
  available for local execution.
- Remote GitHub Actions and the Windows Python 3.13 matrix.
- Application launch, foreground fallback, scrolling/drag and other operations
  explicitly listed as unsupported in the provider README.

The preview is ready for code review and further validation. It is **not** a
claim that the maintainer's full Windows acceptance milestone has been met.
