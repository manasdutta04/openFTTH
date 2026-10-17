//! The window: chat column (messages, tool chips, approval cards, stage) on the left and the
//! workspace tabs (Twin, Versions, Backstage) on the right. Frames arrive from the harness SSE
//! subscription on a channel and are folded into this state on every repaint.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use egui::{Color32, RichText};
use openftth_client::{Client, DesignContext, Frame};
use openftth_core::{Design, StreetLayer};
use serde_json::Value;

use crate::twin::TwinView;

#[derive(PartialEq, Clone, Copy)]
enum Tab {
    Twin,
    Versions,
    Backstage,
}

#[derive(Clone)]
struct ChatMessage {
    id: String,
    role: String,
    folk: Option<String>,
    kind: String,
    text: String,
}

#[derive(Clone)]
struct ApprovalCard {
    id: String,
    folk: String,
    tool: String,
    summary: String,
    state: String,
}

pub struct OpenFtthApp {
    client: Client,
    rx: Receiver<Frame>,
    connected: bool,
    crew: Vec<(String, String, String)>, // key, name, title
    folk_states: BTreeMap<String, String>,
    messages: Vec<ChatMessage>,
    approvals: Vec<ApprovalCard>,
    tool_chips: Vec<String>,
    stage: Option<String>,
    project: Option<Value>,
    project_dir: Option<PathBuf>,
    design: Option<Design>,
    streets: Option<StreetLayer>,
    changed: Vec<String>,
    versions: Vec<Value>,
    backstage: Vec<Value>,
    input: String,
    selection: Vec<String>,
    cursor: Option<(f64, f64)>,
    tab: Tab,
    twin: TwinView,
    // open-project form
    area_path: String,
    osm_path: String,
    rulebook: String,
    status: String,
}

impl OpenFtthApp {
    pub fn new(cc: &eframe::CreationContext<'_>, client: Client) -> Self {
        cc.egui_ctx.set_visuals(egui::Visuals::light());
        let (tx, rx) = mpsc::channel();
        client.subscribe(tx);
        let crew = client
            .crew()
            .ok()
            .and_then(|v| v["members"].as_array().cloned())
            .map(|ms| {
                ms.iter()
                    .map(|m| (s(&m["key"]), s(&m["name"]), s(&m["title"])))
                    .collect()
            })
            .unwrap_or_default();
        let repo_fixture = repo_root().map(|r| r.join("fixtures").join("gent"));
        let mut app = Self {
            client,
            rx,
            connected: false,
            crew,
            folk_states: BTreeMap::new(),
            messages: Vec::new(),
            approvals: Vec::new(),
            tool_chips: Vec::new(),
            stage: None,
            project: None,
            project_dir: None,
            design: None,
            streets: None,
            changed: Vec::new(),
            versions: Vec::new(),
            backstage: Vec::new(),
            input: String::new(),
            selection: Vec::new(),
            cursor: None,
            tab: Tab::Twin,
            twin: TwinView::default(),
            area_path: repo_fixture
                .as_ref()
                .map(|p| p.join("area.geojson").display().to_string())
                .unwrap_or_default(),
            osm_path: repo_fixture
                .as_ref()
                .map(|p| p.join("gent.osm").display().to_string())
                .unwrap_or_default(),
            rulebook: "belgium".into(),
            status: String::new(),
        };
        app.refresh_project();
        app
    }

    fn refresh_project(&mut self) {
        match self.client.project() {
            Ok(v) if v["open"].as_bool() == Some(true) => {
                self.project_dir = v["dir"].as_str().map(PathBuf::from);
                if let Some(stage) = v["summary"]["stage"].as_str() {
                    self.stage = Some(stage.to_string());
                }
                if let Some(folks) = v["folks"].as_object() {
                    for (k, st) in folks {
                        self.folk_states.insert(k.clone(), s(st));
                    }
                }
                self.project = Some(v);
                self.reload_design();
                self.reload_messages();
                self.refresh_versions();
            }
            Ok(_) => {
                self.project = None;
            }
            Err(e) => self.status = format!("harness: {e}"),
        }
    }

    fn reload_design(&mut self) {
        let Some(dir) = &self.project_dir else { return };
        match Design::read_dir(&dir.join("design")) {
            Ok(d) => {
                self.design = Some(d);
            }
            Err(e) => self.status = format!("design: {e}"),
        }
        let streets = dir.join("twin").join("streets.geojson");
        if streets.exists() {
            self.streets = StreetLayer::read(&streets).ok();
        }
        self.twin.refit();
    }

    fn reload_messages(&mut self) {
        if let Ok(Value::Array(ms)) = self.client.messages(0) {
            self.messages = ms.iter().map(message_from_value).collect();
        }
    }

