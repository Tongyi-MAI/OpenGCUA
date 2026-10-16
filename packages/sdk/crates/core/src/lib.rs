//! Provider-independent snapshot matching and structural AX diffing.
use serde_json::{json, Value};
pub mod ax;
pub mod semantic;

/// Convert model coordinates into the pixels of the actual observed frame.
/// Pixel inputs may refer to a full-frame resize shown to the model. Normalized
/// inputs describe image extents; inclusive far edges map to the last pixel.
pub fn image_point(
    x: f64,
    y: f64,
    space: &str,
    frame: (u32, u32),
    basis: (u32, u32),
) -> Result<(f64, f64), &'static str> {
    if !x.is_finite()
        || !y.is_finite()
        || frame.0 == 0
        || frame.1 == 0
        || basis.0 == 0
        || basis.1 == 0
    {
        return Err("coordinates must be finite and image dimensions positive");
    }
    let (nx, ny) = match space {
        "pixels" => {
            if x < 0.0 || y < 0.0 || x >= basis.0 as f64 || y >= basis.1 as f64 {
                return Err("pixel coordinates outside the model image");
            }
            (x / basis.0 as f64, y / basis.1 as f64)
        }
        "normalized" | "normalized_1000" => {
            let scale = if space == "normalized" { 1.0 } else { 1000.0 };
            if !(0.0..=scale).contains(&x) || !(0.0..=scale).contains(&y) {
                return Err("normalized coordinates outside the declared range");
            }
            (x / scale, y / scale)
        }
        _ => return Err("unknown coordinate space"),
    };
    Ok((
        (nx * frame.0 as f64).min((frame.0 - 1) as f64),
        (ny * frame.1 as f64).min((frame.1 - 1) as f64),
    ))
}
pub fn matching(nodes: &Value, query: &Value) -> Vec<String> {
    nodes
        .as_array()
        .into_iter()
        .flatten()
        .filter(|node| {
            query
                .as_object()
                .into_iter()
                .flatten()
                .all(|(k, v)| v.is_null() || node.get(k) == Some(v))
        })
        .filter_map(|n| n["id"].as_str().map(str::to_owned))
        .collect()
}
pub fn delta(before: &Value, after: &Value) -> Value {
    let old: std::collections::BTreeMap<String, Value> = before
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|n| Some((n["id"].as_str()?.to_owned(), n.clone())))
        .collect();
    let new: std::collections::BTreeMap<String, Value> = after
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|n| Some((n["id"].as_str()?.to_owned(), n.clone())))
        .collect();
    json!({"removed":old.keys().filter(|id|!new.contains_key(*id)).collect::<Vec<_>>(),
        "upserted":new.iter().filter(|(id,n)| old.get(*id)!=Some(*n)).map(|(_,n)|n).collect::<Vec<_>>()})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn coordinate_spaces_and_resized_model_image() {
        let frame = (720, 1600);
        for (x, y, space, basis) in [
            (360.0, 608.0, "pixels", frame),
            (180.0, 304.0, "pixels", (360, 800)),
            (0.5, 0.38, "normalized", frame),
            (500.0, 380.0, "normalized_1000", frame),
        ] {
            assert_eq!(
                image_point(x, y, space, frame, basis).unwrap(),
                (360.0, 608.0)
            );
        }
        assert_eq!(
            image_point(1.0, 1.0, "normalized", frame, frame).unwrap(),
            (719.0, 1599.0)
        );
        for (x, y, space) in [
            (f64::NAN, 0.0, "pixels"),
            (720.0, 0.0, "pixels"),
            (1001.0, 0.0, "normalized_1000"),
            (0.0, -0.1, "normalized"),
            (0.0, 0.0, "auto"),
        ] {
            assert!(image_point(x, y, space, frame, frame).is_err());
        }
    }
    #[test]
    fn exact_and_and_duplicates() {
        let n = json!([{"id":"1","name":"More","role":"button"},{"id":"2","name":"More","role":"text"}]);
        assert_eq!(
            matching(&n, &json!({"name":"More","role":"button"})),
            vec!["1"]
        );
        assert_eq!(matching(&n, &json!({"name":"More"})).len(), 2);
    }
    #[test]
    fn diff_add_update_remove() {
        assert_eq!(
            delta(
                &json!([{"id":"1"},{"id":"2","name":"old"}]),
                &json!([{"id":"2","name":"new"},{"id":"3"}])
            ),
            json!({"removed":["1"],"upserted":[{"id":"2","name":"new"},{"id":"3"}]})
        );
    }
}

pub mod apps;
