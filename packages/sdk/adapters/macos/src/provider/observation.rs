use super::*;

impl MacosProvider {
    pub(super) async fn observe(&self, q: &Value) -> Result<Value, Error> {
        let tree = q["tree"] == true;
        let screenshot = q["screenshot"] == true;
        let mut result = json!({"nodes":[],"images":[],"components":[],"width":1,"height":1,"captured_at":now(),"age_ms":0,"source":"macos.cua","truncated":tree,"pending_invalidations":0,"identity_mode":"snapshot","identity_epoch":null});
        {
            let mut state = self.state.lock().await;
            state.counter += 1;
            result["revision"] = json!(format!("capture:{}", state.counter));
        }
        if q["app_id"].is_null() && q["window_id"].is_null() {
            if tree {
                return Err(error(
                    "UNSUPPORTED_CAPABILITY",
                    "desktop AX requires a bound app or window",
                ));
            }
            let r = self.tool("get_desktop_state", json!({})).await?;
            let image = self.image(&r, None).await?;
            result["images"] = json!([image]);
            result["components"] = json!([{"id":"display:0","status":"ok"}]);
            return Ok(result);
        }
        let inventory = self.windows(q["app_id"].as_str()).await?;
        let mut windows = Vec::new();
        {
            let state = self.state.lock().await;
            for w in inventory["windows"].as_array().unwrap() {
                if q["window_id"].is_null() || w["id"] == q["window_id"] {
                    windows.push(state.windows[text(w, "id")].clone());
                }
            }
        }
        windows.sort_by_key(|w| w.native_id);
        if q["window_id"].is_string() && windows.is_empty() {
            return Err(error("STALE_TARGET", "window is closed"));
        }
        let mut nodes = Vec::new();
        let mut images = Vec::new();
        let mut components = Vec::new();
        let app_id = text(q, "app_id");
        let app_root = format!("{}:app", text(&result, "revision"));
        if tree {
            nodes.push(node(
                &app_root,
                None,
                app_id,
                None,
                "application",
                app_id,
                None,
            ));
        }
        for (i, w) in windows.iter().enumerate() {
            if tree && i >= 8 {
                components.push(
                    json!({"id":w.id,"title":w.title,"status":"not_captured_limit","limit":8,
                        "bounds":rect(&w.bounds),"observed_at":now(),"captured_at":null,"ax_complete":false}),
                );
                continue;
            }
            let pid = self
                .state
                .lock()
                .await
                .apps
                .get(&w.app_id)
                .map(|a| a.pid)
                .ok_or_else(|| error("STALE_TARGET", "app ended"))?;
            if tree {
                let mut state = self.state.lock().await;
                state.snapshots.remove(&w.id);
                state.nodes.retain(|_, n| n["target"]["window_id"] != w.id);
            }
            let r=self.tool("get_window_state",json!({"pid":pid,"window_id":w.native_id,"include_accessibility_tree":tree,"include_screenshot":screenshot})).await;
            let r = match r {
                Ok(v) => v,
                Err(e)
                    if matches!(
                        e.code.as_str(),
                        "STALE_TARGET"
                            | "TARGET_NOT_FOUND"
                            | "INPUT_REFUSED"
                            | "PROVIDER_ERROR"
                            | "PERMISSION_DENIED"
                    ) =>
                {
                    components.push(json!({"id":w.id,"title":w.title,"status":"unavailable",
                        "bounds":rect(&w.bounds),"observed_at":now(),"captured_at":null,
                        "ax_complete":false,"error":e.code,"message":e.message}));
                    continue;
                }
                Err(e) => return Err(e),
            };
            let data = &r["structuredContent"];
            let mut component = json!({"id":w.id,"title":w.title,"status":"ok","captured_at":now(),"bounds":rect(&w.bounds),"ax_complete":data["elements_complete"]});
            if tree {
                let snapshot = text(data, "snapshot_id");
                if snapshot.is_empty() {
                    component["status"] = json!("unavailable");
                    component["reason"] = json!(text(data, "degraded_reason")
                        .split(':')
                        .next()
                        .unwrap_or("unavailable"));
                } else {
                    let root = format!("{}:{}:window", text(&result, "revision"), w.id);
                    nodes.push(node(
                        &root,
                        Some(&app_root),
                        &w.app_id,
                        Some(&w.id),
                        "window",
                        &w.title,
                        None,
                    ));
                    let mut state = self.state.lock().await;
                    state.snapshots.insert(w.id.clone(), snapshot.to_owned());
                    let queue = state.retained.entry(w.app_id.clone()).or_default();
                    queue.retain(|id| id != &w.id);
                    queue.push_back(w.id.clone());
                    let evicted = if queue.len() > 8 {
                        queue.pop_front()
                    } else {
                        None
                    };
                    if let Some(id) = evicted {
                        state.snapshots.remove(&id);
                    }
                    state.nodes.retain(|_, n| n["target"]["window_id"] != w.id);
                    for row in data["elements"]
                        .as_array()
                        .ok_or_else(|| error("PROTOCOL_MISMATCH", "AX elements missing"))?
                    {
                        let Some(token) = row["element_token"].as_str() else {
                            continue;
                        };
                        let id = format!("{}:{token}", w.id);
                        let parent = row["parent_index"]
                            .as_u64()
                            .map(|i| format!("{}:{}:{i}", w.id, snapshot))
                            .unwrap_or(root.clone());
                        let role = text(row, "role");
                        let editable = matches!(
                            role,
                            "AXTextField" | "AXTextArea" | "AXComboBox" | "AXSecureTextField"
                        );
                        let password = role == "AXSecureTextField";
                        let mut n = node(
                            &id,
                            Some(&parent),
                            &w.app_id,
                            Some(&w.id),
                            role,
                            if password {
                                "secure text field"
                            } else {
                                text(row, "label")
                            },
                            if password {
                                None
                            } else {
                                row["value"].as_str()
                            },
                        );
                        n["editable"] = json!(editable);
                        n["password"] = json!(password);
                        n["text_status"] = json!(if password { "redacted" } else { "readable" });
                        n["bounds"] = rect(&row["frame"]);
                        n["enabled"] = row["enabled"].clone();
                        n["focused"] = row["focused"].clone();
                        n["checked"] = row["selected"].clone();
                        n["actions"] = json!(row["actions"]
                            .as_array()
                            .unwrap_or(&Vec::new())
                            .iter()
                            .filter_map(|a| a.as_str())
                            .filter_map(action_name)
                            .map(|a| json!({"id":a}))
                            .collect::<Vec<_>>());
                        n["target_token"] = json!(token);
                        n["target"] =
                            json!({"app_id":w.app_id,"window_id":w.id,"snapshot_id":snapshot});
                        state.nodes.insert(id, n.clone());
                        nodes.push(n);
                    }
                }
            }
            if screenshot {
                match self.image(&r, Some(w)).await {
                    Ok(image) => images.push(image),
                    Err(e) => {
                        component["status"] = json!("unavailable");
                        component["message"] = json!(e.message);
                    }
                }
            }
            components.push(component);
        }
        result["nodes"] = json!(nodes);
        result["images"] = json!(images);
        result["components"] = json!(components);
        Ok(result)
    }
    async fn image(&self, r: &Value, w: Option<&NativeWindow>) -> Result<Value, Error> {
        let parts = r["content"]
            .as_array()
            .ok_or_else(|| error("OBSERVATION_FAILED", "image content missing"))?;
        let image = parts
            .iter()
            .find(|p| p["type"] == "image")
            .ok_or_else(|| error("OBSERVATION_FAILED", "target image unavailable"))?;
        if image["mimeType"] != "image/png" {
            return Err(error("PROTOCOL_MISMATCH", "expected Cua PNG capture"));
        }
        let bytes = STANDARD
            .decode(text(image, "data"))
            .map_err(|_| error("PROTOCOL_MISMATCH", "invalid image encoding"))?;
        if bytes.len() < 24 || &bytes[..8] != b"\x89PNG\r\n\x1a\n" {
            return Err(error("PROTOCOL_MISMATCH", "invalid PNG"));
        }
        let width = u32::from_be_bytes(bytes[16..20].try_into().unwrap());
        let height = u32::from_be_bytes(bytes[20..24].try_into().unwrap());
        let mut state = self.state.lock().await;
        state.counter += 1;
        let token = format!("image:{}", state.counter);
        let data = &r["structuredContent"];
        let bounds = if w.is_some() {
            rect(&data["window_bounds"])
        } else {
            rect(&json!({"x":0,"y":0,"width":data["screen_width"],"height":data["screen_height"]}))
        };
        let basis = json!({"width":width,"height":height,"window_id":w.map(|w|&w.id),"app_id":w.map(|w|&w.app_id),"bounds":bounds,"scale_factor":data["scale_factor"]});
        state.images.insert(token.clone(), basis);
        Ok(
            json!({"width":width,"height":height,"png":image["data"],"mime_type":"image/png","captured_at":now(),"geometry":bounds,"status":"ok","label":w.map(|w|format!("{} / {}",w.app_id,w.title)).unwrap_or("Mac / primary display".into()),"target":{"image_token":token,"app_id":w.map(|w|&w.app_id),"window_id":w.map(|w|&w.id)}}),
        )
    }
}
