//! Runtime-owned capture identities, indices, artifacts and acknowledged output history.
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::collections::HashMap;

#[derive(Default)]
struct Scope {
    next_index: u64,
    latest: Option<String>,
    baseline: Option<String>,
    ax: Option<String>,
    image: Option<String>,
    images: Vec<String>,
    turn_ax: Option<String>,
    turn_image: Option<String>,
    turn_images: Vec<String>,
}
struct Capture {
    scope: String,
    data: Value,
    indices: Vec<(u64, String)>,
    rows: Vec<Value>,
    tree: bool,
    sequence: u64,
}
impl Capture {
    fn stable_with(&self, other: &Self) -> bool {
        self.data["identity_mode"] == "native_lifetime"
            && !self.data["identity_epoch"].is_null()
            && self.data["identity_epoch"] == other.data["identity_epoch"]
            && self.data["identity_mode"] == other.data["identity_mode"]
    }
    fn same(&self, other: &Self) -> bool {
        self.indices == other.indices && self.data["nodes"] == other.data["nodes"]
    }
    fn complete(&self) -> bool {
        self.tree && self.data["truncated"] != true
    }
}
struct Point {
    capture: String,
    x: f64,
    y: f64,
}
#[derive(Default)]
pub struct ObservationStore {
    scopes: HashMap<String, Scope>,
    captures: HashMap<String, Capture>,
    points: HashMap<String, Point>,
    sequence: u64,
}
impl ObservationStore {
    pub fn forget_attachment(&mut self, attachment: &str) {
        let prefix = format!("{attachment}/");
        self.scopes.retain(|scope, _| !scope.starts_with(&prefix));
        self.captures
            .retain(|_, capture| !capture.scope.starts_with(&prefix));
        self.points
            .retain(|_, point| self.captures.contains_key(&point.capture));
    }

