//! Serializable browser service contracts. Validate complete rule sets before dispatch.
use crate::embedded::err;
use mu_protocol::Error;
use serde_json::Value;
fn invalid() -> Error {
    err("INVALID_ARGUMENT")
}
fn strings(q: &Value, keys: &[&str]) -> Result<(), Error> {
    for k in keys {
        if q.get(k).is_some_and(|v| !v.is_string()) {
            return Err(invalid());
        }
    }
    Ok(())
}
fn headers(q: &Value) -> Result<(), Error> {
    if !q
        .as_object()
        .is_some_and(|h| h.values().all(Value::is_string))
    {
        return Err(invalid());
    }
    Ok(())
}
pub fn validate(q: &Value) -> Result<(), Error> {
    match q["operation"].as_str().unwrap_or("") {
        "playwright" => {
            let kind = q["kind"].as_str().ok_or_else(invalid)?;
            let method = q["method"].as_str().ok_or_else(invalid)?;
            let spec = mu_protocol::playwright_method(kind, method)
                .ok_or_else(|| err("UNSUPPORTED_CAPABILITY"))?;
            let args = q["args"].as_array().ok_or_else(invalid)?;
            if args.len() < spec["min"].as_u64().unwrap_or(0) as usize
                || args.len() > spec["max"].as_u64().unwrap_or(0) as usize
            {
                return Err(invalid());
            }
            if kind == "context" && !q["tab_id"].is_null()
                || kind != "context" && !q["tab_id"].is_string()
            {
                return Err(err("PERMISSION_DENIED"));
            }
            if kind == "locator" && !q["query"].is_array() {
                return Err(invalid());
            }
        }
        "evaluate" => {
            if !q["source"].is_string() {
                return Err(invalid());
            }
        }
        "goto" | "new_tab" => {
            if !q["url"].is_string() {
                return Err(invalid());
            }
        }
        "dialogs_set_rules" | "network_set_rules" => {
            for r in q["rules"].as_array().ok_or_else(invalid)? {
                let o = r.as_object().ok_or_else(invalid)?;
                if q["operation"] == "dialogs_set_rules" {
                    if o.keys().any(|k| {
                        !["type", "message", "action", "prompt_text"].contains(&k.as_str())
                    }) || !matches!(r["action"].as_str(), Some("accept" | "dismiss"))
                        || !matches!(
                            r["type"].as_str(),
                            Some("alert" | "confirm" | "prompt" | "beforeunload")
                        )
                    {
                        return Err(invalid());
                    }
                    strings(r, &["message", "prompt_text"])?;
                    if r.get("prompt_text").is_some()
                        && (r["type"] != "prompt" || r["action"] != "accept")
                    {
                        return Err(invalid());
                    }
                } else {
                    if o.keys().any(|k| {
                        ![
                            "url",
                            "method",
                            "resource_type",
                            "action",
                            "error_code",
                            "status",
                            "headers",
                            "content_type",
                            "body",
                            "body_base64",
                            "overrides",
                        ]
                        .contains(&k.as_str())
                    }) || !matches!(
                        r["action"].as_str(),
                        Some("continue" | "abort" | "fulfill" | "override")
                    ) {
                        return Err(invalid());
                    }
                    strings(
                        r,
                        &[
                            "url",
                            "method",
                            "resource_type",
                            "error_code",
                            "content_type",
                            "body",
                            "body_base64",
                        ],
                    )?;
                    if let Some(h) = r.get("headers") {
                        headers(h)?;
                    }
                    if let Some(code) = r.get("status") {
                        if !code.as_u64().is_some_and(|n| (100..=599).contains(&n)) {
                            return Err(invalid());
                        }
                    }
                    if r.get("body").is_some() && r.get("body_base64").is_some() {
                        return Err(invalid());
                    }
                    if let Some(v) = r.get("overrides") {
                        if !v.as_object().is_some_and(|v| {
                            v.keys()
                                .all(|k| ["url", "method", "headers", "body"].contains(&k.as_str()))
                        }) {
                            return Err(invalid());
                        }
                        strings(v, &["url", "method", "body"])?;
                        if let Some(h) = v.get("headers") {
                            headers(h)?;
                        }
                    } else if r["action"] == "override" {
                        return Err(invalid());
                    }
                }
            }
        }
        "upload" => {
            for f in q["files"].as_array().ok_or_else(invalid)? {
                if !f["name"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty() && !s.contains(['/', '\\', '\0']))
                    || !f["data"].is_string()
                {
                    return Err(invalid());
                }
                strings(f, &["mime_type"])?;
            }
        }
        "storage_read" | "storage_set" | "storage_remove" | "storage_clear" => {
            if !matches!(q["area"].as_str(), Some("local" | "session")) {
                return Err(invalid());
            }
            if matches!(
                q["operation"].as_str(),
                Some("storage_set" | "storage_remove")
            ) && !q["key"].is_string()
            {
                return Err(invalid());
            }
            if q["operation"] == "storage_set" && !q["value"].is_string() {
                return Err(invalid());
            }
        }
        "artifact_read" => {
            if q.get("offset").is_some_and(|v| v.as_u64().is_none())
                || q.get("max_bytes")
                    .is_some_and(|v| !v.as_u64().is_some_and(|n| (1..=1048576).contains(&n)))
            {
                return Err(invalid());
            }
        }
        _ => {}
    }
    Ok(())
}
