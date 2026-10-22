//! Connect AI apps: openFTTH's MCP link. One local URL any MCP client can add, a switch to turn
//! it on, and one-click installs for Claude Desktop, Claude Code, Cursor and VS Code.

use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use egui::{pos2, vec2, Align2, CursorIcon, Rect, Sense, Ui};
use serde_json::{json, Value};

use crate::icons::{self, Icon};
use crate::state::AppState;
use crate::theme::{font, Palette, Weight, R};
use crate::widgets::{self, caps, text, Kind};

enum Job {
    Settings,
    ClaudeBundle,
}

pub struct ConnectWindow {
    pub open: bool,
    pending: Option<(Job, Receiver<Result<Value, String>>)>,
    error: Option<String>,
    note: Option<String>,
}

impl ConnectWindow {
    pub fn new() -> Self {
        Self {
            open: false,
            pending: None,
            error: None,
            note: None,
        }
    }

    /// Open the window and read the link's current state from the harness.
    pub fn show_window(&mut self, st: &AppState) {
        self.open = true;
        self.note = None;
        let c = st.client.clone();
        self.run(Job::Settings, move || c.mcp_info());
    }

    pub fn show(&mut self, ctx: &egui::Context, st: &mut AppState) {
        if !self.open {
            return;
        }
        self.poll(st);
        if self.pending.is_some() {
            ctx.request_repaint_after(Duration::from_millis(120));
        }
        let p = st.palette;
        let info = st.mcp.clone().unwrap_or(Value::Null);
        let on = info["enabled"] == true;
        let listening = info["listening"] == true;
        let link = info["link"]
            .as_str()
            .unwrap_or("http://127.0.0.1:3920/mcp")
            .to_string();
        let busy = self.pending.is_some();
        let mut open = true;
        egui::Window::new("Connect AI apps")
            .open(&mut open)
            .collapsible(false)
            .resizable(false)
            .anchor(Align2::CENTER_CENTER, vec2(0.0, 0.0))
            .show(ctx, |ui| {
                ui.set_width(540.0);
                ui.horizontal(|ui| {
                    ui.vertical(|ui| {
                        ui.label(text("openFTTH MCP link", 15.0, Weight::Semibold, p.text));
                        ui.add(
                            egui::Label::new(text(
                                "Add this link to an AI app and it can open projects, design automatically, check, trace and pull the deliverables. Risky steps still need your approval here.",
                                12.0,
                                Weight::Regular,
                                p.text2,
                            ))
                            .wrap(),
                        );
                    });
                });
                ui.add_space(12.0);

                // The switch and what it means right now.
                ui.horizontal(|ui| {
                    if switch(ui, &p, on, !busy && !info.is_null()).clicked() {
                        let c = st.client.clone();
                        self.run(Job::Settings, move || c.set_mcp(&json!({ "enabled": !on })));
                    }
                    ui.add_space(6.0);
                    let (dot, line) = match (on, listening, info["error"].as_str()) {
                        (true, true, _) => (p.green, format!("On · listening on 127.0.0.1:{}", info["port"])),
                        (true, false, Some(e)) => (p.red, e.to_string()),
                        (true, false, None) => (p.amber, "Starting…".to_string()),
                        _ => (p.text3, "Off · AI apps cannot reach openFTTH".to_string()),
                    };
                    widgets::dot(ui, dot, 3.5);
                    ui.label(text(line, 12.0, Weight::Medium, if on { p.text } else { p.text3 }));
                    if busy {
                        ui.spinner();
                    }
                });
                ui.add_space(12.0);

                // The one link.
                ui.label(caps("Link", p.text3));
                ui.add_space(4.0);
                ui.horizontal(|ui| {
                    egui::Frame::new()
                        .fill(p.bg2)
                        .stroke(egui::Stroke::new(1.0, p.line))
                        .corner_radius(R as f32)
                        .inner_margin(egui::Margin::symmetric(10, 7))
                        .show(ui, |ui| {
                            ui.set_width(430.0);
                            ui.add(
                                egui::Label::new(widgets::mono_text(&link, 12.5, if on { p.text } else { p.text3 }))
                                    .truncate(),
                            );
                        });
                    if widgets::button_enabled(ui, &p, "Copy", Kind::Prominent, on).clicked() {
                        ui.ctx().copy_text(link.clone());
                        self.note = Some("Link copied. Paste it where the app asks for an MCP server URL.".into());
                    }
                });
                ui.add_space(14.0);

                // One click per app.
                ui.label(caps("Add to", p.text3));
                ui.add_space(4.0);
                let installs = info["installs"].clone();
                let cards: [(&str, &str, &str); 4] = [
                    ("Claude Desktop", "One-click extension", "Install"),
                    ("Claude Code", "One terminal command", "Copy"),
                    ("Cursor", "Adds the server in Cursor", "Open"),
                    ("VS Code", "Adds the server in VS Code", "Open"),
                ];
                egui::Grid::new("connect-apps").num_columns(2).spacing(vec2(10.0, 10.0)).show(ui, |ui| {
                    for (i, (name, hint, action)) in cards.iter().enumerate() {
                        if app_card(ui, &p, name, hint, action, on && !busy).clicked() {
                            match *name {
                                "Claude Desktop" => {
                                    let c = st.client.clone();
                                    self.run(Job::ClaudeBundle, move || c.mcp_claude_bundle());
                                }
                                "Claude Code" => {
                                    ui.ctx().copy_text(installs["claude_code"].as_str().unwrap_or_default().to_string());
                                    self.note = Some("Command copied. Run it once in a terminal.".into());
                                }
                                "Cursor" => {
                                    open_external(installs["cursor"].as_str().unwrap_or_default());
                                    self.note = Some("Cursor asks to add openFTTH; confirm there.".into());
                                }
                                _ => {
                                    open_external(installs["vscode"].as_str().unwrap_or_default());
                                    self.note = Some("VS Code asks to add openFTTH; confirm there.".into());
                                }
                            }
                        }
                        if i % 2 == 1 {
                            ui.end_row();
                        }
                    }
                });
                if let Some(n) = &self.note {
                    ui.add_space(8.0);
                    ui.horizontal(|ui| {
                        icons::paint(ui.painter(), icons::square(ui.cursor().min + vec2(7.0, 8.0), 13.0), Icon::Check, p.green);
                        ui.add_space(18.0);
                        ui.add(egui::Label::new(text(n, 12.0, Weight::Regular, p.text2)).wrap());
                    });
                }
                if let Some(e) = &self.error {
                    ui.add_space(8.0);
                    ui.add(egui::Label::new(text(e, 12.0, Weight::Regular, p.red)).wrap());
                }
                ui.add_space(12.0);
                widgets::hairline(ui, &p);
                ui.add_space(8.0);

                // Security: who can use the link.
                ui.horizontal(|ui| {
                    let mut key = info["requireKey"] == true;
                    if ui
                        .add_enabled(!busy && !info.is_null(), egui::Checkbox::new(&mut key, text("Require a key", 12.0, Weight::Medium, p.text)))
                        .changed()
                    {
                        let c = st.client.clone();
                        self.run(Job::Settings, move || c.set_mcp(&json!({ "requireKey": key })));
                    }
                    if info["requireKey"] == true
                        && widgets::button_enabled(ui, &p, "New key", Kind::Ghost, !busy)
                            .on_hover_text("Apps added with the old link must be added again; Claude Desktop follows on its own.")
                            .clicked()
                    {
                        let c = st.client.clone();
                        self.run(Job::Settings, move || c.set_mcp(&json!({ "rotate": true })));
                    }
                });
                ui.add(
                    egui::Label::new(text(
                        "The link only answers on this computer, and web pages cannot reach it. With a key, the link itself carries it, so it is still one link to paste. ChatGPT and other web-based assistants connect over the internet and cannot reach a link on this computer.",
                        11.5,
                        Weight::Regular,
                        p.text3,
                    ))
                    .wrap(),
                );
            });
        self.open = open;
    }

