//! Cua is a native driver dependency; agent-facing semantics belong to mai-ui.
#[cfg(unix)]
mod provider;
#[cfg(not(unix))]
#[path = "unsupported.rs"]
mod provider;
pub mod setup;
pub use provider::MacosProvider;
