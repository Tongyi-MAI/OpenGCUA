//! Embedded, binding-independent provider catalog and operation executor.
//! Language callbacks are transports only: admission, deadlines and effects live here.
use crate::{Attachment, Runtime};
use mu_adapter::{Provider, ProviderFuture};
use mu_protocol::{Error, Reply, Request};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tokio::sync::{mpsc, oneshot, watch, Mutex as AsyncMutex, Notify, RwLock};

static NEXT_ID: AtomicU64 = AtomicU64::new(1);
pub fn uid() -> String {
    format!(
        "{}-{}",
        std::process::id(),
        NEXT_ID.fetch_add(1, Ordering::Relaxed)
    )
}
pub fn err(code: &str) -> Error {
    Error::new(code, code)
}
pub fn budget(q: &Value, default: u64) -> Result<Duration, Error> {
    let n = q
        .get("timeout_ms")
        .map(|v| v.as_u64().ok_or_else(|| err("INVALID_ARGUMENT")))
        .transpose()?
        .unwrap_or(default);
    if !(1..=120000).contains(&n) {
        return Err(err("INVALID_ARGUMENT"));
    }
    Ok(Duration::from_millis(n))
}

/// Bridge for host-injected providers (including Python test/custom adapters).
/// Built-in providers attach directly and do not pass through language callbacks.
pub struct Bridge {
    tx: mpsc::UnboundedSender<(Request, oneshot::Sender<Result<Reply, Error>>)>,
    rx: AsyncMutex<mpsc::UnboundedReceiver<(Request, oneshot::Sender<Result<Reply, Error>>)>>,
    pending: Mutex<HashMap<u64, oneshot::Sender<Result<Reply, Error>>>>,
    sequence: AtomicU64,
    closed: AtomicBool,
    close_event: Notify,
}
impl Bridge {
    pub fn new() -> Arc<Self> {
        let (tx, rx) = mpsc::unbounded_channel();
        Arc::new(Self {
            tx,
            rx: AsyncMutex::new(rx),
            pending: Mutex::default(),
            sequence: AtomicU64::new(1),
            closed: AtomicBool::new(false),
            close_event: Notify::new(),
        })
    }
    pub async fn next(&self) -> Result<Value, Error> {
        let closed = self.close_event.notified();
        if self.closed.load(Ordering::SeqCst) {
            return Err(err("SESSION_CLOSED"));
        }
        let mut rx = self.rx.lock().await;
        let (mut q, tx) = tokio::select! { v=rx.recv()=>v.ok_or_else(||err("SESSION_CLOSED"))?, _=closed=>return Err(err("SESSION_CLOSED")) };
        let id = self.sequence.fetch_add(1, Ordering::Relaxed);
        q.request_id = Some(id);
        self.pending.lock().unwrap().insert(id, tx);
        Ok(serde_json::to_value(q).unwrap())
    }
    pub fn reply(&self, q: Value) -> Result<(), Error> {
        let r: Reply = serde_json::from_value(q)
            .map_err(|e| Error::new("PROTOCOL_MISMATCH", e.to_string()))?;
        r.validate()?;
        let id = r.request_id.ok_or_else(|| err("PROTOCOL_MISMATCH"))?;
        if let Some(tx) = self.pending.lock().unwrap().remove(&id) {
            let _ = tx.send(Ok(r));
        }
        Ok(())
    }
}
impl Provider for Bridge {
    fn request(&self, q: Request) -> ProviderFuture<'_, Reply> {
        Box::pin(async move {
            let (tx, rx) = oneshot::channel();
            self.tx
                .send((q, tx))
                .map_err(|_| Error::new("DEVICE_OFFLINE", "host adapter bridge is closed"))?;
            rx.await.map_err(|_| {
                Error::new("DEVICE_OFFLINE", "host adapter bridge dropped the request without a reply")
            })?
        })
    }
    fn close(&self) -> ProviderFuture<'_, ()> {
        Box::pin(async move {
            self.closed.store(true, Ordering::SeqCst);
            self.close_event.notify_waiters();
            for (_, tx) in self.pending.lock().unwrap().drain() {
                let _ = tx.send(Err(Error::new(
                    "DEVICE_OFFLINE",
                    "host adapter bridge closed before replying",
                )));
            }
            Ok(())
        })
    }
}

