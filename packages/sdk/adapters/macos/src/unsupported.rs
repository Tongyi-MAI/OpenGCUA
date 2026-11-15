//! Preserve the native API on hosts which cannot run the macOS driver.
use mu_adapter::{Provider, ProviderFuture};
use mu_protocol::{Error, Reply, Request};
use serde_json::Value;
use std::sync::Arc;

pub struct MacosProvider;

fn unsupported() -> Error {
    Error::new("UNSUPPORTED_CAPABILITY", "macOS adapter requires macOS")
}

impl MacosProvider {
    pub async fn connect() -> Result<Arc<Self>, Error> {
        Err(unsupported())
    }

    pub async fn connect_at(
        _app_path: Option<String>,
        _socket: Option<String>,
    ) -> Result<Arc<Self>, Error> {
        Err(unsupported())
    }

    pub async fn doctor(&self) -> Result<Value, Error> {
        Err(unsupported())
    }
}

impl Provider for MacosProvider {
    fn request(&self, _request: Request) -> ProviderFuture<'_, Reply> {
        Box::pin(async { Err(unsupported()) })
    }

    fn close(&self) -> ProviderFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn macos_connection_is_explicitly_unsupported() {
        match MacosProvider::connect_at(None, None).await {
            Ok(_) => panic!("macOS driver unexpectedly connected on a non-Unix host"),
            Err(error) => assert_eq!(error.code, "UNSUPPORTED_CAPABILITY"),
        }
    }
}
