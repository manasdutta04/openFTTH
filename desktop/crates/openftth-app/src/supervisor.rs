//! The harness sidecar: spawn Node on the bundled harness (or the checkout's source), hand it a
//! fresh app token, read the port it chose, and kill it when the app closes.

use std::env;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use rand::RngCore;

pub struct Sidecar {
    pub url: String,
    pub token: String,
    child: Child,
}

impl Sidecar {
    pub fn spawn() -> Result<Sidecar> {
        let (script, cwd) = locate_harness().ok_or_else(|| anyhow!("harness not found: expected dist/index.js next to the app or harness/src/index.ts in a checkout"))?;
        let mut bytes = [0u8; 24];
        rand::rng().fill_bytes(&mut bytes);
        let token = bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
        let node = env::var("OPENFTTH_NODE").unwrap_or_else(|_| bundled_node());
        let mut cmd = Command::new(&node);
        #[cfg(windows)]
        {
            // The harness runs in the background: no console window for it.
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = cmd
            .arg("--no-warnings")
            .arg(&script)
            .current_dir(&cwd)
            .env("OPENFTTH_APP_TOKEN", &token)
            .env("OPENFTTH_PORT", "0")
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::piped())
            .spawn()
            .with_context(|| format!("spawning {node} {}", script.display()))?;
        let stderr = child.stderr.take().expect("piped stderr");
        let (tx, rx) = mpsc::channel::<String>();
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                eprintln!("{line}");
                if let Some(url) = line
                    .split("listening on ")
                    .nth(1)
                    .map(|s| s.split_whitespace().next().unwrap_or("").to_string())
                {
                    let _ = tx.send(url);
                }
            }
        });
        let url = rx
            .recv_timeout(Duration::from_secs(20))
            .map_err(|_| anyhow!("the harness did not report its port within 20 s"))?;
        Ok(Sidecar { url, token, child })
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// The installed app ships Node next to itself (`<exe dir>/node/node.exe`); a checkout uses PATH.
fn bundled_node() -> String {
    env::current_exe()
        .ok()
        .and_then(|e| e.parent().map(|d| d.join("node").join("node.exe")))
        .filter(|p| p.exists())
        .map(|p| p.display().to_string())
        .unwrap_or_else(|| "node".into())
}

/// A resource folder shipped beside the app (`<exe dir>/<name>`) or in the checkout above it.
pub fn resource_dir(name: &str) -> Option<PathBuf> {
    let exe_dir = env::current_exe().ok()?.parent()?.to_path_buf();
    let mut dir: Option<&Path> = Some(exe_dir.as_path());
    while let Some(d) = dir {
        let candidate = d.join(name);
        if candidate.is_dir() {
            return Some(candidate);
        }
        dir = d.parent();
    }
    None
}

/// Bundled layout: `<exe dir>/harness/dist/index.js`. Checkout layout: `<repo>/harness/src/index.ts`.
fn locate_harness() -> Option<(PathBuf, PathBuf)> {
    if let Ok(p) = env::var("OPENFTTH_HARNESS") {
        let p = PathBuf::from(p);
        return Some((p.clone(), p.parent()?.to_path_buf()));
    }
    let exe_dir = env::current_exe().ok()?.parent()?.to_path_buf();
    let bundled = exe_dir.join("harness").join("dist").join("index.js");
    if bundled.exists() {
        return Some((bundled, exe_dir.join("harness")));
    }
    let mut dir: Option<&Path> = Some(exe_dir.as_path());
    while let Some(d) = dir {
        let src = d.join("harness").join("src").join("index.ts");
        if src.exists() {
            return Some((src, d.join("harness")));
        }
        dir = d.parent();
    }
    None
}