    fn run(
        &mut self,
        job: Job,
        f: impl FnOnce() -> Result<Value, openftth_client::ClientError> + Send + 'static,
    ) {
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = tx.send(f().map_err(|e| e.to_string()));
        });
        self.error = None;
        self.pending = Some((job, rx));
    }

    fn poll(&mut self, st: &mut AppState) {
        let Some((_, rx)) = &self.pending else {
            return;
        };
        let out = match rx.try_recv() {
            Ok(o) => o,
            Err(mpsc::TryRecvError::Empty) => return,
            Err(mpsc::TryRecvError::Disconnected) => Err("the request was lost".into()),
        };
        let (job, _) = self.pending.take().expect("pending job");
        match (job, out) {
            (_, Err(e)) => self.error = Some(e),
            (Job::Settings, Ok(v)) => st.mcp = Some(v),
            (Job::ClaudeBundle, Ok(v)) => {
                let path = v["path"].as_str().unwrap_or_default().to_string();
                open_external(&path);
                self.note = Some("Claude Desktop opens an install prompt for openFTTH. If Windows asks which app to use, Claude Desktop is not installed yet.".into());
            }
        }
    }
}

/// A pill switch.
fn switch(ui: &mut Ui, p: &Palette, on: bool, enabled: bool) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(
        vec2(34.0, 20.0),
        if enabled {
            Sense::click()
        } else {
            Sense::hover()
        },
    );
    let t = ui.ctx().animate_bool(resp.id, on);
    let track = if on { p.accent } else { p.bg3 };
    ui.painter().rect_filled(rect, 10.0, track);
    let x = egui::lerp(rect.left() + 10.0..=rect.right() - 10.0, t);
    ui.painter()
        .circle_filled(pos2(x, rect.center().y), 7.0, egui::Color32::WHITE);
    if enabled {
        resp.on_hover_cursor(CursorIcon::PointingHand)
    } else {
        resp
    }
}

