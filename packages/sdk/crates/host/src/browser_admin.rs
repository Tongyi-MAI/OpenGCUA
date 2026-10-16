//! Explicit optional browser installation. SDK import and Runtime construction never install.
use mu_protocol::Error;
use serde_json::{json, Value};
use std::{path::PathBuf, time::Duration};
use tokio::process::Command;
fn cache() -> Result<PathBuf, Error> {
    std::env::var_os("MAI_UI_BROWSER_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME")
                .or_else(|| std::env::var_os("USERPROFILE"))
                .map(|h| PathBuf::from(h).join(".cache/mai-ui/browser/1.63.0"))
        })
        .ok_or_else(|| Error::new("INVALID_ARGUMENT", "Set MAI_UI_BROWSER_HOME"))
}
async fn run(program: &str, args: &[String], cwd: &std::path::Path) -> Result<String, Error> {
    let mut command = Command::new(program);
    command
        .args(args)
        .current_dir(cwd)
        .env("PLAYWRIGHT_SKIP_BROWSER_GC", "1")
        .kill_on_drop(true);
    let output = tokio::time::timeout(Duration::from_secs(120), command.output())
        .await
        .map_err(|_| {
            Error::new(
                "DEADLINE_EXCEEDED",
                "Browser setup command exceeded 120 seconds",
            )
        })?
        .map_err(|e| Error::new("UNSUPPORTED_CAPABILITY", e.to_string()))?;
    if !output.status.success() {
        return Err(Error::new(
            "SETUP_FAILED",
            String::from_utf8_lossy(&output.stderr).to_string(),
        )
        .with_details(json!({"stdout":String::from_utf8_lossy(&output.stdout)})));
    }
    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}
pub async fn admin(operation: &str, engine: &str) -> Result<Value, Error> {
    if !["all", "chromium", "firefox", "webkit"].contains(&engine) {
        return Err(Error::new("INVALID_ARGUMENT", "Unknown engine"));
    }
    let cache = cache()?;
    let node = std::env::var("MAI_UI_NODE").unwrap_or("node".into());
    if operation == "setup" {
        std::fs::create_dir_all(cache.join("dist"))
            .map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
        for (name, content) in [
            (
                "package.json",
                include_str!("../../../adapters/browser-playwright/package.json"),
            ),
            (
                "package-lock.json",
                include_str!("../../../adapters/browser-playwright/package-lock.json"),
            ),
            (
                "dist/worker.js",
                include_str!("../../../adapters/browser-playwright/dist/worker.js"),
            ),
            (
                "dist/identity.js",
                include_str!("../../../adapters/browser-playwright/dist/identity.js"),
            ),
            (
                "dist/errors.js",
                include_str!("../../../adapters/browser-playwright/dist/errors.js"),
            ),
            (
                "dist/playwright.js",
                include_str!("../../../adapters/browser-playwright/dist/playwright.js"),
            ),
            (
                "dist/playwright-contract.js",
                include_str!("../../../adapters/browser-playwright/dist/playwright-contract.js"),
            ),
            (
                "dist/input.js",
                include_str!("../../../adapters/browser-playwright/dist/input.js"),
            ),
        ] {
            std::fs::write(cache.join(name), content)
                .map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
        }
        let npm = if cfg!(windows) { "npm.cmd" } else { "npm" };
        run(
            npm,
            &["ci".into(), "--omit=dev".into(), "--ignore-scripts".into()],
            &cache,
        )
        .await?;
        let mut args = vec!["node_modules/playwright/cli.js".into(), "install".into()];
        args.extend(if engine == "all" {
            vec!["chromium".into(), "firefox".into(), "webkit".into()]
        } else {
            vec![engine.into()]
        });
        run(&node, &args, &cache).await?;
    } else if operation != "doctor" {
        return Err(Error::new("INVALID_ARGUMENT", "Use setup or doctor"));
    }
    let worker = super::browser_worker()?;
    let directory = worker
        .parent()
        .and_then(|p| p.parent())
        .ok_or_else(|| Error::new("INVALID_ARGUMENT", "Invalid worker location"))?;
    let script="const fs=require('fs');const pw=require('playwright');const version=require('playwright/package.json').version;const engines=Object.fromEntries(['chromium','firefox','webkit'].map(n=>[n,{executable:pw[n].executablePath(),installed:fs.existsSync(pw[n].executablePath())}]));console.log(JSON.stringify({node:process.version,playwright:version,engines}));";
    let result = run(&node, &["-e".into(), script.into()], directory).await?;
    let mut result: Value = serde_json::from_str(result.trim())
        .map_err(|e| Error::new("PROTOCOL_MISMATCH", e.to_string()))?;
    result["worker"] = json!(worker);
    result["state"] = json!(if result["playwright"] == "1.63.0"
        && ["chromium", "firefox", "webkit"]
            .iter()
            .filter(|n| engine == "all" || **n == engine)
            .all(|n| result["engines"][*n]["installed"] == true)
    {
        "ready"
    } else {
        "needs_setup"
    });
    Ok(result)
}