    fn refresh_versions(&mut self) {
        match self.client.versions(50) {
            Ok(Value::Array(v)) => self.versions = v,
            Ok(_) => self.versions.clear(),
            Err(e) => self.status = format!("versions: {e}"),
        }
    }

    fn refresh_backstage(&mut self) {
        if let Ok(Value::Array(v)) = self.client.backstage(300) {
            self.backstage = v;
        }
    }

    fn drain_frames(&mut self) {
        while let Ok(f) = self.rx.try_recv() {
            self.apply(f);
        }
    }

    fn apply(&mut self, f: Frame) {
        match f.event.as_str() {
            "hello" => {
                self.connected = true;
                if f.data["resumed"].as_bool() != Some(true) {
                    self.refresh_project();
                }
            }
            "disconnected" => self.connected = false,
            "message" => {
                let m = message_from_value(&f.data);
                if let Some(existing) = self
                    .messages
                    .iter_mut()
                    .find(|x| x.id == m.id && !m.id.is_empty())
                {
                    *existing = m;
                } else {
                    self.messages.push(m);
                }
            }
            "message.patch" => {
                if let (Some(id), Some(delta)) =
                    (f.data["id"].as_str(), f.data["textDelta"].as_str())
                {
                    if let Some(m) = self.messages.iter_mut().find(|m| m.id == id) {
                        m.text.push_str(delta);
                    }
                }
            }
            "tool.activity" => {
                let chip = format!(
                    "{} · {} {}",
                    s(&f.data["folk"]),
                    s(&f.data["tool"]),
                    s(&f.data["state"])
                );
                self.tool_chips.push(chip);
                if self.tool_chips.len() > 6 {
                    self.tool_chips.remove(0);
                }
            }
            "approval" => {
                let card = ApprovalCard {
                    id: s(&f.data["id"]),
                    folk: s(&f.data["folk"]),
                    tool: s(&f.data["tool"]),
                    summary: s(&f.data["summary"]),
                    state: s(&f.data["state"]),
                };
                if let Some(existing) = self.approvals.iter_mut().find(|a| a.id == card.id) {
                    *existing = card;
                } else {
                    self.approvals.push(card);
                }
            }
            "design.version" => {
                self.changed = f.data["changes"]
                    .as_object()
                    .map(|_| Vec::new())
                    .unwrap_or_default();
                self.reload_design();
                self.refresh_versions();
                if let Some(diff) = self
                    .versions
                    .get(1)
                    .and_then(|v| v["version"].as_str())
                    .map(str::to_string)
                {
                    if let Some(head) = self
                        .versions
                        .first()
                        .and_then(|v| v["version"].as_str())
                        .map(str::to_string)
                    {
                        if let Ok(d) = self.client.diff(&diff, &head) {
                            let mut ch: Vec<String> = Vec::new();
                            for k in ["added", "modified"] {
                                if let Some(arr) = d[k].as_array() {
                                    ch.extend(
                                        arr.iter().filter_map(|v| v.as_str().map(str::to_string)),
                                    );
                                }
                            }
                            self.changed = ch;
                        }
                    }
                }
            }
            "design.stage" => self.stage = f.data["stage"].as_str().map(str::to_string),
            "folk" => {
                self.folk_states
                    .insert(s(&f.data["key"]), s(&f.data["state"]));
            }
            "project" => {
                if f.data["engine"].as_str() == Some("ready") {
                    self.refresh_project();
                }
            }
            "runtime" => {
                self.backstage.push(f.data);
                if self.backstage.len() > 500 {
                    self.backstage.remove(0);
                }
            }
            _ => {}
        }
    }

    fn send(&mut self) {
        let text = self.input.trim().to_string();
        if text.is_empty() {
            return;
        }
        let ctx = DesignContext {
            selection: self.selection.clone(),
            cursor: self.cursor.map(|(lon, lat)| [lon, lat]),
            stage: self.stage.clone(),
            active_version: self
                .versions
                .first()
                .and_then(|v| v["version"].as_str())
                .map(str::to_string),
        };
        match self.client.send_message(&text, Some(&ctx)) {
            Ok(_) => self.input.clear(),
            Err(e) => self.status = format!("send: {e}"),
        }
    }

    fn open_project(&mut self) {
        let osm = if self.osm_path.trim().is_empty() {
            None
        } else {
            Some(self.osm_path.trim())
        };
        match self
            .client
            .open_project(self.area_path.trim(), osm, self.rulebook.trim())
        {
            Ok(_) => {
                self.status = "project opened".into();
                self.messages.clear();
                self.approvals.clear();
                self.refresh_project();
            }
            Err(e) => self.status = format!("open: {e}"),
        }
    }

    // ---- UI -----------------------------------------------------------------------------

