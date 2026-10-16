//! Optional local X11 worker. Only explicit setup installs dependencies.
use super::*;
use std::{path::Path, time::Duration};
use tokio::process::Command;

const ASSETS: &[(&str, &str)] = &[
    (
        "worker.py",
        include_str!("../../../adapters/linux/mai_ui_linux/worker.py"),
    ),
    (
        "common.py",
        include_str!("../../../adapters/linux/mai_ui_linux/common.py"),
    ),
    (
        "x11.py",
        include_str!("../../../adapters/linux/mai_ui_linux/x11.py"),
    ),
    (
        "ax.py",
        include_str!("../../../adapters/linux/mai_ui_linux/ax.py"),
    ),
    (
        "services.py",
        include_str!("../../../adapters/linux/mai_ui_linux/services.py"),
    ),
    (
        "requirements.lock",
        include_str!("../../../adapters/linux/requirements.lock"),
    ),
];
fn home() -> Result<PathBuf, Error> {
    std::env::var_os("MAI_UI_LINUX_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|p| PathBuf::from(p).join(".cache/mai-ui/linux/0.3"))
        })
        .ok_or_else(|| err("INVALID_ARGUMENT"))
}
fn worker() -> Result<PathBuf, Error> {
    let p = std::env::var_os("MAI_UI_LINUX_WORKER")
        .map(PathBuf::from)
        .unwrap_or(home()?.join("worker.py"));
    if !p.is_file() {
        return Err(Error::new(
            "UNSUPPORTED_CAPABILITY",
            "Linux adapter not installed; run mu setup linux",
        ));
    }
    Ok(p)
}
fn interpreter(q: &Value) -> Result<String, Error> {
    Ok(q["python"]
        .as_str()
        .map(str::to_owned)
        .or_else(|| std::env::var("MAI_UI_LINUX_PYTHON").ok())
        .unwrap_or(home()?.join("venv/bin/python").to_string_lossy().into()))
}
fn environment(q: &Value) -> Result<BTreeMap<String, String>, Error> {
    let mut env = BTreeMap::new();
    for (key, var) in [("display", "DISPLAY"), ("xauthority", "XAUTHORITY")] {
        if !q[key].is_null() {
            let v = s(q, key)?;
            if v.is_empty() || v.contains('\0') {
                return Err(err("INVALID_ARGUMENT"));
            }
            env.insert(var.into(), v.into());
            if key == "display" {
                env.insert("MAI_UI_EXPLICIT_X11".into(), "1".into());
            }
        }
    }
    Ok(env)
}
impl Host {
    pub(super) async fn add_linux(self: &Arc<Self>, q: &Value) -> Result<Value, Error> {
        let name = s(q, "name")?;
        if name.trim().is_empty() {
            return Err(err("INVALID_ARGUMENT"));
        }
        if !q["python"].is_null() && (s(q, "python")?.is_empty() || s(q, "python")?.contains('\0'))
        {
            return Err(err("INVALID_ARGUMENT"));
        }
        let _guard = self.creation.lock().await;
        let p = mu_client::process::ProcessClient::spawn(
            &interpreter(q)?,
            &[worker()?.to_string_lossy().into()],
            &environment(q)?,
        )?;
        let result=async {
            let reply=tokio::time::timeout(Duration::from_secs(10),p.request(serde_json::from_value(json!({"op":"describe","timeout_ms":9000})).unwrap())).await.map_err(|_|err("DEADLINE_EXCEEDED"))??;
            reply.validate()?;
            if let Some(e)=reply.error { return Err(e); }
            let description=reply.result.unwrap();
            let id=s(&description,"desktop_id")?.to_string();
            if !id.starts_with("linux:x11:") { return Err(err("PROTOCOL_MISMATCH")); }
            let info=json!({"id":id,"name":name,"kind":"device","platform":"linux","provider":"linux.x11"});
            let info=self.runtime.add(info,description,p.clone())?;
            let mut events=p.subscribe(); let weak=Arc::downgrade(self); let event_id=id.clone();
            tokio::spawn(async move {
                loop {
                    let event=events.recv().await;
                    let Some(host)=weak.upgrade() else {break};
                    match event {
                        Ok(v)=>host.runtime.provider_event(&event_id,json!({"type":v.event,"resource_id":event_id,"data":v.data})),
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(n))=>host.runtime.provider_event(&event_id,json!({"type":"event_gap","resource_id":event_id,"data":{"dropped":n}})),
                        Err(_)=>break,
                    }
                }
            });
            let mut closed=p.closed_signal(); let weak=Arc::downgrade(self);
            tokio::spawn(async move {
                while !*closed.borrow_and_update() {if closed.changed().await.is_err(){break}}
                if let Some(host)=weak.upgrade() {if let Ok(e)=host.runtime.endpoint(&id) {
                    e.offline.store(true,Ordering::SeqCst);
                    host.runtime.provider_event(&id,json!({"type":"resource_offline","resource_id":id,"data":{"reason":"worker_exited"}}));
                }}
            });
            Ok(info)
        }.await;
        if result.is_err() {
            let _ = p.close().await;
        }
        result
    }
}
async fn command(program: &str, args: &[String], cwd: &Path) -> Result<(), Error> {
    let output = tokio::time::timeout(
        Duration::from_secs(120),
        Command::new(program)
            .args(args)
            .current_dir(cwd)
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| err("DEADLINE_EXCEEDED"))?
    .map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
    if !output.status.success() {
        return Err(Error::new(
            "SETUP_FAILED",
            String::from_utf8_lossy(&output.stderr).to_string(),
        ));
    }
    Ok(())
}
pub async fn admin(q: &Value) -> Result<Value, Error> {
    let operation = s(q, "operation")?;
    if !matches!(operation, "setup" | "doctor") {
        return Err(err("INVALID_ARGUMENT"));
    }
    if operation == "setup" {
        if !cfg!(target_os = "linux") {
            return Err(Error::new(
                "UNSUPPORTED_CAPABILITY",
                "Run Linux setup inside Ubuntu",
            ));
        }
        let root = home()?;
        std::fs::create_dir_all(&root).map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
        for (name, content) in ASSETS {
            std::fs::write(root.join(name), content)
                .map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
        }
        let python = q["python"].as_str().unwrap_or("/usr/bin/python3");
        command(
            python,
            &[
                "-m".into(),
                "venv".into(),
                "--system-site-packages".into(),
                "venv".into(),
            ],
            &root,
        )
        .await?;
        command(
            &root.join("venv/bin/python").to_string_lossy(),
            &[
                "-m".into(),
                "pip".into(),
                "install".into(),
                "--requirement".into(),
                "requirements.lock".into(),
            ],
            &root,
        )
        .await?;
    }
    let python = if operation == "setup" {
        home()?
            .join("venv/bin/python")
            .to_string_lossy()
            .into_owned()
    } else {
        interpreter(q)?
    };
    let path = match worker() {
        Ok(p) => p,
        Err(e) => {
            return Ok(
                json!({"state":"needs_setup","error":e.to_string(),"system_packages":"python3-venv python3-pyatspi python3-gi gir1.2-atspi-2.0 libxcb1 xclip"}),
            )
        }
    };
    let result = tokio::time::timeout(
        Duration::from_secs(15),
        Command::new(python)
            .arg(path)
            .arg("--doctor")
            .envs(environment(q)?)
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| err("DEADLINE_EXCEEDED"))?
    .map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
    serde_json::from_slice(&result.stdout).map_err(|_| {
        Error::new(
            "PROTOCOL_MISMATCH",
            String::from_utf8_lossy(&result.stderr).to_string(),
        )
    })
}
