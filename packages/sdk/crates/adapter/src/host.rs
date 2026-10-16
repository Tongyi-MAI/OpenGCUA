//! Local host services, shared by native desktop providers. Never available through app scopes.
use base64::{engine::general_purpose::STANDARD, Engine};
use mu_protocol::Error;
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
    process::Stdio,
    time::Instant,
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::Command,
};

fn io_error(e: std::io::Error) -> Error {
    Error::new(
        match e.kind() {
            std::io::ErrorKind::NotFound => "TARGET_NOT_FOUND",
            std::io::ErrorKind::AlreadyExists => "ALREADY_EXISTS",
            std::io::ErrorKind::PermissionDenied => "PERMISSION_DENIED",
            _ => "PROVIDER_ERROR",
        },
        e.to_string(),
    )
}
fn path(q: &Value, key: &str) -> Result<PathBuf, Error> {
    let p = q[key]
        .as_str()
        .filter(|p| p.starts_with('/') && !p.contains('\0'))
        .ok_or_else(|| Error::new("INVALID_ARGUMENT", "use an absolute host path"))?;
    Ok(PathBuf::from(p))
}
fn info(path: &Path) -> Result<Value, Error> {
    let m = path.symlink_metadata().map_err(io_error)?;
    Ok(
        json!({"path":path,"size":m.len(),"type":if m.is_symlink(){"symlink"}else if m.is_dir(){"directory"}else{"file"},"modified_ms":m.modified().ok().and_then(|v|v.duration_since(std::time::UNIX_EPOCH).ok()).map(|v|v.as_millis())}),
    )
}
pub fn file(q: &Value) -> Result<Value, Error> {
    let p = path(q, "path")?;
    match q["operation"].as_str().unwrap_or("") {
        "stat" => info(&p),
        "list" => {
            let mut entries = Vec::new();
            for e in std::fs::read_dir(&p).map_err(io_error)?.take(1001) {
                entries.push(info(&e.map_err(io_error)?.path())?);
            }
            let complete = entries.len() <= 1000;
            entries.truncate(1000);
            entries.sort_by_key(|e| e["path"].as_str().unwrap_or("").to_owned());
            Ok(json!({"entries":entries,"complete":complete}))
        }
        "read" => {
            let cap = q["max_bytes"].as_u64().unwrap_or(1048576).min(1048576);
            let mut data = Vec::new();
            std::fs::File::open(&p)
                .map_err(io_error)?
                .take(cap + 1)
                .read_to_end(&mut data)
                .map_err(io_error)?;
            if data.len() as u64 > cap {
                return Err(Error::new("FILE_TOO_LARGE", "read exceeds byte budget"));
            }
            Ok(
                json!({"data":STANDARD.encode(&data),"bytes":data.len(),"eof":true,"truncated":false}),
            )
        }
        "write" => {
            let data = STANDARD
                .decode(q["data"].as_str().unwrap_or(""))
                .map_err(|e| Error::new("INVALID_ARGUMENT", e.to_string()))?;
            if data.len() > 1048576 {
                return Err(Error::new("FILE_TOO_LARGE", "write limit is 1 MiB"));
            }
            let mut tmp = tempfile::NamedTempFile::new_in(p.parent().unwrap()).map_err(io_error)?;
            tmp.write_all(&data).map_err(io_error)?;
            tmp.as_file().sync_all().map_err(io_error)?;
            if q["overwrite"] == true {
                tmp.persist(&p).map_err(|e| io_error(e.error))?;
            } else {
                tmp.persist_noclobber(&p).map_err(|e| io_error(e.error))?;
            }
            Ok(json!({"bytes":data.len(),"atomic":true}))
        }
        "mkdir" => {
            if q["parents"] == true {
                std::fs::create_dir_all(&p)
            } else {
                std::fs::create_dir(&p)
            }
            .map_err(io_error)?;
            Ok(json!({"created":true}))
        }
        "remove" => {
            let m = p.symlink_metadata().map_err(io_error)?;
            if m.is_dir() && q["recursive"] == true {
                std::fs::remove_dir_all(&p)
            } else if m.is_dir() {
                std::fs::remove_dir(&p)
            } else {
                std::fs::remove_file(&p)
            }
            .map_err(io_error)?;
            Ok(json!({"removed":true}))
        }
        "move" => {
            let dest = path(q, "destination")?;
            if q["overwrite"] == true {
                std::fs::rename(&p, &dest).map_err(io_error)?;
            } else {
                rename_exclusive(&p, &dest)?;
            }
            Ok(json!({"moved":true,"atomic":true}))
        }
        _ => Err(Error::new(
            "UNSUPPORTED_CAPABILITY",
            "unknown file operation",
        )),
    }
}
#[cfg(target_os = "macos")]
fn rename_exclusive(src: &Path, dest: &Path) -> Result<(), Error> {
    use std::os::unix::ffi::OsStrExt;
    let a = std::ffi::CString::new(src.as_os_str().as_bytes())
        .map_err(|_| Error::new("INVALID_ARGUMENT", "NUL path"))?;
    let b = std::ffi::CString::new(dest.as_os_str().as_bytes())
        .map_err(|_| Error::new("INVALID_ARGUMENT", "NUL path"))?;
    // RENAME_EXCL makes no-overwrite atomic, including directories and symlinks.
    if unsafe { libc::renamex_np(a.as_ptr(), b.as_ptr(), libc::RENAME_EXCL) } != 0 {
        return Err(io_error(std::io::Error::last_os_error()));
    }
    Ok(())
}
#[cfg(not(target_os = "macos"))]
fn rename_exclusive(_: &Path, _: &Path) -> Result<(), Error> {
    Err(Error::new(
        "UNSUPPORTED_CAPABILITY",
        "exclusive host rename unsupported on this platform",
    ))
}
async fn drain(
    mut stream: impl AsyncRead + Unpin,
    cap: usize,
) -> Result<(Vec<u8>, bool), std::io::Error> {
    let mut result = Vec::new();
    let mut truncated = false;
    let mut buf = [0u8; 8192];
    loop {
        let n = stream.read(&mut buf).await?;
        if n == 0 {
            break;
        }
        let keep = n.min(cap.saturating_sub(result.len()));
        result.extend_from_slice(&buf[..keep]);
        truncated |= keep < n;
    }
    Ok((result, truncated))
}
pub async fn shell(q: &Value) -> Result<Value, Error> {
    let start = Instant::now();
    let budget = q["timeout_ms"].as_u64().unwrap_or(30000).clamp(1, 120000);
    let cap = q["max_output_bytes"]
        .as_u64()
        .unwrap_or(1048576)
        .min(1048576) as usize;
    let argv: Vec<String> = if let Some(a) = q["argv"].as_array() {
        a.iter()
            .map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .ok_or_else(|| Error::new("INVALID_ARGUMENT", "invalid argv"))
            })
            .collect::<Result<_, _>>()?
    } else {
        vec![
            "/bin/sh".into(),
            "-c".into(),
            q["script"]
                .as_str()
                .ok_or_else(|| Error::new("INVALID_ARGUMENT", "script or argv required"))?
                .into(),
        ]
    };
    if argv.is_empty() || argv.iter().any(|a| a.contains('\0')) {
        return Err(Error::new("INVALID_ARGUMENT", "invalid argv"));
    }
    let mut command = Command::new(&argv[0]);
    command.current_dir("/");
    command
        .args(&argv[1..])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if !q["cwd"].is_null() {
        command.current_dir(path(q, "cwd")?);
    }
    if let Some(env) = q["env"].as_object() {
        for (k, v) in env {
            let v = v.as_str().ok_or_else(|| {
                Error::new("INVALID_ARGUMENT", "environment values must be strings")
            })?;
            if k.contains(['\0', '=']) || v.contains('\0') {
                return Err(Error::new("INVALID_ARGUMENT", "invalid environment"));
            }
            command.env(k, v);
        }
    }
    #[cfg(unix)]
    command.process_group(0);
    let input = STANDARD
        .decode(q["stdin"].as_str().unwrap_or(""))
        .map_err(|_| Error::new("INVALID_ARGUMENT", "invalid stdin"))?;
    if input.len() > 65536 {
        return Err(Error::new("INVALID_ARGUMENT", "stdin exceeds 64 KiB"));
    }
    let mut child = command.spawn().map_err(io_error)?;
    let pid = child.id().unwrap();
    let _group = ProcessGroup(pid);
    let out = tokio::spawn(drain(child.stdout.take().unwrap(), cap));
    let err = tokio::spawn(drain(child.stderr.take().unwrap(), cap));
    let mut stdin = child.stdin.take().unwrap();
    let writer = tokio::spawn(async move {
        let _ = stdin.write_all(&input).await;
    });
    let wait = tokio::time::timeout(
        std::time::Duration::from_millis(budget.saturating_sub(250).max(1)),
        child.wait(),
    )
    .await;
    let timed_out = wait.is_err();
    // A bounded run owns the process group: descendants cannot outlive its result.
    #[cfg(unix)]
    unsafe {
        libc::kill(-(pid as i32), libc::SIGKILL);
    }
    let status = match wait {
        Ok(r) => r.map_err(io_error)?,
        Err(_) => child.wait().await.map_err(io_error)?,
    };
    writer.abort();
    let outputs = tokio::time::timeout(std::time::Duration::from_millis(150), async {
        Ok::<_, Error>((
            out.await
                .map_err(|e| Error::new("PROVIDER_ERROR", e.to_string()))?
                .map_err(io_error)?,
            err.await
                .map_err(|e| Error::new("PROVIDER_ERROR", e.to_string()))?
                .map_err(io_error)?,
        ))
    })
    .await;
    let ((stdout, ot), (stderr, et)) = outputs
        .map_err(|_| Error::new("ACTION_OUTCOME_UNKNOWN", "process output did not drain"))??;
    Ok(
        json!({"state":if timed_out{"timed_out"}else{"exited"},"exit_code":status.code(),"stdout":STANDARD.encode(stdout),"stderr":STANDARD.encode(stderr),"stdout_truncated":ot,"stderr_truncated":et,"duration_ms":start.elapsed().as_millis(),"cleanup":"complete"}),
    )
}