    fn chat_panel(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading("openFTTH");
            ui.label(if self.connected {
                RichText::new("● live").color(Color32::from_rgb(0x43, 0xa0, 0x47))
            } else {
                RichText::new("○ offline").color(Color32::GRAY)
            });
        });
        ui.separator();
        if self.project.is_none() {
            ui.label(RichText::new("Open a project").strong());
            ui.add(
                egui::TextEdit::singleline(&mut self.area_path)
                    .hint_text("area polygon (GeoJSON)")
                    .desired_width(f32::INFINITY),
            );
            ui.add(
                egui::TextEdit::singleline(&mut self.osm_path)
                    .hint_text("local .osm file")
                    .desired_width(f32::INFINITY),
            );
            ui.horizontal(|ui| {
                ui.label("rulebook");
                ui.text_edit_singleline(&mut self.rulebook);
            });
            if ui.button("Open").clicked() {
                self.open_project();
            }
            ui.separator();
        }

        // crew presence + stage checklist
        ui.horizontal_wrapped(|ui| {
            for (key, name, _title) in &self.crew {
                let state = self
                    .folk_states
                    .get(key)
                    .cloned()
                    .unwrap_or_else(|| "idle".into());
                let c = match state.as_str() {
                    "thinking" => Color32::from_rgb(0x1e, 0x88, 0xe5),
                    "tooling" => Color32::from_rgb(0xfb, 0x8c, 0x00),
                    "waiting" => Color32::from_rgb(0xd8, 0x1b, 0x60),
                    _ => Color32::GRAY,
                };
                ui.label(RichText::new(format!("● {name}")).color(c))
                    .on_hover_text(state);
            }
        });
        ui.horizontal_wrapped(|ui| {
            for st in [
                "ingest", "twin", "rules", "solve", "validate", "explain", "deliver",
            ] {
                let done = self
                    .stage
                    .as_deref()
                    .map(|cur| stage_index(cur) >= stage_index(st))
                    .unwrap_or(false);
                ui.label(RichText::new(format!("{} {st}", if done { "☑" } else { "☐" })).small());
            }
        });
        if !self.tool_chips.is_empty() {
            ui.horizontal_wrapped(|ui| {
                for chip in &self.tool_chips {
                    ui.label(
                        RichText::new(chip)
                            .small()
                            .background_color(Color32::from_gray(0xee)),
                    );
                }
            });
        }
        ui.separator();

        // approval cards
        let mut decide: Option<(String, bool)> = None;
        for a in self.approvals.iter().filter(|a| a.state == "pending") {
            egui::Frame::group(ui.style())
                .fill(Color32::from_rgb(0xff, 0xf3, 0xe0))
                .show(ui, |ui| {
                    ui.label(RichText::new(format!("Approval needed · {}", a.folk)).strong());
                    ui.label(format!("{}: {}", a.tool, a.summary));
                    ui.horizontal(|ui| {
                        if ui.button("Allow").clicked() {
                            decide = Some((a.id.clone(), true));
                        }
                        if ui.button("Deny").clicked() {
                            decide = Some((a.id.clone(), false));
                        }
                    });
                });
        }
        if let Some((id, allow)) = decide {
            if let Err(e) = self.client.decide(&id, allow, None) {
                self.status = format!("approval: {e}");
            }
        }

        // transcript
        let avail = ui.available_height() - 70.0;
        egui::ScrollArea::vertical()
            .max_height(avail.max(80.0))
            .stick_to_bottom(true)
            .show(ui, |ui| {
                for m in &self.messages {
                    let who = match m.role.as_str() {
                        "user" => "You".to_string(),
                        "folk" => self
                            .crew
                            .iter()
                            .find(|(k, _, _)| Some(k) == m.folk.as_ref())
                            .map(|(_, n, _)| n.clone())
                            .unwrap_or_else(|| m.folk.clone().unwrap_or_default()),
                        _ => "·".to_string(),
                    };
                    match m.kind.as_str() {
                        "tool" => {
                            ui.label(
                                RichText::new(format!("  ⚙ {}", m.text))
                                    .small()
                                    .color(Color32::DARK_GRAY),
                            );
                        }
                        "handoff" => {
                            ui.label(RichText::new(format!("  ↪ {}", m.text)).small().italics());
                        }
                        "error" => {
                            ui.label(
                                RichText::new(format!("  ! {}", m.text))
                                    .small()
                                    .color(Color32::RED),
                            );
                        }
                        _ => {
                            ui.label(RichText::new(who).strong().small());
                            ui.label(&m.text);
                            ui.add_space(4.0);
                        }
                    }
                }
            });

        ui.separator();
        ui.horizontal(|ui| {
            let r = ui.add(
                egui::TextEdit::multiline(&mut self.input)
                    .desired_rows(2)
                    .desired_width(ui.available_width() - 70.0)
                    .hint_text("Tell the crew what to do…"),
            );
            let enter = r.has_focus()
                && ui.input(|i| i.key_pressed(egui::Key::Enter) && !i.modifiers.shift);
            if ui.button("Send").clicked() || enter {
                self.send();
            }
        });
        if !self.selection.is_empty() {
            ui.label(
                RichText::new(format!(
                    "selection: {}",
                    self.selection
                        .iter()
                        .map(|u| self
                            .design
                            .as_ref()
                            .and_then(|d| d.get(u))
                            .map(|e| e.name())
                            .unwrap_or_else(|| u[..8.min(u.len())].to_string()))
                        .collect::<Vec<_>>()
                        .join(", ")
                ))
                .small(),
            );
        }
        if !self.status.is_empty() {
            ui.label(
                RichText::new(&self.status)
                    .small()
                    .color(Color32::DARK_GRAY),
            );
        }
    }

    fn workspace(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.selectable_value(&mut self.tab, Tab::Twin, "Twin");
            ui.selectable_value(&mut self.tab, Tab::Versions, "Versions");
            if ui
                .selectable_value(&mut self.tab, Tab::Backstage, "Backstage")
                .clicked()
            {
                self.refresh_backstage();
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if let Some(d) = &self.design {
                    let counts = d.counts();
                    let line = ["home", "fat", "fdh", "feeder", "distribution", "drop"]
                        .iter()
                        .filter_map(|k| counts.get(*k).map(|n| format!("{n} {k}")))
                        .collect::<Vec<_>>()
                        .join(" · ");
                    ui.label(RichText::new(line).small());
                }
                if ui.small_button("fit").clicked() {
                    self.twin.refit();
                }
            });
        });
        ui.separator();
        match self.tab {
            Tab::Twin => {
                let resp = self.twin.show(
                    ui,
                    self.design.as_ref(),
                    self.streets.as_ref(),
                    &self.selection,
                    &self.changed,
                );
                self.cursor = resp.cursor;
                if let Some(id) = resp.clicked {
                    self.selection = vec![id];
                } else if resp.clicked_empty {
                    self.selection.clear();
                }
            }
            Tab::Versions => {
                let mut approve: Option<String> = None;
                egui::ScrollArea::vertical().show(ui, |ui| {
                    for v in &self.versions {
                        ui.horizontal(|ui| {
                            let ver = s(&v["version"]);
                            ui.label(RichText::new(&ver[..8.min(ver.len())]).monospace());
                            ui.label(s(&v["subject"]));
                            if ui.small_button("approve").clicked() {
                                approve = Some(ver.clone());
                            }
                        });
                    }
                });
                if let Some(v) = approve {
                    match self.client.approve_version(&v) {
                        Ok(_) => self.status = format!("approved {}", &v[..8]),
                        Err(e) => self.status = format!("approve: {e}"),
                    }
                }
            }
            Tab::Backstage => {
                egui::ScrollArea::vertical()
                    .stick_to_bottom(true)
                    .show(ui, |ui| {
                        for e in &self.backstage {
                            ui.label(
                                RichText::new(format!(
                                    "{} · {} · {}",
                                    s(&e["folk"]),
                                    s(&e["event"]),
                                    truncate(&e["detail"].to_string(), 160)
                                ))
                                .monospace()
                                .small(),
                            );
                        }
                    });
            }
        }
    }
}

