//! Trusted embedding host. Built-in provider construction is shared by both bindings.
use base64::{engine::general_purpose::STANDARD, Engine};
use mu_adapter::Provider;
use mu_protocol::Error;
use mu_runtime::{
    embedded::{budget, err, uid, Bridge, Embedded},
    Attachment,
};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    path::PathBuf,
    sync::{atomic::Ordering, Arc, Mutex},
};

struct PendingBrowser {
    session: String,
    provider: Arc<mu_client::process::ProcessClient>,
    _artifacts: Arc<tempfile::TempDir>,
}
pub struct Host {
    pub runtime: Arc<Embedded>,
    factories: Mutex<HashMap<String, Value>>,
    owned: Mutex<HashMap<String, Vec<String>>>,
    creation: tokio::sync::Mutex<()>,
    artifacts: Mutex<HashMap<String, Arc<tempfile::TempDir>>>,
    pending_browsers: Mutex<HashMap<String, PendingBrowser>>,
    composed: Mutex<HashMap<(String, String), Arc<composition::Composition>>>,
}
fn s<'a>(q: &'a Value, k: &str) -> Result<&'a str, Error> {
    q[k].as_str()
        .ok_or_else(|| Error::new("INVALID_ARGUMENT", format!("{k} must be a string")))
}
fn n(q: &Value, k: &str) -> Result<u64, Error> {
    q[k].as_u64().ok_or_else(|| err("INVALID_ARGUMENT"))
}
fn legacy<T>(r: Result<T, &str>) -> Result<T, Error> {
    r.map_err(err)
}
impl Default for Host {
    fn default() -> Self {
        Self {
            runtime: Embedded::new(),
            factories: Mutex::default(),
            owned: Mutex::default(),
            creation: tokio::sync::Mutex::new(()),
            artifacts: Mutex::default(),
            pending_browsers: Mutex::default(),
            composed: Mutex::default(),
        }
    }
}
impl Host {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }
    /// Synchronous local value operations: no provider I/O or language callbacks.
    pub fn state(&self, q: Value) -> Result<Value, Error> {
        let op = s(&q, "op")?;
        match op {
            "prepare_shell" => return shell::prepare(&q),
            "help" => return Ok(json!("Use a bound handle.ui.ax or handle.ui.vision. Captures are silent; await mu.emit(observation) delivers output. Use emitted indices in a later turn, or captured nodes and image.point() programmatically. See docs/browser-playwright.md and the native platform guides.")),
            "discover" => {
                self.runtime.check_session(s(&q,"session")?)?;
                for k in ["kind","query"] {if q.get(k).is_some_and(|v|!v.is_null()&&!v.is_string()){return Err(err("INVALID_ARGUMENT"));}}
                let kind=q["kind"].as_str();let query=q["query"].as_str().map(str::to_lowercase);
                let resources:Vec<_>=self.runtime.endpoints.lock().unwrap().values().filter(|e|!e.offline.load(Ordering::SeqCst)&&kind.is_none_or(|k|e.info["kind"]==k)&&query.as_ref().is_none_or(|search|e.info["name"].as_str().unwrap_or("").to_lowercase().contains(search))).map(|e|json!({"id":e.info["id"],"name":e.info["name"],"kind":e.info["kind"],"platform":e.info["platform"],"parent_id":e.info["parent_id"]})).collect();
                return Ok(json!({"resources":resources}));
            },
            "devices" | "device" => {
                let session=s(&q,"session")?;self.runtime.check_session(session)?;
                let core=self.runtime.core.lock().unwrap();
                let values:Vec<_>=core.authority.attachments().filter(|a|a.session==session && !self.runtime.is_detached(&a.id)).map(|a|json!({"id":a.id,"name":a.name,"resource_id":a.resource,"access":a.access})).collect();
                if op=="devices" {return Ok(json!({"devices":values}));}
                return values.into_iter().find(|v|v["id"]==q["name"]||v["name"]==q["name"]).ok_or_else(||err("TARGET_NOT_FOUND"));
            },
            "register" => {
                let session = s(&q, "session")?;
                self.runtime.check_session(session)?;
                let routing = q.get("routing").cloned().unwrap_or(json!({}));
                let source = q["source_attachment"]
                    .as_str()
                    .map(|id| self.runtime.check(id, false))
                    .transpose()?;
                let key = source
                    .as_ref()
                    .map(|a| a.conflict_domain.as_str())
                    .or(q["resource"].as_str())
                    .ok_or_else(|| err("INVALID_ARGUMENT"))?;
                let matches: Vec<_> = self
                    .runtime
                    .endpoints
                    .lock()
                    .unwrap()
                    .values()
                    .filter(|e| e.info["id"] == key || e.info["name"] == key)
                    .cloned()
                    .collect();
                if matches.len() != 1 {
                    return Err(err(if matches.is_empty() {
                        "TARGET_NOT_FOUND"
                    } else {
                        "AMBIGUOUS_TARGET"
                    }));
                }
                let e = &matches[0];
                if e.offline.load(Ordering::SeqCst) {
                    return Err(err("DEVICE_OFFLINE"));
                }
                if e.info["platform"] == "linux" && ["app_id", "window_id", "tab_id"].iter().any(|k| !routing[k].is_null()) {
                    return Err(err("UNSUPPORTED_CAPABILITY"));
                }
                let endpoint = s(&e.info, "id")?;
                let mut resource = endpoint.to_string();
                for key in ["app_id", "window_id", "display_id", "tab_id"] {
                    if let Some(v) = routing[key].as_str() {
                        resource.push('/');
                        resource.push_str(v);
                    }
                }
                if let Some(a) = source {
                    if resource != a.resource && !resource.starts_with(&(a.resource + "/")) {
                        return Err(err("PERMISSION_DENIED"));
                    }
                }
                let id = uid();
                let name = s(&q, "name")?;
                let access = q["access"].as_str().unwrap_or("control");
                self.runtime.attach(Attachment {
                    id: id.clone(),
                    session: session.into(),
                    resource: resource.clone(),
                    conflict_domain: endpoint.into(),
                    name: name.into(),
                    access: access.into(),
                })?;
                return Ok(
                    json!({"attachment":{"id":id,"name":name,"resource_id":resource,"access":access},"resource":e.info,"routing":routing}),
                );
            }

            "capabilities" => {
                let a = self.runtime.check(s(&q, "attachment")?, false)?;
                let e = self.runtime.endpoint(&a.conflict_domain)?;
                let desc = q
                    .get("description")
                    .cloned()
                    .unwrap_or_else(|| e.description.lock().unwrap().clone());
                return Ok(mu_runtime::capabilities::describe(
                    &desc["capabilities"],
                    e.info["platform"].as_str().unwrap_or(""),
                    e.info["provider"].as_str().unwrap_or(""),
                    &q["routing"],
                    &a.access,
                ));
            }
            "format_data" => return Ok(json!(q["data"].to_string())),
            "normalize_app" => {
                let name = mu_core::apps::normalize(s(&q, "name")?);
                if name.is_empty() {
                    return Err(err("INVALID_ARGUMENT"));
                }
                return Ok(json!(name));
            }
            "match_app" => {
                return mu_core::apps::find(s(&q, "name")?, &q["inventory"])
                    .map_err(|(code, msg)| Error::new(code, msg))
            }
            "format_apps" => return Ok(json!(mu_core::apps::format(&q["inventory"]))),
            "session" => {
                self.runtime.session(s(&q, "id")?)?;
                return Ok(Value::Null);
            }
            "session_check" => {
                self.runtime.check_session(s(&q, "id")?)?;
                return Ok(Value::Null);
            }
            "check" => {
                self.runtime.check(s(&q, "id")?, q["write"] == true)?;
                return Ok(Value::Null);
            }
            "attach" => {
                let a: Attachment = serde_json::from_value(q["attachment"].clone())
                    .map_err(|_| err("INVALID_ARGUMENT"))?;
                self.runtime.attach(a)?;
                return Ok(Value::Null);
            }
            "endpoint" => {
                let e = self.runtime.endpoint(s(&q, "id")?)?;
                return Ok(
                    json!({"info":e.info,"description":*e.description.lock().unwrap(),"offline":e.offline.load(Ordering::SeqCst)}),
                );
            }
            "catalog" => {
                return Ok(json!(self
                    .runtime
                    .endpoints
                    .lock()
                    .unwrap()
                    .values()
                    .filter(|e| !e.offline.load(Ordering::SeqCst))
                    .map(|e| e.info.clone())
                    .collect::<Vec<_>>()));
            }
            "events" => {
                return self
                    .runtime
                    .events(s(&q, "session")?, q["after"].as_u64().unwrap_or(0) as usize)
            }
            "cancel" => return self.cancel_composed(s(&q, "session")?, s(&q, "id")?),
            "bridge_reply" => {
                self.runtime
                    .bridges
                    .lock()
                    .unwrap()
                    .get(s(&q, "id")?)
                    .ok_or_else(|| err("DEVICE_OFFLINE"))?
                    .reply(q["reply"].clone())?;
                return Ok(Value::Null);
            }
            "add_bridge" => {
                let b = Bridge::new();
                let info =
                    self.runtime
                        .add(q["info"].clone(), q["description"].clone(), b.clone())?;
                self.runtime
                    .bridges
                    .lock()
                    .unwrap()
                    .insert(s(&info, "id")?.into(), b);
                return Ok(info);
            }
            "add_browser_factory" => {
                let name = s(&q, "name")?;
                if name.is_empty() {
                    return Err(err("INVALID_ARGUMENT"));
                }
                if !matches!(s(&q, "engine")?, "chromium" | "firefox" | "webkit") {
                    return Err(err("INVALID_ARGUMENT"));
                }
                let mut factories = self.factories.lock().unwrap();
                if factories.contains_key(name) {
                    return Err(err("ALREADY_REGISTERED"));
                }
                for key in ["channel", "locale", "user_agent"] {
                    if q.get(key).is_some_and(|v| !v.is_null() && !v.is_string()) { return Err(err("INVALID_ARGUMENT")); }
                }
                if q.get("service_workers").is_some_and(|v| !v.is_null() && !matches!(v.as_str(), Some("allow" | "block"))) { return Err(err("INVALID_ARGUMENT")); }
                if let Some(proxy) = q.get("proxy").filter(|v| !v.is_null()) {
                    if !proxy["server"].is_string() || !proxy.as_object().is_some_and(|o| o.iter().all(|(k,v)| ["server","bypass","username","password"].contains(&k.as_str()) && v.is_string())) { return Err(err("INVALID_ARGUMENT")); }
                }
                for key in ["headless", "background_launch"] {
                    if q.get(key).is_some_and(|v| !v.is_boolean()) {
                        return Err(err("INVALID_ARGUMENT"));
                    }
                }
                if let Some(v) = q.get("viewport") {
                    for k in ["width", "height"] {
                        if !v[k].as_u64().is_some_and(|n| (1..=16384).contains(&n)) {
                            return Err(err("INVALID_ARGUMENT"));
                        }
                    }
                }
                if q.get("device_scale_factor")
                    .is_some_and(|v| !v.as_f64().is_some_and(|n| n > 0.0 && n <= 4.0))
                {
                    return Err(err("INVALID_ARGUMENT"));
                }
                factories.insert(name.into(), q.clone());
                return Ok(q);
            }
            _ => {}
        }
        let mut core = self.runtime.core.lock().unwrap();
        let scope = q["scope"].as_str().unwrap_or("");
        Ok(match op {
            "capture" => legacy(core.observations.capture(
                scope,
                s(&q, "id")?,
                q["data"].clone(),
                q["tree"] == true,
                q["app"].as_str(),
            ))?,
            "node_wire" => legacy(core.observations.node_wire(
                scope,
                s(&q, "id")?,
                s(&q, "node")?,
            ))?,
            "resolve_index" => legacy(core.observations.resolve_index(
                scope,
                n(&q, "index")?,
                q["in_turn"] == true,
            ))?,
            "point" => legacy(core.observations.point(
                scope,
                s(&q, "id")?,
                s(&q, "token")?,
                (
                    q["x"].as_f64().ok_or_else(|| err("INVALID_ARGUMENT"))?,
                    q["y"].as_f64().ok_or_else(|| err("INVALID_ARGUMENT"))?,
                ),
                s(&q, "space")?,
            ))?,
            "point_wire" => legacy(core.observations.point_wire(scope, s(&q, "token")?))?,
            "image_bytes" => {
                json!(STANDARD.encode(legacy(core.observations.image_bytes(scope, s(&q, "id")?))?))
            }
            "historical" => json!(legacy(core.observations.historical(
                scope,
                s(&q, "id")?,
                q["image"] == true
            ))?),
            "render_capture" => legacy(core.observations.render(
                scope,
                s(&q, "id")?,
                s(&q, "header")?,
                q["full"] == true,
            ))?,
            "acknowledge" => {
                legacy(
                    core.observations
                        .acknowledge(scope, s(&q, "id")?, q["image"] == true),
                )?;
                Value::Null
            }
            "failed_delivery" => {
                core.observations.failed_delivery(scope);
                Value::Null
            }
            "freeze" => {
                core.observations.freeze(scope);
                Value::Null
            }
            "reset" => {
                core.observations.reset(scope);
                Value::Null
            }
            "views" => core.observations.views(scope),
            "detach" => {
                core.authority.detach(s(&q, "id")?);
                Value::Null
            }
            "begin" => legacy(core.authority.begin(
                s(&q, "session")?,
                s(&q, "id")?,
                q["request"].clone(),
            ))?,
            "finish" => {
                legacy(core.authority.finish(
                    s(&q, "session")?,
                    s(&q, "id")?,
                    q["result"].clone(),
                ))?;
                Value::Null
            }
            "status" => legacy(core.authority.status(s(&q, "session")?, s(&q, "id")?))?,
            _ => {
                return Err(Error::new(
                    "UNSUPPORTED_CAPABILITY",
                    format!("unknown local operation: {op}"),
                ))
            }
        })
    }
    async fn add_provider(
        self: &Arc<Self>,
        q: &Value,
        provider: Arc<dyn Provider>,
    ) -> Result<Value, Error> {
        let reply = provider
            .request(serde_json::from_value(json!({"op":"describe","timeout_ms":5000})).unwrap())
            .await?;
        reply.validate()?;
        if let Some(e) = reply.error {
            provider.close().await?;
            return Err(e);
        }
        let result = self
            .runtime
            .add(q.clone(), reply.result.unwrap(), provider.clone());
        if result.is_err() {
            let _ = provider.close().await;
        }
        result
    }
    pub async fn call(self: &Arc<Self>, q: Value) -> Result<Value, Error> {
        match s(&q, "op")? {
            "linux_admin" => linux::admin(&q).await,
            "add_linux" => self.add_linux(&q).await,
            "browser_admin" => browser_admin::admin(s(&q, "operation")?, s(&q, "engine")?).await,
            "request" => {
                self.runtime
                    .request(s(&q, "attachment")?, s(&q, "resource")?, q["wire"].clone())
                    .await
            }
            "bridge_next" => {
                let bridge = self
                    .runtime
                    .bridges
                    .lock()
                    .unwrap()
                    .get(s(&q, "id")?)
                    .cloned()
                    .ok_or_else(|| err("DEVICE_OFFLINE"))?;
                bridge.next().await
            }
            "unregister" => {
                self.runtime
                    .detach(s(&q, "id")?, budget(&q, 5000)?.as_millis() as u64)
                    .await
            }
            "events_wait" => {
                let notified = self.runtime.event_changed.notified();
                let result = self
                    .runtime
                    .events(s(&q, "session")?, q["after"].as_u64().unwrap_or(0) as usize)?;
                if result["events"].as_array().unwrap().is_empty() && result["closed"] != true {
                    notified.await;
                }
                self.runtime
                    .events(s(&q, "session")?, q["after"].as_u64().unwrap_or(0) as usize)
            }
            "remove" => {
                self.runtime.remove(s(&q, "id")?).await?;
                Ok(Value::Null)
            }
            "close_session" => {
                let session = s(&q, "session")?;
                let result = self
                    .runtime
                    .close_session(session, budget(&q, 5000)?.as_millis() as u64)
                    .await?;
                let owned = self
                    .owned
                    .lock()
                    .unwrap()
                    .remove(session)
                    .unwrap_or_default();
                for id in owned {
                    if self.runtime.endpoint(&id).is_ok() {
                        self.runtime.remove(&id).await?;
                    }
                    self.artifacts.lock().unwrap().remove(&id);
                }
                self.close_pending(Some(session)).await;
                self.composed
                    .lock()
                    .unwrap()
                    .retain(|(s, _), _| s != session);
                Ok(result)
            }
            "close" => {
                let sessions: Vec<String> = self.owned.lock().unwrap().keys().cloned().collect();
                for session in sessions {
                    let _ = self.runtime.close_session(&session, 5000).await;
                }
                let ids: Vec<String> = self
                    .runtime
                    .endpoints
                    .lock()
                    .unwrap()
                    .keys()
                    .cloned()
                    .collect();
                for id in ids {
                    let _ = self.runtime.remove(&id).await;
                }
                self.close_pending(None).await;
                self.artifacts.lock().unwrap().clear();
                self.runtime.closed.store(true, Ordering::SeqCst);
                Ok(Value::Null)
            }
            "add_process" => {
                let env = serde_json::from_value(q.get("env").cloned().unwrap_or(json!({})))
                    .map_err(|_| err("INVALID_ARGUMENT"))?;
                let args: Vec<String> = serde_json::from_value(q["args"].clone())
                    .map_err(|_| err("INVALID_ARGUMENT"))?;
                let p = mu_client::process::ProcessClient::spawn(s(&q, "program")?, &args, &env)?;
                self.add_provider(&q["info"], p).await
            }
            "add_android" => {
                let serial = s(&q, "serial")?;
                if serial.trim().is_empty() {
                    return Err(err("INVALID_ARGUMENT"));
                }
                let root = repo_root();
                let binary = std::env::var("MAI_UI_BROKER").unwrap_or_else(|_| {
                    root.join("target/debug/mu-broker").to_string_lossy().into()
                });
                let jar = std::env::var("MAI_UI_AGENT_JAR").unwrap_or_else(|_| {
                    root.join("adapters/android/out/agent.jar")
                        .to_string_lossy()
                        .into()
                });
                let p = mu_client::process::ProcessClient::spawn(
                    &binary,
                    &[serial.into(), jar],
                    &BTreeMap::from([("MAI_UI_DEFER_OBSERVATION".into(), "1".into())]),
                )?;
                self.add_provider(&json!({"id":format!("android:{serial}"),"name":q["name"],"kind":"device","platform":"android","provider":"android.uiautomation"}),p).await
            }
            "add_macos" => {
                let p = mu_adapter_macos::MacosProvider::connect_at(
                    q["app_path"].as_str().map(str::to_string),
                    q["socket"].as_str().map(str::to_string),
                )
                .await?;
                self.add_provider(&json!({"id":"macos:local","name":q["name"],"kind":"device","platform":"macos","provider":"macos.cua"}),p).await
            }
            "browser_create" | "copy_files" | "upload_files" | "apps_launch" => {
                self.compose(q).await
            }
            _ => Err(err("UNSUPPORTED_CAPABILITY")),
        }
    }
    async fn copy_files(
        self: &Arc<Self>,
        q: Value,
        control: Arc<composition::Composition>,
    ) -> Result<Value, Error> {
        let started = std::time::Instant::now();
        let limit = budget(&q, 10000)?;
        let remaining = || {
            control.check()?;
            limit
                .checked_sub(started.elapsed())
                .map(|d| d.as_millis().max(1) as u64)
                .ok_or_else(|| err("DEADLINE_EXCEEDED"))
        };
        for key in ["source", "destination"] {
            let a = self
                .runtime
                .check(s(&q[key], "attachment")?, key == "destination")?;
            if a.session != s(&q, "session")? {
                return Err(err("INVALID_ARGUMENT"));
            }
        }
        let src = &q["source"];
        let dst = &q["destination"];
        let data=self.runtime.request(s(src,"attachment")?,s(src,"resource")?,json!({"op":"file","operation":"read","path":src["path"],"max_bytes":1048576,"timeout_ms":remaining()?})).await?;
        if data["truncated"] == true || data["eof"] == false {
            return Err(err("FILE_TOO_LARGE"));
        }
        let payload = s(&data, "data")?;
        let write=self.runtime.request(s(dst,"attachment")?,s(dst,"resource")?,json!({"op":"file","operation":"write","path":dst["path"],"data":payload,"overwrite":q["overwrite"].as_bool().unwrap_or(false),"command_id":format!("{}:write",s(&q,"action_id")?),"timeout_ms":remaining()?})).await?;
        let verify=self.runtime.request(s(dst,"attachment")?,s(dst,"resource")?,json!({"op":"file","operation":"read","path":dst["path"],"max_bytes":1048576,"timeout_ms":remaining().map_err(|_|err("ACTION_OUTCOME_UNKNOWN"))?})).await.map_err(|e|Error::new("ACTION_OUTCOME_UNKNOWN",format!("Write completed; verification failed: {e}")))?;
        if verify["data"] != data["data"] {
            return Err(Error::new(
                "ACTION_OUTCOME_UNKNOWN",
                "Write completed; transfer verification failed",
            ));
        }
        let bytes = STANDARD
            .decode(payload)
            .map_err(|_| err("PROTOCOL_MISMATCH"))?;
        use sha2::Digest;
        Ok(
            json!({"source":src["path"],"destination":dst["path"],"bytes":bytes.len(),"sha256":format!("{:x}",sha2::Sha256::digest(&bytes)),"verified":true,"write":write}),
        )
    }
    async fn create_browser(
        self: &Arc<Self>,
        q: Value,
        control: Arc<composition::Composition>,
    ) -> Result<Value, Error> {
        let session = s(&q, "session")?;
        self.runtime.check_session(session)?;
        if q.get("lifetime").is_some_and(|v| v != "session") {
            return Err(err("UNSUPPORTED_CAPABILITY"));
        }
        let factory = self
            .factories
            .lock()
            .unwrap()
            .get(s(&q, "provider")?)
            .cloned()
            .ok_or_else(|| err("TARGET_NOT_FOUND"))?;
        let limit = budget(&q, 30000)?;
        let deadline = tokio::time::Instant::now() + limit;
        let _gate = tokio::select! {
            gate = tokio::time::timeout_at(deadline, self.creation.lock()) => gate.map_err(|_|err("DEADLINE_EXCEEDED"))?,
            _ = control.wake.notified() => return Err(err("CANCELLED_BEFORE_DISPATCH")),
        };
        control.check()?;
        let name = s(&q, "name")?;
        let id = format!("browser:{}", uid());
        let aid = uid();
        let attachment = Attachment {
            id: aid.clone(),
            session: session.into(),
            resource: id.clone(),
            conflict_domain: id.clone(),
            name: name.into(),
            access: "control".into(),
        };
        let artifact_dir = Arc::new(
            tempfile::Builder::new()
                .prefix("mai-ui-browser-")
                .tempdir()
                .map_err(|e| Error::new("IO_ERROR", e.to_string()))?,
        );
        let worker = browser_worker()?;
        let p = mu_client::process::ProcessClient::spawn(
            &std::env::var("MAI_UI_NODE").unwrap_or("node".into()),
            &[worker.to_string_lossy().into()],
            &BTreeMap::new(),
        )?;
        self.pending_browsers.lock().unwrap().insert(
            id.clone(),
            PendingBrowser {
                session: session.into(),
                provider: p.clone(),
                _artifacts: artifact_dir.clone(),
            },
        );
        let mut closed = p.closed_signal();
        let weak = Arc::downgrade(self);
        let failed_endpoint = id.clone();
        tokio::spawn(async move {
            while !*closed.borrow_and_update() {
                if closed.changed().await.is_err() {
                    break;
                }
            }
            if let Some(host) = weak.upgrade() {
                if let Ok(e) = host.runtime.endpoint(&failed_endpoint) {
                    e.offline.store(true, Ordering::SeqCst);
                    host.runtime.provider_event(&failed_endpoint,json!({"type":"resource_offline","resource_id":failed_endpoint,"data":{"reason":"worker_exited"}}));
                }
            }
        });
        let mut events = p.subscribe();
        let host = Arc::downgrade(self);
        let endpoint_id = id.clone();
        tokio::spawn(async move {
            loop {
                let received = events.recv().await;
                let Some(host) = host.upgrade() else { break };
                match received {
                    Ok(event) => host.runtime.provider_event(&endpoint_id,
                        json!({"type":event.event,"resource_id":event.resource_id,"data":event.data})),
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(count)) =>
                        host.runtime.provider_event(&endpoint_id, json!({"type":"event_gap","resource_id":endpoint_id,"data":{"dropped":count}})),
                    Err(_) => break,
                }
            }
        });
        let mut launch = factory;
        launch["op"] = json!("init");
        launch["artifact_directory"] = json!(artifact_dir.path());
        launch["context_id"] = json!(id);
        launch["storage_state"] = q["storage_state"].clone();
        launch["timeout_ms"] = json!(limit.as_millis() as u64);
        let result = tokio::select! { result=tokio::time::timeout_at(deadline, async{
            self.runtime.check_session(session)?;
            let reply=p.request(serde_json::from_value(launch).unwrap()).await?;reply.validate()?;if let Some(e)=reply.error{return Err(e);}
            let info=json!({"id":id,"name":name,"kind":"browser_context","platform":"browser","provider":"browser.playwright"});
            self.add_provider(&info,p.clone()).await?;self.runtime.attach(attachment)?;
            self.owned.lock().unwrap().entry(session.into()).or_default().push(id.clone());
            Ok(json!({"resource":info,"attachment":{"id":aid,"name":name,"resource_id":id,"access":"control"}}))
        }) => result.unwrap_or_else(|_| Err(err("DEADLINE_EXCEEDED"))),
            _ = control.wake.notified() => Err(err("ACTION_OUTCOME_UNKNOWN")),
        };
        if result
            .as_ref()
            .err()
            .is_some_and(|e| e.code == "DEADLINE_EXCEEDED" || e.code == "ACTION_OUTCOME_UNKNOWN")
        {
            let host = self.clone();
            let cleanup_id = id.clone();
            tokio::spawn(async move {
                let _ = p.close().await;
                host.pending_browsers.lock().unwrap().remove(&cleanup_id);
                host.runtime.endpoints.lock().unwrap().remove(&cleanup_id);
            });
            return Err(Error::new(
                "ACTION_OUTCOME_UNKNOWN",
                "Creation deadline or cancellation reached; private allocation cleanup is draining",
            ));
        }
        if result.is_err() {
            let _ = p.close().await;
            self.runtime.endpoints.lock().unwrap().remove(&id);
        } else {
            self.artifacts
                .lock()
                .unwrap()
                .insert(id.clone(), artifact_dir);
        }
        self.pending_browsers.lock().unwrap().remove(&id);
        result
    }
    async fn close_pending(&self, session: Option<&str>) {
        let pending: Vec<_> = self
            .pending_browsers
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, p)| session.is_none_or(|s| p.session == s))
            .map(|(id, p)| (id.clone(), p.provider.clone()))
            .collect();
        for (id, p) in pending {
            let _ = p.close().await;
            self.pending_browsers.lock().unwrap().remove(&id);
        }
    }
}
pub fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
pub fn browser_worker() -> Result<PathBuf, Error> {
    let validate = |worker: PathBuf| {
        let has_playwright = worker.parent().is_some_and(|dir| {
            dir.ancestors()
                .any(|root| root.join("node_modules/playwright/package.json").is_file())
        });
        let has_background_module = worker
            .parent()
            .is_some_and(|dir| dir.join("background.js").is_file());
        if worker.is_file() && has_playwright && has_background_module {
            Ok(worker)
        } else {
            Err(Error::new(
                "UNSUPPORTED_CAPABILITY",
                "Browser worker is missing Playwright or its background-browser module; run mu setup browser",
            ))
        }
    };
    if let Some(explicit) = std::env::var_os("MAI_UI_PLAYWRIGHT_WORKER") {
        return validate(PathBuf::from(explicit));
    }
    let cache = std::env::var_os("MAI_UI_BROWSER_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME")
                .or_else(|| std::env::var_os("USERPROFILE"))
                .map(|h| PathBuf::from(h).join(".cache/mai-ui/browser/1.63.0"))
        })
        .map(|p| p.join("dist/worker.js"));
    for p in cache
        .into_iter()
        .chain([repo_root().join("adapters/browser-playwright/dist/worker.js")])
    {
        if p.is_file() {
            return validate(p);
        }
    }
    Err(Error::new(
        "UNSUPPORTED_CAPABILITY",
        "Browser adapter is not installed; run mu setup browser",
    ))
}

