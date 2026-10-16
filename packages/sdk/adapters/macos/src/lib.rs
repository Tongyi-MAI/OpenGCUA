//! Cua is a native driver dependency; agent-facing semantics belong to mai-ui.
mod provider;
pub mod setup;
pub use provider::MacosProvider;
