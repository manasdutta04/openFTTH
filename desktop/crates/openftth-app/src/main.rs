//! openFTTH desktop app. Spawns the harness sidecar (or attaches to one), then opens the window:
//! chat column on the left, workspace (Twin, Versions, Backstage) on the right.

mod app;
mod chat;
mod cursors;
mod dictate;
mod icons;
mod import;
mod inspector;
mod markdown;
mod state;
mod supervisor;
mod tabs;
mod theme;
mod twin;
mod welcome;
mod widgets;

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

    // wgpu's default backend list includes OpenGL, whose WGL probe thread has overflowed its
    // stack at start-up on Windows. DirectX 12 and Vulkan cover every supported Windows GPU;
    // WGPU_BACKEND still overrides this for debugging.
    let mut wgpu_setup = eframe::egui_wgpu::WgpuSetupCreateNew::without_display_handle();
    if cfg!(windows) && eframe::wgpu::Backends::from_env().is_none() {
        wgpu_setup.instance_descriptor.backends =
            eframe::wgpu::Backends::DX12 | eframe::wgpu::Backends::VULKAN;
    }
    let options = eframe::NativeOptions {
        wgpu_options: eframe::egui_wgpu::WgpuConfiguration {
            wgpu_setup: eframe::egui_wgpu::WgpuSetup::CreateNew(wgpu_setup),
            ..Default::default()
        },
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1400.0, 800.0])
            .with_min_inner_size([960.0, 600.0])
            .with_maximized(true)
            .with_decorations(false)
            .with_icon(icons::app_icon(256))
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
