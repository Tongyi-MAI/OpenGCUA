//! Public capability names and scope filtering shared by both bindings.
use serde_json::{json, Value};
pub fn describe(
    offered: &Value,
    platform: &str,
    provider: &str,
    routing: &Value,
    access: &str,
) -> Value {
    let schema: Value =
        serde_json::from_str(include_str!("../../../api/contract/operations-v0.3.json")).unwrap();
    let mut result = serde_json::Map::new();
    let app = routing["app_id"].is_string();
    let display = routing["display_id"].is_string();
    let window = routing["window_id"].is_string();
    let tab = routing["tab_id"].is_string();
    for (public, backend) in schema["capabilities"].as_object().unwrap() {
        let mut detail = offered[backend.as_str().unwrap()]
            .as_object()
            .cloned()
            .unwrap_or_default();
        let mut state = detail
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("unsupported");
        if (app || display)
            && [
                "apps",
                "displays",
                "shell",
                "processes",
                "files",
                "android",
                "clipboard",
            ]
            .contains(&public.split('.').next().unwrap_or(""))
        {
            state = "unsupported";
        }
        if platform == "linux"
            && (public.starts_with("apps.")
                || public.starts_with("windows.")
                || matches!(public.as_str(), "ui.vision.swipe" | "ui.vision.long_press"))
        {
            state = "unsupported";
        }
        if platform == "android" && app && public.starts_with("ui.vision") {
            state = "unsupported";
        }
        if platform == "macos"
            && ((!app && public.starts_with("ui.ax"))
                || matches!(public.as_str(), "ui.vision.swipe" | "ui.vision.long_press")
                || (app && public == "ui.vision.move"))
        {
            state = "unsupported";
        }
        if window && public.starts_with("windows.") {
            state = "unsupported";
        }
        if platform == "browser"
            && ((!tab && public.starts_with("ui."))
                || matches!(public.as_str(), "ui.vision.swipe" | "ui.vision.long_press"))
        {
            state = "unsupported";
        }
        if access == "read"
            && !["observe", ".list", ".read", ".stat", ".wait_for_stabilize"]
                .iter()
                .any(|s| public.ends_with(s))
        {
            state = "denied";
        }
        let state = state.to_string();
        detail.insert("state".into(), json!(state));
        detail.insert("provider".into(), json!(provider));
        result.insert(public.clone(), Value::Object(detail));
    }
    if platform == "browser" {
        for (k, v) in offered.as_object().into_iter().flatten() {
            if k.starts_with("browser.") {
                let mut v = v.clone();
                if tab
                    && matches!(
                        k.as_str(),
                        "browser.cookies_read"
                            | "browser.cookies_set"
                            | "browser.cookies_clear"
                            | "browser.storage_state"
                            | "browser.network_set_rules"
                            | "browser.new_tab"
                            | "browser.list_tabs"
                            | "browser.get_tab"
                            | "browser.destroy"
                    )
                {
                    v["state"] = json!("denied");
                } else if access == "read"
                    && !matches!(
                        k.as_str(),
                        "browser.list_tabs"
                            | "browser.get_tab"
                            | "browser.downloads_list"
                            | "browser.downloads_get"
                            | "browser.artifact_read"
                            | "browser.cookies_read"
                            | "browser.storage_read"
                            | "browser.storage_state"
                    )
                {
                    v["state"] = json!("denied");
                }
                result.insert(k.clone(), v);
            }
        }
    }
    for feature in [
        "browser",
        "macos",
        "ios",
        "windows_os",
        "linux",
        "clipboard",
        "android.displays.create",
        "processes.spawn",
        "ui.vision.pinch",
        "ui.vision.drag",
    ] {
        if !result.contains_key(feature)
            && !(platform == "macos" && matches!(feature, "macos" | "clipboard"))
        {
            result.insert(
                feature.into(),
                json!({"state":if feature=="browser"&&platform=="browser"{"ready"}else{"wip"}}),
            );
        }
    }
    Value::Object(result)
}
