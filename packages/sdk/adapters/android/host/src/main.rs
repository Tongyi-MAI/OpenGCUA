//! Owned stdio broker. One leased ADB target per process; no REPL evaluation here.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    env,
    fs::{File, OpenOptions},
    io::{self, BufRead, BufReader, Write},
    process::{Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    time::Duration,
};
fn emit(v: &Value) {
    // The Java bridge's v1 string errors stop at this Android-specific boundary.
    let mut v = v.clone();
    if let Some(message) = v["error"].as_str().map(str::to_owned) {
        let prefix = message.split(':').next().unwrap_or("");
        let code =
            if !prefix.is_empty() && prefix.chars().all(|c| c.is_ascii_uppercase() || c == '_') {
                prefix
            } else {
                "PROVIDER_ERROR"
            };
        v["error"] = serde_json::to_value(mu_protocol::Error::new(code, &message)).unwrap();
    }
    println!("{v}");
    io::stdout().flush().ok();
}
fn main() {
    if let Err(e) = run() {
        eprintln!("mu-broker: {e}");
        std::process::exit(1)
    }
}
fn execution_profile() -> Result<bool, Box<dyn std::error::Error>> {
    match env::var("MAI_UI_ANDROID_EXECUTION_PROFILE").as_deref() {
        Err(env::VarError::NotPresent) | Ok("inherit") => Ok(false),
        Ok("shell") => Ok(true),
        _ => Err("MAI_UI_ANDROID_EXECUTION_PROFILE must be inherit or shell".into()),
    }
}

fn push(serial: &str, local: &str, remote: &str) -> Result<(), Box<dyn std::error::Error>> {
    let result = Command::new("adb")
        .args(["-s", serial, "push", local, remote])
        .output()?;
    if !result.status.success() {
        return Err(String::from_utf8_lossy(&result.stderr).to_string().into());
    }
    Ok(())
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let a: Vec<_> = env::args().collect();
    if a.len() != 3 {
        return Err("usage: mu-broker SERIAL AGENT_JAR".into());
    }
    let serial = &a[1];
    let jar = &a[2];
    let restricted = execution_profile()?;
    let launcher = if restricted {
        Some(
            env::var("MAI_UI_ANDROID_LAUNCHER")
                .map_err(|_| "shell profile requires MAI_UI_ANDROID_LAUNCHER")?,
        )
    } else {
        None
    };
    let digest = format!("{:x}", Sha256::digest(serial.as_bytes()));
    let lockpath = env::temp_dir().join(format!("mai-ui-{digest}.lock"));
    let lease: File = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(lockpath)?;
    lease
        .try_lock()
        .map_err(|_| "DEVICE_BUSY: another mai-ui broker owns this endpoint")?;
    let bytes = std::fs::read(jar)?;
    let hash = format!("{:x}", Sha256::digest(&bytes));
    let remote = format!("/data/local/tmp/mai-ui-{}.jar", &hash[..16]);
    push(serial, jar, &remote)?;
    let mut command = Command::new("adb");
    command.args(["-s", serial, "shell", "-T"]);
    command.arg(format!("CLASSPATH={remote}"));
    command.arg(format!(
        "MAI_UI_DEFER_OBSERVATION={}",
        if env::var("MAI_UI_DEFER_OBSERVATION").as_deref() == Ok("1") {
            "1"
        } else {
            "0"
        }
    ));
    if let Some(launcher) = launcher {
        let hash = format!("{:x}", Sha256::digest(std::fs::read(&launcher)?));
        let remote_launcher = format!("/data/local/tmp/mu-shell-{}", &hash[..16]);
        push(serial, &launcher, &remote_launcher)?;
        let prepared = Command::new("adb")
            .args(["-s", serial, "shell", "chmod", "0555", &remote_launcher])
            .stdin(Stdio::null())
            .output()?;
        if !prepared.status.success() {
            return Err("Could not prepare shell launcher".into());
        }
        command.arg(remote_launcher);
    }
    let mut child = command
        .args(["/system/bin/app_process", "/", "dev.maiui.agent.Main"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()?;
    let input = child.stdin.take().unwrap();
    let mut output = BufReader::new(child.stdout.take().unwrap());
    let (tx, rx) = mpsc::channel::<(Value, u64)>();
    let gate = Arc::new(Mutex::new(false));
    let readgate = gate.clone();
    std::thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            let Ok(line) = line else { break };
            let Ok(q) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            let seq = q["_request_id"].as_u64().unwrap_or(0);
            let mut fenced = readgate.lock().unwrap();
            if q["op"] == "stop" {
                *fenced = true;
            }
            if tx.send((q, seq)).is_err() {
                break;
            }
        }
    });
    // Target reads run separately so a lost executor has a bounded, unknown outcome.
    let (rtx, rrx) = mpsc::channel();
    std::thread::spawn(move || loop {
        let mut line = String::new();
        match output.read_line(&mut line) {
            Ok(0) | Err(_) => break,
            _ => {
                if let Ok(v) = serde_json::from_str::<Value>(&line) {
                    if rtx.send(v).is_err() {
                        break;
                    }
                }
            }
        }
    });
    let mut input = input;
    let mut ledger = std::collections::HashMap::<String, (Value, Value)>::new();
    while let Ok((q, id)) = rx.recv() {
        let mut wire = q.clone();
        wire.as_object_mut().unwrap().remove("_request_id");
        let request: mu_protocol::Request = serde_json::from_value(q.clone())?;
        let mutation = request.is_mutation();
        let file_write = q["op"] == "file" && mutation;
        let key = if q["op"] == "action" {
            q["action_id"].as_str()
        } else if q["op"] == "shell" || file_write {
            q["command_id"].as_str()
        } else {
            None
        };
        if mutation && *gate.lock().unwrap() {
            emit(&json!({"_request_id":id,"error":"SESSION_CLOSED"}));
            continue;
        }
        if let Some(k) = key {
            if let Some((prev, result)) = ledger.get(k) {
                let mut r = if prev == &wire {
                    result.clone()
                } else {
                    json!({"error":"INVALID_ARGUMENT: reused id with different request"})
                };
                r["_request_id"] = json!(id);
                emit(&r);
                continue;
            }
        }
        let mut native_wire = wire.clone();
        let result = translate_portable_input(&mut native_wire);
        if let Err(message) = result {
            emit(
                &json!({"_request_id":id,"error":{"code":"UNSUPPORTED_CAPABILITY","message":message}}),
            );
            continue;
        }
        writeln!(input, "{native_wire}")?;
        input.flush()?;
        let timeout = q["timeout_ms"]
            .as_u64()
            .unwrap_or(1500)
            .saturating_add(15000);
        let mut result = match rrx.recv_timeout(Duration::from_millis(timeout)) {
            Ok(r) => r,
            Err(_) => {
                *gate.lock().unwrap() = true;
                child.kill().ok();
                emit(
                    &json!({"_request_id":id,"error":"ACTION_OUTCOME_UNKNOWN: executor transport lost; session fenced"}),
                );
                break;
            }
        };
        if let Some(k) = key {
            if ledger.len() >= 10000 {
                *gate.lock().unwrap() = true;
            }
            ledger.insert(k.to_string(), (wire, result.clone()));
        }
        result["_request_id"] = json!(id);
        emit(&result);
        if q["op"] == "stop" {
            break;
        }
    }
    drop(input);
    child.kill().ok();
    child.wait().ok();
    drop(lease);
    Ok(())
}

