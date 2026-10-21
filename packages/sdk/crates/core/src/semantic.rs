//! Common accessibility normalization and model-facing semantic projection.
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

pub fn normalize(row: &Value, captured_at: f64, revision: &Value, dirty: bool) -> Value {
    let mut node = row.clone();
    let object = node.as_object_mut().unwrap();
    object.remove("target_token");
    object.remove("target");
    object.remove("targetable");
    let start = object.remove("selection_start").and_then(|v| v.as_u64());
    let end = object.remove("selection_end").and_then(|v| v.as_u64());
    object.insert("last_verified_at".into(), json!(captured_at));
    object.insert("dirty".into(), json!(dirty));
    let offset = |wanted: u64| -> Option<usize> {
        let mut units = 0;
        let mut points = 0;
        for c in row["text"].as_str().unwrap_or("").chars() {
            if units == wanted {
                return Some(points);
            }
            units += c.len_utf16() as u64;
            points += 1;
            if units > wanted {
                return None;
            }
        }
        (units == wanted).then_some(points)
    };
    let selection = match (start, end) {
        (Some(a), Some(b)) if a <= b && row["password"] != true => offset(a)
            .zip(offset(b))
            .map(|(a, b)| json!({"start":a,"end":b,"revision":revision}))
            .unwrap_or(Value::Null),
        _ => Value::Null,
    };
    object.insert("selection".into(), selection);
    capability_provenance(object);
    node
}

/// AX capability metadata is conservative: missing or unrecognized provenance
/// is unknown, and only an explicit native report may carry a settable verdict.
fn capability_provenance(object: &mut serde_json::Map<String, Value>) {
    let source = |object: &serde_json::Map<String, Value>, key: &str, allowed: &[&str]| {
        match object.get(key).and_then(Value::as_str) {
            Some(value) if allowed.contains(&value) => value.to_owned(),
            _ => "unknown".to_owned(),
        }
    };
    let editable = if object.get("editable").is_some_and(Value::is_boolean) {
        source(object, "editable_source", &["native", "role_hint"])
    } else {
        "unknown".into()
    };
    let actions = source(object, "actions_source", &["native"]);
    let (verdict, settable) = match object.get("value_settable").filter(|v| v.is_boolean()) {
        Some(v) if source(object, "value_settable_source", &["native"]) == "native" => (v.clone(), "native"),
        _ => (Value::Null, "unknown"),
    };
    object.insert("editable_source".into(), json!(editable));
    object.insert("actions_source".into(), json!(actions));
    object.insert("value_settable".into(), verdict);
    object.insert("value_settable_source".into(), json!(settable));
}

