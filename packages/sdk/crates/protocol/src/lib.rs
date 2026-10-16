//! Versioned provider messages and errors, without a binding or platform dependency.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub const PROTOCOL_MAJOR: u32 = 1;
pub const SDK_API_VERSION: &str = "0.3";

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Request {
    pub op: String,
    #[serde(
        default,
        rename = "_request_id",
        skip_serializing_if = "Option::is_none"
    )]
    pub request_id: Option<u64>,
    #[serde(flatten)]
    pub parameters: BTreeMap<String, Value>,
}

impl Request {
    pub fn is_mutation(&self) -> bool {
        if self.op == "browser"
            && self.parameters.get("operation").and_then(Value::as_str) == Some("playwright")
        {
            return playwright_method(
                self.parameters
                    .get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
                self.parameters
                    .get("method")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )
            .is_none_or(|v| v["effect"] != "read");
        }
        matches!(self.op.as_str(), "action" | "shell" | "create" | "destroy")
            || (self.op == "browser"
                && !matches!(
                    self.parameters.get("operation").and_then(Value::as_str),
                    Some(
                        "list_tabs"
                            | "get_tab"
                            | "downloads_list"
                            | "downloads_get"
                            | "cookies_read"
                            | "storage_state"
                            | "storage_read"
                            | "artifact_read"
                    )
                ))
            || (self.op == "file"
                && matches!(
                    self.parameters.get("operation").and_then(Value::as_str),
                    Some("write" | "mkdir" | "remove" | "move")
                ))
    }
}

pub fn playwright_method(kind: &str, method: &str) -> Option<&'static Value> {
    static CONTRACT: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    CONTRACT
        .get_or_init(|| {
            serde_json::from_str(include_str!("../../../api/contract/playwright-v1.json"))
                .expect("valid embedded contract")
        })
        .get("methods")?
        .get(kind)?
        .get(method)
}

/// Unsolicited provider notifications never compete with operation replies.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub event: String,
    #[serde(default)]
    pub resource_id: Option<String>,
    #[serde(default)]
    pub data: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Error {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub details: Option<Value>,
}
impl Error {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            details: None,
        }
    }
}
impl Error {
    pub fn with_details(mut self, details: Value) -> Self {
        self.details = Some(details);
        self
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}
impl std::error::Error for Error {}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Reply {
    #[serde(
        default,
        rename = "_request_id",
        skip_serializing_if = "Option::is_none"
    )]
    pub request_id: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<Error>,
}
impl Reply {
    pub fn validate(&self) -> Result<(), Error> {
        if self.result.is_some() == self.error.is_some() {
            return Err(Error::new(
                "PROTOCOL_MISMATCH",
                "reply requires exactly one result or error",
            ));
        }
        Ok(())
    }
}