fn translate_portable_input(wire: &mut Value) -> Result<(), &'static str> {
    if wire["action"] == "press_key" && wire["key"].is_string() {
        if wire["modifiers"].as_array().is_some_and(|m| !m.is_empty()) {
            return Err("Android key modifiers are unsupported");
        }
        let code = match wire["key"].as_str().unwrap() {
            "enter" | "return" => 66,
            "tab" => 61,
            "escape" | "esc" => 111,
            "backspace" => 67,
            "delete" => 112,
            "space" => 62,
            "up" => 19,
            "down" => 20,
            "left" => 21,
            "right" => 22,
            "home" => 122,
            "end" => 123,
            _ => return Err("unsupported Android key"),
        };
        wire["key_code"] = json!(code);
        wire.as_object_mut().unwrap().remove("key");
        wire.as_object_mut().unwrap().remove("modifiers");
    }
    if wire["action"] == "select_text" && wire["offset_unit"] == "code_points" {
        let text = wire["selection_text"]
            .as_str()
            .ok_or("missing selection text")?
            .to_string();
        for k in ["start", "end"] {
            let offset = wire[k].as_u64().ok_or("invalid selection offset")? as usize;
            if offset > text.chars().count() {
                return Err("invalid selection offset");
            }
            wire[k] = json!(text
                .chars()
                .take(offset)
                .map(char::len_utf16)
                .sum::<usize>());
        }
        wire.as_object_mut().unwrap().remove("selection_text");
        wire.as_object_mut().unwrap().remove("offset_unit");
    }
    Ok(())
}

#[cfg(test)]
mod portable_input_tests {
    use super::*;
    #[test]
    fn translates_unicode_and_keys_at_provider_boundary() {
        let mut q = json!({"action":"select_text","start":1,"end":2,"selection_text":"a😀b","offset_unit":"code_points"});
        translate_portable_input(&mut q).unwrap();
        assert_eq!(q["end"], 3);
        assert!(q.get("selection_text").is_none());
        let mut q = json!({"action":"press_key","key":"enter","modifiers":[]});
        translate_portable_input(&mut q).unwrap();
        assert_eq!(q["key_code"], 66);
        assert!(translate_portable_input(
            &mut json!({"action":"press_key","key":"enter","modifiers":["cmd"]})
        )
        .is_err());
    }
}