pub fn project(nodes: &[Value], indices: &[(u64, String)]) -> Vec<Value> {
    let by_id: HashMap<_, _> = nodes
        .iter()
        .filter_map(|n| Some((n["id"].as_str()?, n)))
        .collect();
    let index_of: HashMap<_, _> = indices.iter().map(|(i, id)| (id.as_str(), *i)).collect();
    let mut siblings = HashMap::new();
    indices.iter().map(|(index,id)| {
        let node = by_id[id.as_str()];
        let parent = node["parent_id"].as_str().and_then(|p| index_of.get(p)).copied();
        let after = siblings.insert(parent,*index);
        let mut seen = HashSet::from([id.as_str()]);
        let mut ancestor = node["parent_id"].as_str();
        while let Some(next) = ancestor.and_then(|p| by_id.get(p)) {
            let id = next["id"].as_str().unwrap();
            if !seen.insert(id) || seen.len() >= 32 { break; }
            ancestor = next["parent_id"].as_str();
        }
        let mut fields = vec![node["role"].as_str().unwrap_or("unknown").to_owned()];
        for key in ["name","text","hint"] {
            let value = node[key].as_str().unwrap_or("");
            if key == "text" && node["editable"] != true && node[key] == node["name"] { continue; }
            if !value.is_empty() || (key == "text" && node["editable"] == true && node["password"] != true) {
                fields.push(format!("{key}={}", json!(value)));
            }
        }
        for key in ["editable","focused","password"] {
            if node[key] != true { continue; }
            // A role-derived editable flag is a hint, never proof of a writable AXValue.
            fields.push(if key == "editable" && node["editable_source"] == "role_hint" { "editable=role_hint".into() } else { key.into() });
        }
        if let Some(settable) = node["value_settable"].as_bool().filter(|_| node["value_settable_source"] == "native") {
            fields.push(format!("value_settable={settable}"));
        }
        for key in ["enabled","visible"] { if node[key] == false { fields.push(format!("{key}=false")); } }
        if !node["checked"].is_null() { fields.push(format!("checked={}", node["checked"])); }
        if !node["selection"].is_null() { fields.push(format!("selection=[{}:{}]",node["selection"]["start"],node["selection"]["end"])); }
        if let Some(actions) = node["actions"].as_array().filter(|a| !a.is_empty()) {
            fields.push(format!("actions={}", json!(actions.iter().map(|a| &a["id"]).collect::<Vec<_>>())));
        }
        json!({"index":index,"parent":parent,"after":after,"depth":seen.len()-1,"text":fields.join(" ")})
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn utf16_selection_rejects_split_surrogate_and_hides_password_offsets() {
        let mut row = json!({"text":"a😀b","selection_start":1,"selection_end":3});
        assert_eq!(
            normalize(&row, 0.0, &json!("r"), false)["selection"]["end"],
            2
        );
        row["selection_end"] = json!(2);
        assert!(normalize(&row, 0.0, &json!("r"), false)["selection"].is_null());
        row["selection_end"] = json!(3);
        row["password"] = json!(true);
        assert!(normalize(&row, 0.0, &json!("r"), false)["selection"].is_null());
    }

    #[test]
    fn legacy_rows_default_to_unknown_capability_provenance() {
        let node = normalize(&json!({"id":"a","role":"textbox","editable":true,"actions":[]}), 0.0, &json!("r"), false);
        assert_eq!(node["editable_source"], "unknown");
        assert_eq!(node["actions_source"], "unknown");
        assert!(node["value_settable"].is_null());
        assert_eq!(node["value_settable_source"], "unknown");
        let rows = project(&[node], &[(1, "a".into())]);
        assert_eq!(rows[0]["text"], "textbox text=\"\" editable");
    }

    #[test]
    fn settable_verdicts_require_an_explicit_native_report() {
        let at = |row: Value| normalize(&row, 0.0, &json!("r"), false);
        for row in [
            json!({"id":"a","editable":true,"editable_source":"role_hint","value_settable":true}),
            json!({"id":"a","editable":true,"value_settable":true,"value_settable_source":"role_hint"}),
            json!({"id":"a","value_settable":"yes","value_settable_source":"native"}),
            json!({"id":"a","value_settable_source":"native"}),
        ] {
            let node = at(row);
            assert!(node["value_settable"].is_null());
            assert_eq!(node["value_settable_source"], "unknown");
        }
        let hinted = at(json!({"id":"a","role":"AXTextField","name":"Name","editable":true,"editable_source":"role_hint","actions_source":"guess","actions":[]}));
        assert_eq!(hinted["actions_source"], "unknown");
        let text = project(&[hinted], &[(1, "a".into())])[0]["text"].as_str().unwrap().to_owned();
        assert_eq!(text, "AXTextField name=\"Name\" text=\"\" editable=role_hint");
        let native = at(json!({"id":"a","role":"AXTextField","editable":true,"editable_source":"native","value_settable":false,"value_settable_source":"native","actions":[]}));
        assert_eq!(native["value_settable"], false);
        let text = project(&[native], &[(1, "a".into())])[0]["text"].as_str().unwrap().to_owned();
        assert_eq!(text, "AXTextField text=\"\" editable value_settable=false");
        let unreported = at(json!({"id":"a","editable":null,"editable_source":"native"}));
        assert_eq!(unreported["editable_source"], "unknown");
    }
}
