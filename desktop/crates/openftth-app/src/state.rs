//! Application state: everything the panels draw from, and how harness frames and harness
//! calls change it. UI modules read `AppState` and call its methods; they never touch the
//! client directly.

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver};
use std::time::Instant;

use openftth_client::{Client, DesignContext, Frame};
use openftth_core::{Design, StreetLayer};
use serde_json::Value;

use crate::theme::{Mode, Palette};
use crate::widgets::Toasts;

pub const STAGES: [&str; 7] = [
    "ingest", "twin", "rules", "solve", "validate", "explain", "deliver",
];

#[derive(Clone, Debug)]
pub struct ChatMessage {
    pub id: String,
    pub role: String,
    pub folk: Option<String>,
    pub kind: String,
    pub text: String,
    pub at: i64,
    pub json: Value,
}

impl ChatMessage {
    pub fn from_value(v: &Value) -> Self {
        Self {
            id: s(&v["id"]),
            role: s(&v["role"]),
            folk: v["folk"].as_str().map(str::to_string),
            kind: if v["kind"].is_null() {
                "text".into()
            } else {
                s(&v["kind"])
            },
            text: s(&v["text"]),
            at: v["at"].as_i64().unwrap_or(0),
            json: v["json"].clone(),
        }
    }

    pub fn streaming(&self) -> bool {
        self.json["streaming"].as_bool() == Some(true)
    }
}

#[derive(Clone, Debug)]
pub struct ApprovalCard {
    pub id: String,
    pub folk: String,
    pub tool: String,
    pub summary: String,
    pub state: String,
    pub created_at: i64,
    pub input: Value,
    pub note: String,
}

#[derive(Clone, Debug)]
pub struct ToolChip {
    pub folk: String,
    pub tool: String,
    pub state: String,
    pub summary: Option<String>,
    pub at: Instant,
}

#[derive(Clone, Debug)]
pub struct CrewMember {
    pub key: String,
    pub name: String,
    pub title: String,
}

