use super::*;

impl MacosProvider {
    pub(super) async fn action(&self, q: &Value) -> Result<Value, Error> {
        let action = text(q, "action");
        if self.managed_input && matches!(action, "key_up" | "button_up" | "release_all") {
            return self.managed_action(q, &json!({})).await;
        }
        if action == "launch" {
            let app = text(q, "app");
            if app.is_empty() {
                return Err(error("INVALID_ARGUMENT", "provide app bundle ID or name"));
            }
            self.tool(
                "launch_app",
                if app.contains('.') {
                    json!({"bundle_id":app})
                } else {
                    json!({"name":app})
                },
            )
            .await?;
            self.apps().await?;
            let state = self.state.lock().await;
            let matches: Vec<_> = state
                .apps
                .values()
                .filter(|a| a.native_id == app || a.label.eq_ignore_ascii_case(app))
                .collect();
            if matches.len() != 1 {
                return Err(error(
                    "ACTION_OUTCOME_UNKNOWN",
                    "launch completed but the resulting app is ambiguous",
                ));
            }
            return Ok(json!({"execution":"completed","app_id":matches[0].id,"effect":"unknown"}));
        }
        if action == "clipboard_write" {
            if !q["app_id"].is_null() || !q["window_id"].is_null() || !q["display_id"].is_null() {
                return Err(error(
                    "PERMISSION_DENIED",
                    "clipboard requires whole Mac scope",
                ));
            }
            let r = self
                .tool("clipboard_write", json!({"text":q["text"]}))
                .await?;
            return Ok(receipt(&r));
        }
        if action == "activate" {
            let pid = self.pid(text(q, "app_id")).await?;
            let mut args = json!({"pid":pid});
            if q["window_id"].is_string() {
                args["window_id"] = json!(self.target_window(q).await?.native_id);
            }
            let r = self.tool("bring_to_front", args).await?;
            return Ok(receipt(&r));
        }
        let delivery = text(q, "delivery");
        let delivery = if delivery.is_empty() || delivery == "auto" {
            "background"
        } else {
            delivery
        };
        if !matches!(delivery, "background" | "foreground") {
            return Err(error("INVALID_ARGUMENT", "invalid delivery policy"));
        }
        if action == "set_text" && delivery == "foreground" {
            return Err(error(
                "UNSUPPORTED_CAPABILITY",
                "macOS AX set_text uses background set_value; use input.text with foreground delivery for visible typing",
            ));
        }
        let desktop = q["app_id"].is_null() && q["window_id"].is_null();
        let mut args = json!({});
        if !desktop {
            let w = self.target_window(q).await?;
            let state = self.state.lock().await;
            let app = state
                .apps
                .get(&w.app_id)
                .ok_or_else(|| error("STALE_TARGET", "app ended"))?;
            args["pid"] = json!(app.pid);
            args["window_id"] = json!(w.native_id);
            if let Some(node) = q["node_id"].as_str() {
                let node = state
                    .nodes
                    .get(node)
                    .ok_or_else(|| error("STALE_TARGET", "AX snapshot was replaced"))?;
                if node["target_token"] != q["target_token"]
                    || node["target"]["window_id"] != w.id
                    || state.snapshots.get(&w.id).map(String::as_str) != q["snapshot_id"].as_str()
                {
                    return Err(error("STALE_TARGET", "AX token was superseded"));
                }
                args["element_token"] = q["target_token"].clone();
            }
        } else if delivery != "foreground" && !matches!(action, "set_frame" | "invoke_menu") {
            return Err(error(
                "INPUT_REFUSED",
                "global desktop input requires explicit foreground delivery",
            ));
        }
        if let Some(image_id) = q["image_token"].as_str() {
            let screen = if desktop {
                Some(self.tool("get_screen_size", json!({})).await?)
            } else {
                None
            };
            let state = self.state.lock().await;
            let basis = state
                .images
                .get(image_id)
                .ok_or_else(|| error("STALE_TARGET", "image basis expired"))?;
            if basis["window_id"] != q["window_id"] || basis["app_id"] != q["app_id"] {
                return Err(error("INVALID_ARGUMENT", "image scope mismatch"));
            }
            if let Some(screen) = screen {
                let screen = &screen["structuredContent"];
                if basis["bounds"].is_null()
                    || screen["width"] != basis["bounds"]["width"]
                    || screen["height"] != basis["bounds"]["height"]
                    || screen["scale_factor"] != basis["scale_factor"]
                {
                    return Err(error(
                        "GEOMETRY_CHANGED",
                        "primary display geometry changed; observe again",
                    ));
                }
            }
            if !desktop {
                let w = state
                    .windows
                    .get(text(q, "window_id"))
                    .ok_or_else(|| error("STALE_TARGET", "window closed"))?;
                let current = rect(&w.bounds);
                let captured = &basis["bounds"];
                if captured.is_null()
                    || current.is_null()
                    || number(&current, "width") != number(captured, "width")
                    || number(&current, "height") != number(captured, "height")
                {
                    return Err(error(
                        "GEOMETRY_CHANGED",
                        "window capture geometry changed; observe again",
                    ));
                }
            }
            args["x"] = q["x"].clone();
            args["y"] = q["y"].clone();
        }
        if self.managed_input
            && matches!(
                action,
                "key_down"
                    | "button_down"
                    | "wheel"
                    | "move"
                    | "drag"
                    | "point_click"
                    | "press_key"
                    | "input_text"
            )
        {
            if delivery != "foreground" {
                return Err(error(
                    "INPUT_REFUSED",
                    "Managed native input requires foreground delivery",
                ));
            }
            if q["node_id"].is_string() {
                return Err(error(
                    "UNSUPPORTED_CAPABILITY",
                    "Focus the AX target separately before managed input",
                ));
            }
            return self.managed_action(q, &args).await;
        }
        let mut tool = match action {
            "click" | "point_click" | "perform" => "click",
            "set_text" => "set_value",
            "input_text" => "type_text",
            "press_key" => "press_key",
            "wheel_scroll" => "scroll",
            "drag" => "drag",
            "move" => "move_cursor",
            "set_frame" => "set_window_frame",
            "invoke_menu" => "invoke_menu",
            _ => {
                return Err(error(
                    "UNSUPPORTED_CAPABILITY",
                    format!("unsupported macOS action {action}"),
                ))
            }
        };
        match action {
            "click" | "point_click" => {
                args["button"] = json!(match text(q, "button") {
                    "" | "primary" => "left",
                    "secondary" => "right",
                    "middle" => "middle",
                    _ => return Err(error("INVALID_ARGUMENT", "invalid click button")),
                });
                let count = q["count"].as_u64().unwrap_or(1);
                if !(1..=3).contains(&count) {
                    return Err(error("INVALID_ARGUMENT", "click count must be 1..3"));
                }
                if action == "click" && count != 1 {
                    return Err(error("UNSUPPORTED_CAPABILITY", "AX click supports count=1"));
                }
                args["count"] = json!(count);
            }
            "perform" => {
                let action = match text(q, "node_action") {
                    "macos.ax.press" => "press",
                    "macos.ax.show_menu" => "show_menu",
                    "macos.ax.pick" => "pick",
                    "macos.ax.confirm" => "confirm",
                    "macos.ax.cancel" => "cancel",
                    "macos.ax.open" => "open",
                    _ => return Err(error("UNSUPPORTED_CAPABILITY", "unknown native action")),
                };
                args["action"] = json!(action);
            }
            "set_text" => {
                args["value"] = q["text"].clone();
            }
            "input_text" => {
                args["text"] = q["text"].clone();
            }
            "press_key" => {
                let (key, modifiers) = portable_key(text(q, "key"), &q["modifiers"])?;
                if modifiers.is_empty() {
                    args["key"] = json!(key);
                } else {
                    // Cua's dedicated chord route preserves native menu-equivalent
                    // semantics. This is initial translation, never action replay.
                    tool = "hotkey";
                    let mut keys: Vec<String> = modifiers.into_iter().map(str::to_owned).collect();
                    keys.push(key.to_owned());
                    args["keys"] = json!(keys);
                }
            }
            "wheel_scroll" => {
                if q["unit"] != "lines" || q["amount"].as_u64().is_none_or(|v| v == 0 || v > 50) {
                    return Err(error(
                        "UNSUPPORTED_CAPABILITY",
                        "Cua scroll requires integer amount in 1..50 and unit=lines",
                    ));
                }
                args["direction"] = q["direction"].clone();
                args["amount"] = q["amount"].clone();
                args["by"] = json!("line");
            }
            "drag" => {
                if q["hold_ms"].as_u64().unwrap_or(0) != 0 {
                    return Err(error(
                        "UNSUPPORTED_CAPABILITY",
                        "Initial drag hold requires the signed Cua managed-input extension",
                    ));
                }
                args["from_x"] = q["x"].clone();
                args["from_y"] = q["y"].clone();
                args["to_x"] = q["end_x"].clone();
                args["to_y"] = q["end_y"].clone();
                args["duration_ms"] = q["duration_ms"].clone();
                args.as_object_mut().unwrap().remove("x");
                args.as_object_mut().unwrap().remove("y");
            }
            "move" => {
                if q["steps"].as_u64().unwrap_or(1) != 1 {
                    return Err(error(
                        "UNSUPPORTED_CAPABILITY",
                        "Stepped movement requires the signed Cua managed-input extension",
                    ));
                }
                if !desktop {
                    return Err(error(
                        "UNSUPPORTED_CAPABILITY",
                        "Cua window cursor is an overlay, not pointer hover; bind the desktop",
                    ));
                }
                tool = "move_cursor";
            }
            "set_frame" => {
                for k in ["x", "y", "width", "height"] {
                    if q[k].as_f64().is_none_or(|n| {
                        !n.is_finite() || (matches!(k, "width" | "height") && n <= 0.0)
                    }) {
                        return Err(error("INVALID_ARGUMENT", "invalid window frame"));
                    }
                    args[k] = q[k].clone();
                }
            }
            "invoke_menu" => {
                args["path"] = q["path"].clone();
            }
            _ => {}
        }
        if !matches!(
            tool,
            "set_value" | "set_window_frame" | "invoke_menu" | "move_cursor"
        ) {
            args["delivery_mode"] = json!(delivery);
        }
        if desktop {
            args["scope"] = json!("desktop");
        }
        let r = self.tool(tool, args).await?;
        let mut result = receipt(&r);
        result["policy"] = json!("cua_activation");
        Ok(result)
    }
}

