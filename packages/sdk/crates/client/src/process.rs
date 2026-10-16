//! Correlated JSON-lines subprocess transport. Cancellation never replays a request.
use mu_adapter::{Provider, ProviderFuture};
use mu_protocol::{Error, Reply, Request};
use std::{
    collections::{BTreeMap, HashMap},
    path::Path,
    process::Stdio,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, Command},
    sync::{broadcast, oneshot, watch, Mutex as AsyncMutex},
};
type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Result<Reply, Error>>>>>;

fn spawn_error(program: &str, error: std::io::Error) -> Error {
    if error.kind() != std::io::ErrorKind::NotFound {
        return Error::new("DEVICE_OFFLINE", error.to_string());
    }
    let executable = Path::new(program)
        .file_stem()
        .and_then(|name| name.to_str())
        .unwrap_or(program);
    if executable.eq_ignore_ascii_case("node") {
        return Error::new(
            "SETUP_REQUIRED",
            format!(
                "Node.js executable {program:?} was not found; install Node.js or set MAI_UI_NODE to its absolute path"
            ),
        );
    }
    Error::new(
        "SETUP_REQUIRED",
        format!("Required executable {program:?} was not found"),
    )
}

pub struct ProcessClient {
    input: Arc<AsyncMutex<Option<ChildStdin>>>,
    child: AsyncMutex<Child>,
    pending: Pending,
    sequence: AtomicU64,
    closed: watch::Receiver<bool>,
    events: broadcast::Sender<mu_protocol::Event>,
}
impl ProcessClient {
    pub fn spawn(
        program: &str,
        args: &[String],
        env: &BTreeMap<String, String>,
    ) -> Result<Arc<Self>, Error> {
        let mut child = Command::new(program)
            .args(args)
            .envs(env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .map_err(|error| spawn_error(program, error))?;
        let input = child.stdin.take();
        let stdout = child.stdout.take().unwrap();
        let pending: Pending = Arc::default();
        let reader_pending = pending.clone();
        let (closed_tx, closed) = watch::channel(false);
        let (events, _) = broadcast::channel(4096);
        let event_tx = events.clone();
        tokio::spawn(async move {
            let mut reader = BufReader::new(stdout);
            let failure = loop {
                let mut line = Vec::new();
                match (&mut reader)
                    .take(16 * 1024 * 1024 + 1)
                    .read_until(b'\n', &mut line)
                    .await
                {
                    Ok(size)
                        if size > 0 && size <= 16 * 1024 * 1024 && line.last() == Some(&b'\n') =>
                    {
                        if let Ok(event) = serde_json::from_slice::<mu_protocol::Event>(&line) {
                            let _ = event_tx.send(event);
                            continue;
                        }
                        let reply = serde_json::from_slice::<Reply>(&line).and_then(|reply| {
                            reply.validate().map_err(serde::de::Error::custom)?;
                            Ok(reply)
                        });
                        match reply {
                            Ok(reply) if reply.request_id.is_some() => {
                                let sender = reader_pending
                                    .lock()
                                    .unwrap()
                                    .remove(&reply.request_id.unwrap());
                                if let Some(sender) = sender {
                                    let _ = sender.send(Ok(reply));
                                }
                            }
                            _ => break Error::new("PROTOCOL_MISMATCH", "invalid provider reply"),
                        }
                    }
                    _ => {
                        break Error::new(
                            "DEVICE_OFFLINE",
                            "broker exited or invalid frame; outcome unknown",
                        )
                    }
                }
            };
            for (_, sender) in reader_pending.lock().unwrap().drain() {
                let _ = sender.send(Err(failure.clone()));
            }
            let _ = closed_tx.send(true);
        });
        Ok(Arc::new(Self {
            input: Arc::new(AsyncMutex::new(input)),
            child: AsyncMutex::new(child),
            pending,
            sequence: AtomicU64::new(0),
            closed,
            events,
        }))
    }
    pub fn closed_signal(&self) -> watch::Receiver<bool> {
        self.closed.clone()
    }
    pub fn subscribe(&self) -> broadcast::Receiver<mu_protocol::Event> {
        self.events.subscribe()
    }
    pub async fn send(&self, mut request: Request) -> Result<Reply, Error> {
        if *self.closed.borrow() {
            return Err(Error::new("DEVICE_OFFLINE", "broker exited"));
        }
        let id = self.sequence.fetch_add(1, Ordering::Relaxed) + 1;
        request.request_id = Some(id);
        let mut wire = serde_json::to_vec(&request)
            .map_err(|e| Error::new("INVALID_ARGUMENT", e.to_string()))?;
        wire.push(b'\n');
        let (tx, rx) = oneshot::channel();
        // Once a frame is admitted, an owned writer finishes it even when the
        // language waiter is cancelled. Otherwise a partial JSON line could be
        // joined to the next request and corrupt the connection.
        self.pending.lock().unwrap().insert(id, tx);
        let input = self.input.clone();
        let pending = self.pending.clone();
        let closed = self.closed.clone();
        tokio::spawn(async move {
            let result = async {
                let mut input = input.lock().await;
                if *closed.borrow() {
                    return Err(Error::new("DEVICE_OFFLINE", "broker exited"));
                }
                let input = input
                    .as_mut()
                    .ok_or_else(|| Error::new("SESSION_CLOSED", "transport closed"))?;
                input
                    .write_all(&wire)
                    .await
                    .map_err(|e| Error::new("DEVICE_OFFLINE", e.to_string()))
            }
            .await;
            if let Err(error) = result {
                if let Some(sender) = pending.lock().unwrap().remove(&id) {
                    let _ = sender.send(Err(error));
                }
            }
        });
        // If the awaiting language task is cancelled, its sender remains in the
        // reader's table until the corresponding reply drains; no request repeats.
        tokio::select! {
            biased;
            result = rx => result.map_err(|_| Error::new("DEVICE_OFFLINE", "reply channel closed"))?,
            _ = self.wait() => {
                self.pending.lock().unwrap().remove(&id);
                Err(Error::new("DEVICE_OFFLINE", "broker exited"))
            }
        }
    }
    pub async fn wait(&self) {
        let mut closed = self.closed.clone();
        while !*closed.borrow_and_update() {
            if closed.changed().await.is_err() {
                break;
            }
        }
    }
    pub async fn shutdown(&self) -> Result<(), Error> {
        let mut child = self.child.lock().await;
        match tokio::time::timeout(Duration::from_secs(5), self.input.lock()).await {
            Ok(mut input) => {
                input.take();
            }
            Err(_) => {
                child
                    .start_kill()
                    .map_err(|e| Error::new("DEVICE_OFFLINE", e.to_string()))?;
            }
        }
        match tokio::time::timeout(Duration::from_secs(5), child.wait()).await {
            Ok(result) => {
                result.map_err(|e| Error::new("DEVICE_OFFLINE", e.to_string()))?;
            }
            Err(_) => {
                child
                    .kill()
                    .await
                    .map_err(|e| Error::new("DEVICE_OFFLINE", e.to_string()))?;
                child
                    .wait()
                    .await
                    .map_err(|e| Error::new("DEVICE_OFFLINE", e.to_string()))?;
            }
        }
        self.wait().await;
        Ok(())
    }
}
impl Provider for ProcessClient {
    fn request(&self, request: Request) -> ProviderFuture<'_, Reply> {
        Box::pin(self.send(request))
    }
    fn close(&self) -> ProviderFuture<'_, ()> {
        Box::pin(self.shutdown())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn missing_node_executable_reports_setup_required() {
        let result = ProcessClient::spawn(
            "/definitely/not-installed/mai-ui/node",
            &[],
            &BTreeMap::new(),
        );
        let error = match result {
            Ok(_) => panic!("missing executable unexpectedly started"),
            Err(error) => error,
        };

        assert_eq!(error.code, "SETUP_REQUIRED");
        assert!(error.message.contains("Node.js"), "{}", error.message);
        assert!(error.message.contains("MAI_UI_NODE"), "{}", error.message);
    }
}