#[derive(Clone, Debug)]
pub struct ProjectInfo {
    pub id: String,
    pub dir: PathBuf,
    pub rulebook: String,
    pub area_path: String,
    pub osm_path: Option<String>,
    pub approved: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct RulebookPreset {
    pub name: String,
    pub description: String,
    pub country: Option<String>,
}

#[derive(Clone, Debug)]
pub struct RecentProject {
    pub id: String,
    pub area_path: String,
    pub osm_path: Option<String>,
    pub rulebook: String,
    pub created_at: i64,
}

#[derive(Clone, Copy, Debug)]
pub struct Layers {
    pub streets: bool,
    pub buildings: bool,
    pub homes: bool,
    pub drops: bool,
    pub distribution: bool,
    pub feeder: bool,
    pub fats: bool,
    pub fdhs: bool,
    pub chambers: bool,
}

impl Default for Layers {
    fn default() -> Self {
        Self {
            streets: true,
            buildings: true,
            homes: true,
            drops: true,
            distribution: true,
            feeder: true,
            fats: true,
            fdhs: true,
            chambers: true,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tab {
    Twin,
    Boq,
    Splice,
    Versions,
    Backstage,
}

impl Tab {
    pub const ALL: [Tab; 5] = [
        Tab::Twin,
        Tab::Boq,
        Tab::Splice,
        Tab::Versions,
        Tab::Backstage,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Tab::Twin => "Twin",
            Tab::Boq => "BOQ",
            Tab::Splice => "Splices",
            Tab::Versions => "Versions",
            Tab::Backstage => "Backstage",
        }
    }
}

pub struct AppState {
    pub client: Client,
    rx: Receiver<Frame>,
    pub connected: bool,
    pub palette: Palette,
    pub crew: Vec<CrewMember>,
    pub folk_states: BTreeMap<String, String>,
    pub messages: Vec<ChatMessage>,
    pub approvals: Vec<ApprovalCard>,
    pub chips: Vec<ToolChip>,
    pub stage: Option<String>,
    pub project: Option<ProjectInfo>,
    pub design: Option<Design>,
    pub streets: Option<StreetLayer>,
    pub changed: Vec<String>,
    pub versions: Vec<Value>,
    pub backstage: Vec<Value>,
    pub boq: Option<Value>,
    pub validation: Option<Value>,
    pub rulebooks: Vec<RulebookPreset>,
    pub recents: Vec<RecentProject>,
    pub selection: Vec<String>,
    pub hovered: Option<String>,
    pub cursor: Option<(f64, f64)>,
    pub explanations: HashMap<String, Value>,
    pub layers: Layers,
    pub tab: Tab,
    pub toasts: Toasts,
    pub status: String,
    pub last_version_summary: Option<String>,
    pub engines: Vec<EngineInfo>,
    pub engine: Option<EngineCurrent>,
}

impl AppState {
    pub fn new(client: Client) -> Self {
        let (tx, rx) = mpsc::channel();
        client.subscribe(tx);
        let mut st = Self {
            client,
            rx,
            connected: false,
            palette: Palette::dark(),
            crew: Vec::new(),
            folk_states: BTreeMap::new(),
            messages: Vec::new(),
            approvals: Vec::new(),
            chips: Vec::new(),
            stage: None,
            project: None,
            design: None,
            streets: None,
            changed: Vec::new(),
            versions: Vec::new(),
            backstage: Vec::new(),
            boq: None,
            validation: None,
            rulebooks: Vec::new(),
            recents: Vec::new(),
            selection: Vec::new(),
            hovered: None,
            cursor: None,
            explanations: HashMap::new(),
            layers: Layers::default(),
            tab: Tab::Twin,
            toasts: Toasts::default(),
            status: String::new(),
            last_version_summary: None,
            engines: Vec::new(),
            engine: None,
        };
        st.load_crew();
        st.load_engines();
        st.load_rulebooks();
        st.load_recents();
        st.refresh_project();
        st
    }

    pub fn set_mode(&mut self, mode: Mode) {
        self.palette = Palette::for_mode(mode);
    }

    pub fn busy(&self) -> bool {
        self.folk_states.values().any(|s| s != "idle")
    }

    pub fn folk_name(&self, key: &str) -> String {
        self.crew
            .iter()
            .find(|m| m.key == key)
            .map(|m| m.name.clone())
            .unwrap_or_else(|| key.to_string())
    }

    pub fn element_name(&self, uuid: &str) -> String {
        self.design
            .as_ref()
            .and_then(|d| d.get(uuid))
            .map(|e| e.name())
            .unwrap_or_else(|| uuid.chars().take(8).collect())
    }

    pub fn pending_approvals(&self) -> impl Iterator<Item = &ApprovalCard> {
        self.approvals.iter().filter(|a| a.state == "pending")
    }

    // ---- loading ---------------------------------------------------------------------------

    fn load_crew(&mut self) {
        match self.client.crew() {
            Ok(v) => {
                self.crew = v["members"]
                    .as_array()
                    .map(|ms| {
                        ms.iter()
                            .map(|m| CrewMember {
                                key: s(&m["key"]),
                                name: s(&m["name"]),
                                title: s(&m["title"]),
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                if let Some(ms) = v["members"].as_array() {
                    for m in ms {
                        self.folk_states.insert(s(&m["key"]), s(&m["state"]));
                    }
                }
            }
            Err(e) => self
                .toasts
                .error(format!("Could not reach the harness: {e}")),
        }
    }

    pub fn load_rulebooks(&mut self) {
        if let Ok(Value::Array(rb)) = self.client.rulebooks() {
            self.rulebooks = rb
                .iter()
                .map(|r| RulebookPreset {
                    name: s(&r["name"]),
                    description: s(&r["description"]),
                    country: r["country"].as_str().map(str::to_string),
                })
                .collect();
        }
    }

    pub fn load_recents(&mut self) {
        if let Ok(Value::Array(ps)) = self.client.projects() {
            self.recents = ps
                .iter()
                .map(|p| RecentProject {
                    id: s(&p["id"]),
                    area_path: s(&p["areaPath"]),
                    osm_path: p["osmPath"].as_str().map(str::to_string),
                    rulebook: s(&p["rulebook"]),
                    created_at: p["createdAt"].as_i64().unwrap_or(0),
                })
                .collect();
        }
    }

    pub fn refresh_project(&mut self) {
        match self.client.project() {
            Ok(v) if v["open"].as_bool() == Some(true) => {
                self.project = Some(ProjectInfo {
                    id: s(&v["id"]),
                    dir: PathBuf::from(s(&v["dir"])),
                    rulebook: s(&v["rulebook"]),
                    area_path: s(&v["areaPath"]),
                    osm_path: v["osmPath"].as_str().map(str::to_string),
                    approved: v["approvedVersions"]
                        .as_array()
                        .map(|a| {
                            a.iter()
                                .filter_map(|x| x.as_str().map(str::to_string))
                                .collect()
                        })
                        .unwrap_or_default(),
                });
                if let Some(stage) = v["summary"]["stage"].as_str() {
                    self.stage = Some(stage.to_string());
                }
                if let Some(folks) = v["folks"].as_object() {
                    for (k, st) in folks {
                        self.folk_states.insert(k.clone(), s(st));
                    }
                }
                self.reload_design();
                self.reload_messages();
                self.refresh_versions();
                self.refresh_backstage();
            }
            Ok(_) => {
                self.project = None;
                self.design = None;
                self.streets = None;
                self.boq = None;
            }
            Err(e) => self.status = format!("harness: {e}"),
        }
    }

    pub fn reload_design(&mut self) {
        let Some(dir) = self.project.as_ref().map(|p| p.dir.clone()) else {
            return;
        };
        let bundle = dir.join("design");
        if bundle.exists() {
            match Design::read_dir(&bundle) {
                Ok(d) => self.design = Some(d),
                Err(e) => self.toasts.error(format!("design bundle: {e}")),
            }
        }
        let streets = dir.join("twin").join("streets.geojson");
        if streets.exists() {
            self.streets = StreetLayer::read(&streets).ok();
        }
        let boq = dir.join("deliverables").join("boq.json");
        self.boq = std::fs::read_to_string(&boq)
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok());
        let validation = dir.join("deliverables").join("validation.json");
        self.validation = std::fs::read_to_string(&validation)
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok());
    }

    pub fn reload_messages(&mut self) {
        if let Ok(Value::Array(ms)) = self.client.messages(0) {
            self.messages = ms.iter().map(ChatMessage::from_value).collect();
        }
        if let Ok(Value::Array(ap)) = self.client.approvals() {
            self.approvals = ap.iter().map(approval_from_value).collect();
            self.approvals.sort_by_key(|a| a.created_at);
        }
    }

    pub fn refresh_versions(&mut self) {
        match self.client.versions(60) {
            Ok(Value::Array(v)) => self.versions = v,
            Ok(_) => self.versions.clear(),
            Err(e) => self.status = format!("versions: {e}"),
        }
    }

    pub fn refresh_backstage(&mut self) {
        if let Ok(Value::Array(v)) = self.client.backstage(400) {
            self.backstage = v;
        }
    }

    pub fn explain(&mut self, uuid: &str) {
        if self.explanations.contains_key(uuid) {
            return;
        }
        match self.client.explain_element(uuid) {
            Ok(v) => {
                self.explanations.insert(uuid.to_string(), v);
            }
            Err(e) => self.toasts.error(format!("explain: {e}")),
        }
    }

    // ---- frames ----------------------------------------------------------------------------

    pub fn drain_frames(&mut self) {
        while let Ok(f) = self.rx.try_recv() {
            self.apply(f);
        }
        let cutoff = Instant::now() - std::time::Duration::from_secs(20);
        self.chips.retain(|c| c.at > cutoff || c.state == "started");
    }

    fn apply(&mut self, f: Frame) {
        match f.event.as_str() {
            "hello" => {
                let was = self.connected;
                self.connected = true;
                if f.data["resumed"].as_bool() != Some(true) || !was {
                    self.refresh_project();
                    self.load_engines();
                }
            }
            "disconnected" => self.connected = false,
            "message" => {
                let m = ChatMessage::from_value(&f.data);
                if let Some(existing) = self
                    .messages
                    .iter_mut()
                    .find(|x| !m.id.is_empty() && x.id == m.id)
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
                let chip = ToolChip {
                    folk: s(&f.data["folk"]),
                    tool: s(&f.data["tool"]),
                    state: s(&f.data["state"]),
                    summary: f.data["summary"].as_str().map(str::to_string),
                    at: Instant::now(),
                };
                if let Some(existing) = self
                    .chips
                    .iter_mut()
                    .find(|c| c.folk == chip.folk && c.tool == chip.tool && c.state == "started")
                {
                    *existing = chip;
                } else {
                    self.chips.push(chip);
                }
                if self.chips.len() > 8 {
                    self.chips.remove(0);
                }
            }
            "approval" => {
                let card = approval_from_value(&f.data);
                if card.state == "pending" {
                    self.toasts.info(format!(
                        "{} asks for approval: {}",
                        self.folk_name(&card.folk),
                        card.summary
                    ));
                }
                if let Some(existing) = self.approvals.iter_mut().find(|a| a.id == card.id) {
                    let note = existing.note.clone();
                    *existing = card;
                    existing.note = note;
                } else {
                    self.approvals.push(card);
                }
            }
            "design.version" => {
                self.last_version_summary = f.data["summary"].as_str().map(str::to_string);
                self.reload_design();
                self.refresh_versions();
                self.explanations.clear();
                self.changed.clear();
                if self.versions.len() >= 2 {
                    let head = s(&self.versions[0]["version"]);
                    let prev = s(&self.versions[1]["version"]);
                    if let Ok(d) = self.client.diff(&prev, &head) {
                        for k in ["added", "modified"] {
                            if let Some(arr) = d[k].as_array() {
                                self.changed.extend(
                                    arr.iter()
                                        .filter_map(|v| v.as_str())
                                        .filter(|u| !u.starts_with("table:"))
                                        .map(str::to_string),
                                );
                            }
                        }
                    }
                }
            }
            "design.stage" => self.stage = f.data["stage"].as_str().map(str::to_string),
            "folk" => {
                self.folk_states
                    .insert(s(&f.data["key"]), s(&f.data["state"]));
            }
            "engine" => self.load_engines(),
            "project" => {
                if f.data["engine"].as_str() == Some("ready") {
                    self.refresh_project();
                    self.load_recents();
                } else if f.data["engine"].as_str() == Some("exited") && self.project.is_some() {
                    self.toasts
                        .error("The design engine stopped. Re-open the project to restart it.");
                }
            }
            "runtime" => {
                self.backstage.push(f.data);
                if self.backstage.len() > 800 {
                    self.backstage.remove(0);
                }
            }
            _ => {}
        }
    }

    // ---- actions ---------------------------------------------------------------------------

    pub fn send(&mut self, text: &str) {
        let text = text.trim();
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
        if let Err(e) = self.client.send_message(text, Some(&ctx)) {
            self.toasts.error(format!("Could not send: {e}"));
        }
    }

    pub fn interrupt(&mut self) {
        match self.client.interrupt() {
            Ok(_) => self.toasts.info("Stopped the current turn"),
            Err(e) => self.toasts.error(format!("stop: {e}")),
        }
    }

    pub fn decide(&mut self, id: &str, allow: bool) {
        let note = self
            .approvals
            .iter()
            .find(|a| a.id == id)
            .map(|a| a.note.trim().to_string())
            .filter(|n| !n.is_empty());
        match self.client.decide(id, allow, note.as_deref()) {
            Ok(_) => self
                .toasts
                .success(if allow { "Approved" } else { "Denied" }),
            Err(e) => self.toasts.error(format!("approval: {e}")),
        }
    }

    pub fn open_project(&mut self, area: &str, osm: Option<&str>, rulebook: &str) {
        self.status = "Starting the design engine…".into();
        match self.client.open_project(
            area.trim(),
            osm.map(str::trim).filter(|s| !s.is_empty()),
            rulebook.trim(),
        ) {
            Ok(_) => {
                self.messages.clear();
                self.approvals.clear();
                self.selection.clear();
                self.changed.clear();
                self.explanations.clear();
                self.refresh_project();
                self.load_recents();
                self.toasts.success("Project opened");
                self.status.clear();
            }
            Err(e) => {
                self.status.clear();
                self.toasts
                    .error(format!("Could not open the project: {e}"));
            }
        }
    }

    pub fn close_project(&mut self) {
        if let Err(e) = self.client.close_project() {
            self.toasts.error(format!("close: {e}"));
        }
        self.project = None;
        self.design = None;
        self.streets = None;
        self.boq = None;
        self.messages.clear();
        self.approvals.clear();
        self.selection.clear();
        self.load_recents();
    }

    pub fn approve_version(&mut self, version: &str) {
        match self.client.approve_version(version) {
            Ok(_) => {
                self.toasts.success(format!(
                    "Version {} approved",
                    crate::widgets::short_id(version)
                ));
                self.refresh_project();
            }
            Err(e) => self.toasts.error(format!("approve: {e}")),
        }
    }

    pub fn compare_with_head(&mut self, version: &str) {
        let Some(head) = self
            .versions
            .first()
            .and_then(|v| v["version"].as_str())
            .map(str::to_string)
        else {
            return;
        };
        match self.client.diff(version, &head) {
            Ok(d) => {
                self.changed.clear();
                let mut n_tables = 0;
                for k in ["added", "modified"] {
                    if let Some(arr) = d[k].as_array() {
                        for u in arr.iter().filter_map(|v| v.as_str()) {
                            if u.starts_with("table:") {
                                n_tables += 1;
                            } else {
                                self.changed.push(u.to_string());
                            }
                        }
                    }
                }
                let removed = d["removed"].as_array().map(|a| a.len()).unwrap_or(0);
                self.toasts.info(format!(
                    "{} elements changed since {}, {} removed, {} table rows",
                    self.changed.len(),
                    crate::widgets::short_id(version),
                    removed,
                    n_tables
                ));
                self.tab = Tab::Twin;
            }
            Err(e) => self.toasts.error(format!("diff: {e}")),
        }
    }
}

fn approval_from_value(v: &Value) -> ApprovalCard {
    ApprovalCard {
        id: s(&v["id"]),
        folk: s(&v["folk"]),
        tool: if v["tool"].is_null() {
            s(&v["shortTool"])
        } else {
            s(&v["tool"])
        },
        summary: s(&v["summary"]),
        state: s(&v["state"]),
        created_at: v["createdAt"].as_i64().unwrap_or(0),
        input: v["input"].clone(),
        note: String::new(),
    }
}

pub fn s(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

pub fn stage_index(stage: &str) -> usize {
    STAGES.iter().position(|s| *s == stage).unwrap_or(0)
}

/// One folk engine as the harness describes it: what it is and whether it can run here.
#[derive(Clone, Debug)]
pub struct EngineInfo {
    pub kind: String,
    pub label: String,
    pub description: String,
    pub needs: Vec<String>,
    pub available: bool,
    pub detail: String,
    pub models: Vec<String>,
    pub model: Option<String>,
    pub base_url: Option<String>,
    pub has_key: bool,
    pub current: bool,
}

#[derive(Clone, Debug)]
pub struct EngineCurrent {
    pub kind: String,
    pub label: String,
    pub model: Option<String>,
}

impl AppState {
    pub fn load_engines(&mut self) {
        if let Ok(v) = self.client.engines() {
            self.engine = v["current"].as_object().map(|c| EngineCurrent {
                kind: s(&c["kind"]),
                label: s(&c["label"]),
                model: c["model"].as_str().map(str::to_string),
            });
            self.engines = v["engines"]
                .as_array()
                .map(|es| {
                    es.iter()
                        .map(|e| EngineInfo {
                            kind: s(&e["kind"]),
                            label: s(&e["label"]),
                            description: s(&e["description"]),
                            needs: e["needs"]
                                .as_array()
                                .map(|a| a.iter().map(s).collect())
                                .unwrap_or_default(),
                            available: e["available"].as_bool().unwrap_or(false),
                            detail: s(&e["detail"]),
                            models: e["models"]
                                .as_array()
                                .map(|a| a.iter().map(s).collect())
                                .unwrap_or_default(),
                            model: e["model"].as_str().map(str::to_string),
                            base_url: e["baseUrl"].as_str().map(str::to_string),
                            has_key: e["hasKey"].as_bool().unwrap_or(false),
                            current: e["current"].as_bool().unwrap_or(false),
                        })
                        .collect()
                })
                .unwrap_or_default();
        }
    }

    /// Switch the crew to another engine (and save that engine's settings). `None` keeps a value.
    pub fn set_engine(
        &mut self,
        kind: &str,
        model: Option<&str>,
        base_url: Option<&str>,
        api_key: Option<&str>,
    ) {
        let mut body = serde_json::json!({ "kind": kind });
        if let Some(m) = model {
            body["model"] = Value::String(m.to_string());
        }
        if let Some(u) = base_url {
            body["baseUrl"] = Value::String(u.to_string());
        }
        if let Some(k) = api_key {
            body["apiKey"] = Value::String(k.to_string());
        }
        match self.client.set_engine(&body) {
            Ok(v) => {
                let label = s(&v["label"]);
                self.toasts.success(format!("Crew now runs on {label}"));
                self.load_engines();
            }
            Err(e) => self.toasts.error(format!("Could not switch engine: {e}")),
        }
    }
}