impl MacosProvider {
    async fn managed_action(&self, q: &Value, target: &Value) -> Result<Value, Error> {
        let mut args = json!({"scope":"desktop", "action":q["action"], "input_scope":format!("{}/{}/{}",text(q,"app_id"),text(q,"window_id"),text(q,"display_id")), "timeout_ms":q["timeout_ms"]});
        if !target["pid"].is_null() {
            args["expected_pid"] = target["pid"].clone();
            args["input_scope"] = json!(format!("{}/{}", target["pid"], target["window_id"]));
        }
        for key in [
            "key",
            "modifiers",
            "text",
            "button",
            "count",
            "steps",
            "hold_ms",
            "duration_ms",
            "delta_x",
            "delta_y",
            "unit",
        ] {
            if let Some(value) = q.get(key) {
                args[key] = value.clone();
            }
        }
        if let Some(token) = q["image_token"].as_str() {
            let state = self.state.lock().await;
            let basis = state
                .images
                .get(token)
                .ok_or_else(|| error("STALE_TARGET", "image expired"))?;
            let current_bounds = basis["window_id"]
                .as_str()
                .and_then(|id| state.windows.get(id))
                .map(|w| rect(&w.bounds));
            let bounds = current_bounds.as_ref().unwrap_or(&basis["bounds"]);
            let (width, height) = (number(basis, "width"), number(basis, "height"));
            if width <= 0. || height <= 0. {
                return Err(error("GEOMETRY_CHANGED", "image geometry unavailable"));
            }
            for (x, y, out_x, out_y) in [
                ("x", "y", "screen_x", "screen_y"),
                ("end_x", "end_y", "end_screen_x", "end_screen_y"),
            ] {
                if q[x].is_number() {
                    args[out_x] =
                        json!(number(bounds, "x") + number(q, x) * number(bounds, "width") / width);
                    args[out_y] = json!(
                        number(bounds, "y") + number(q, y) * number(bounds, "height") / height
                    );
                }
            }
        }
        let result = self.tool("managed_input", args).await?;
        Ok(receipt(&result))
    }
}