struct ProcessGroup(u32);
impl Drop for ProcessGroup {
    fn drop(&mut self) {
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.0 as i32), libc::SIGKILL);
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn file_publication_never_overwrites_without_permission() {
        let root = tempfile::tempdir().unwrap();
        let a = root.path().join("a");
        let b = root.path().join("b");
        let q = json!({"operation":"write","path":a,"data":STANDARD.encode(b"first")});
        file(&q).unwrap();
        assert_eq!(file(&q).unwrap_err().code, "ALREADY_EXISTS");
        std::fs::write(&b, b"second").unwrap();
        #[cfg(target_os = "macos")]
        assert_eq!(
            file(&json!({"operation":"move","path":a,"destination":b}))
                .unwrap_err()
                .code,
            "ALREADY_EXISTS"
        );
        assert_eq!(std::fs::read(&a).unwrap(), b"first");
        assert_eq!(std::fs::read(&b).unwrap(), b"second");
        assert_eq!(
            file(&json!({"operation":"read","path":a,"max_bytes":2}))
                .unwrap_err()
                .code,
            "FILE_TOO_LARGE"
        );
    }
    #[tokio::test]
    async fn process_output_drains_beyond_limit_and_timeout_kills_group() {
        let q = json!({"argv":["/bin/sh","-c","printf 123456789; printf error >&2"],"max_output_bytes":3,"timeout_ms":2000});
        let r = shell(&q).await.unwrap();
        assert_eq!(
            STANDARD.decode(r["stdout"].as_str().unwrap()).unwrap(),
            b"123"
        );
        assert_eq!(r["stdout_truncated"], true);
        assert_eq!(r["stderr_truncated"], true);
        assert_eq!(r["exit_code"], 0);
        let r = shell(&json!({"argv":["/bin/sh","-c","sleep 60 & wait"],"timeout_ms":400}))
            .await
            .unwrap();
        assert_eq!(r["state"], "timed_out");
        assert_eq!(r["cleanup"], "complete");
    }
}