/// One app: its name, what the click does, and the action on the right.
fn app_card(
    ui: &mut Ui,
    p: &Palette,
    name: &str,
    hint: &str,
    action: &str,
    enabled: bool,
) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(
        vec2(265.0, 50.0),
        if enabled {
            Sense::click()
        } else {
            Sense::hover()
        },
    );
    let hovered = enabled && resp.hovered();
    let painter = ui.painter();
    painter.rect_filled(rect, R as f32, if hovered { p.bg3 } else { p.bg2 });
    painter.rect_stroke(
        rect,
        R as f32,
        egui::Stroke::new(1.0, p.line),
        egui::StrokeKind::Inside,
    );
    let fg = if enabled { p.text } else { p.text3 };
    painter.text(
        pos2(rect.left() + 12.0, rect.top() + 17.0),
        Align2::LEFT_CENTER,
        name,
        font(13.0, Weight::Semibold),
        fg,
    );
    painter.text(
        pos2(rect.left() + 12.0, rect.bottom() - 15.0),
        Align2::LEFT_CENTER,
        hint,
        font(11.0, Weight::Regular),
        p.text3,
    );
    let a = painter.layout_no_wrap(
        action.to_string(),
        font(11.5, Weight::Medium),
        if enabled { p.accent } else { p.text3 },
    );
    let ar = Rect::from_min_size(
        pos2(
            rect.right() - a.size().x - 12.0,
            rect.center().y - a.size().y / 2.0,
        ),
        a.size(),
    );
    painter.galley(ar.min, a, p.accent);
    if enabled {
        resp.on_hover_cursor(CursorIcon::PointingHand)
    } else {
        resp
    }
}

/// Hand a URL (an app's install deeplink) or a file to Windows to open with its app.
pub fn open_external(target: &str) {
    if target.is_empty() {
        return;
    }
    #[cfg(target_os = "windows")]
    {
        let _ = std::process::Command::new("rundll32")
            .args(["url.dll,FileProtocolHandler", target])
            .spawn();
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = target;
    }
}
