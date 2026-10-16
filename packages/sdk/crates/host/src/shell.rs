use base64::{engine::general_purpose::STANDARD, Engine};
use mu_protocol::Error;
use mu_runtime::embedded::{budget, err, uid};
use serde_json::{json, Value};
pub fn prepare(q: &Value) -> Result<Value, Error> {
    let providers = q["description"]["shell_providers"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let eligible: Vec<_> = providers
        .iter()
        .filter(|p| p["state"] == "ready" && (q["provider"].is_null() || q["provider"] == p["id"]))
        .collect();
    if eligible.len() != 1 {
        return Err(err("SHELL_PROVIDER_UNAVAILABLE"));
    }
    let p = eligible[0];
    let timeout = budget(q, 30000)?.as_millis() as u64;
    let max = match q.get("max_output_bytes") {
        Some(v) => v.as_u64().ok_or_else(|| err("INVALID_ARGUMENT"))?,
        None => 1048576,
    };
    if max > 1048576 {
        return Err(err("INVALID_ARGUMENT"));
    }
    if q.get("cwd").is_some_and(|v| !v.is_null() && !v.is_string()) {
        return Err(err("INVALID_ARGUMENT"));
    }
    if q.get("env").is_some_and(|v| {
        !v.is_null()
            && !v.as_object().is_some_and(|e| {
                e.iter().all(|(k, v)| {
                    !k.is_empty()
                        && !k.contains(['=', '\0'])
                        && v.as_str().is_some_and(|s| !s.contains('\0'))
                })
            })
    }) {
        return Err(err("INVALID_ARGUMENT"));
    }
    let id = q["action_id"]
        .as_str()
        .map(str::to_string)
        .unwrap_or_else(uid);
    let mut wire = json!({"op":"shell","provider_id":p["id"],"command_id":id,"timeout_ms":timeout,"max_output_bytes":max});
    let command = &q["command"];
    if command.is_string() {
        wire["script"] = command.clone();
        wire["interpreter"] = p["default_interpreter"].clone();
    } else if command.as_array().is_some_and(|a| {
        !a.is_empty()
            && a.iter()
                .all(|v| v.as_str().is_some_and(|s| !s.contains('\0')))
    }) {
        wire["argv"] = command.clone();
    } else if command["source"].is_string() {
        if !command["interpreter"].is_null() && command["interpreter"] != p["default_interpreter"] {
            return Err(err("UNSUPPORTED_CAPABILITY"));
        }
        wire["script"] = command["source"].clone();
        wire["interpreter"] = p["default_interpreter"].clone();
    } else {
        return Err(err("INVALID_ARGUMENT"));
    }
    for field in ["cwd", "env", "stdin"] {
        if !q[field].is_null() {
            wire[field] = q[field].clone();
        }
    }
    if wire["script"].as_str().is_some_and(|s| s.contains('\0'))
        || wire["cwd"].as_str().is_some_and(|s| s.contains('\0'))
    {
        return Err(err("INVALID_ARGUMENT"));
    }
    if let Some(data) = wire["stdin"].as_str() {
        if STANDARD
            .decode(data)
            .map_err(|_| err("INVALID_ARGUMENT"))?
            .len()
            > 65536
        {
            return Err(err("INVALID_ARGUMENT"));
        }
    }
    Ok(json!({"wire":wire,"provider":p,"command_id":id}))
}
