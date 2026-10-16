//! Exact shared application-label normalization and deterministic fuzzy matching.
use serde_json::Value;
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;
pub fn normalize(s: &str) -> String {
    s.nfkc()
        .case_fold()
        .filter(|c| c.is_alphanumeric())
        .collect()
}
fn folded(s: &str) -> String {
    s.case_fold().collect()
}
fn label(a: &Value) -> &str {
    a["label"].as_str().unwrap_or("")
}
fn id(a: &Value) -> &str {
    a["id"].as_str().unwrap_or("")
}
fn labels(apps: &[Value]) -> Vec<String> {
    apps.iter()
        .map(|a| {
            let duplicate = apps
                .iter()
                .filter(|b| normalize(label(a)) == normalize(label(b)))
                .count()
                > 1;
            if duplicate {
                format!(
                    "{} [{}]",
                    label(a),
                    a["native_id"].as_str().unwrap_or(id(a))
                )
            } else {
                label(a).into()
            }
        })
        .collect()
}
fn quoted(strings: &[String]) -> String {
    format!(
        "[{}]",
        strings
            .iter()
            .map(|s| serde_json::to_string(s).unwrap())
            .collect::<Vec<_>>()
            .join(", ")
    )
}
/// SequenceMatcher's matching-block ratio, with autojunk disabled.
fn ratio(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    fn count(a: &[char], b: &[char]) -> usize {
        let mut best = (0, 0, 0);
        let mut previous = vec![0; b.len() + 1];
        for (i, ca) in a.iter().enumerate() {
            let mut current = vec![0; b.len() + 1];
            for (j, cb) in b.iter().enumerate() {
                if ca == cb {
                    current[j + 1] = previous[j] + 1;
                    if current[j + 1] > best.2 {
                        best = (
                            i + 1 - current[j + 1],
                            j + 1 - current[j + 1],
                            current[j + 1],
                        );
                    }
                }
            }
            previous = current;
        }
        if best.2 == 0 {
            0
        } else {
            best.2
                + count(&a[..best.0], &b[..best.1])
                + count(&a[best.0 + best.2..], &b[best.1 + best.2..])
        }
    }
    if a.is_empty() && b.is_empty() {
        1.0
    } else {
        2.0 * count(&a, &b) as f64 / (a.len() + b.len()) as f64
    }
}
pub fn find(query: &str, inventory: &Value) -> Result<Value, (&'static str, String)> {
    let normalized = normalize(query);
    if normalized.is_empty() {
        return Err((
            "INVALID_ARGUMENT",
            "app name must be a nonempty label or ID".into(),
        ));
    }
    let rows = inventory["apps"]
        .as_array()
        .ok_or(("INVALID_ARGUMENT", "missing apps".into()))?;
    let direct: Vec<_> = rows
        .iter()
        .filter(|a| query == id(a) || a["native_id"].as_str() == Some(query))
        .collect();
    if direct.len() == 1 {
        return Ok(direct[0].clone());
    }
    if direct.len() > 1 {
        return Err((
            "AMBIGUOUS_TARGET",
            "multiple running app instances match".into(),
        ));
    }
    if inventory["complete"] != true {
        return Err((
            "APP_INVENTORY_INCOMPLETE",
            "App inventory is incomplete; refresh before opening an app".into(),
        ));
    }
    let mut apps: Vec<Value> = rows
        .iter()
        .filter(|a| a["launchable"] == true)
        .cloned()
        .collect();
    apps.sort_by(|a, b| id(a).cmp(id(b)));
    let names = labels(&apps);
    let q = folded(query.trim());
    let mut candidates: Vec<Value> = apps
        .iter()
        .zip(&names)
        .filter(|(a, name)| {
            [
                folded(id(a)),
                folded(a["native_id"].as_str().unwrap_or("")),
                folded(name),
            ]
            .contains(&q)
        })
        .map(|(a, _)| a.clone())
        .collect();
    if candidates.is_empty() {
        candidates = apps
            .iter()
            .filter(|a| normalize(label(a)) == normalized)
            .cloned()
            .collect();
    }
    if candidates.is_empty() && normalized.chars().count() >= 2 {
        candidates = apps
            .iter()
            .filter(|a| normalize(label(a)).starts_with(&normalized))
            .cloned()
            .collect();
        if candidates.is_empty() {
            candidates = apps
                .iter()
                .filter(|a| normalize(label(a)).contains(&normalized))
                .cloned()
                .collect();
        }
    }
    if candidates.is_empty() && normalized.chars().count() >= 4 {
        let mut ranks: Vec<_> = apps
            .iter()
            .map(|a| (ratio(&normalized, &normalize(label(a))), a))
            .collect();
        ranks.sort_by(|a, b| b.0.total_cmp(&a.0).then_with(|| id(a.1).cmp(id(b.1))));
        if let Some((best, _)) = ranks.first() {
            if *best >= 0.8 {
                candidates = ranks
                    .iter()
                    .filter(|(score, _)| best - score < 0.1)
                    .map(|(_, a)| (*a).clone())
                    .collect();
            }
        }
    }
    match candidates.len(){1=>Ok(candidates.remove(0)),0=>Err(("TARGET_NOT_FOUND",format!("No launchable app matches {query:?}; use await mu.emit(await device.apps.list()) to see app labels"))),_=>Err(("AMBIGUOUS_TARGET",format!("Multiple apps match; use an exact label: {}",quoted(&labels(&candidates).into_iter().take(5).collect::<Vec<_>>()))))}
}
pub fn format(inventory: &Value) -> String {
    let mut apps = inventory["apps"].as_array().cloned().unwrap_or_default();
    apps.sort_by_key(|a| (normalize(label(a)), id(a).to_string()));
    let labels = labels(&apps)
        .into_iter()
        .zip(&apps)
        .map(|(label, a)| {
            if a["launchable"] == true {
                label
            } else {
                format!("{label} (not launchable)")
            }
        })
        .collect::<Vec<_>>();
    format!(
        "Apps ({}{}): {}",
        apps.len(),
        if inventory["complete"] == true {
            ""
        } else {
            "; incomplete"
        },
        quoted(&labels)
    )
}
