mod actions;
mod observation;

use base64::{engine::general_purpose::STANDARD, Engine};
use mu_adapter::{Provider, ProviderFuture};
use mu_protocol::{Error, Reply, Request};
use rmcp::{
    model::{CallToolRequestParams, ClientInfo},
    service::RunningService,
    transport::TokioChildProcess,
    RoleClient, ServiceExt,
};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, VecDeque},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::Mutex;

struct NativeApp {
    pid: i64,
    id: String,
    native_id: String,
    label: String,
}
#[derive(Clone)]
struct NativeWindow {
    id: String,
    native_id: u64,
    app_id: String,
    title: String,
    bounds: Value,
}
struct State {
    apps: HashMap<String, NativeApp>,
    windows: HashMap<String, NativeWindow>,
    snapshots: HashMap<String, String>,
    retained: HashMap<String, VecDeque<String>>,
    nodes: HashMap<String, Value>,
    images: HashMap<String, Value>,
    counter: u64,
    daemon_pid: Option<i64>,
}
pub struct MacosProvider {
    service: Mutex<Option<RunningService<RoleClient, ClientInfo>>>,
    state: Mutex<State>,
    _lease: std::fs::File,
    executable: String,
    managed_input: bool,
}
fn error(code: &str, message: impl Into<String>) -> Error {
    Error::new(code, message)
}
fn now() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis()
}
fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
fn number(v: &Value, key: &str) -> f64 {
    v[key].as_f64().unwrap_or(0.0)
}
impl MacosProvider {
    pub async fn connect() -> Result<Arc<Self>, Error> {
        Self::connect_at(None, None).await
    }
    pub async fn connect_at(
        app_path: Option<String>,
        socket: Option<String>,
    ) -> Result<Arc<Self>, Error> {
        if app_path.is_some() && socket.is_none() {
            return Err(error(
                "INVALID_ARGUMENT",
                "a custom signed app requires an explicit private socket",
            ));
        }
        let app = app_path.unwrap_or_else(|| super::setup::APP.to_owned());
        let verify = app.clone();
        tokio::task::spawn_blocking(move || {
            super::setup::verify_app(std::path::Path::new(&verify))
        })
        .await
        .map_err(|e| error("SETUP_FAILED", e.to_string()))??;
        let lease = super::setup::acquire_lease()?;
        let mut command = tokio::process::Command::new(
            std::path::Path::new(&app).join("Contents/MacOS/cua-driver"),
        );
        command.arg("mcp");
        if let Some(socket) = socket {
            command.args(["--socket", &socket]);
        }
        Self::connect_command(
            command,
            lease,
            std::env::var("MAC_AGENT_DEMO").as_deref() == Ok("1"),
        )
        .await
    }
    async fn connect_command(
        command: tokio::process::Command,
        lease: std::fs::File,
        demo_mode: bool,
    ) -> Result<Arc<Self>, Error> {
        let executable = command
            .as_std()
            .get_program()
            .to_string_lossy()
            .into_owned();
        let transport =
            TokioChildProcess::new(command).map_err(|e| error("DEVICE_OFFLINE", e.to_string()))?;
        let info:ClientInfo=serde_json::from_value(json!({"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mai-ui","version":"0.2.0"}})).unwrap();
        let service = info
            .serve(transport)
            .await
            .map_err(|e| error("PROTOCOL_MISMATCH", e.to_string()))?;
        if service
            .peer_info()
            .and_then(|info| info.server_info.clone())
            .is_none_or(|info| info.version != super::setup::VERSION)
        {
            return Err(error(
                "VERSION_MISMATCH",
                "MCP server did not negotiate the pinned Cua version",
            ));
        }
        // Opt in only through a reviewed signed dependency lock update.
        let locked = serde_json::from_str::<Value>(include_str!("../cua-driver.lock.json"))
            .unwrap()["managed_input_protocol"]
            == 1;
        let managed_input = if locked {
            service
                .list_all_tools()
                .await
                .map_err(|e| error("PROTOCOL_MISMATCH", e.to_string()))?
                .iter()
                .any(|tool| {
                    tool.name == "managed_input"
                        && tool.input_schema.get("x-mai-ui-input-version") == Some(&json!(1))
                })
        } else {
            false
        };
        let provider = Arc::new(Self {
            service: Mutex::new(Some(service)),
            state: Mutex::new(State {
                apps: HashMap::new(),
                windows: HashMap::new(),
                snapshots: HashMap::new(),
                retained: HashMap::new(),
                nodes: HashMap::new(),
                images: HashMap::new(),
                counter: 0,
                daemon_pid: None,
            }),
            _lease: lease,
            executable,
            managed_input,
        });
        provider.tool("start_session", json!({})).await?;
        if demo_mode {
            // An empty public label selects this MCP connection's implicit session,
            // which is also used by subsequent click calls without a session arg.
            // The signed driver requires the field in its cursor-motion schema.
            let _ = provider
                .tool(
                    "set_agent_cursor_motion",
                    json!({
                        "session": "",
                        "glide_duration_ms": 100.0,
                        "turn_radius": 10.0,
                        "spring": 1.0,
                        "dwell_after_click_ms": 0.0
                    }),
                )
                .await;
        }
        Ok(provider)
    }
    async fn tool(&self, name: &str, args: Value) -> Result<Value, Error> {
        let service = self.service.lock().await;
        let service = service
            .as_ref()
            .ok_or_else(|| error("SESSION_CLOSED", "Mac provider closed"))?;
        let result = service
            .call_tool(
                CallToolRequestParams::new(name.to_owned())
                    .with_arguments(args.as_object().cloned().unwrap_or_default()),
            )
            .await
            .map_err(|e| error("DEVICE_OFFLINE", e.to_string()))?;
        let result =
            serde_json::to_value(result).map_err(|e| error("PROTOCOL_MISMATCH", e.to_string()))?;
        if result["isError"] == true || result["structuredContent"]["effect"] == "refused" {
            let detail = &result["structuredContent"];
            let message = result["content"]
                .as_array()
                .map(|parts| {
                    parts
                        .iter()
                        .filter_map(|p| p["text"].as_str())
                        .collect::<Vec<_>>()
                        .join("\n")
                })
                .unwrap_or_default();
            let code = text(detail, "code");
            let lower = format!("{code} {message}").to_lowercase();
            let mapped = if lower.contains("action_outcome_unknown") {
                "ACTION_OUTCOME_UNKNOWN"
            } else if lower.contains("session_expired")
                || lower.contains("session_not_found")
                || lower.contains("session_closed")
            {
                "SESSION_EXPIRED"
            } else if lower.contains("stale") || lower.contains("generation") {
                "STALE_TARGET"
            } else if lower.contains("permission")
                || lower.contains("consent")
                || lower.contains("not_authorized")
            {
                "PERMISSION_DENIED"
            } else if lower.contains("ambiguous") {
                "AMBIGUOUS_TARGET"
            } else if lower.contains("window_id_not_found") || lower.contains("owner_pid_mismatch")
            {
                "TARGET_NOT_FOUND"
            } else if lower.contains("invalid") {
                "INVALID_ARGUMENT"
            } else if detail["effect"] == "refused"
                || lower.contains("foreground")
                || lower.contains("background")
                || lower.contains("refus")
            {
                "INPUT_REFUSED"
            } else {
                "PROVIDER_ERROR"
            };
            let summary = match mapped {
                "STALE_TARGET" => "captured native target expired",
                "INPUT_REFUSED" => {
                    "requested input route was refused; no alternate delivery was attempted"
                }
                "PERMISSION_DENIED" => "native permission or policy denied the operation",
                "AMBIGUOUS_TARGET" => "bind an exact window before this operation",
                "INVALID_ARGUMENT" => "native operation rejected its arguments",
                "TARGET_NOT_FOUND" => "bound native target is no longer available",
                _ => "native operation failed",
            };
            let mut details = receipt(&result);
            if !message.is_empty() {
                details["native_message"] = json!(message.chars().take(512).collect::<String>());
            }
            if !detail["observed"].is_null() {
                if !details["evidence"].is_array() {
                    details["evidence"] = json!([]);
                }
                details["evidence"].as_array_mut().unwrap().push(json!({
                    "kind":"native_observation", "native_code":code,
                    "observed":detail["observed"],
                    "exact_window_effect":detail["exact_window_effect"]
                }));
            }
            return Err(
                error(mapped, format!("{summary} (native code: {code})")).with_details(details)
            );
        }
        Ok(result)
    }
    pub async fn doctor(&self) -> Result<Value, Error> {
        let result = self
            .tool(
                "check_permissions",
                json!({"prompt":false,"probe_direct_capture":false}),
            )
            .await?;
        Ok(
            json!({"version":super::setup::VERSION,"app":std::path::Path::new(&self.executable).ancestors().nth(3),"executable":self.executable,"protocol":"2025-06-18","permissions":result["structuredContent"],"capabilities":self.describe(&result["structuredContent"])["capabilities"]}),
        )
    }
    fn describe(&self, permissions: &Value) -> Value {
        let mut caps = serde_json::Map::new();
        for key in [
            "ui.observe.tree",
            "ui.observe.screenshot",
            "ui.click.primary",
            "ui.click.coordinates",
            "ui.perform",
            "ui.set_text",
            "ui.input_text",
            "ui.press_key",
            "ui.scroll.coordinates",
            "ui.move",
            "ui.drag",
            "device.list_apps",
            "device.list_windows",
            "device.list_displays",
            "app.launch",
            "macos.set_frame",
            "macos.invoke_menu",
            "clipboard.read",
            "clipboard.write",
            "shell.run",
            "files.read",
            "files.write",
            "files.stat",
            "files.list",
            "files.mkdir",
            "files.remove",
            "files.move",
        ] {
            caps.insert(key.into(), json!({"state":"ready"}));
        }
        for key in [
            "ui.key_state",
            "ui.button_state",
            "ui.release_all",
            "ui.wheel",
        ] {
            caps.insert(key.into(), if self.managed_input { json!({"state":"ready","delivery":"foreground"}) } else {json!({"state":"unsupported","reason":"requires a verified signed Cua managed-input extension"})});
        }
        caps["ui.click.primary"] =
            json!({"state":"ready","policy":"cua_activation","strict_native":false});
        caps["ui.set_text"] =
            json!({"state":"ready","delivery":"background","mechanism":"set_value"});
        caps["ui.scroll.coordinates"] =
            json!({"state":"ready","units":["lines"],"viewport":"unsupported"});
        caps["ui.drag"] =
            json!({"state":"ready","delivery":"foreground","background":"unsupported"});
        if self.managed_input {
            caps["ui.wheel"]["units"] = json!(["pixels", "lines"]);
        }
        caps["ui.move"] = json!({"state":"ready","scope":"desktop","delivery":"foreground"});
        for (name, value) in caps.iter_mut() {
            let required = if name == "ui.observe.screenshot" {
                Some("screen_recording")
            } else if name.starts_with("ui.") || name.starts_with("macos.") {
                Some("accessibility")
            } else {
                None
            };
            if let Some(required) = required {
                value["permission"] = json!(required);
                if permissions[required] == false {
                    value["state"] = json!("needs_permission");
                }
            }
        }
        json!({"protocol_major":1,"platform":"macos","provider":"macos.cua","driver_version":super::setup::VERSION,"capabilities":caps,"shell_providers":[{"id":"local","state":"ready","default_interpreter":"/bin/sh","interpreters":["/bin/sh"],"kind":"host_process","default_cwd":"/","argv":true,"streaming":false,"stdin":true,"pty":false,"cancellation":"process_group","identity":{"platform":"macos","principal":std::env::var("USER").unwrap_or_default(),"uid":unsafe{libc::geteuid()},"gid":unsafe{libc::getegid()}}}]})
    }
    async fn apps(&self) -> Result<Value, Error> {
        let result = self.tool("list_apps", json!({})).await?;
        let data = &result["structuredContent"];
        let rows = data["apps"]
            .as_array()
            .or_else(|| data.as_array())
            .ok_or_else(|| error("PROTOCOL_MISMATCH", "list_apps missing structured apps"))?;
        let process_list = super::setup::command("/bin/ps", &["-axo", "pid=,lstart="])?;
        let starts: HashMap<i64, String> = process_list
            .lines()
            .filter_map(|line| {
                let mut p = line.split_whitespace();
                let pid = p.next()?.parse().ok()?;
                Some((pid, p.collect::<Vec<_>>().join("_")))
            })
            .collect();
        let mut state = self.state.lock().await;
        let mut current = HashMap::new();
        let mut apps = Vec::new();
        for row in rows {
            let Some(pid) = row["pid"].as_i64().filter(|p| *p > 0) else {
                continue;
            };
            let native_id = text(row, "bundle_id").to_owned();
            // Process start time prevents PID reuse from reviving an old binding.
            let started = starts.get(&pid).cloned().unwrap_or_default();
            if started.is_empty() {
                continue;
            }
            let id = format!("app:{pid}:{}", started.replace(' ', "_"));
            let label = if row["name"].is_string() {
                text(row, "name")
            } else {
                text(row, "app_name")
            }
            .to_owned();
            // Surface the liveness fields the agent vocabulary relies on for identity
            // disambiguation; every row here has a live pid, so running is always true.
            let launch_path = row["launch_path"]
                .as_str()
                .or_else(|| row["path"].as_str())
                .or_else(|| row["bundle_path"].as_str());
            apps.push(json!({"id":id,"native_id":native_id,"label":label,"launchable":true,"running":true,"pid":pid,"launch_path":launch_path,"instance_id":id}));
            current.insert(
                id.clone(),
                NativeApp {
                    pid,
                    id,
                    native_id,
                    label,
                },
            );
        }
        state.apps = current;
        Ok(json!({"apps":apps,"complete":false}))
    }
    async fn pid(&self, app: &str) -> Result<i64, Error> {
        let pid = self
            .state
            .lock()
            .await
            .apps
            .get(app)
            .map(|a| a.pid)
            .ok_or_else(|| error("STALE_TARGET", "application instance ended"))?;
        let start = super::setup::command("/bin/ps", &["-p", &pid.to_string(), "-o", "lstart="])
            .unwrap_or_default()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join("_");
        if start.is_empty() || app != format!("app:{pid}:{start}") {
            return Err(error("STALE_TARGET", "application instance ended"));
        }
        Ok(pid)
    }
    async fn windows(&self, app: Option<&str>) -> Result<Value, Error> {
        let mut args = json!({"on_screen_only":false});
        if let Some(app) = app {
            args["pid"] = json!(self.pid(app).await?);
        } else {
            self.apps().await?;
        }
        let result = self.tool("list_windows", args).await?;
        let data = &result["structuredContent"];
        let rows = data["windows"]
            .as_array()
            .or_else(|| data.as_array())
            .ok_or_else(|| {
                error(
                    "PROTOCOL_MISMATCH",
                    "list_windows missing structured windows",
                )
            })?;
        let mut state = self.state.lock().await;
        let mut result = Vec::new();
        let mut alive = Vec::new();
        for row in rows {
            let pid = row["pid"].as_i64().or_else(|| row["owner_pid"].as_i64());
            let Some(a) = state.apps.values().find(|a| Some(a.pid) == pid) else {
                continue;
            };
            let native_id = row["window_id"]
                .as_u64()
                .or_else(|| row["id"].as_u64())
                .ok_or_else(|| error("PROTOCOL_MISMATCH", "window ID missing"))?;
            let app_id = a.id.clone();
            let existing = state
                .windows
                .values()
                .find(|w| w.native_id == native_id && w.app_id == app_id)
                .map(|w| w.id.clone());
            let id = existing.unwrap_or_else(|| {
                state.counter += 1;
                format!("window:{native_id}:{}", state.counter)
            });
            let title = if row["title"].is_string() {
                text(row, "title")
            } else {
                text(row, "window_title")
            }
            .to_owned();
            let bounds = row["bounds"].clone();
            result.push(json!({"id":id,"app_id":app_id,"title":title,"bounds":bounds}));
            alive.push(id.clone());
            state.windows.insert(
                id.clone(),
                NativeWindow {
                    id,
                    native_id,
                    app_id,
                    title,
                    bounds,
                },
            );
        }
        state
            .windows
            .retain(|id, w| app.is_some_and(|a| a != w.app_id) || alive.contains(id));
        Ok(json!({"windows":result,"complete":true}))
    }
    async fn target_window(&self, q: &Value) -> Result<NativeWindow, Error> {
        let app = q["app_id"].as_str();
        let rows = self.windows(app).await?;
        let rows = rows["windows"].as_array().unwrap();
        let id = if let Some(id) = q["window_id"].as_str() {
            id.to_owned()
        } else {
            if rows.len() != 1 {
                return Err(error(
                    "AMBIGUOUS_TARGET",
                    "bind one window or supply a captured target",
                ));
            }
            text(&rows[0], "id").to_owned()
        };
        self.state
            .lock()
            .await
            .windows
            .get(&id)
            .filter(|w| app.is_none_or(|a| a == w.app_id))
            .cloned()
            .ok_or_else(|| error("STALE_TARGET", "target window ended"))
    }
    async fn execute(&self, q: Value) -> Result<Value, Error> {
        if !q["display_id"].is_null() && q["display_id"] != "display:0" {
            return Err(error(
                "UNSUPPORTED_CAPABILITY",
                "only the primary display is supported",
            ));
        }
        let mut permissions = json!({});
        // Lease maintenance reads only session metadata. Even a permission
        // probe can emit a System cursor cue on an existing MCP session.
        if !matches!(text(&q, "op"), "file" | "shell" | "stop" | "keepalive") {
            let result = self
                .tool(
                    "check_permissions",
                    json!({"prompt":false,"probe_direct_capture":false}),
                )
                .await?;
            permissions = result["structuredContent"].clone();
            let pid = permissions["source"]["pid"].as_i64();
            let mut state = self.state.lock().await;
            if state.daemon_pid.is_some() && state.daemon_pid != pid {
                return Err(error(
                    "DEVICE_OFFLINE",
                    "Cua daemon changed; reconnect the Mac endpoint",
                ));
            }
            state.daemon_pid = pid;
        }
        match text(&q, "op") {
            "describe" => Ok(self.describe(&permissions)),
            "keepalive" => {
                // This session-scoped read refreshes the lease without emitting a
                // cursor action. Inventory reads are visible App actions in Cua.
                self.tool("get_agent_cursor_state", json!({})).await?;
                Ok(json!({"alive":true}))
            }
            "list_apps" => self.apps().await,
            "list_windows" => self.windows(q["app_id"].as_str()).await,
            "observe" => self.observe(&q).await,
            "action" => self.action(&q).await,
            "file" | "shell" | "clipboard" => {
                if !q["app_id"].is_null() || !q["window_id"].is_null() || !q["display_id"].is_null()
                {
                    return Err(error(
                        "PERMISSION_DENIED",
                        "host services require a whole-Mac scope",
                    ));
                }
                match text(&q, "op") {
                    "file" => tokio::task::spawn_blocking(move || mu_adapter::host::file(&q))
                        .await
                        .map_err(|e| error("PROVIDER_ERROR", e.to_string()))?,
                    "shell" => mu_adapter::host::shell(&q).await,
                    _ => {
                        let r = self
                            .tool("clipboard_read", json!({"include_text":true}))
                            .await?;
                        let d = &r["structuredContent"];
                        let formats = d["types"]
                            .as_array()
                            .ok_or_else(|| {
                                error("PROTOCOL_MISMATCH", "clipboard format inventory missing")
                            })?
                            .clone();
                        let omitted: Vec<_> = formats
                            .iter()
                            .filter(|f| {
                                !matches!(
                                    f.as_str(),
                                    Some(
                                        "public.utf8-plain-text"
                                            | "public.utf16-plain-text"
                                            | "public.text"
                                            | "NSStringPboardType"
                                            | "text/plain"
                                    )
                                )
                            })
                            .cloned()
                            .collect();
                        Ok(
                            json!({"formats":if d["text"].is_string(){json!({"text/plain":d["text"]})}else{json!({})},"available_formats":formats,"omitted_formats":omitted}),
                        )
                    }
                }
            }
            "stop" => Ok(json!({"stopped":true})),
            "release_inputs" if self.managed_input => {
                self.tool(
                    "managed_input",
                    json!({"scope":"desktop","action":"release_all"}),
                )
                .await?;
                Ok(json!({"execution":"completed"}))
            }
            "displays" => {
                let r = self.tool("get_screen_size", json!({})).await?;
                let d = &r["structuredContent"];
                Ok(
                    json!({"displays":[{"id":"display:0","generation":"local","width":d["width"],"height":d["height"],"rotation":0,"scale_factor":d["scale_factor"].as_f64().unwrap_or(1.0),"insets":null}]}),
                )
            }
            _ => Err(error(
                "UNSUPPORTED_CAPABILITY",
                "unknown macOS provider operation",
            )),
        }
    }
}
impl Provider for MacosProvider {
    fn request(&self, q: Request) -> ProviderFuture<'_, Reply> {
        Box::pin(async move {
            let id = q.request_id;
            let q =
                serde_json::to_value(q).map_err(|e| error("INVALID_ARGUMENT", e.to_string()))?;
            let result = self.execute(q).await;
            Ok(match result {
                Ok(v) => Reply {
                    request_id: id,
                    result: Some(v),
                    error: None,
                },
                Err(e) => Reply {
                    request_id: id,
                    result: None,
                    error: Some(e),
                },
            })
        })
    }
    fn close(&self) -> ProviderFuture<'_, ()> {
        Box::pin(async move {
            let cleanup = if self.managed_input {
                self.tool(
                    "managed_input",
                    json!({"scope":"desktop","action":"release_all"}),
                )
                .await
                .map(|_| ())
            } else {
                Ok(())
            };
            if let Some(service) = self.service.lock().await.take() {
                let _ = service.cancel().await;
            }
            #[cfg(unix)]
            {
                use std::os::fd::AsRawFd;
                unsafe {
                    libc::flock(self._lease.as_raw_fd(), libc::LOCK_UN);
                }
            }
            cleanup
        })
    }
}
fn rect(v: &Value) -> Value {
    let r = json!({"x":v["x"],"y":v["y"],"width":v.get("width").unwrap_or(&v["w"]),"height":v.get("height").unwrap_or(&v["h"])});
    if ["x", "y", "width", "height"].iter().any(|k| {
        r[*k]
            .as_f64()
            .is_none_or(|n| !n.is_finite() || (matches!(*k, "width" | "height") && n <= 0.0))
    }) {
        return Value::Null;
    }
    r
}