impl eframe::App for OpenFtthApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.drain_frames();
        egui::Panel::left("chat")
            .resizable(true)
            .default_size(440.0)
            .min_size(320.0)
            .show(ui, |ui| self.chat_panel(ui));
        egui::CentralPanel::default().show(ui, |ui| self.workspace(ui));
        ui.ctx().request_repaint_after(Duration::from_millis(200));
    }
}

fn message_from_value(v: &Value) -> ChatMessage {
    ChatMessage {
        id: s(&v["id"]),
        role: s(&v["role"]),
        folk: v["folk"].as_str().map(str::to_string),
        kind: if v["kind"].is_null() {
            "text".into()
        } else {
            s(&v["kind"])
        },
        text: s(&v["text"]),
    }
}

fn s(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn truncate(text: &str, n: usize) -> String {
    if text.chars().count() <= n {
        text.to_string()
    } else {
        format!("{}…", text.chars().take(n).collect::<String>())
    }
}

fn stage_index(stage: &str) -> usize {
    [
        "ingest", "twin", "rules", "solve", "validate", "explain", "deliver",
    ]
    .iter()
    .position(|s| *s == stage)
    .unwrap_or(0)
}

fn repo_root() -> Option<PathBuf> {
    let mut dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    for _ in 0..6 {
        if dir.join("crews").exists() && dir.join("engine").exists() {
            return Some(dir);
        }
        dir = dir.parent()?.to_path_buf();
    }
    None
}
