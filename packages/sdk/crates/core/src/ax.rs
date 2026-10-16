//! Emission diffing over the shared semantic projection, independent of adapter and binding.
use serde_json::{json, Value};
use std::collections::BTreeMap;

pub fn render(q: &Value) -> Result<Value, &'static str> {
    let header = q["header"].as_str().ok_or("missing header")?;
    let rows = q["rows"].as_array().ok_or("missing rows")?;
    let old_rows = q["before"].as_array();
    let mut full = format!("{header}\nFull tree; use only indices listed below.");
    if let Some(reason) = q["reason"].as_str() {
        full.push_str(&format!("\n{reason}"));
    }
    for row in rows {
        full.push_str(&format!(
            "\n{}[{}] {}",
            "  ".repeat(row["depth"].as_u64().unwrap_or(0).min(64) as usize),
            row["index"],
            row["text"].as_str().ok_or("missing record text")?
        ));
    }
    if rows.is_empty() {
        full.push_str("\n(empty tree)");
    }
    let mut kind = "full";
    let mut selected = full.clone();
    if let Some(old_rows) = old_rows.filter(|_| !q["full"].as_bool().unwrap_or(false)) {
        let index = |r: &Value| r["index"].as_u64().unwrap_or(0);
        let old: BTreeMap<_, _> = old_rows.iter().map(|r| (index(r), r)).collect();
        let new: BTreeMap<_, _> = rows.iter().map(|r| (index(r), r)).collect();
        let mut removed: Vec<_> = old_rows
            .iter()
            .filter(|r| !new.contains_key(&index(r)))
            .collect();
        removed.sort_by_key(|r| {
            (
                std::cmp::Reverse(r["depth"].as_u64().unwrap_or(0)),
                index(r),
            )
        });
        let mut changes: Vec<String> = removed
            .iter()
            .map(|r| format!("- [{}]", index(r)))
            .collect();
        for row in rows {
            let id = index(row);
            let prev = old.get(&id);
            if prev.is_some_and(|r| {
                r["text"] == row["text"]
                    && r["parent"] == row["parent"]
                    && r["after"] == row["after"]
            }) {
                continue;
            }
            let mut line = format!(
                "{} [{id}] {}",
                if prev.is_none() { "+" } else { "~" },
                row["text"].as_str().unwrap_or("")
            );
            if prev.is_none_or(|r| r["parent"] != row["parent"] || r["after"] != row["after"]) {
                for key in ["parent", "after"] {
                    let value = if row[key].is_null() {
                        "null".into()
                    } else {
                        format!("[{}]", row[key])
                    };
                    line.push_str(&format!(" {key}={value}"));
                }
            }
            changes.push(line);
        }
        let delta = if changes.is_empty() {
            format!("{header}\nNo AX changes. Existing indices remain valid.")
        } else {
            format!(
                "{header}\nChanges since previous emitted AX; unchanged indices remain valid.\n{}",
                changes.join("\n")
            )
        };
        if delta.len() < full.len() {
            kind = if changes.is_empty() {
                "unchanged"
            } else {
                "diff"
            };
            selected = delta;
        }
    }
    Ok(
        json!({"text":selected,"representation":kind,"full_bytes":full.len(),"emitted_bytes":selected.len(),"node_count":rows.len(),"size_unit":"utf8_bytes"}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn content_clear_and_move_are_replacement_records() {
        let before:Vec<_> = (1..30).map(|i|json!({"index":i,"text":"textbox text=\"old\" editable","depth":0,"parent":null,"after":if i==1 {Value::Null} else {json!(i-1)}})).collect();
        let mut after = before.clone();
        after[1]["text"] = json!("textbox text=\"\" editable");
        after[1]["parent"] = json!(1);
        let result = render(&json!({"header":"AX phone","rows":after,"before":before})).unwrap();
        assert_eq!(result["representation"], "diff");
        assert!(result["text"]
            .as_str()
            .unwrap()
            .contains("~ [2] textbox text=\"\" editable parent=[1] after=[1]"));
        assert!(result["emitted_bytes"].as_u64().unwrap() < result["full_bytes"].as_u64().unwrap());
    }
}