fn portable_key<'a>(
    key: &'a str,
    modifiers: &Value,
) -> Result<(&'a str, Vec<&'static str>), Error> {
    let key = match key {
        "enter" | "return" => "return",
        "escape" | "esc" => "escape",
        "backspace" => "delete",
        "delete" => "forward_delete",
        "page_up" => "pageup",
        "page_down" => "pagedown",
        other => other,
    };
    let keys = [
        "return",
        "escape",
        "delete",
        "forward_delete",
        "pageup",
        "pagedown",
        "tab",
        "space",
        "home",
        "end",
        "up",
        "down",
        "left",
        "right",
    ];
    if !keys.contains(&key)
        && !(key.len() == 1 && key.as_bytes()[0].is_ascii_alphanumeric())
        && !(key.starts_with('f') && key[1..].parse::<u8>().is_ok_and(|n| (1..=12).contains(&n)))
    {
        return Err(error(
            "UNSUPPORTED_CAPABILITY",
            "unsupported portable Mac key",
        ));
    }
    let mut translated = Vec::new();
    for modifier in modifiers
        .as_array()
        .ok_or_else(|| error("INVALID_ARGUMENT", "modifier list required"))?
    {
        let value = match modifier.as_str() {
            Some("meta" | "command" | "cmd") => "cmd",
            Some("control" | "ctrl") => "ctrl",
            Some("alt" | "option") => "option",
            Some("shift") => "shift",
            Some("fn") => "fn",
            _ => return Err(error("UNSUPPORTED_CAPABILITY", "unsupported Mac modifier")),
        };
        if !translated.contains(&value) {
            translated.push(value);
        }
    }
    Ok((key, translated))
}
fn action_name(a: &str) -> Option<&'static str> {
    match a {
        "AXPress" => Some("macos.ax.press"),
        "AXShowMenu" => Some("macos.ax.show_menu"),
        "AXPick" => Some("macos.ax.pick"),
        "AXConfirm" => Some("macos.ax.confirm"),
        "AXCancel" => Some("macos.ax.cancel"),
        "AXOpen" => Some("macos.ax.open"),
        _ => None,
    }
}
fn node(
    id: &str,
    parent: Option<&str>,
    app: &str,
    window: Option<&str>,
    role: &str,
    label: &str,
    value: Option<&str>,
) -> Value {
    json!({"id":id,"parent_id":parent,"app_id":app,"window_id":window,"class_name":role,"automation_id":null,"role":role,"name":label,"text":value,"hint":null,"text_status":"readable","bounds":null,"enabled":null,"visible":null,"focused":null,"editable":false,"password":false,"actions":[]})
}
fn receipt(r: &Value) -> Value {
    let d = &r["structuredContent"];
    let activation = d["activated"] == true && d["exact_window_effect"]["verified"] == true;
    let effect = if activation {
        "confirmed"
    } else {
        match text(d, "effect") {
            "confirmed" => "confirmed",
            "partial" => "partial",
            "suspected_noop" => "suspected_noop",
            _ => "unknown",
        }
    };
    json!({"execution":"completed","effect":effect,
        "mechanism":d.get("route").or_else(||d.get("path")),
        "delivery":d.get("delivery").cloned().unwrap_or_else(|| if d.get("activated").is_some() { json!({"mode":"foreground"}) } else { Value::Null }),
        "evidence":d.get("evidence").cloned().unwrap_or_else(|| if activation { json!([{"kind":"exact_window_activation","observed":d["observed"]}]) } else { json!([]) })})
}

