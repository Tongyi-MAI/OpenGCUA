//! Multi-step mutations have one public ledger entry and one end-to-end budget.
use super::*;
use std::sync::atomic::AtomicBool;
use std::time::Instant;
use tokio::sync::{watch, Notify};
pub(super) struct Composition {
    result: watch::Sender<Option<Result<Value, Error>>>,
    cancelled: AtomicBool,
    pub wake: Notify,
}
impl Composition {
    pub fn check(&self) -> Result<(), Error> {
        if self.cancelled.load(Ordering::SeqCst) {
            Err(err("CANCELLED_BEFORE_DISPATCH"))
        } else {
            Ok(())
        }
    }
}
impl Host {
    pub(super) fn cancel_composed(&self, session: &str, id: &str) -> Result<Value, Error> {
        if let Some(op) = self
            .composed
            .lock()
            .unwrap()
            .get(&(session.into(), id.into()))
        {
            op.cancelled.store(true, Ordering::SeqCst);
            op.wake.notify_one();
            for step in ["write", "upload", "launch"] {
                let _ = self.runtime.cancel(session, &format!("{id}:{step}"));
            }
            let status = self
                .runtime
                .core
                .lock()
                .unwrap()
                .authority
                .status(session, id)
                .map_err(err)?;
            return Ok(
                json!({"action_id":id,"execution":if status["execution"]=="queued"{"cancelled_before_dispatch"}else{status["execution"].as_str().unwrap_or("unknown")}}),
            );
        }
        self.runtime.cancel(session, id)
    }
    pub(super) async fn compose(self: &Arc<Self>, mut q: Value) -> Result<Value, Error> {
        let session = s(&q, "session")?.to_string();
        self.runtime.check_session(&session)?;
        let limit = budget(
            &q,
            if q["op"] == "browser_create" {
                30000
            } else {
                10000
            },
        )?;
        let id = q["action_id"]
            .as_str()
            .map(str::to_string)
            .unwrap_or_else(uid);
        q["action_id"] = json!(id);
        self.runtime
            .core
            .lock()
            .unwrap()
            .authority
            .begin(&session, &id, q.clone())
            .map_err(err)?;
        let control = {
            let mut map = self.composed.lock().unwrap();
            map.entry((session.clone(),id.clone())).or_insert_with(|| {
                let (result,_) = watch::channel(None);
                let op=Arc::new(Composition{result,cancelled:AtomicBool::new(false),wake:Notify::new()});
                let c=op.clone(); let host=self.clone();
                tokio::spawn(async move {
                    let started=Instant::now();
                    let mut record=host.runtime.core.lock().unwrap().authority.status(&session,&id).unwrap();
                    let attachment=q["attachment"].as_str().unwrap_or("");
                    let provider=host.runtime.check(attachment,false).ok().and_then(|a|host.runtime.endpoint(&a.conflict_domain).ok()).map(|e|e.info["provider"].clone()).unwrap_or(json!("runtime"));
                    for (k,v) in json!({"action_id":id,"session_id":session,"attachment_id":attachment,"resource_id":q["resource"].as_str().unwrap_or(""),"provider":provider,"execution":"running","effect":"unknown"}).as_object().unwrap(){record[k]=v.clone();}
                    let _=host.runtime.core.lock().unwrap().authority.finish(&session,&id,record.clone());
                    let mut result=if let Err(e)=c.check(){Err(e)}else{match q["op"].as_str().unwrap(){
                        "browser_create"=>host.create_browser(q.clone(),c.clone()).await,
                        "copy_files"=>host.copy_files(q.clone(),c.clone()).await,
                        _=>host.prepare_mutation(&q,&c,started,limit).await,
                    }};
                    if q["op"]=="browser_create" {
                        if let Ok(value)=&result {
                            record["attachment_id"]=value["attachment"]["id"].clone();
                            record["resource_id"]=value["resource"]["id"].clone();
                            record["provider"]=json!("browser.playwright");
                        }
                    }
                    if !q["wire"]["_based_on"].is_null(){record["based_on"]=q["wire"]["_based_on"].clone();}
                    record["duration_ms"]=json!(started.elapsed().as_millis() as u64);
                    let unknown=result.as_ref().err().is_some_and(|e|e.code=="ACTION_OUTCOME_UNKNOWN");
                    record["execution"]=json!(if result.is_ok(){"completed"}else if unknown{"unknown"}else if result.as_ref().err().unwrap().code=="CANCELLED_BEFORE_DISPATCH"{"cancelled_before_dispatch"}else{"rejected"});
                    if result.is_err() {record["effect"]=json!(if unknown{"unknown"}else{"not_applied"});}
                    if let Err(e)=&mut result {record["error"]=serde_json::to_value(&e).unwrap();e.details=Some(json!({"receipt":record}));}
                    let _=host.runtime.core.lock().unwrap().authority.finish(&session,&id,record);
                    c.result.send_replace(Some(result));
                });
                op
            }).clone()
        };
        let mut rx = control.result.subscribe();
        loop {
            if let Some(result) = rx.borrow().clone() {
                return result;
            }
            rx.changed().await.map_err(|_| err("DEVICE_OFFLINE"))?;
        }
    }
    async fn prepare_mutation(
        self: &Arc<Self>,
        q: &Value,
        c: &Composition,
        started: Instant,
        limit: std::time::Duration,
    ) -> Result<Value, Error> {
        let remaining = || {
            c.check()?;
            limit
                .checked_sub(started.elapsed())
                .map(|d| d.as_millis().max(1) as u64)
                .ok_or_else(|| err("DEADLINE_EXCEEDED"))
        };
        let attachment = s(q, "attachment")?;
        let resource = s(q, "resource")?;
        let a = self.runtime.check(attachment, true)?;
        if a.session != s(q, "session")? {
            return Err(err("PERMISSION_DENIED"));
        }
        if q["op"] == "upload_files" {
            let mut files = Vec::new();
            for file in q["files"]
                .as_array()
                .ok_or_else(|| err("INVALID_ARGUMENT"))?
            {
                if let Some(src) = file.get("source") {
                    let source = self.runtime.check(s(src, "attachment")?, false)?;
                    if source.session != a.session {
                        return Err(err("PERMISSION_DENIED"));
                    }
                    let data=self.runtime.request(s(src,"attachment")?,s(src,"resource")?,json!({"op":"file","operation":"read","path":src["path"],"max_bytes":1048576,"timeout_ms":remaining()?})).await?;
                    if data["truncated"] == true || data["eof"] == false {
                        return Err(err("FILE_TOO_LARGE"));
                    }
                    files.push(json!({"name":s(src,"path")?.rsplit('/').next().unwrap(),"mime_type":"application/octet-stream","data":data["data"]}));
                } else {
                    files.push(file.clone());
                }
            }
            let mut wire = q["wire"].clone();
            wire["op"] = json!("browser");
            wire["operation"] = json!("upload");
            wire["files"] = json!(files);
            wire["timeout_ms"] = json!(remaining()?);
            wire["action_id"] = json!(format!("{}:upload", s(q, "action_id")?));
            self.runtime.request(attachment, resource, wire).await?;
            return Ok(Value::Null);
        }
        let e = self.runtime.endpoint(&a.conflict_domain)?;
        let name = s(q, "name")?;
        if mu_core::apps::normalize(name).is_empty() {
            return Err(err("INVALID_ARGUMENT"));
        }
        let found = if e.info["platform"] == "macos" {
            let data=self.runtime.request(attachment,resource,json!({"op":"action","action":"launch","app":name,"action_id":format!("{}:launch",s(q,"action_id")?),"timeout_ms":remaining()?})).await?;
            json!({"id":data["app_id"]})
        } else {
            let inventory = self
                .runtime
                .request(
                    attachment,
                    resource,
                    json!({"op":"list_apps","launchable_only":true,"timeout_ms":remaining()?}),
                )
                .await?;
            let app = mu_core::apps::find(name, &inventory)
                .map_err(|(code, msg)| Error::new(code, msg))?;
            self.runtime.request(attachment,resource,json!({"op":"action","action":"launch","app_id":app["id"],"action_id":format!("{}:launch",s(q,"action_id")?),"timeout_ms":remaining()?})).await?;
            app
        };
        Ok(found)
    }
}
