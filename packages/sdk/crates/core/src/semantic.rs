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
    node
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
        for key in ["editable","focused","password"] { if node[key] == true { fields.push(key.into()); } }
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
}
