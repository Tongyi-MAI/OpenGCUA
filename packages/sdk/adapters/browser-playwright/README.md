# Playwright provider

Private TypeScript JSON-lines worker, pinned to Playwright 1.63.0. Rust starts one
worker/browser process for each session-owned context. Public callers use SDK
handles. `identity.ts` is the only module coupled to the pinned `aria-ref` mapping.

```sh
npm ci
npm run build
```

Compiled `dist` files are embedded in the native distributions so Android/macOS-only
builds do not need Node. Regenerate them whenever TypeScript changes. Setup is
explicit: `mu setup browser` (requires Node.js 22+). See
[SDK installation](../../README.md#install). The Python distribution includes the
compiled worker used by the native SDK; the source tree also contains the worker's
TypeScript implementation and local diagnostics.

`tests/cached-browser-worker.mjs` is an opt-in local diagnostic for an already
installed Chromium. Results from it do not certify matching Playwright binaries.