#[cfg(test)]
mod browser_worker_tests {
    use super::browser_worker;

    #[test]
    fn explicit_worker_requires_resolvable_playwright_dependency() {
        let temp = tempfile::tempdir().unwrap();
        let dist = temp.path().join("dist");
        std::fs::create_dir(&dist).unwrap();
        let worker = dist.join("worker.js");
        std::fs::write(&worker, "").unwrap();
        let previous = std::env::var_os("MAI_UI_PLAYWRIGHT_WORKER");
        std::env::set_var("MAI_UI_PLAYWRIGHT_WORKER", &worker);
        let missing = browser_worker();
        let playwright = dist.join("node_modules/playwright");
        std::fs::create_dir_all(&playwright).unwrap();
        std::fs::write(playwright.join("package.json"), "{}").unwrap();
        let outdated = browser_worker();
        std::fs::write(dist.join("background.js"), "").unwrap();
        let installed = browser_worker();
        if let Some(value) = previous {
            std::env::set_var("MAI_UI_PLAYWRIGHT_WORKER", value);
        } else {
            std::env::remove_var("MAI_UI_PLAYWRIGHT_WORKER");
        }
        let error = missing.unwrap_err();
        assert_eq!(error.code, "UNSUPPORTED_CAPABILITY");
        assert!(error.message.contains("mu setup browser"));
        let outdated_error = outdated.unwrap_err();
        assert_eq!(outdated_error.code, "UNSUPPORTED_CAPABILITY");
        assert!(outdated_error.message.contains("mu setup browser"));
        assert_eq!(installed.unwrap(), worker);
    }
}

pub mod browser_admin;

pub mod shell;

mod composition;

pub mod linux;
