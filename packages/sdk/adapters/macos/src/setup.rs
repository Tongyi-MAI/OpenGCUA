//! Explicit host administration. Imports and adapter construction never install software.
use mu_protocol::Error;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{io::Read, path::Path, process::Command};
pub const VERSION: &str = "0.36.0";
pub const APP: &str = "/Applications/CuaDriver.app";
pub const BINARY: &str = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver";
pub const ARCHIVE_SHA256: &str = "2a83cf9c3a2fa7ae27f3fe62e426aeca5acf76511bb14dd64d10ccb2de6aecae";
pub const URL:&str="https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.36.0/cua-driver-rs-0.36.0-darwin-universal.tar.gz";
pub fn command(program: &str, args: &[&str]) -> Result<String, Error> {
    let r = Command::new(program)
        .args(args)
        .output()
        .map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
    if !r.status.success() {
        return Err(Error::new(
            "SETUP_FAILED",
            String::from_utf8_lossy(&r.stderr),
        ));
    }
    Ok(String::from_utf8_lossy(&r.stdout).trim().to_owned())
}
pub fn verify_version() -> Result<(), Error> {
    verify_app(Path::new(APP))
}
pub fn verify_app(app: &Path) -> Result<(), Error> {
    if !cfg!(target_os = "macos") {
        return Err(Error::new(
            "UNSUPPORTED_CAPABILITY",
            "macOS adapter requires macOS",
        ));
    }
    let binary = app.join("Contents/MacOS/cua-driver");
    if !binary.is_file() {
        return Err(Error::new("SETUP_REQUIRED", "run mu setup macos"));
    }
    verify_signature(app)?;
    let version = command(
        binary
            .to_str()
            .ok_or_else(|| Error::new("INVALID_ARGUMENT", "invalid app path"))?,
        &["--version"],
    )?;
    if version != format!("cua-driver {VERSION}") {
        return Err(Error::new("VERSION_MISMATCH",format!("expected Cua Driver {VERSION}, found {version}; mu setup macos preserves existing installations unless --replace is explicit")));
    }
    Ok(())
}
fn verify_signature(app: &Path) -> Result<(), Error> {
    let app = app
        .to_str()
        .ok_or_else(|| Error::new("SETUP_FAILED", "invalid app path"))?;
    command("/usr/bin/codesign",&["--verify","--deep","--strict","-R","=anchor apple generic and identifier \"com.trycua.driver\" and certificate leaf[subject.OU] = \"YCK386LBJ7\"",app])?;
    Ok(())
}
pub fn verify_archive(archive: &Path) -> Result<(), Error> {
    let mut f =
        std::fs::File::open(archive).map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
    let mut hash = Sha256::new();
    let mut buf = [0; 65536];
    loop {
        let n = f
            .read(&mut buf)
            .map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
        if n == 0 {
            break;
        }
        hash.update(&buf[..n]);
    }
    if format!("{:x}", hash.finalize()) != ARCHIVE_SHA256 {
        return Err(Error::new(
            "CHECKSUM_MISMATCH",
            "downloaded archive did not match the pinned digest",
        ));
    }
    Ok(())
}
pub fn install(replace: bool, archive: Option<&str>) -> Result<Value, Error> {
    if !cfg!(target_os = "macos") {
        return Err(Error::new(
            "UNSUPPORTED_CAPABILITY",
            "macOS setup requires macOS",
        ));
    }
    let _lease = acquire_lease()?;
    if verify_version().is_ok() {
        command("/usr/bin/open", &["-g", "-a", APP, "--args", "serve"])?;
        return Ok(
            json!({"state":"already_installed","version":VERSION,"app":APP,"next":"mu doctor macos"}),
        );
    }
    if Path::new(APP).exists() && !replace {
        return Err(Error::new("VERSION_MISMATCH","existing CuaDriver.app preserved; use mu setup macos --replace to explicitly replace it"));
    }
    let stage = tempfile::tempdir().map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
    let downloaded = stage.path().join("driver.tar.gz");
    let archive = archive.map(Path::new).unwrap_or(&downloaded);
    if archive == downloaded {
        command(
            "/usr/bin/curl",
            &[
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--max-time",
                "300",
                URL,
                "--output",
                archive.to_str().unwrap(),
            ],
        )?;
    }
    verify_archive(archive)?;
    command(
        "/usr/bin/tar",
        &[
            "-xzf",
            archive.to_str().unwrap(),
            "-C",
            stage.path().to_str().unwrap(),
        ],
    )?;
    let src = stage.path().join(format!(
        "cua-driver-rs-{VERSION}-darwin-universal/CuaDriver.app"
    ));
    verify_signature(&src)?;
    let backup = Path::new("/Applications/.mai-ui-CuaDriver-backup.app");
    if backup.exists() {
        return Err(Error::new(
            "SETUP_FAILED",
            "previous backup exists; resolve it before replacing the app",
        ));
    }
    if Path::new(APP).exists() {
        command(BINARY, &["stop"])?;
        std::fs::rename(APP, backup).map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
    }
    let result =
        command("/usr/bin/ditto", &[src.to_str().unwrap(), APP]).and_then(|_| verify_version());
    if let Err(error) = result {
        let _ = std::fs::remove_dir_all(APP);
        if backup.exists() {
            let _ = std::fs::rename(backup, APP);
            let _ = command("/usr/bin/open", &["-g", "-a", APP, "--args", "serve"]);
        }
        return Err(error);
    }
    if backup.exists() {
        std::fs::remove_dir_all(backup).map_err(|e| Error::new("SETUP_FAILED", e.to_string()))?;
    }
    command("/usr/bin/open", &["-n", "-g", "-a", APP, "--args", "serve"])?;
    Ok(
        json!({"state":"installed","version":VERSION,"sha256":ARCHIVE_SHA256,"app":APP,"next":"mu doctor macos","permissions":"Grant Accessibility and Screen Recording to CuaDriver in System Settings. The SDK never modifies TCC records."}),
    )
}

/// Installation and runtime connections share the same endpoint lease.
pub(crate) fn acquire_lease() -> Result<std::fs::File, Error> {
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        let path =
            std::env::temp_dir().join(format!("mai-ui-macos-{}.lock", unsafe { libc::geteuid() }));
        let lease = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(path)
            .map_err(|e| Error::new("DEVICE_OFFLINE", e.to_string()))?;
        if unsafe { libc::flock(lease.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(Error::new(
                "RESOURCE_BUSY",
                "another mai-ui runtime or setup owns this Mac endpoint",
            ));
        }
        Ok(lease)
    }
    #[cfg(not(unix))]
    {
        Err(Error::new(
            "UNSUPPORTED_CAPABILITY",
            "macOS requires a Unix host",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn corrupted_archive_is_rejected_before_extraction() {
        let file = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(file.path(), b"not the pinned release").unwrap();
        assert_eq!(
            verify_archive(file.path()).unwrap_err().code,
            "CHECKSUM_MISMATCH"
        );
    }
    #[test]
    fn release_manifest_matches_compiled_pin() {
        let manifest: Value =
            serde_json::from_str(include_str!("../cua-driver.lock.json")).unwrap();
        assert_eq!(manifest["version"], VERSION);
        assert_eq!(manifest["archive"]["sha256"], ARCHIVE_SHA256);
        assert_eq!(manifest["archive"]["url"], URL);
    }
}
