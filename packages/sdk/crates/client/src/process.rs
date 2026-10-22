//! Correlated JSON-lines subprocess transport. Cancellation never replays a request.
use mu_adapter::{Provider, ProviderFuture};
use mu_protocol::{Error, Reply, Request};
use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    io::Write,
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
    sync::{broadcast, oneshot, watch, Mutex as AsyncMutex, Notify},
};
type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Result<Reply, Error>>>>>;

/// Retained provider stderr: enough to explain a crash without unbounded memory.
const STDERR_TAIL_LINES: usize = 20;
const STDERR_LINE_CHARS: usize = 400;

/// Appends provider diagnostics to `$MAI_UI_PROVIDER_LOG` when the host sets it;
/// otherwise stderr is forwarded to this process's stderr as before.
fn provider_log() -> Option<std::fs::File> {
    let path = std::env::var_os("MAI_UI_PROVIDER_LOG")?;
    let mut options = std::fs::OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path).ok()
}

fn log_line(log: &Option<Arc<Mutex<std::fs::File>>>, line: &str) {
    match log {
        Some(file) => {
            let stamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| format!("{}.{:03}", d.as_secs(), d.subsec_millis()))
                .unwrap_or_default();
            let _ = writeln!(file.lock().unwrap(), "[{stamp}] {line}");
        }
        None => eprintln!("{line}"),
    }
}

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
    child: Arc<AsyncMutex<Child>>,
    pending: Pending,
    sequence: AtomicU64,
    closed: watch::Receiver<bool>,
    events: broadcast::Sender<mu_protocol::Event>,
    exit_reason: Arc<Mutex<Option<String>>>,
    exit_status: Arc<Mutex<Option<String>>>,
    exit_done: Arc<Notify>,
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
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|error| spawn_error(program, error))?;
        let label = format!(
            "{} pid={}",
            args.first()
                .and_then(|a| Path::new(a).file_name())
                .or_else(|| Path::new(program).file_name())
                .and_then(|n| n.to_str())
                .unwrap_or(program),
            child.id().unwrap_or(0)
        );
        let log = provider_log().map(|f| Arc::new(Mutex::new(f)));
        log_line(&log, &format!("[{label}] started: {program} {}", args.join(" ")));
        let input = child.stdin.take();
        let stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        let tail: Arc<Mutex<VecDeque<String>>> = Arc::default();
        let stderr_done = Arc::new(Notify::new());
        {
            let (tail, log, label, done) = (tail.clone(), log.clone(), label.clone(), stderr_done.clone());
            tokio::spawn(async move {
                let mut reader = BufReader::new(stderr);
                loop {
                    // 分块排空超长 stderr 行，避免无换行输出无限积攒在内存。
                    let mut bytes = Vec::new();
                    match (&mut reader).take(16 * 1024).read_until(b'\n', &mut bytes).await {
                        Ok(0) | Err(_) => break,
                        _ => {}
                    }
                    let line = String::from_utf8_lossy(&bytes);
                    let line = line.trim_end_matches(['\r', '\n']);
                    log_line(&log, &format!("[{label}] {line}"));
                    let mut tail = tail.lock().unwrap();
                    if tail.len() == STDERR_TAIL_LINES {
                        tail.pop_front();
                    }
                    tail.push_back(line.chars().take(STDERR_LINE_CHARS).collect());
                }
                done.notify_one();
            });
        }
        let child = Arc::new(AsyncMutex::new(child));
        let exit_reason: Arc<Mutex<Option<String>>> = Arc::default();
        let exit_status: Arc<Mutex<Option<String>>> = Arc::default();
        let exit_done = Arc::new(Notify::new());
        let reader_status = exit_status.clone();
        let reader_exit_done = exit_done.clone();
        let pending: Pending = Arc::default();
        let reader_pending = pending.clone();
        let (closed_tx, closed) = watch::channel(false);
        let (events, _) = broadcast::channel(4096);
        let event_tx = events.clone();
        let reader_child = Arc::downgrade(&child);
        let reader_reason = exit_reason.clone();
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
                    Ok(0) => break Error::new("DEVICE_OFFLINE", "provider closed its output"),
                    Ok(_) => {
                        break Error::new("PROTOCOL_MISMATCH", "provider wrote an oversized or unterminated frame")
                    }
                    Err(e) => break Error::new("DEVICE_OFFLINE", format!("reading provider output failed: {e}")),
                }
            };
            // A host shutdown holds the child lock; otherwise collect the real exit status.
            let child = reader_child.upgrade();
            let status = match child.as_ref().map(|c| c.try_lock()) {
                Some(Ok(mut child)) => match tokio::time::timeout(Duration::from_secs(2), child.wait()).await {
                    Ok(Ok(status)) => status.to_string(),
                    Ok(Err(e)) => format!("exit status unavailable: {e}"),
                    Err(_) => "process still running after its output closed".into(),
                },
                Some(Err(_)) => {
                    // shutdown 持有 Child 锁，等它收集真实退出码后再发离线通知。
                    let _ = tokio::time::timeout(Duration::from_secs(11), reader_exit_done.notified()).await;
                    reader_status.lock().unwrap().clone()
                        .unwrap_or_else(|| "host shutdown did not report an exit status".into())
                }
                None => "provider owner was dropped; exit status unavailable".into(),
            };
            drop(child);
            let _ = tokio::time::timeout(Duration::from_millis(500), stderr_done.notified()).await;
            let stderr_tail = tail.lock().unwrap().iter().cloned().collect::<Vec<_>>().join(" | ");
            let mut reason = format!("{label} exited ({status}) after: {}", failure.message);
            if !stderr_tail.is_empty() {
                reason.push_str(&format!("; last stderr: {stderr_tail}"));
            }
            log_line(&log, &reason);
            *reader_reason.lock().unwrap() = Some(reason.clone());
            let failure = Error::new(
                &failure.code,
                format!("{reason}; in-flight request outcome unknown"),
            );
            for (_, sender) in reader_pending.lock().unwrap().drain() {
                let _ = sender.send(Err(failure.clone()));
            }
            let _ = closed_tx.send(true);
        });
        Ok(Arc::new(Self {
            input: Arc::new(AsyncMutex::new(input)),
            child,
            pending,
            sequence: AtomicU64::new(0),
            closed,
            events,
            exit_reason,
            exit_status,
            exit_done,
        }))
    }
    /// Why the provider process stopped (exit status and stderr tail), once known.
    pub fn exit_reason(&self) -> Option<String> {
        self.exit_reason.lock().unwrap().clone()
    }
    fn exited(&self) -> Error {
        Error::new(
            "DEVICE_OFFLINE",
            self.exit_reason()
                .unwrap_or_else(|| "provider process exited".into()),
        )
    }
    pub fn closed_signal(&self) -> watch::Receiver<bool> {
        self.closed.clone()
    }
    pub fn subscribe(&self) -> broadcast::Receiver<mu_protocol::Event> {
        self.events.subscribe()
    }
    pub async fn send(&self, mut request: Request) -> Result<Reply, Error> {
        if *self.closed.borrow() {
            return Err(self.exited());
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
        let exit_reason = self.exit_reason.clone();
        tokio::spawn(async move {
            let result = async {
                let mut input = input.lock().await;
                if *closed.borrow() {
                    let reason = exit_reason.lock().unwrap().clone();
                    return Err(Error::new(
                        "DEVICE_OFFLINE",
                        reason.unwrap_or_else(|| "provider process exited".into()),
                    ));
                }
                let input = input
                    .as_mut()
                    .ok_or_else(|| Error::new("SESSION_CLOSED", "transport closed"))?;
                input
                    .write_all(&wire)
                    .await
                    .map_err(|e| Error::new("DEVICE_OFFLINE", format!("writing to provider failed: {e}")))
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
            result = rx => result.map_err(|_| Error::new("DEVICE_OFFLINE", "provider reply channel closed"))?,
            _ = self.wait() => {
                self.pending.lock().unwrap().remove(&id);
                Err(self.exited())
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
        let status = match tokio::time::timeout(Duration::from_secs(5), child.wait()).await {
            Ok(result) => result.map_err(|e| Error::new("DEVICE_OFFLINE", e.to_string()))?,
            Err(_) => {
                child
                    .kill()
                    .await
                    .map_err(|e| Error::new("DEVICE_OFFLINE", e.to_string()))?;
                child
                    .wait()
                    .await
                    .map_err(|e| Error::new("DEVICE_OFFLINE", e.to_string()))?
            }
        };
        *self.exit_status.lock().unwrap() = Some(status.to_string());
        self.exit_done.notify_one();
        drop(child);
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

    #[tokio::test]
    async fn exited_provider_reports_status_and_stderr() {
        let client = ProcessClient::spawn(
            "/bin/sh",
            &["-c".into(), "echo chrome-crashed >&2; exit 3".into()],
            &BTreeMap::new(),
        )
        .unwrap();
        client.wait().await;
        let request: Request =
            serde_json::from_value(serde_json::json!({"op":"describe","timeout_ms":1000})).unwrap();
        let error = client.send(request).await.unwrap_err();
        assert_eq!(error.code, "DEVICE_OFFLINE");
        assert!(error.message.contains("exit status: 3"), "{}", error.message);
        assert!(error.message.contains("chrome-crashed"), "{}", error.message);
        assert_eq!(client.exit_reason().as_deref(), Some(error.message.as_str()));
    }

    #[tokio::test]
    async fn host_shutdown_records_real_exit_status() {
        let client = ProcessClient::spawn(
            "/bin/sh",
            &["-c".into(), "read line; echo clean-stop >&2; exit 0".into()],
            &BTreeMap::new(),
        ).unwrap();
        tokio::time::timeout(Duration::from_secs(3), client.shutdown()).await.unwrap().unwrap();
        let reason = client.exit_reason().unwrap();
        assert!(reason.contains("exit status: 0"), "{reason}");
        assert!(reason.contains("clean-stop"), "{reason}");
    }

    #[tokio::test]
    async fn malformed_reply_is_protocol_error_not_device_offline() {
        let client = ProcessClient::spawn(
            "/bin/sh",
            &["-c".into(), "read line; printf 'not-json\\n'; exit 0".into()],
            &BTreeMap::new(),
        ).unwrap();
        let request: Request =
            serde_json::from_value(serde_json::json!({"op":"describe","timeout_ms":1000})).unwrap();
        let error = client.send(request).await.unwrap_err();
        assert_eq!(error.code, "PROTOCOL_MISMATCH");
        assert!(error.message.contains("invalid provider reply"), "{}", error.message);
    }
}