    pub fn capture(
        &mut self,
        scope: &str,
        id: &str,
        mut data: Value,
        tree: bool,
        app: Option<&str>,
    ) -> Result<Value, &'static str> {
        if self.captures.contains_key(id) {
            return Err("INVALID_ARGUMENT");
        }
        if !["width", "height"].iter().all(|key| {
            data[*key]
                .as_u64()
                .is_some_and(|v| v > 0 && v <= u32::MAX as u64)
        }) {
            return Err("PROTOCOL_MISMATCH");
        }
        // Each component image is an independently targetable capture, grouped
        // only for acknowledged presentation. AX capture history stays separate.
        let mut images = Vec::new();
        if let Some(parts) = data["images"].as_array() {
            for (i, part) in parts.iter().enumerate() {
                let image_id = format!("{id}:image:{i}");
                let mut image = part.clone();
                image["image_group"] = json!(id);
                image["nodes"] = json!([]);
                image["revision"] = data["revision"].clone();
                self.capture(scope, &image_id, image.clone(), false, None)?;
                image["id"] = json!(image_id);
                images.push(image);
            }
        }
        let raw = if tree {
            data["nodes"]
                .as_array()
                .ok_or("PROTOCOL_MISMATCH")?
                .iter()
                .filter(|n| app.is_none_or(|a| n["app_id"] == a))
                .cloned()
                .collect()
        } else {
            Vec::new()
        };
        let ids: Vec<_> = raw
            .iter()
            .map(|n| n["id"].as_str().ok_or("PROTOCOL_MISMATCH"))
            .collect::<Result<_, _>>()?;
        if ids.iter().collect::<std::collections::HashSet<_>>().len() != ids.len() {
            return Err("PROTOCOL_MISMATCH");
        }
        data["nodes"] = json!(raw);
        let state = self.scopes.entry(scope.into()).or_default();
        let previous = state.latest.as_ref().and_then(|id| self.captures.get(id));
        let compatible = previous.is_some_and(|p| {
            (data["identity_mode"] == "native_lifetime"
                && !data["identity_epoch"].is_null()
                && data["identity_epoch"] == p.data["identity_epoch"]
                && data["identity_mode"] == p.data["identity_mode"])
                || (data["revision"] == p.data["revision"] && data["nodes"] == p.data["nodes"])
        });
        let old: HashMap<_, _> = previous
            .filter(|_| compatible)
            .map(|p| p.indices.iter().map(|(i, id)| (id.clone(), *i)).collect())
            .unwrap_or_default();
        let indices: Vec<_> = raw
            .iter()
            .map(|n| {
                let id = n["id"].as_str().unwrap().to_owned();
                let index = *old.get(&id).unwrap_or(&0);
                let index = if index > 0 {
                    index
                } else {
                    state.next_index += 1;
                    state.next_index
                };
                (index, id)
            })
            .collect();
        let normalized: Vec<_> = raw
            .iter()
            .map(|n| {
                mu_core::semantic::normalize(
                    n,
                    data["captured_at"].as_f64().unwrap_or(0.0) / 1000.0,
                    &data["revision"],
                    data["pending_invalidations"].as_u64().unwrap_or(0) > 0,
                )
            })
            .collect();
        let rows = mu_core::semantic::project(&normalized, &indices);
        self.sequence += 1;
        let result = json!({"indices":indices,"nodes":normalized,"rows":rows,"sequence":self.sequence,"data":data,"images":images});
        self.captures.insert(
            id.into(),
            Capture {
                scope: scope.into(),
                data,
                indices,
                rows,
                tree,
                sequence: self.sequence,
            },
        );
        if tree {
            state.latest = Some(id.into());
        }
        Ok(result)
    }
    fn get(&self, scope: &str, id: &str) -> Result<&Capture, &'static str> {
        let c = self.captures.get(id).ok_or("STALE_TARGET")?;
        if c.scope != scope {
            return Err("INVALID_ARGUMENT");
        }
        Ok(c)
    }
    pub fn node_wire(&self, scope: &str, id: &str, node: &str) -> Result<Value, &'static str> {
        let c = self.get(scope, id)?;
        let latest = self
            .scopes
            .get(scope)
            .and_then(|s| s.latest.as_ref())
            .and_then(|id| self.captures.get(id))
            .ok_or("STALE_TARGET")?;
        if !(c.stable_with(latest) || c.same(latest))
            || !latest.indices.iter().any(|(_, n)| n == node)
        {
            return Err("STALE_TARGET");
        }
        let raw = c.data["nodes"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["id"] == node)
            .ok_or("STALE_TARGET")?;
        if raw["targetable"] == false {
            return Err("UNSUPPORTED_CAPABILITY");
        }
        let mut wire = json!({"node_id":node,"revision":c.data["revision"],"_based_on":id,"_node_actions":raw["actions"]});
        if !raw["target_token"].is_null() {
            wire["target_token"] = raw["target_token"].clone();
        }
        if let Some(target) = raw["target"].as_object() {
            for (key, value) in target {
                wire[key] = value.clone();
            }
        }
        Ok(wire)
    }
    pub fn resolve_index(
        &self,
        scope: &str,
        index: u64,
        in_turn: bool,
    ) -> Result<Value, &'static str> {
        let s = self.scopes.get(scope).ok_or("STALE_TARGET")?;
        let id = if in_turn { &s.turn_ax } else { &s.ax };
        let id = id.as_ref().ok_or("STALE_TARGET")?;
        let c = self.get(scope, id)?;
        let node = c
            .indices
            .iter()
            .find(|(i, _)| *i == index)
            .ok_or("STALE_TARGET")?;
        Ok(json!({"observation_id":id,"node_id":node.1}))
    }
    pub fn point(
        &mut self,
        scope: &str,
        id: &str,
        token: &str,
        point: (f64, f64),
        space: &str,
    ) -> Result<Value, &'static str> {
        if self.points.contains_key(token) {
            return Err("INVALID_ARGUMENT");
        }
        let c = self.get(scope, id)?;
        if c.data["png"].is_null() {
            return Err("INVALID_ARGUMENT");
        }
        let frame = (
            c.data["width"].as_u64().unwrap() as u32,
            c.data["height"].as_u64().unwrap() as u32,
        );
        let (x, y) = mu_core::image_point(point.0, point.1, space, frame, frame)
            .map_err(|_| "INVALID_ARGUMENT")?;
        self.points.insert(
            token.into(),
            Point {
                capture: id.into(),
                x,
                y,
            },
        );
        self.point_wire(scope, token)
    }
    pub fn point_wire(&self, scope: &str, token: &str) -> Result<Value, &'static str> {
        let p = self.points.get(token).ok_or("INVALID_ARGUMENT")?;
        let c = self.get(scope, &p.capture)?;
        let mut wire = json!({"_based_on":p.capture,"x":p.x,"y":p.y,"frame_width":c.data["width"],"frame_height":c.data["height"],"frame_rotation":c.data["rotation"].as_u64().unwrap_or(0)});
        if let Some(target) = c.data["target"].as_object() {
            for (key, value) in target {
                wire[key] = value.clone();
            }
        }
        Ok(wire)
    }
    pub fn image_bytes(&self, scope: &str, id: &str) -> Result<Vec<u8>, &'static str> {
        STANDARD
            .decode(
                self.get(scope, id)?.data["png"]
                    .as_str()
                    .ok_or("STALE_TARGET")?,
            )
            .map_err(|_| "PROTOCOL_MISMATCH")
    }
    pub fn historical(&self, scope: &str, id: &str, image: bool) -> Result<bool, &'static str> {
        let c = self.get(scope, id)?;
        let s = self.scopes.get(scope).ok_or("STALE_TARGET")?;
        let active = if image { &s.image } else { &s.ax };
        Ok(active
            .as_ref()
            .and_then(|id| self.captures.get(id))
            .is_some_and(|a| {
                a.sequence > c.sequence
                    && (!image
                        || a.data["image_group"].is_null()
                        || a.data["image_group"] != c.data["image_group"])
            }))
    }
    pub fn render(
        &self,
        scope: &str,
        id: &str,
        header: &str,
        full: bool,
    ) -> Result<Value, &'static str> {
        let c = self.get(scope, id)?;
        let s = self.scopes.get(scope).unwrap();
        let base = s.baseline.as_ref().and_then(|id| self.captures.get(id));
        let historical = self.historical(scope, id, false)?;
        let compatible = base
            .is_some_and(|b| b.complete() && c.complete() && (c.stable_with(b) || c.same(b)))
            && !historical;
        let reason = if historical {
            Some("Historical observation; does not update live targets.")
        } else if !c.complete() {
            Some("Partial view: missing nodes do not imply removal.")
        } else if base.is_some_and(|b| !c.stable_with(b) && !c.same(b)) {
            Some("Identity reset: previous indices retired.")
        } else {
            None
        };
        let mut result = mu_core::ax::render(
            &json!({"header":header,"rows":c.rows,"before":if compatible{base.map(|b|&b.rows)}else{None},"full":full||historical,"reason":reason}),
        )?;
        result["reset_reason"] = json!(reason);
        Ok(result)
    }
    pub fn acknowledge(&mut self, scope: &str, id: &str, image: bool) -> Result<(), &'static str> {
        if self.historical(scope, id, image)? {
            return Ok(());
        }
        let group = self.get(scope, id)?.data["image_group"].clone();
        let same_group = self
            .scopes
            .get(scope)
            .and_then(|s| s.image.as_ref())
            .and_then(|id| self.captures.get(id))
            .is_some_and(|c| !group.is_null() && c.data["image_group"] == group);
        let s = self.scopes.get_mut(scope).unwrap();
        if image {
            if !same_group {
                s.images.clear();
            }
            if !s.images.iter().any(|i| i == id) {
                s.images.push(id.into());
            }
            s.image = Some(id.into());
        } else {
            s.ax = Some(id.into());
            s.baseline = Some(id.into());
        }
        Ok(())
    }
    pub fn failed_delivery(&mut self, scope: &str) {
        self.scopes.entry(scope.into()).or_default().baseline = None;
    }
    pub fn freeze(&mut self, scope: &str) {
        let s = self.scopes.entry(scope.into()).or_default();
        s.turn_ax = s.ax.clone();
        s.turn_image = s.image.clone();
        s.turn_images = s.images.clone();
    }
    pub fn reset(&mut self, scope: &str) {
        let s = self.scopes.entry(scope.into()).or_default();
        s.ax = None;
        s.baseline = None;
        s.image = None;
        s.images.clear();
        s.turn_ax = None;
        s.turn_image = None;
        s.turn_images.clear();
    }
    pub fn views(&self, scope: &str) -> Value {
        self.scopes.get(scope).map(|s|json!({"baseline":s.baseline,"ax":s.ax,"image":s.image,"turn_ax":s.turn_ax,"turn_image":s.turn_image,"images":s.images,"turn_images":s.turn_images})).unwrap_or(json!({}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn data(revision: &str) -> Value {
        json!({"width":100,"height":200,"png":"aW1hZ2U=","rotation":0,"captured_at":0,
            "revision":revision,"identity_mode":"native_lifetime","identity_epoch":"e1","truncated":false,
            "nodes":(0..30).map(|i|json!({"id":i.to_string(),"role":"button","name":format!("Button {i}"),"actions":[]})).collect::<Vec<_>>()})
    }
    #[test]
    fn silent_captures_coalesce_against_acknowledged_baseline() {
        let mut s = ObservationStore::default();
        s.capture("a", "old", data("1"), true, None).unwrap();
        assert_eq!(s.resolve_index("a", 1, false), Err("STALE_TARGET"));
        s.acknowledge("a", "old", false).unwrap();
        let mut next = data("2");
        next["nodes"][0]["name"] = json!("intermediate");
        s.capture("a", "silent", next.clone(), true, None).unwrap();
        next["nodes"][0]["name"] = json!("final");
        s.capture("a", "new", next, true, None).unwrap();
        let rendered = s.render("a", "new", "header", false).unwrap();
        assert_eq!(rendered["representation"], "diff");
        assert!(rendered["text"].as_str().unwrap().contains("final"));
        assert!(!rendered["text"].as_str().unwrap().contains("intermediate"));
        assert_eq!(s.views("a")["ax"], "old");
        s.failed_delivery("a");
        assert_eq!(
            s.render("a", "new", "header", false).unwrap()["representation"],
            "full"
        );
    }
    #[test]
    fn modality_history_and_frozen_turns_are_scope_local() {
        let mut s = ObservationStore::default();
        s.freeze("a");
        s.capture("a", "ax", data("1"), true, None).unwrap();
        s.acknowledge("a", "ax", false).unwrap();
        assert_eq!(s.resolve_index("a", 1, true), Err("STALE_TARGET"));
        s.freeze("a");
        assert!(s.resolve_index("a", 1, true).is_ok());
        s.capture("a", "image", data("2"), false, None).unwrap();
        s.acknowledge("a", "image", true).unwrap();
        s.point("a", "image", "point", (10.0, 20.0), "pixels")
            .unwrap();
        let mut next = data("3");
        next["identity_epoch"] = json!("e2");
        s.capture("a", "reset", next, true, None).unwrap();
        assert_eq!(s.node_wire("a", "ax", "0"), Err("STALE_TARGET"));
        assert_eq!(s.point_wire("a", "point").unwrap()["x"], 10.0);
        assert!(s
            .point_wire("a", "point")
            .unwrap()
            .get("revision")
            .is_none());
        assert_eq!(s.point_wire("b", "point"), Err("INVALID_ARGUMENT"));
        assert_eq!(s.views("a")["image"], "image");
    }
    #[test]
    fn image_point_has_no_time_or_user_boundary_expiry() {
        let mut s = ObservationStore::default();
        s.capture("a", "before", data("1"), false, None).unwrap();
        s.point("a", "before", "old", (10.0, 20.0), "pixels").unwrap();
        assert!(s.point_wire("a", "old").is_ok());
        s.capture("a", "after", data("2"), false, None).unwrap();
        assert!(s.point_wire("a", "old").is_ok());
        s.point("a", "before", "reused", (10.0, 20.0), "pixels").unwrap();
        s.point("a", "after", "fresh", (10.0, 20.0), "pixels").unwrap();
        assert!(s.point_wire("a", "fresh").is_ok());
    }
    #[test]
    fn partial_and_historical_views_cannot_rewind_live_indices() {
        let mut s = ObservationStore::default();
        s.capture("a", "old", data("1"), true, None).unwrap();
        s.acknowledge("a", "old", false).unwrap();
        let mut next = data("2");
        next["nodes"].as_array_mut().unwrap().remove(0);
        s.capture("a", "new", next.clone(), true, None).unwrap();
        s.acknowledge("a", "new", false).unwrap();
        s.acknowledge("a", "old", false).unwrap();
        assert_eq!(s.views("a")["ax"], "new");
        assert_eq!(s.resolve_index("a", 1, false), Err("STALE_TARGET"));
        assert!(s.render("a", "old", "header", false).unwrap()["text"]
            .as_str()
            .unwrap()
            .contains("Historical"));
        next["truncated"] = json!(true);
        s.capture("a", "partial", next, true, None).unwrap();
        assert_eq!(
            s.render("a", "partial", "header", false).unwrap()["representation"],
            "full"
        );
    }
}
