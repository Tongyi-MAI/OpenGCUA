//! Owned Windows CUA worker. Desktop authority remains in the embedded runtime.
use super::*;
use std::time::Duration;

const ASSETS: &[(&str, &str)] = &[
    (
        "worker.py",
        include_str!("../../../adapters/windows/mai_ui_windows/worker.py"),
    ),
    (
        "provider.py",
        include_str!("../../../adapters/windows/mai_ui_windows/provider.py"),
    ),
    (
        "transport.py",
        include_str!("../../../adapters/windows/mai_ui_windows/transport.py"),
    ),
    (
        "native.py",
        include_str!("../../../adapters/windows/mai_ui_windows/native.py"),
    ),
];

fn absolute_file(q: &Value, key: &str) -> Result<PathBuf, Error> {
    let value = s(q, key)?;
    let path = PathBuf::from(value);
    if value.contains('\0') || !path.is_absolute() {
        return Err(Error::new(
            "INVALID_ARGUMENT",
            format!("{key} must be an absolute file path"),
        ));
    }
    if !path.is_file() {
        return Err(Error::new(
            "SETUP_REQUIRED",
            format!("{key} does not exist: {}", path.display()),
        ));
    }
    Ok(path)
}

impl Host {
    pub(super) async fn add_windows(self: &Arc<Self>, q: &Value) -> Result<Value, Error> {
        if !cfg!(windows) {
            return Err(Error::new(
                "UNSUPPORTED_CAPABILITY",
                "The Windows adapter requires a native Windows host",
            ));
        }
        let name = s(q, "name")?;
        if name.trim().is_empty() {
            return Err(err("INVALID_ARGUMENT"));
        }
        let python = absolute_file(q, "python")?;
        let driver = absolute_file(q, "driver_path")?;
        let _guard = self.creation.lock().await;
        let assets = Arc::new(
            tempfile::Builder::new()
                .prefix("mai-ui-windows-")
                .tempdir()
                .map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?,
        );
        for (file, contents) in ASSETS {
            std::fs::write(assets.path().join(file), contents)
                .map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
        }
        let provider = mu_client::process::ProcessClient::spawn(
            &python.to_string_lossy(),
            &[
                "-u".into(),
                assets.path().join("worker.py").to_string_lossy().into(),
                "--driver".into(),
                driver.to_string_lossy().into(),
            ],
            &BTreeMap::from([("PYTHONUTF8".into(), "1".into())]),
        )?;
        let result = async {
            let reply = tokio::time::timeout(
                Duration::from_secs(20),
                provider.request(
                    serde_json::from_value(json!({"op":"describe","timeout_ms":18000})).unwrap(),
                ),
            )
            .await
            .map_err(|_| err("DEADLINE_EXCEEDED"))??;
            reply.validate()?;
            if let Some(error) = reply.error {
                return Err(error);
            }
            let description = reply.result.unwrap();
            let id = s(&description, "desktop_id")?.to_string();
            if !id.starts_with("windows:")
                || description["platform"] != "windows"
                || description["provider"] != "windows.cua"
            {
                return Err(err("PROTOCOL_MISMATCH"));
            }
            let info = self.runtime.add(
                json!({"id":id,"name":name,"kind":"device",
                "platform":"windows","provider":"windows.cua"}),
                description,
                provider.clone(),
            )?;
            self.artifacts
                .lock()
                .unwrap()
                .insert(id.clone(), assets.clone());
            let endpoint = Arc::downgrade(&self.runtime.endpoint(&id)?);
            let mut events = provider.subscribe();
            let weak = Arc::downgrade(self);
            let event_id = id.clone();
            let event_endpoint = endpoint.clone();
            tokio::spawn(async move {
                loop {
                    let event = events.recv().await;
                    let Some(host) = weak.upgrade() else { break };
                    let Some(endpoint) = event_endpoint.upgrade() else {
                        break;
                    };
                    match event {
                        Ok(value) => host.runtime.provider_event_from(
                            &endpoint,
                            json!({
                            "type":value.event,"resource_id":event_id,"data":value.data}),
                        ),
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(count)) => {
                            host.runtime.provider_event_from(
                                &endpoint,
                                json!({"type":"event_gap",
                                "resource_id":event_id,"data":{"dropped":count}}),
                            )
                        }
                        Err(_) => break,
                    }
                }
            });
            let mut closed = provider.closed_signal();
            let weak = Arc::downgrade(self);
            let process = Arc::downgrade(&provider);
            tokio::spawn(async move {
                while !*closed.borrow_and_update() {
                    if closed.changed().await.is_err() {
                        break;
                    }
                }
                if let Some(host) = weak.upgrade() {
                    if let Some(endpoint) = endpoint.upgrade() {
                        let detail = process
                            .upgrade()
                            .and_then(|p| p.exit_reason())
                            .unwrap_or_else(|| "Windows worker exited".into());
                        endpoint.mark_offline(format!("Windows worker stopped: {detail}"));
                        host.runtime.provider_event_from(
                            &endpoint,
                            json!({"type":"resource_offline",
                            "resource_id":id,"data":{"reason":"worker_exited","detail":detail}}),
                        );
                    }
                }
            });
            Ok(info)
        }
        .await;
        if result.is_err() {
            let _ = provider.close().await;
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn executable_paths_are_explicit_existing_files() {
        assert_eq!(
            absolute_file(&json!({"python":"python"}), "python")
                .unwrap_err()
                .code,
            "INVALID_ARGUMENT"
        );
        let root = tempfile::tempdir().unwrap();
        let missing = root.path().join("missing.exe");
        assert_eq!(
            absolute_file(&json!({"python":missing}), "python")
                .unwrap_err()
                .code,
            "SETUP_REQUIRED"
        );
        let file = root.path().join("python.exe");
        std::fs::write(&file, b"fixture").unwrap();
        assert_eq!(
            absolute_file(&json!({"python":file}), "python").unwrap(),
            file
        );
        assert_eq!(
            absolute_file(&json!({"python":root.path()}), "python")
                .unwrap_err()
                .code,
            "SETUP_REQUIRED"
        );
    }
}
