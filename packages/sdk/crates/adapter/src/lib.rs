//! Provider contract shared by runtime clients and platform adapters.
use mu_protocol::{Error, Reply, Request};
use std::{future::Future, pin::Pin};

pub type ProviderFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, Error>> + Send + 'a>>;

pub trait Provider: Send + Sync {
    fn request(&self, request: Request) -> ProviderFuture<'_, Reply>;
    fn close(&self) -> ProviderFuture<'_, ()>;
}

pub mod host;