pub struct Endpoint {
    pub info: Value,
    pub description: Mutex<Value>,
    pub provider: Arc<dyn Provider>,
    pub offline: AtomicBool,
    pub context_gate: Arc<RwLock<()>>,
    pub fenced: Mutex<HashSet<String>>,
    /// Why the endpoint went offline (key "") or a scope was fenced (key = resource).
    pub reasons: Mutex<HashMap<String, String>>,
}
impl Endpoint {
    /// Latch the endpoint offline; the first recorded cause wins.
    pub fn mark_offline(&self, reason: impl Into<String>) {
        self.reasons
            .lock()
            .unwrap()
            .entry(String::new())
            .or_insert_with(|| reason.into());
        self.offline.store(true, Ordering::SeqCst);
    }
    pub fn fence(&self, resource: &str, reason: impl Into<String>) {
        self.reasons
            .lock()
            .unwrap()
            .insert(resource.to_string(), reason.into());
        self.fenced.lock().unwrap().insert(resource.to_string());
    }
    pub fn unfence(&self, resource: &str) {
        self.fenced.lock().unwrap().remove(resource);
        self.reasons.lock().unwrap().remove(resource);
    }
    pub fn offline_reason(&self) -> Option<String> {
        self.reasons.lock().unwrap().get("").cloned()
    }
    /// DEVICE_OFFLINE carrying the recorded cause instead of a bare code.
    pub fn offline_error(&self, resource: &str) -> Error {
        let id = self.info["id"].as_str().unwrap_or("endpoint");
        if self.offline.load(Ordering::SeqCst) {
            let reason = self
                .offline_reason()
                .unwrap_or_else(|| "cause was not recorded".into());
            return Error::new("DEVICE_OFFLINE", format!("{id} is offline: {reason}"))
                .with_details(json!({"cause":"endpoint_offline", "scope":id,
                    "reason":reason, "next_step":"host_reconnect"}));
        }
        let scope = self
            .fenced
            .lock()
            .unwrap()
            .iter()
            .find(|f| resource == f.as_str() || resource.starts_with(&format!("{f}/")))
            .cloned();
        let scope = scope.unwrap_or_else(|| resource.to_string());
        let reason = self.reasons.lock().unwrap().get(&scope).cloned();
        let message = reason.as_ref()
            .map(|r| format!("{scope} is temporarily blocked: {r}"))
            .unwrap_or_else(|| format!("{resource} is blocked by an unfinished operation"));
        Error::new("DEVICE_OFFLINE", message)
            .with_details(json!({"cause":"scope_fenced", "scope":scope,
                "reason":reason.unwrap_or_else(|| "unfinished operation".into()),
                "next_step":"wait_for_operation"}))
    }
}
/// Short operation label for diagnostics, e.g. "browser.playwright.locator.click".
fn describe_wire(wire: &Value) -> String {
    let mut parts = vec![wire["op"].as_str().unwrap_or("request").to_string()];
    for key in ["operation", "action", "kind", "method"] {
        if let Some(v) = wire[key].as_str() {
            parts.push(v.to_string());
        }
    }
    parts.join(".")
}
struct Operation {
    result: watch::Sender<Option<Result<Value, Error>>>,
    cancelled: AtomicBool,
    wake: Notify,
}
#[derive(Default)]
pub struct Embedded {
    pub core: Mutex<Runtime>,
    pub endpoints: Mutex<BTreeMap<String, Arc<Endpoint>>>,
    pub bridges: Mutex<HashMap<String, Arc<Bridge>>>,
    operations: Mutex<HashMap<(String, String), Arc<Operation>>>,
    sessions: Mutex<HashMap<String, bool>>,
    completed_sessions: Mutex<HashSet<String>>,
    detached: Mutex<HashSet<String>>,
    events: Mutex<HashMap<String, Vec<Value>>>,
    pub event_changed: Notify,
    pub closed: AtomicBool,
}
impl Embedded {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }
    pub fn session(&self, id: &str) -> Result<(), Error> {
        if self.closed.load(Ordering::SeqCst) {
            return Err(err("SESSION_CLOSED"));
        }
        self.sessions.lock().unwrap().insert(id.into(), false);
        Ok(())
    }
    pub fn check_session(&self, id: &str) -> Result<(), Error> {
        if self.closed.load(Ordering::SeqCst)
            || self.sessions.lock().unwrap().get(id) != Some(&false)
        {
            Err(err("SESSION_CLOSED"))
        } else {
            Ok(())
        }
    }
    pub fn endpoint(&self, id: &str) -> Result<Arc<Endpoint>, Error> {
        self.endpoints
            .lock()
            .unwrap()
            .get(id)
            .cloned()
            .ok_or_else(|| {
                Error::new(
                    "DEVICE_OFFLINE",
                    format!("{id} is no longer registered (it was removed or its session closed)"),
                )
            })
    }
    pub fn add(
        &self,
        info: Value,
        description: Value,
        provider: Arc<dyn Provider>,
    ) -> Result<Value, Error> {
        if self.closed.load(Ordering::SeqCst) {
            return Err(err("SESSION_CLOSED"));
        }
        let id = info["id"]
            .as_str()
            .ok_or_else(|| err("INVALID_ARGUMENT"))?
            .to_string();
        let mut endpoints = self.endpoints.lock().unwrap();
        if endpoints.contains_key(&id)
            || (info["kind"] != "browser_context"
                && endpoints.values().any(|e| e.info["name"] == info["name"]))
        {
            return Err(err("ALREADY_REGISTERED"));
        }
        if description["protocol_major"] != 1 || description["platform"] != info["platform"] {
            return Err(err("PROTOCOL_MISMATCH"));
        }
        endpoints.insert(
            id,
            Arc::new(Endpoint {
                info: info.clone(),
                description: Mutex::new(description),
                provider,
                offline: AtomicBool::new(false),
                context_gate: Arc::new(RwLock::new(())),
                fenced: Mutex::default(),
                reasons: Mutex::default(),
            }),
        );
        Ok(info)
    }
    pub fn attach(&self, a: Attachment) -> Result<(), Error> {
        self.check_session(&a.session)?;
        self.endpoint(&a.conflict_domain)?;
        self.core
            .lock()
            .unwrap()
            .authority
            .attach(a.clone())
            .map_err(err)?;
        self.event(&a.session,json!({"type":"attachment_added","attachment":{"id":a.id,"name":a.name,"resource_id":a.resource,"access":a.access}}));
        Ok(())
    }
    pub fn check(&self, id: &str, write: bool) -> Result<Attachment, Error> {
        if self.detached.lock().unwrap().contains(id) {
            return Err(err("STALE_ATTACHMENT"));
        }
        let a = self
            .core
            .lock()
            .unwrap()
            .authority
            .check(id, write)
            .map_err(err)?
            .clone();
        self.check_session(&a.session)?;
        let e = self.endpoint(&a.conflict_domain)?;
        if e.offline.load(Ordering::SeqCst) || e.fenced.lock().unwrap().contains(&a.resource) {
            return Err(e.offline_error(&a.resource));
        }
        Ok(a)
    }
    pub fn is_detached(&self, id: &str) -> bool {
        self.detached.lock().unwrap().contains(id)
    }
    pub fn event(&self, session: &str, mut value: Value) {
        let mut e = self.events.lock().unwrap();
        let log = e.entry(session.into()).or_default();
        value["cursor"] = json!(log.len() + 1);
        log.push(value);
        drop(e);
        self.event_changed.notify_waiters();
    }
    pub fn events(&self, session: &str, after: usize) -> Result<Value, Error> {
        let all = self.events.lock().unwrap();
        let log = all.get(session);
        let len = log.map_or(0, |v| v.len());
        if after > len {
            return Err(err("INVALID_ARGUMENT"));
        }
        Ok(
            json!({"events":log.map(|v|v[after..].to_vec()).unwrap_or_default(),"closed":self.completed_sessions.lock().unwrap().contains(session)}),
        )
    }
    pub fn provider_event(&self, endpoint: &str, value: Value) {
        let sessions: HashSet<String> = self
            .core
            .lock()
            .unwrap()
            .authority
            .attachments
            .values()
            .filter(|a| {
                a.conflict_domain == endpoint
                    && value["resource_id"].as_str().is_some_and(|r| {
                        r == a.resource || r.starts_with(&(a.resource.clone() + "/"))
                    })
            })
            .map(|a| a.session.clone())
            .collect();
        for s in sessions {
            self.event(&s, value.clone());
        }
    }
    pub async fn request(
        self: &Arc<Self>,
        attachment: &str,
        resource: &str,
        mut wire: Value,
    ) -> Result<Value, Error> {
        let a = self.check(attachment, false)?;
        if resource != a.resource && !resource.starts_with(&(a.resource.clone() + "/")) {
            return Err(err("PERMISSION_DENIED"));
        }
        let endpoint = self.endpoint(&a.conflict_domain)?;
        if endpoint
            .fenced
            .lock()
            .unwrap()
            .iter()
            .any(|f| resource == f || resource.starts_with(&(f.clone() + "/")))
        {
            return Err(endpoint.offline_error(resource));
        }

        crate::policy::validate(
            &mut wire,
            endpoint.info["platform"].as_str().unwrap_or(""),
            &endpoint.description.lock().unwrap(),
            resource,
            &a.conflict_domain,
        )?;
        let limit = budget(&wire, 3000)?;
        let remaining = wire
            .as_object_mut()
            .unwrap()
            .remove("_budget_ms")
            .map(|n| json!({"timeout_ms":n}));
        let limit = if let Some(v) = remaining {
            budget(&v, 3000)?
        } else {
            limit
        };
        let request: Request = serde_json::from_value(wire.clone())
            .map_err(|e| Error::new("INVALID_ARGUMENT", e.to_string()))?;
        let mutation = request.is_mutation();
        if mutation {
            self.check(attachment, true)?;
        }
        let key = if mutation {
            wire["action_id"]
                .as_str()
                .or(wire["command_id"].as_str())
                .map(str::to_string)
                .unwrap_or_else(uid)
        } else {
            uid()
        };
        if mutation {
            let field = if wire["op"] == "action" || wire["op"] == "browser" {
                "action_id"
            } else {
                "command_id"
            };
            wire[field] = json!(key);
        }
        let mapkey = (a.session.clone(), key.clone());
        let op = {
            let mut ops = self.operations.lock().unwrap();
            if mutation {
                self.core
                    .lock()
                    .unwrap()
                    .authority
                    .begin(
                        &a.session,
                        &key,
                        json!({"attachment":attachment,"resource":resource,"wire":wire}),
                    )
                    .map_err(err)?;
            }
            if let Some(op) = ops.get(&mapkey) {
                op.clone()
            } else {
                let (tx, _) = watch::channel(None);
                let op = Arc::new(Operation {
                    result: tx,
                    cancelled: AtomicBool::new(false),
                    wake: Notify::new(),
                });
                ops.insert(mapkey, op.clone());
                let this = self.clone();
                let op2 = op.clone();
                let resource = resource.to_string();
                let key2 = key.clone();
                tokio::spawn(async move {
                    let result = this
                        .dispatch(a, resource, wire, limit, mutation, &key2, &op2)
                        .await;
                    op2.result.send_replace(Some(result));
                });
                op
            }
        };
        let mut rx = op.result.subscribe();
        loop {
            if let Some(result) = rx.borrow().clone() {
                return result;
            }
            rx.changed().await.map_err(|_| {
                Error::new("DEVICE_OFFLINE", "operation was dropped before it produced a result")
            })?;
        }
    }
    async fn dispatch(
        self: &Arc<Self>,
        a: Attachment,
        resource: String,
        mut wire: Value,
        limit: Duration,
        mutation: bool,
        key: &str,
        op: &Arc<Operation>,
    ) -> Result<Value, Error> {
        let e = self.endpoint(&a.conflict_domain)?;
        let started = Instant::now();
        let mut record = self
            .core
            .lock()
            .unwrap()
            .authority
            .status(&a.session, key)
            .unwrap_or(json!({"sequence":0}));
        for (k,v) in json!({"action_id":key,"session_id":a.session,"attachment_id":a.id,"resource_id":resource,"provider":e.info["provider"],"execution":"queued","effect":"unknown"}).as_object().unwrap(){record[k]=v.clone();}
        let save = |record: &Value| {
            if mutation {
                let _ = self
                    .core
                    .lock()
                    .unwrap()
                    .authority
                    .finish(&a.session, key, record.clone());
            }
        };
        if let Some(basis) = wire.as_object_mut().unwrap().remove("_based_on") {
            record["based_on"] = basis;
        }
        wire.as_object_mut().unwrap().remove("_node_actions");
        save(&record);
        let scheduler = self.core.lock().unwrap().scheduler.clone();
        let browser = e.info["platform"] == "browser";
        let cancellable = browser || e.info["platform"] == "linux";
        let tab = wire["tab_id"].as_str().map(str::to_string);
        let domain = if browser {
            tab.as_ref()
                .map(|t| format!("{}/{t}", a.conflict_domain))
                .unwrap_or(a.conflict_domain.clone())
        } else {
            a.conflict_domain.clone()
        };
        let dispatched = Arc::new(AtomicBool::new(false));
        let dispatched2 = dispatched.clone();
        let observation = !mutation && wire["op"] == "observe";
        let what = describe_wire(&wire);
        // The owned task retains every guard until the provider reply drains, even
        // when the caller's deadline expires. No next write can overtake it.
        let this = self.clone();
        let a2 = a.clone();
        let e2 = e.clone();
        let key2 = key.to_string();
        let op2 = op.clone();
        let resource2 = resource.clone();
        let mut task = tokio::spawn(async move {
            let _tab = scheduler.acquire(&domain).await;
            let _context_read = if browser && tab.is_some() {
                Some(e2.context_gate.clone().read_owned().await)
            } else {
                None
            };
            let _context_write = if browser && tab.is_none() {
                Some(e2.context_gate.clone().write_owned().await)
            } else {
                None
            };
            this.check(&a2.id, mutation)?;
            if op2.cancelled.load(Ordering::SeqCst) {
                return Err(err("CANCELLED_BEFORE_DISPATCH"));
            }
            let remaining = limit
                .checked_sub(started.elapsed())
                .ok_or_else(|| err("DEADLINE_EXCEEDED"))?;
            wire["timeout_ms"] = json!(remaining.as_millis().max(1) as u64);
            if let Some(cap) = wire.as_object_mut().unwrap().remove("_capability") {
                let desc = e2.description.lock().unwrap();
                if desc["capabilities"][cap.as_str().unwrap_or("")]["state"] != "ready" {
                    return Err(err("UNSUPPORTED_CAPABILITY"));
                }
            }
            if cancellable {
                wire["_operation_id"] = json!(key2);
            }
            dispatched2.store(true, Ordering::SeqCst);
            if mutation {
                let mut c = this.core.lock().unwrap();
                let mut r = c.authority.status(&a2.session, &key2).map_err(err)?;
                r["execution"] = json!("running");
                c.authority.finish(&a2.session, &key2, r).map_err(err)?;
            }
            let reply = e2
                .provider
                .request(serde_json::from_value(wire).map_err(|_| err("INVALID_ARGUMENT"))?)
                .await?;
            reply.validate()?;
            // Cancellable provider failures only fence this scope. A drained reply
            // establishes that no adapter operation remains in flight.
            if cancellable {
                e2.unfence(&resource2);
            }
            if let Some(error) = reply.error {
                return Err(error);
            }
            Ok(reply.result.unwrap())
        });
        let (outcome, deadline_expired) = tokio::select! {
            result=&mut task=>(result.map_err(|_|Error::new("DEVICE_OFFLINE","provider task was aborted before replying")).and_then(|v|v), false),
            _=tokio::time::sleep(limit)=>(Err(Error::new("DEADLINE_EXCEEDED",format!("{what} did not finish within its {} ms budget",limit.as_millis()))), true),
            _=op.wake.notified()=>(Err(err(if dispatched.load(Ordering::SeqCst){"ACTION_OUTCOME_UNKNOWN"}else{"CANCELLED_BEFORE_DISPATCH"})), false),
        };
        let was_dispatched = dispatched.load(Ordering::SeqCst);
        let outcome = outcome.and_then(|v| {
            if (mutation && v.get("execution").is_some_and(|s| s != "completed"))
                || v["state"] == "timed_out"
                || v["cleanup"] == "unknown"
            {
                Err(err("ACTION_OUTCOME_UNKNOWN"))
            } else {
                Ok(v)
            }
        });
        record["duration_ms"] = json!(started.elapsed().as_millis() as u64);
        match outcome {
            Ok(v) => {
                record["execution"] = json!("completed");
                for k in ["effect", "mechanism", "delivery", "evidence", "policy"] {
                    if let Some(v) = v.get(k) {
                        record[k] = v.clone();
                    }
                }
                save(&record);
                Ok(v)
            }
            Err(mut error) => {
                if observation && error.code == "DEADLINE_EXCEEDED" {
                    if was_dispatched && deadline_expired {
                        // The capture has no input effect. Keep its scheduler guard
                        // until the correlated reply drains, discard that late image,
                        // and allow the caller to continue or request a fresh capture.
                        let endpoint = e.clone();
                        tokio::spawn(async move {
                            let lost = match task.await {
                                Err(_) => Some("the capture task was aborted".to_string()),
                                Ok(Err(error))
                                    if matches!(
                                        error.code.as_str(),
                                        "DEVICE_OFFLINE"
                                            | "SESSION_CLOSED"
                                            | "PROTOCOL_MISMATCH"
                                            | "ACTION_OUTCOME_UNKNOWN"
                                    ) =>
                                {
                                    Some(error.to_string())
                                }
                                _ => None,
                            };
                            if let Some(cause) = lost {
                                endpoint.mark_offline(format!(
                                    "a capture that exceeded its deadline later failed ({cause})"
                                ));
                            }
                        });
                    } else if !was_dispatched {
                        op.cancelled.store(true, Ordering::SeqCst);
                        task.abort();
                    }
                    return Err(Error::new(
                        "OBSERVATION_FAILED",
                        "capture deadline exceeded; the task may continue and request a fresh observation",
                    ));
                }
                let known = matches!(
                    error.code.as_str(),
                    "STALE_TARGET"
                        | "INVALID_ARGUMENT"
                        | "UNSUPPORTED_CAPABILITY"
                        | "PERMISSION_DENIED"
                        | "STALE_ATTACHMENT"
                        | "SESSION_CLOSED"
                        | "TARGET_NOT_FOREGROUND"
                        | "TARGET_OCCLUDED"
                        | "TARGET_NOT_FOUND"
                        | "ALREADY_EXISTS"
                        | "AMBIGUOUS_TARGET"
                        | "INPUT_REFUSED"
                        | "ACTION_REJECTED"
                        | "GEOMETRY_CHANGED"
                        | "VERSION_MISMATCH"
                        | "FILE_TOO_LARGE"
                        | "CANCELLED_BEFORE_DISPATCH"
                ) || (!mutation
                    && matches!(
                        error.code.as_str(),
                        "OBSERVATION_FAILED" | "PROVIDER_ERROR" | "FILE_READ_FAILED"
                    ));
                let unknown = was_dispatched && !known;
                if !was_dispatched {
                    op.cancelled.store(true, Ordering::SeqCst);
                    task.abort();
                }
                if unknown {
                    let cause = format!(
                        "{what} (id {key}) failed after it was dispatched ({error}); its outcome is unknown"
                    );
                    if cancellable {
                        e.fence(
                            &resource,
                            format!("{cause}, and the provider is still running it. Requests to this scope are rejected until that operation finishes; other tabs are unaffected"),
                        );
                    } else {
                        e.mark_offline(cause);
                    }
                    if mutation {
                        error.code = "ACTION_OUTCOME_UNKNOWN".into();
                    }
                    if cancellable {
                        let provider = e.provider.clone();
                        let operation_id = key.to_string();
                        tokio::spawn(async move {
                            let _ = tokio::time::timeout(
                                Duration::from_secs(1),
                                provider.request(
                                    serde_json::from_value(
                                        json!({"op":"cancel","operation_id":operation_id}),
                                    )
                                    .unwrap(),
                                ),
                            )
                            .await;
                        });
                    }
                    if !task.is_finished() {
                        let e3 = e.clone();
                        let resource3 = resource.clone();
                        tokio::spawn(async move {
                            let _ = task.await;
                            if cancellable {
                                e3.unfence(&resource3);
                            }
                        });
                    } else if cancellable {
                        e.unfence(&resource);
                    }
                }
                record["execution"] = json!(if unknown {
                    "unknown"
                } else if error.code == "CANCELLED_BEFORE_DISPATCH" {
                    "cancelled_before_dispatch"
                } else {
                    "rejected"
                });
                record["effect"] = json!(if unknown { "unknown" } else { "not_applied" });
                if let Some(details) = &error.details {
                    for k in ["mechanism", "delivery", "evidence", "policy"] {
                        if let Some(v) = details.get(k) {
                            record[k] = v.clone();
                        }
                    }
                }
                record["error"] = serde_json::to_value(&error).unwrap();
                save(&record);
                if mutation {
                    error.details = Some(json!({"receipt":record}));
                }
                Err(error)
            }
        }
    }
    pub fn cancel(&self, session: &str, key: &str) -> Result<Value, Error> {
        let ops = self.operations.lock().unwrap();
        let op = ops
            .get(&(session.into(), key.into()))
            .ok_or_else(|| err("TARGET_NOT_FOUND"))?;
        op.cancelled.store(true, Ordering::SeqCst);
        op.wake.notify_one();
        let status = self
            .core
            .lock()
            .unwrap()
            .authority
            .status(session, key)
            .map_err(err)?;
        Ok(
            json!({"action_id":key,"execution":if status["execution"]=="queued"{"cancelled_before_dispatch"}else{status["execution"].as_str().unwrap_or("unknown")}}),
        )
    }
    pub async fn detach(&self, id: &str, ms: u64) -> Result<Value, Error> {
        let a = self
            .core
            .lock()
            .unwrap()
            .authority
            .check(id, false)
            .map_err(err)?
            .clone();
        self.detached.lock().unwrap().insert(id.into());
        self.core.lock().unwrap().observations.forget_attachment(id);
        let e = self.endpoint(&a.conflict_domain)?;
        let scheduler = self.core.lock().unwrap().scheduler.clone();
        let tab_only = e.info["platform"] == "browser" && a.resource != a.conflict_domain;
        let release_inputs = a.access == "write"
            && e.description.lock().unwrap()["capabilities"]["ui.release_all"]["state"] == "ready";
        let drained = tokio::time::timeout(Duration::from_millis(ms), async {
            let cleanup = async {
                if release_inputs {
                    let mut wire = json!({"op":"release_inputs","timeout_ms":ms});
                    if tab_only {
                        wire["tab_id"] = json!(a
                            .resource
                            .strip_prefix(&(a.conflict_domain.clone() + "/"))
                            .unwrap_or(""));
                    }
                    let reply = e
                        .provider
                        .request(serde_json::from_value(wire).map_err(|_| err("INVALID_ARGUMENT"))?)
                        .await?;
                    if let Some(error) = reply.error {
                        return Err(error);
                    }
                }
                Ok::<(), Error>(())
            };
            if tab_only {
                let _tab = scheduler.acquire(&a.resource).await;
                let _context = e.context_gate.clone().read_owned().await;
                cleanup.await
            } else {
                let _endpoint = scheduler.acquire(&a.conflict_domain).await;
                let _context = e.context_gate.clone().write_owned().await;
                cleanup.await
            }
        })
        .await
        .is_ok_and(|v| v.is_ok());
        if drained {
            self.core.lock().unwrap().authority.detach(id);
        } else {
            let cause = format!(
                "detaching {} could not drain its in-flight work within {ms} ms",
                a.resource
            );
            if tab_only {
                e.fence(&a.resource, cause);
            } else {
                e.mark_offline(cause);
            }
        }
        self.event(&a.session,json!({"type":"attachment_removed","attachment":{"id":a.id,"name":a.name,"resource_id":a.resource,"access":a.access}}));
        Ok(json!({"attachment_id":id,"state":"detached","in_flight_drained":drained}))
    }
    pub async fn close_session(&self, session: &str, ms: u64) -> Result<Value, Error> {
        let deadline = Instant::now() + Duration::from_millis(ms);
        self.sessions.lock().unwrap().insert(session.into(), true);
        let ids: Vec<String> = self
            .core
            .lock()
            .unwrap()
            .authority
            .attachments
            .values()
            .filter(|a| a.session == session)
            .map(|a| a.id.clone())
            .collect();
        let mut drained = true;
        for id in ids {
            drained &= self
                .detach(
                    &id,
                    deadline
                        .saturating_duration_since(Instant::now())
                        .as_millis()
                        .max(1) as u64,
                )
                .await?["in_flight_drained"]
                == true;
        }
        self.completed_sessions
            .lock()
            .unwrap()
            .insert(session.into());
        self.event_changed.notify_waiters();
        let unresolved: Vec<String> = self
            .core
            .lock()
            .unwrap()
            .authority
            .actions
            .iter()
            .filter(|((s, _), (_, r))| {
                s == session
                    && !matches!(
                        r["execution"].as_str(),
                        Some("completed" | "rejected" | "cancelled_before_dispatch")
                    )
            })
            .map(|((_, id), _)| id.clone())
            .collect();
        self.operations
            .lock()
            .unwrap()
            .retain(|(s, _), _| s != session);
        Ok(
            json!({"state":if drained&&unresolved.is_empty(){"stopped"}else{"unknown"},"queue_fenced":true,"in_flight_drained":drained,"unresolved_actions":unresolved}),
        )
    }
    pub async fn remove(&self, id: &str) -> Result<(), Error> {
        let e = self.endpoint(id)?;
        e.mark_offline("the endpoint was removed by the host");
        e.provider.close().await?;
        let mut core = self.core.lock().unwrap();
        let ids: Vec<_> = core
            .authority
            .attachments
            .values()
            .filter(|a| a.conflict_domain == id)
            .map(|a| a.id.clone())
            .collect();
        for id in ids {
            core.observations.forget_attachment(&id);
            core.authority.detach(&id);
        }
        self.endpoints.lock().unwrap().remove(id);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (Arc<Embedded>, Arc<Bridge>) {
        let runtime = Embedded::new();
        let bridge = Bridge::new();
        runtime.session("s").unwrap();
        runtime.add(json!({"id":"web","name":"web","platform":"browser","provider":"fixture"}),json!({"protocol_major":1,"platform":"browser","capabilities":{"browser.evaluate":{"state":"ready"},"browser.cookies_read":{"state":"ready"}}}),bridge.clone()).unwrap();
        runtime
            .attach(Attachment {
                id: "a".into(),
                session: "s".into(),
                resource: "web".into(),
                conflict_domain: "web".into(),
                name: "web".into(),
                access: "control".into(),
            })
            .unwrap();
        (runtime, bridge)
    }
    #[tokio::test]
    async fn independent_tabs_context_conflicts_and_queued_cancellation() {
        let (r, b) = fixture();
        let r1 = r.clone();
        let first = tokio::spawn(async move {
            r1.request("a","web/t1",json!({"op":"browser","operation":"evaluate","tab_id":"t1","source":"()=>1","action_id":"first","timeout_ms":2000})).await
        });
        let first_wire = b.next().await.unwrap();
        let r2 = r.clone();
        let second = tokio::spawn(async move {
            r2.request("a","web/t2",json!({"op":"browser","operation":"evaluate","tab_id":"t2","source":"()=>2","action_id":"second","timeout_ms":2000})).await
        });
        // A second tab reaches the provider while the first tab is still executing.
        let second_wire = tokio::time::timeout(Duration::from_secs(1), b.next())
            .await
            .unwrap()
            .unwrap();
        let context = r
            .request(
                "a",
                "web",
                json!({"op":"browser","operation":"cookies_read","timeout_ms":20}),
            )
            .await
            .unwrap_err();
        assert_eq!(context.code, "DEADLINE_EXCEEDED");
        let r3 = r.clone();
        let queued = tokio::spawn(async move {
            r3.request("a","web/t1",json!({"op":"browser","operation":"evaluate","tab_id":"t1","source":"()=>3","action_id":"queued","timeout_ms":2000})).await
        });
        for _ in 0..100 {
            if r.core
                .lock()
                .unwrap()
                .authority
                .status("s", "queued")
                .is_ok()
            {
                break;
            }
            tokio::task::yield_now().await;
        }
        r.cancel("s", "queued").unwrap();
        assert_eq!(
            queued.await.unwrap().unwrap_err().code,
            "CANCELLED_BEFORE_DISPATCH"
        );
        for wire in [first_wire, second_wire] {
            b.reply(json!({"_request_id":wire["_request_id"],"result":{"execution":"completed","value":1}})).unwrap();
        }
        first.await.unwrap().unwrap();
        second.await.unwrap().unwrap();
        assert!(tokio::time::timeout(Duration::from_millis(20), b.next())
            .await
            .is_err());
    }
    #[tokio::test]
    async fn tab_events_do_not_grant_context_or_sibling_visibility() {
        let (r, _) = fixture();
        r.session("reader").unwrap();
        r.attach(Attachment {
            id: "tab".into(),
            session: "reader".into(),
            resource: "web/t1".into(),
            conflict_domain: "web".into(),
            name: "tab".into(),
            access: "read".into(),
        })
        .unwrap();
        for resource in ["web", "web/t1", "web/t2"] {
            r.provider_event("web", json!({"type":"fixture","resource_id":resource}));
        }
        let events = r.events("reader", 1).unwrap();
        assert_eq!(events["events"].as_array().unwrap().len(), 1);
        assert_eq!(events["events"][0]["resource_id"], "web/t1");
    }
    #[test]
    fn offline_diagnostics_distinguish_endpoint_and_scope_without_changing_state() {
        let (r, _) = fixture();
        let e = r.endpoint("web").unwrap();
        e.fence("web/t1", "previous operation is running");
        let blocked = e.offline_error("web/t1/child");
        assert_eq!(blocked.code, "DEVICE_OFFLINE");
        assert_eq!(blocked.details, Some(json!({"cause":"scope_fenced", "scope":"web/t1",
            "reason":"previous operation is running", "next_step":"wait_for_operation"})));
        assert!(!e.offline.load(Ordering::SeqCst));
        assert!(e.fenced.lock().unwrap().contains("web/t1"));
        e.mark_offline("worker stopped after being temporarily blocked");
        e.mark_offline("later reason must not overwrite the first");
        let offline = e.offline_error("web/t1");
        assert_eq!(offline.details, Some(json!({"cause":"endpoint_offline", "scope":"web",
            "reason":"worker stopped after being temporarily blocked", "next_step":"host_reconnect"})));
        assert!(e.offline.load(Ordering::SeqCst));
        e.unfence("web/t1");
        assert_eq!(e.offline_error("web").details.unwrap()["cause"], "endpoint_offline");
    }
    #[tokio::test]
    async fn fenced_tab_reports_the_blocking_operation() {
        let (r, b) = fixture();
        let r1 = r.clone();
        let slow = tokio::spawn(async move {
            r1.request("a","web/t1",json!({"op":"browser","operation":"evaluate","tab_id":"t1","source":"()=>1","action_id":"slow","timeout_ms":50})).await
        });
        let wire = tokio::time::timeout(Duration::from_secs(1), b.next())
            .await.expect("fixture request must reach the provider").unwrap();
        assert_eq!(slow.await.unwrap().unwrap_err().code, "ACTION_OUTCOME_UNKNOWN");
        let blocked = r
            .request("a","web/t1",json!({"op":"browser","operation":"evaluate","tab_id":"t1","source":"()=>1","action_id":"next","timeout_ms":50}))
            .await
            .unwrap_err();
        assert_eq!(blocked.code, "DEVICE_OFFLINE");
        assert!(blocked.message.contains("temporarily blocked"), "{}", blocked.message);
        assert!(blocked.message.contains("browser.evaluate (id slow)"), "{}", blocked.message);
        assert!(blocked.message.contains("50 ms budget"), "{}", blocked.message);
        let details = blocked.details.unwrap();
        assert_eq!(details["cause"], "scope_fenced");
        assert_eq!(details["scope"], "web/t1");
        assert_eq!(details["next_step"], "wait_for_operation");
        assert!(details["reason"].as_str().unwrap().contains("browser.evaluate (id slow)"));
        b.reply(json!({"_request_id":wire["_request_id"],"result":{"execution":"completed"}})).unwrap();
        for _ in 0..100 {
            if r.endpoint("web").unwrap().fenced.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let endpoint = r.endpoint("web").unwrap();
        assert!(endpoint.fenced.lock().unwrap().is_empty());
        assert!(endpoint.reasons.lock().unwrap().is_empty());
    }
}
