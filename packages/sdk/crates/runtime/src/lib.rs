//! Language-independent runtime policy, scheduling and observation authority.
pub mod embedded;
pub mod observations;
pub mod policy;
pub mod scheduler;

#[derive(Default)]
pub struct Runtime {
    pub authority: Authority,
    pub scheduler: scheduler::Scheduler,
    pub observations: observations::ObservationStore,
}
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Attachment {
    pub id: String,
    pub session: String,
    pub resource: String,
    pub conflict_domain: String,
    pub name: String,
    pub access: String,
}

#[derive(Default)]
pub struct Authority {
    attachments: BTreeMap<String, Attachment>,
    actions: BTreeMap<(String, String), (Value, Value)>,
    sequence: u64,
}

impl Authority {
    pub fn attach(&mut self, a: Attachment) -> Result<(), &'static str> {
        if a.name.is_empty() || !["control", "read"].contains(&a.access.as_str()) {
            return Err("INVALID_ARGUMENT");
        }
        if self.attachments.contains_key(&a.id)
            || self
                .attachments
                .values()
                .any(|b| b.session == a.session && b.name == a.name)
        {
            return Err("ALREADY_REGISTERED");
        }
        if a.access == "control"
            && self.attachments.values().any(|b| {
                b.conflict_domain == a.conflict_domain
                    && (a.resource == b.resource
                        || a.resource.starts_with(&(b.resource.clone() + "/"))
                        || b.resource.starts_with(&(a.resource.clone() + "/")))
                    && b.access == "control"
                    && b.session != a.session
            })
        {
            return Err("RESOURCE_BUSY");
        }
        self.attachments.insert(a.id.clone(), a);
        Ok(())
    }

    pub fn attachments(&self) -> impl Iterator<Item = &Attachment> {
        self.attachments.values()
    }

    pub fn check(&self, id: &str, write: bool) -> Result<&Attachment, &'static str> {
        let a = self.attachments.get(id).ok_or("STALE_ATTACHMENT")?;
        if write && a.access != "control" {
            return Err("PERMISSION_DENIED");
        }
        Ok(a)
    }

    pub fn detach(&mut self, id: &str) {
        self.attachments.remove(id);
    }

    /// Compare the complete immutable request, including routing, before any dispatch.
    pub fn begin(
        &mut self,
        session: &str,
        id: &str,
        request: Value,
    ) -> Result<Value, &'static str> {
        let key = (session.to_owned(), id.to_owned());
        if let Some((old, result)) = self.actions.get(&key) {
            return if old == &request {
                Ok(result.clone())
            } else {
                Err("INVALID_ARGUMENT")
            };
        }
        self.sequence += 1;
        let result =
            serde_json::json!({"action_id":id,"sequence":self.sequence,"execution":"queued"});
        self.actions.insert(key, (request, result.clone()));
        Ok(result)
    }

    pub fn finish(&mut self, session: &str, id: &str, result: Value) -> Result<(), &'static str> {
        self.actions
            .get_mut(&(session.to_owned(), id.to_owned()))
            .ok_or("TARGET_NOT_FOUND")?
            .1 = result;
        Ok(())
    }

    pub fn status(&self, session: &str, id: &str) -> Result<Value, &'static str> {
        Ok(self
            .actions
            .get(&(session.to_owned(), id.to_owned()))
            .ok_or("TARGET_NOT_FOUND")?
            .1
            .clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn attachment(id: &str, session: &str, access: &str) -> Attachment {
        Attachment {
            id: id.into(),
            session: session.into(),
            resource: "phone/app".into(),
            conflict_domain: "phone".into(),
            name: id.into(),
            access: access.into(),
        }
    }
    #[test]
    fn overlap_and_detachment() {
        let mut a = Authority::default();
        a.attach(attachment("host", "one", "control")).unwrap();
        a.attach(attachment("app", "one", "control")).unwrap();
        assert_eq!(
            a.attach(attachment("other", "two", "control")),
            Err("RESOURCE_BUSY")
        );
        a.attach(attachment("viewer", "two", "read")).unwrap();
        assert_eq!(a.check("viewer", true).unwrap_err(), "PERMISSION_DENIED");
        a.detach("host");
        assert_eq!(a.check("host", false).unwrap_err(), "STALE_ATTACHMENT");
        assert!(a.check("app", true).is_ok());
    }
    #[test]
    fn identical_ids_deduplicate_but_routing_changes_do_not() {
        let mut a = Authority::default();
        let request = serde_json::json!({"resource":"one","action":"click"});
        a.begin("s", "x", request.clone()).unwrap();
        a.finish("s", "x", serde_json::json!({"execution":"unknown"}))
            .unwrap();
        assert_eq!(a.begin("s", "x", request).unwrap()["execution"], "unknown");
        assert_eq!(
            a.begin("s", "x", serde_json::json!({"resource":"two"})),
            Err("INVALID_ARGUMENT")
        );
    }
}

pub mod capabilities;

mod browser_policy;