#[cfg(test)]
mod tests {
    use super::*;
    async fn call(p: &MacosProvider, q: Value) -> Value {
        let r = p.request(serde_json::from_value(q).unwrap()).await.unwrap();
        assert!(r.error.is_none(), "{:?}", r.error);
        r.result.unwrap()
    }
    #[tokio::test]
    async fn keepalive_never_dispatches_inventory_or_cursor_actions() {
        let fixture = std::env::var_os("MAI_UI_MACOS_TEST_FIXTURE")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("fixture/mock_cua.py"));
        let mut command = tokio::process::Command::new("python3");
        command.arg(fixture);
        let p = MacosProvider::connect_command(command, tempfile::tempfile().unwrap(), false)
            .await.unwrap();
        for _ in 0..3 {
            assert_eq!(call(&p, json!({"op":"keepalive"})).await["alive"], true);
        }
        let calls = p.tool("fixture_get_calls", json!({})).await.unwrap();
        let names: Vec<_> = calls["structuredContent"]["calls"].as_array().unwrap()
            .iter().map(|call| call["name"].as_str().unwrap()).collect();
        assert_eq!(names.iter().filter(|&&name| name == "get_agent_cursor_state").count(), 3);
        assert_eq!(names, vec!["start_session", "get_agent_cursor_state",
            "get_agent_cursor_state", "get_agent_cursor_state"],
            "A heartbeat must not enumerate apps, emit permission-probe cues, or post input");
        p.close().await.unwrap();
    }
    #[tokio::test]
    async fn foreground_set_text_is_rejected_before_driver_dispatch() {
        let mut command = tokio::process::Command::new("python3");
        command.arg(concat!(env!("CARGO_MANIFEST_DIR"), "/fixture/mock_cua.py"));
        let p = MacosProvider::connect_command(command, tempfile::tempfile().unwrap(), false)
            .await
            .unwrap();
        let apps = call(&p, json!({"op":"list_apps"})).await;
        let app = &apps["apps"][0]["id"];
        let windows = call(&p, json!({"op":"list_windows","app_id":app})).await;
        let window = &windows["windows"][0]["id"];
        let observed = call(&p, json!({"op":"observe","app_id":app,"tree":true})).await;
        let node = &observed["nodes"][2];
        let action = json!({"op":"action","action":"set_text","app_id":app,"window_id":window,"node_id":node["id"],"target_token":node["target_token"],"snapshot_id":node["target"]["snapshot_id"],"text":"hello","delivery":"foreground"});
        let error = p
            .request(serde_json::from_value(action).unwrap())
            .await
            .unwrap()
            .error
            .unwrap();
        assert_eq!(error.code, "UNSUPPORTED_CAPABILITY");
        let calls = p.tool("fixture_get_calls", json!({})).await.unwrap();
        assert!(!calls["structuredContent"]["calls"]
            .as_array()
            .unwrap()
            .iter()
            .any(|entry| entry["name"] == "set_value"));
        p.close().await.unwrap();
    }

    #[tokio::test]
    async fn foreground_typing_refuses_when_target_window_focus_cannot_be_verified() {
        let mut command = tokio::process::Command::new("python3");
        command.arg(concat!(env!("CARGO_MANIFEST_DIR"), "/fixture/mock_cua.py"));
        let p = MacosProvider::connect_command(command, tempfile::tempfile().unwrap(), false)
            .await.unwrap();
        let apps = call(&p, json!({"op":"list_apps"})).await;
        let app = &apps["apps"][0]["id"];
        let inventory = call(&p, json!({"op":"list_windows","app_id":app})).await;
        let window = &inventory["windows"][0]["id"];
        p.tool("fixture_set_frontmost", json!({"window_id":2})).await.unwrap();
        let error = p.request(serde_json::from_value(json!({
            "op":"action", "action":"input_text", "app_id":app,
            "window_id":window, "text":"hello", "delivery":"foreground"
        })).unwrap()).await.unwrap().error.unwrap();
        assert_eq!(error.code, "INPUT_REFUSED");
        let calls = p.tool("fixture_get_calls", json!({})).await.unwrap();
        assert!(!calls["structuredContent"]["calls"].as_array().unwrap()
            .iter().any(|call| call["name"] == "type_text"));
        p.close().await.unwrap();
    }

    #[tokio::test]
    async fn foreground_typing_targets_verified_frontmost_window() {
        let mut command = tokio::process::Command::new("python3");
        command.arg(concat!(env!("CARGO_MANIFEST_DIR"), "/fixture/mock_cua.py"));
        let p = MacosProvider::connect_command(command, tempfile::tempfile().unwrap(), false)
            .await.unwrap();
        let apps = call(&p, json!({"op":"list_apps"})).await;
        let app = &apps["apps"][0]["id"];
        let inventory = call(&p, json!({"op":"list_windows","app_id":app})).await;
        let window = &inventory["windows"][0]["id"];
        call(&p, json!({
            "op":"action", "action":"input_text", "app_id":app,
            "window_id":window, "text":"hello", "delivery":"foreground"
        })).await;
        let calls = p.tool("fixture_get_calls", json!({})).await.unwrap();
        assert!(calls["structuredContent"]["calls"].as_array().unwrap()
            .iter().any(|entry| entry["name"] == "type_text"));
        p.close().await.unwrap();
    }

    #[tokio::test]
    async fn desktop_hover_preserves_foreground_consent_and_refuses_background() {
        let mut command = tokio::process::Command::new("python3");
        command.arg(concat!(env!("CARGO_MANIFEST_DIR"), "/fixture/mock_cua.py"));
        let p = MacosProvider::connect_command(command, tempfile::tempfile().unwrap(), false)
            .await.unwrap();
        call(&p, json!({"op":"action", "action":"move", "display_id":"display:0",
                        "x":10, "y":20, "delivery":"foreground"})).await;
        let denied = p.request(serde_json::from_value(json!({"op":"action", "action":"move",
            "display_id":"display:0", "x":10, "y":20, "delivery":"background"})).unwrap())
            .await.unwrap().error.unwrap();
        assert_eq!(denied.code, "INPUT_REFUSED");
        let calls = p.tool("fixture_get_calls", json!({})).await.unwrap();
        let moves: Vec<_> = calls["structuredContent"]["calls"].as_array().unwrap()
            .iter().filter(|c| c["name"] == "move_cursor").collect();
        assert_eq!(moves.len(), 1, "Refused input must never reach the driver");
        assert_eq!(moves[0]["arguments"]["delivery_mode"], "foreground");
        assert_eq!(moves[0]["arguments"]["scope"], "desktop");
        p.close().await.unwrap();
    }

    #[tokio::test]
    async fn native_failure_preserves_bounded_driver_diagnostic() {
        let mut command = tokio::process::Command::new("python3");
        command.arg(concat!(env!("CARGO_MANIFEST_DIR"), "/fixture/mock_cua.py"));
        let p = MacosProvider::connect_command(command, tempfile::tempfile().unwrap(), false)
            .await
            .unwrap();
        let error = p
            .tool("fixture_native_failure", json!({}))
            .await
            .unwrap_err();
        assert_eq!(error.code, "PROVIDER_ERROR");
        let message = error.details.unwrap()["native_message"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(message.starts_with("set_value failed: AXValue is not settable"));
        assert!(message.chars().count() <= 512);
        p.close().await.unwrap();
    }

    #[tokio::test]
    async fn set_text_capability_advertises_background_only() {
        let mut command = tokio::process::Command::new("python3");
        command.arg(concat!(env!("CARGO_MANIFEST_DIR"), "/fixture/mock_cua.py"));
        let p = MacosProvider::connect_command(command, tempfile::tempfile().unwrap(), false)
            .await
            .unwrap();
        let description = call(&p, json!({"op":"describe"})).await;
        assert_eq!(description["capabilities"]["ui.set_text"]["state"], "ready");
        assert_eq!(
            description["capabilities"]["ui.set_text"]["delivery"],
            "background"
        );
        p.close().await.unwrap();
    }
    #[tokio::test]
    async fn mcp_snapshots_are_window_bound_and_do_not_acquire_ax_for_vision() {
        let mut command = tokio::process::Command::new("python3");
        command.arg(concat!(env!("CARGO_MANIFEST_DIR"), "/fixture/mock_cua.py"));
        let p = MacosProvider::connect_command(command, tempfile::tempfile().unwrap(), false)
            .await
            .unwrap();
        let startup = p.tool("fixture_get_calls", json!({})).await.unwrap();
        let startup = startup["structuredContent"]["calls"].as_array().unwrap();
        assert_eq!(startup.len(), 1);
        assert_eq!(startup[0]["name"], "start_session");
        let apps = call(&p, json!({"op":"list_apps"})).await;
        let app = &apps["apps"][0]["id"];
        let inventory = call(&p, json!({"op":"list_windows","app_id":app})).await;
        let window = &inventory["windows"][0]["id"];
        let full = call(&p, json!({"op":"observe","app_id":app,"tree":true})).await;
        assert_eq!(full["components"].as_array().unwrap().len(), 9);
        assert_eq!(full["components"][8]["status"], "not_captured_limit");
        assert_eq!(full["components"][8]["bounds"]["width"], 100);
        assert!(full["components"][8]["captured_at"].is_null());
        let calls = p.tool("fixture_get_calls", json!({})).await.unwrap();
        let observations: Vec<_> = calls["structuredContent"]["calls"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|call| call["name"] == "get_window_state")
            .collect();
        assert_eq!(observations.len(), 8);
        for observation in observations {
            assert_eq!(observation["arguments"]["full_output"], true);
            assert_eq!(observation["arguments"]["tree_format"], "elements");
        }
        let n = &full["nodes"][2];
        let action = json!({"op":"action","action":"click","app_id":app,"window_id":window,"node_id":n["id"],"target_token":n["target_token"],"snapshot_id":n["target"]["snapshot_id"],"delivery":"background"});
        let r = call(&p, action.clone()).await;
        assert_eq!(r["effect"], "unknown");
        assert_eq!(r["delivery"]["mode"], "background");
        let image = call(
            &p,
            json!({"op":"observe","app_id":app,"window_id":window,"tree":false,"screenshot":true}),
        )
        .await;
        assert_eq!(image["images"].as_array().unwrap().len(), 1);
        call(&p, action.clone()).await;
        call(
            &p,
            json!({"op":"observe","app_id":app,"window_id":window,"tree":true}),
        )
        .await;
        assert_eq!(
            p.request(serde_json::from_value(action).unwrap())
                .await
                .unwrap()
                .error
                .unwrap()
                .code,
            "STALE_TARGET"
        );
        // Nine distinct snapshots evict the oldest of Cua's eight retained windows.
        let full = call(&p, json!({"op":"observe","app_id":app,"tree":true})).await;
        let n = &full["nodes"][2];
        let action = json!({"op":"action","action":"click","app_id":app,"window_id":window,"node_id":n["id"],"target_token":n["target_token"],"snapshot_id":n["target"]["snapshot_id"]});
        let ninth = &inventory["windows"][8]["id"];
        call(
            &p,
            json!({"op":"observe","app_id":app,"window_id":ninth,"tree":true}),
        )
        .await;
        assert_eq!(
            p.request(serde_json::from_value(action).unwrap())
                .await
                .unwrap()
                .error
                .unwrap()
                .code,
            "STALE_TARGET"
        );
        p.tool("fixture_close_window", json!({"window_id":9}))
            .await
            .unwrap();
        assert_eq!(
            p.request(
                serde_json::from_value(
                    json!({"op":"observe","app_id":app,"window_id":ninth,"tree":true})
                )
                .unwrap()
            )
            .await
            .unwrap()
            .error
            .unwrap()
            .code,
            "STALE_TARGET"
        );
        let daemon = p.state.lock().await.daemon_pid;
        p.state.lock().await.daemon_pid = Some(-1);
        assert_eq!(
            p.request(serde_json::from_value(json!({"op":"describe"})).unwrap())
                .await
                .unwrap()
                .error
                .unwrap()
                .code,
            "DEVICE_OFFLINE"
        );
        p.state.lock().await.daemon_pid = daemon;
        let refused = p.tool("fixture_refuse", json!({})).await.unwrap_err();
        assert_eq!(refused.code, "INPUT_REFUSED");
        let details = refused.details.unwrap();
        assert_eq!(details["mechanism"], "background_keyboard");
        assert_eq!(details["delivery"]["mode"], "background");
        assert_eq!(details["evidence"][0]["observed"]["focused_pid"], 42);
        p.tool("fixture_expire", json!({})).await.unwrap();
        assert_eq!(
            p.request(serde_json::from_value(json!({"op":"describe"})).unwrap())
                .await
                .unwrap()
                .error
                .unwrap()
                .code,
            "SESSION_EXPIRED"
        );
        p.close().await.unwrap();
    }

    #[tokio::test]
    async fn demo_mode_configures_fast_cursor_for_the_implicit_session() {
        let mut command = tokio::process::Command::new("python3");
        command.arg(concat!(env!("CARGO_MANIFEST_DIR"), "/fixture/mock_cua.py"));
        let p = MacosProvider::connect_command(command, tempfile::tempfile().unwrap(), true)
            .await
            .unwrap();
        let calls = p.tool("fixture_get_calls", json!({})).await.unwrap();
        let calls = calls["structuredContent"]["calls"].as_array().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0]["name"], "start_session");
        assert_eq!(calls[1]["name"], "set_agent_cursor_motion");
        assert_eq!(calls[1]["arguments"]["session"], "");
        assert_eq!(calls[1]["arguments"]["glide_duration_ms"], 100.0);
        assert_eq!(calls[1]["arguments"]["turn_radius"], 10.0);
        p.close().await.unwrap();
    }
}
