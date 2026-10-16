"""macOS adapter.

The Python-side adapter glue re-exports :mod:`mai_ui.adapters`; the native macOS
provider (Rust) that sits above the OS accessibility / screen-capture APIs lives in
``packages/sdk/adapters/macos`` of this repository.
"""
from mai_ui import adapters as _adapters

globals().update({k: v for k, v in vars(_adapters).items() if not k.startswith("_")})
