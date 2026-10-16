//! Provider-independent wire validation and capability/scope admission.
use crate::embedded::err;
use mu_protocol::Error;
use serde_json::Value;

pub fn capability(q: &Value) -> Result<Option<String>, Error> {
    let action = q["action"].as_str().unwrap_or("");
    let c = match q["op"].as_str().unwrap_or("") {
        "describe" => return Ok(None),
        "observe" => {
            if q["tree"] == true {
                "ui.observe.tree"
            } else {
                "ui.observe.screenshot"
            }
        }
        "list_apps" => "device.list_apps",
        "list_windows" => "device.list_windows",
        "displays" => "device.list_displays",
        "wait_for_stabilize" => "ui.wait_for_stabilize",
        "shell" => "shell.run",
        "clipboard" => "clipboard.read",
        "file" => {
            return Ok(Some(format!(
                "files.{}",
                q["operation"]
                    .as_str()
                    .ok_or_else(|| err("INVALID_ARGUMENT"))?
            )))
        }
        "browser" => {
            return Ok(Some(format!(
                "browser.{}",
                q["operation"]
                    .as_str()
                    .ok_or_else(|| err("INVALID_ARGUMENT"))?
            )))
        }
        "action" => match action {
            "click" => "ui.click.primary",
            "point_click" => "ui.click.coordinates",
            "set_text" => "ui.set_text",
            "select_text" => "ui.select_text",
            "perform" => "ui.perform",
            "scroll" => "ui.scroll.steps",
            "node_scroll" => "ui.scroll.viewport",
            "point_scroll" | "wheel_scroll" => "ui.scroll.coordinates",
            "point_long_press" => "ui.long_press",
            "move" => "ui.move",
            "drag" => "ui.drag",
            "input_text" => "ui.input_text",
            "press_key" => "ui.press_key",
            "key_down" | "key_up" => "ui.key_state",
            "button_down" | "button_up" => "ui.button_state",
            "release_all" => "ui.release_all",
            "wheel" => "ui.wheel",
            "launch" | "activate" => "app.launch",
            "system" => "android.press",
            "clipboard_write" => "clipboard.write",
            "set_frame" => "macos.set_frame",
            "invoke_menu" => "macos.invoke_menu",
            _ => return Err(err("UNSUPPORTED_CAPABILITY")),
        },
        _ => return Err(err("UNSUPPORTED_CAPABILITY")),
    };
    Ok(Some(c.into()))
}
pub fn validate(
    q: &mut Value,
    platform: &str,
    description: &Value,
    resource: &str,
    endpoint: &str,
) -> Result<(), Error> {
    if let Some(cap) = capability(q)? {
        if description["capabilities"][&cap]["state"] != "ready" {
            return Err(Error::new("UNSUPPORTED_CAPABILITY", cap));
        }
    }
    let sub = q["app_id"].is_string()
        || q["window_id"].is_string()
        || q["display_id"].is_string()
        || q["tab_id"].is_string();
    let whole = matches!(
        q["op"].as_str(),
        Some("shell" | "file" | "clipboard" | "list_apps" | "displays")
    ) || matches!(q["action"].as_str(), Some("system" | "clipboard_write"));
    if sub && whole {
        return Err(err("UNSUPPORTED_CAPABILITY"));
    }
    if platform == "linux" {
        if ["app_id", "window_id", "tab_id"]
            .iter()
            .any(|k| !q[k].is_null())
            || q["display_id"].as_str().is_some_and(|d| d != "display:0")
        {
            return Err(err("UNSUPPORTED_CAPABILITY"));
        }
        if q["op"] == "action"
            && matches!(
                q["action"].as_str(),
                Some("point_scroll" | "point_long_press" | "node_scroll" | "scroll")
            )
        {
            return Err(err("UNSUPPORTED_CAPABILITY"));
        }
    }
    if platform == "browser" {
        if q["op"] == "browser" {
            crate::browser_policy::validate(q)?;
        }
        if q["op"] == "action" || q["op"] == "observe" {
            if !q["tab_id"].is_string() {
                return Err(Error::new(
                    "UNSUPPORTED_CAPABILITY",
                    "Bind a tab for UI operations",
                ));
            }
        }
        if let Some(tab) = q["tab_id"].as_str() {
            let tab_resource = format!("{endpoint}/{tab}");
            if resource != endpoint && resource != tab_resource {
                return Err(err("PERMISSION_DENIED"));
            }
        }
        if resource != endpoint
            && matches!(
                q["operation"].as_str(),
                Some(
                    "cookies_read"
                        | "cookies_set"
                        | "cookies_clear"
                        | "storage_state"
                        | "network_set_rules"
                        | "new_tab"
                        | "list_tabs"
                        | "get_tab"
                        | "destroy"
                )
            )
        {
            return Err(err("PERMISSION_DENIED"));
        }
        if q["operation"] == "playwright" && q["kind"] == "context" && resource != endpoint {
            return Err(err("PERMISSION_DENIED"));
        }
        if q.get("wait_until")
            .is_some_and(|v| !matches!(v.as_str(), Some("commit" | "domcontentloaded" | "load")))
        {
            return Err(err("INVALID_ARGUMENT"));
        }
    }
    if q["op"] == "file" {
        let path = |v: &Value| {
            v.as_str()
                .is_some_and(|s| s.starts_with('/') && !s.contains('\0'))
        };
        if !path(&q["path"]) {
            return Err(err("INVALID_ARGUMENT"));
        }
        for key in ["overwrite", "parents", "recursive"] {
            if q.get(key).is_some_and(|v| !v.is_boolean()) {
                return Err(err("INVALID_ARGUMENT"));
            }
        }
        if q["operation"] == "read"
            && q.get("max_bytes")
                .is_some_and(|v| !v.as_u64().is_some_and(|n| n <= 1048576))
        {
            return Err(err("INVALID_ARGUMENT"));
        }
        if q["operation"] == "move" && !path(&q["destination"]) {
            return Err(err("INVALID_ARGUMENT"));
        }
        if q["operation"] == "write" {
            use base64::Engine;
            if !q["data"]
                .as_str()
                .and_then(|s| base64::engine::general_purpose::STANDARD.decode(s).ok())
                .is_some_and(|b| b.len() <= 1048576)
            {
                return Err(err("INVALID_ARGUMENT"));
            }
        }
    }
    if platform == "android"
        && q["app_id"].is_string()
        && q["op"] == "observe"
        && q["screenshot"] == true
    {
        return Err(err("UNSUPPORTED_CAPABILITY"));
    }
    if platform == "macos" && q["op"] == "observe" && q["tree"] == true && !q["app_id"].is_string()
    {
        return Err(err("UNSUPPORTED_CAPABILITY"));
    }
    if q["op"] == "action" {
        let delivery = q["delivery"].as_str().unwrap_or("auto");
        if !matches!(delivery, "auto" | "background" | "foreground") {
            return Err(err("INVALID_ARGUMENT"));
        }
        if platform == "linux" {
            if delivery == "background" {
                return Err(err("UNSUPPORTED_CAPABILITY"));
            }
            q["delivery"] = Value::String("foreground".into());
        } else if platform == "android" {
            if delivery == "background" {
                return Err(err("UNSUPPORTED_CAPABILITY"));
            }
            q.as_object_mut().unwrap().remove("delivery");
        } else {
            q["delivery"] = Value::String(
                if delivery == "auto" {
                    "background"
                } else {
                    delivery
                }
                .into(),
            );
        }
        match q["action"].as_str().unwrap_or("") {
            "move" => {
                if q.get("steps")
                    .is_some_and(|v| v.as_u64().is_none_or(|n| !(1..=1000).contains(&n)))
                {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            "key_down" | "key_up" => {
                if !q["key"].as_str().is_some_and(|k| !k.is_empty()) {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            "button_down" | "button_up" => {
                if !matches!(
                    q["button"].as_str(),
                    Some("primary" | "secondary" | "middle")
                ) {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            "wheel" => {
                for key in ["delta_x", "delta_y"] {
                    if !q[key]
                        .as_f64()
                        .is_some_and(|n| n.is_finite() && n.abs() <= 100000.0)
                    {
                        return Err(err("INVALID_ARGUMENT"));
                    }
                }
                if !matches!(q["unit"].as_str(), Some("pixels" | "lines" | "steps")) {
                    return Err(err("INVALID_ARGUMENT"));
                }
                if (platform == "browser" && q["unit"] != "pixels")
                    || (platform == "linux" && q["unit"] != "steps")
                {
                    return Err(err("UNSUPPORTED_CAPABILITY"));
                }
                if platform == "linux"
                    && ["delta_x", "delta_y"]
                        .iter()
                        .any(|k| q[k].as_i64().is_none_or(|n| n.abs() > 100))
                {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            "click" | "point_click" => {
                let button = q["button"].as_str().unwrap_or("primary");
                let count = if q.get("count").is_some() {
                    q["count"].as_u64().ok_or_else(|| err("INVALID_ARGUMENT"))?
                } else {
                    1
                };
                if !matches!(button, "primary" | "secondary" | "middle")
                    || !(1..=3).contains(&count)
                {
                    return Err(err("INVALID_ARGUMENT"));
                }
                if platform == "android" && (button != "primary" || count != 1) {
                    return Err(err("UNSUPPORTED_CAPABILITY"));
                }
            }
            "set_text" | "input_text" => {
                if !q["text"].is_string() {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            "select_text" => {
                let text = q["selection_text"]
                    .as_str()
                    .ok_or_else(|| err("INVALID_ARGUMENT"))?;
                let a = q["start"].as_u64().ok_or_else(|| err("INVALID_ARGUMENT"))?;
                let b = q["end"].as_u64().ok_or_else(|| err("INVALID_ARGUMENT"))?;
                if a > b || b > text.chars().count() as u64 {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            "node_scroll" | "wheel_scroll" => {
                if !matches!(
                    q["direction"].as_str(),
                    Some("up" | "down" | "left" | "right")
                ) {
                    return Err(err("INVALID_ARGUMENT"));
                }
                if q["action"] == "wheel_scroll" && matches!(platform, "macos" | "linux") {
                    if q["unit"]
                        != if platform == "linux" {
                            "native_steps"
                        } else {
                            "lines"
                        }
                    {
                        return Err(err("UNSUPPORTED_CAPABILITY"));
                    }
                    if !q["amount"].as_u64().is_some_and(|n| (1..=50).contains(&n)) {
                        return Err(err("INVALID_ARGUMENT"));
                    }
                } else if !q["amount"].as_f64().is_some_and(|n| n > 0.0 && n <= 1.0) {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            "point_scroll" | "point_long_press" | "drag" => {
                let ms = q["duration_ms"]
                    .as_u64()
                    .ok_or_else(|| err("INVALID_ARGUMENT"))?;
                if !(50..=5000).contains(&ms) || ms >= q["timeout_ms"].as_u64().unwrap_or(3000) {
                    return Err(err("INVALID_ARGUMENT"));
                }
                if platform == "browser" && q["action"] != "drag" {
                    return Err(err("UNSUPPORTED_CAPABILITY"));
                }
                if q["action"] == "drag" && !q["hold_ms"].is_null() {
                    let hold = q["hold_ms"]
                        .as_u64()
                        .filter(|v| *v <= 5000)
                        .ok_or_else(|| err("INVALID_ARGUMENT"))?;
                    if hold + ms >= q["timeout_ms"].as_u64().unwrap_or(5000) {
                        return Err(err("INVALID_ARGUMENT"));
                    }
                }
            }
            "perform" => {
                if !q["_node_actions"]
                    .as_array()
                    .is_some_and(|a| a.iter().any(|a| a["id"] == q["node_action"]))
                {
                    return Err(err("UNSUPPORTED_CAPABILITY"));
                }
            }
            "press_key" => {
                let key = q["key"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| err("INVALID_ARGUMENT"))?
                    .to_lowercase();
                if !q["modifiers"]
                    .as_array()
                    .is_some_and(|a| a.iter().all(Value::is_string))
                {
                    return Err(err("INVALID_ARGUMENT"));
                }
                if platform != "browser" {
                    q["key"] = Value::String(key);
                }
            }
            "system" => {
                if !matches!(
                    q["button"].as_str(),
                    Some("back" | "home" | "recents" | "notifications" | "quick_settings")
                ) {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            "set_frame" => {
                if !q["window_id"].is_string() {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            "invoke_menu" => {
                if !q["app_id"].is_string()
                    || !q["path"].as_array().is_some_and(|p| {
                        !p.is_empty() && p.iter().all(|s| s.as_str().is_some_and(|s| !s.is_empty()))
                    })
                {
                    return Err(err("INVALID_ARGUMENT"));
                }
            }
            _ => {}
        }
    }
    Ok(())
}
