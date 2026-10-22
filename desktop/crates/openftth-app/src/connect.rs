//! Crew → Connect other AI apps: turn the public MCP server on or off and copy a ready config
//! for Claude Desktop, Claude Code, Cursor or any MCP client that speaks HTTP.

use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use egui::{vec2, Align2, Ui};
use openftth_client::Client;
use serde_json::Value;

use crate::state::AppState;
use crate::theme::{Palette, Weight};
use crate::widgets::{self, caps, text, Kind};

pub struct ConnectWindow {
    pub open: bool,
    info: Option<Value>,
    pending: Option<Receiver<Result<Value, String>>>,
    error: Option<String>,
    copied: Option<&'static str>,
}

impl ConnectWindow {
    pub fn new() -> Self {
        Self {
            open: false,
            info: None,
            pending: None,
            error: None,
            copied: None,
        }
    }

    /// Open the window and read the current settings from the harness.
    pub fn show_window(&mut self, client: &Client) {
        self.open = true;
        self.copied = None;
        let c = client.clone();
        self.run(move || c.mcp_info());
    }

    pub fn show(&mut self, ctx: &egui::Context, st: &AppState) {
        if !self.open {
            return;
        }
        self.poll();
        if self.pending.is_some() {
            ctx.request_repaint_after(Duration::from_millis(120));
        }
        let p = st.palette;
        let mut open = true;
        egui::Window::new("Connect other AI apps")
            .open(&mut open)
            .collapsible(false)
            .resizable(false)
            .anchor(Align2::CENTER_CENTER, vec2(0.0, 0.0))
            .show(ctx, |ui| {
                ui.set_width(520.0);
                ui.add(
                    egui::Label::new(text(
                        "Let Claude Desktop, Claude Code, Cursor or any MCP client open projects, design automatically, ask the crew and read the deliverables. It listens on this computer only, behind its own token. Risky steps still open an approval card here.",
                        12.0,
                        Weight::Regular,
                        p.text2,
                    ))
                    .wrap(),
                );
                ui.add_space(10.0);
                let busy = self.pending.is_some();
                let enabled = self.info.as_ref().is_some_and(|i| i["enabled"] == true);
                ui.horizontal(|ui| {
                    widgets::dot(ui, if enabled { p.green } else { p.text3 }, 4.0);
                    ui.label(text(
                        if enabled { "On" } else { "Off" },
                        12.5,
                        Weight::Semibold,
                        p.text,
                    ));
                    ui.add_space(8.0);
                    let label = if enabled { "Turn off" } else { "Turn on" };
                    let kind = if enabled { Kind::Quiet } else { Kind::Prominent };
                    if widgets::button_enabled(ui, &p, label, kind, !busy && self.info.is_some())
                        .clicked()
                    {
                        let c = st.client.clone();
                        self.run(move || c.set_mcp(Some(!enabled), false));
                    }
                    if enabled
                        && widgets::button_enabled(ui, &p, "New token", Kind::Ghost, !busy)
                            .on_hover_text("Configs for Claude Desktop, Claude Code and Cursor keep working; HTTP configs need the new token.")
                            .clicked()
                    {
                        let c = st.client.clone();
                        self.run(move || c.set_mcp(None, true));
                    }
                    if busy {
                        ui.spinner();
                    }
                });
                if let Some(e) = &self.error {
                    ui.add_space(6.0);
                    ui.add(egui::Label::new(text(e, 12.0, Weight::Regular, p.red)).wrap());
                }
                if let (true, Some(info)) = (enabled, self.info.clone()) {
                    let s = &info["snippets"];
                    ui.add_space(10.0);
                    self.snippet(ui, &p, "Claude Desktop", "Add to claude_desktop_config.json (Settings → Developer → Edit config), then restart Claude Desktop.", s["claude_desktop"].as_str());
                    self.snippet(ui, &p, "Claude Code", "Run once in a terminal.", s["claude_code"].as_str());
                    self.snippet(ui, &p, "Cursor", "Add to .cursor/mcp.json in your project or home folder.", s["cursor"].as_str());
                    self.snippet(ui, &p, "Other apps (HTTP)", "Streamable HTTP with a bearer token. The port changes each time openFTTH starts; the configs above follow it.", s["http"].as_str());
                }
            });
        self.open = open;
    }

    fn snippet(
        &mut self,
        ui: &mut Ui,
        p: &Palette,
        title: &'static str,
        hint: &str,
        body: Option<&str>,
    ) {
        let Some(body) = body else {
            return;
        };
        ui.horizontal(|ui| {
            ui.label(caps(title, p.text3));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let label = if self.copied == Some(title) {
                    "Copied"
                } else {
                    "Copy"
                };
                if widgets::button(ui, p, label, Kind::Ghost).clicked() {
                    ui.ctx().copy_text(body.to_string());
                    self.copied = Some(title);
                }
            });
        });
        ui.add(egui::Label::new(text(hint, 11.5, Weight::Regular, p.text3)).wrap());
        ui.add_space(2.0);
        egui::Frame::new()
            .fill(p.bg2)
            .corner_radius(crate::theme::R as f32)
            .inner_margin(egui::Margin::same(6))
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.add(egui::Label::new(widgets::mono_text(body, 10.5, p.text2)).wrap());
            });
        ui.add_space(8.0);
    }

    fn run(
        &mut self,
        f: impl FnOnce() -> Result<Value, openftth_client::ClientError> + Send + 'static,
    ) {
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = tx.send(f().map_err(|e| e.to_string()));
        });
        self.error = None;
        self.pending = Some(rx);
    }

    fn poll(&mut self) {
        let Some(rx) = &self.pending else {
            return;
        };
        let out = match rx.try_recv() {
            Ok(o) => o,
            Err(mpsc::TryRecvError::Empty) => return,
            Err(mpsc::TryRecvError::Disconnected) => Err("the request was lost".into()),
        };
        self.pending = None;
        match out {
            Ok(v) => {
                self.info = Some(v);
                self.copied = None;
            }
            Err(e) => self.error = Some(e),
        }
    }
}
