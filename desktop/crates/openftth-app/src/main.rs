//! openFTTH desktop app. Spawns the harness sidecar (or attaches to one), then opens the window:
//! chat column on the left, workspace (Twin, Versions, Backstage) on the right.

mod app;
mod supervisor;
mod twin;

use std::env;

use anyhow::{Context, Result};

fn main() -> Result<()> {
    let args: Vec<String> = env::args().skip(1).collect();
    let harness_url = flag(&args, "--harness-url");
    let token = flag(&args, "--token");

    let (client, _sidecar) = match (harness_url, token) {
        (Some(url), Some(token)) => (openftth_client::Client::new(url, token), None),
        (Some(_), None) => anyhow::bail!("--harness-url needs --token"),
        _ => {
            let sidecar = supervisor::Sidecar::spawn().context("starting the harness sidecar")?;
            (
                openftth_client::Client::new(sidecar.url.clone(), sidecar.token.clone()),
                Some(sidecar),
            )
        }
    };

    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1480.0, 920.0])
            .with_min_inner_size([960.0, 600.0])
            .with_title("openFTTH"),
        ..Default::default()
    };
    eframe::run_native(
        "openFTTH",
        options,
        Box::new(move |cc| Ok(Box::new(app::OpenFtthApp::new(cc, client)))),
    )
    .map_err(|e| anyhow::anyhow!("{e}"))?;
    Ok(())
}

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1).cloned())
}
