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
    pub project: String,
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
    /// Ducts, poles and manholes that already exist (imported, not designed).
    pub existing: bool,
    /// Bores under motorways, railways, rivers and unmapped obstacles.
    pub crossings: bool,
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
            existing: true,
            crossings: true,
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
    /// Background loads of the design after a version: one in flight at a time; a version
    /// that arrives meanwhile marks the next load as due.
    refresh_rx: Option<Receiver<Refresh>>,
    refresh_due: bool,
    /// The version list and project status still need fetching from the engine once it is free.
    engine_refresh_due: bool,
    pub connected: bool,
    pub palette: Palette,
    pub crew: Vec<CrewMember>,
    pub folk_states: BTreeMap<String, String>,
    pub messages: Vec<ChatMessage>,
    pub approvals: Vec<ApprovalCard>,
    pub chips: Vec<ToolChip>,
    /// The crew's cursors on the map.
    pub cursors: crate::cursors::AgentCursors,
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
    /// The MCP link other AI apps add (`GET /api/mcp`), when the harness has answered.
    pub mcp: Option<Value>,
    /// The version posted as the network of record, if any.
    pub network_of_record: Option<String>,
    /// The open work order (`{id, title, …}`), if any.
    pub work_order: Option<Value>,
    /// The last fibre trace: the home it is for and the engine's result.
    pub trace: Option<(String, Value)>,
    /// Bumped whenever another project opens; the map view refits when it changes.
    pub project_generation: u64,
    pending_open: Option<Receiver<Result<(), String>>>,
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
            network_of_record: None,
            work_order: None,
            trace: None,
            project_generation: 0,
            cursors: Default::default(),
            refresh_rx: None,
            refresh_due: false,
            engine_refresh_due: false,
            engines: Vec::new(),
            engine: None,
            mcp: None,
            pending_open: None,
        };
        st.load_crew();
        st.load_engines();
        st.load_rulebooks();
        st.load_recents();
        st.mcp = st.client.mcp_info().ok();
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
        if key == "external" {
            return "Another AI app".into();
        }
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
                let id = s(&v["id"]);
                if self.project.as_ref().map(|p| p.id != id).unwrap_or(true) {
                    self.forget_project_data();
                }
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
                self.network_of_record = v["summary"]["network_of_record"]
                    .as_str()
                    .map(str::to_string);
                self.work_order = Some(v["summary"]["work_order"].clone()).filter(|w| !w.is_null());
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

    /// Everything that belongs to one project: dropped when another project opens, so its
    /// map, figures and approvals never show under the new one.
    fn forget_project_data(&mut self) {
        self.cursors.clear();
        self.design = None;
        self.streets = None;
        self.boq = None;
        self.validation = None;
        self.versions.clear();
        self.backstage.clear();
        self.messages.clear();
        self.approvals.clear();
        self.chips.clear();
        self.selection.clear();
        self.changed.clear();
        self.explanations.clear();
        self.trace = None;
        self.network_of_record = None;
        self.work_order = None;
        self.stage = None;
        self.last_version_summary = None;
        self.project_generation += 1;
    }

    pub fn reload_messages(&mut self) {
        if let Ok(Value::Array(ms)) = self.client.messages(0) {
            self.messages = ms.iter().map(ChatMessage::from_value).collect();
        }
        if let Ok(Value::Array(ap)) = self.client.approvals() {
            let current = self
                .project
                .as_ref()
                .map(|p| p.id.clone())
                .unwrap_or_default();
            self.approvals = ap
                .iter()
                .map(approval_from_value)
                .filter(|a| a.project.is_empty() || a.project == current)
                .collect();
            self.approvals.sort_by_key(|a| a.created_at);
        }
    }

    /// Load the new design version off the UI thread. Bursts of versions (the automatic design
    /// commits seventeen in a minute) collapse into as few loads as the disk allows.
    fn request_refresh(&mut self) {
        if self.refresh_rx.is_some() {
            self.refresh_due = true;
            return;
        }
        let Some(dir) = self.project.as_ref().map(|p| p.dir.clone()) else {
            return;
        };
        let client = self.client.clone();
        let generation = self.project_generation;
        // While a tool runs the engine answers nothing else, so only the files are read; the
        // version list and status follow once the work stops.
        let with_engine = !self.engine_working();
        self.engine_refresh_due = !with_engine;
        let (tx, rx) = mpsc::channel();
        self.refresh_rx = Some(rx);
        std::thread::spawn(move || {
            let _ = tx.send(Refresh::load(&client, &dir, generation, with_engine));
        });
    }

    /// A crew member or the automatic design has a tool running.
    fn engine_working(&self) -> bool {
        self.busy() || self.chips.iter().any(|c| c.state == "started")
    }

    fn poll_refresh(&mut self) {
        let Some(rx) = &self.refresh_rx else {
            return;
        };
        let r = match rx.try_recv() {
            Ok(r) => r,
            Err(mpsc::TryRecvError::Empty) => return,
            Err(mpsc::TryRecvError::Disconnected) => {
                self.refresh_rx = None;
                return;
            }
        };
        self.refresh_rx = None;
        if r.generation == self.project_generation {
            match r.design {
                Ok(Some(d)) => {
                    // What this version changed, by comparing with the design on screen: no
                    // round trip to the engine, which may be busy with the next step.
                    let (added, moved, updated) = changed_between(self.design.as_ref(), &d);
                    // The cursors visit what is new or has moved; data-only updates just glow.
                    self.cursors.version(&d, &added, &moved);
                    self.changed = added.into_iter().chain(moved).chain(updated).collect();
                    self.design = Some(d);
                }
                Ok(None) => {}
                Err(e) => self.toasts.error(format!("design bundle: {e}")),
            }
            if r.streets.is_some() {
                self.streets = r.streets;
            }
            self.boq = r.boq;
            self.validation = r.validation;
            if let Some(v) = r.versions {
                self.versions = v;
            }
            if let Some(v) = r.project {
                self.network_of_record = v["summary"]["network_of_record"]
                    .as_str()
                    .map(str::to_string);
                self.work_order = Some(v["summary"]["work_order"].clone()).filter(|w| !w.is_null());
            }
        }
        if std::mem::take(&mut self.refresh_due) {
            self.request_refresh();
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

    /// Ask the engine for a home's fibre path; the map lights it up and Properties lists it.
    pub fn trace(&mut self, uuid: &str) {
        match self.client.trace(uuid) {
            Ok(v) => {
                if v["summary"]
                    .as_str()
                    .is_some_and(|s| s.starts_with("rejected"))
                {
                    self.toasts.info(s(&v["summary"]));
                } else {
                    self.trace = Some((uuid.to_string(), v));
                }
            }
            Err(e) => self.toasts.error(format!("trace: {e}")),
        }
    }

    /// Element ids on the current trace, for highlighting.
    pub fn trace_ids(&self) -> Vec<String> {
        self.trace
            .as_ref()
            .and_then(|(_, v)| v["data"]["steps"].as_array().cloned())
            .unwrap_or_default()
            .iter()
            .filter_map(|st| st["element"].as_str().map(str::to_string))
            .collect()
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
        self.poll_open();
        self.poll_refresh();
        if self.engine_refresh_due && self.refresh_rx.is_none() && !self.engine_working() {
            self.request_refresh();
        }
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
                if f.data["state"].as_str() == Some("started") {
                    let folk = s(&f.data["folk"]);
                    let tool = s(&f.data["tool"]);
                    let (key, label, colour) = match folk.as_str() {
                        "" => (
                            "engine".to_string(),
                            "openFTTH engine".to_string(),
                            self.palette.accent,
                        ),
                        "you" => (
                            "you".to_string(),
                            "You".to_string(),
                            egui::Color32::from_rgb(0x2d, 0xd4, 0xbf),
                        ),
                        k => (k.to_string(), self.folk_name(k), self.palette.folk(k)),
                    };
                    let at = crate::cursors::locate(
                        &f.data["target"],
                        self.design.as_ref(),
                        self.streets.as_ref(),
                    );
                    let fallback = crate::cursors::working_spot(self.design.as_ref())
                        .or_else(|| self.streets.as_ref().map(|s| s.bounds().center()));
                    let thought = format!("Now I'm {}…", crate::chat::tool_phrase(&tool));
                    self.cursors
                        .started(&key, &label, colour, &thought, at, fallback);
                }
                if matches!(f.data["state"].as_str(), Some("done") | Some("failed")) {
                    let key = match s(&f.data["folk"]).as_str() {
                        "" => "engine".to_string(),
                        k => k.to_string(),
                    };
                    if let Some(summary) = f.data["summary"].as_str() {
                        self.cursors.step_done(&key, summary);
                    }
                    // The automatic design runs its steps back to back: only the last one ends it.
                    if key != "engine"
                        || s(&f.data["tool"]) == "export_gpkg"
                        || f.data["state"] == "failed"
                    {
                        self.cursors.finished(&key);
                    }
                }
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
                self.trace = None;
                self.explanations.clear();
                self.request_refresh();
            }
            "design.stage" => self.stage = f.data["stage"].as_str().map(str::to_string),
            "folk" => {
                let key = s(&f.data["key"]);
                let state = s(&f.data["state"]);
                // An idle member has no tool running, even if a stopped turn never said so.
                if state == "idle" {
                    for c in self
                        .chips
                        .iter_mut()
                        .filter(|c| c.folk == key && c.state == "started")
                    {
                        c.state = "done".into();
                    }
                    self.cursors.finished(&key);
                }
                self.folk_states.insert(key, state);
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

    /// The engineer's own change: the harness runs the engine tool, records it in the room
    /// and asks the Checker to look again. Returns true when the engine accepted it; the
    /// design reloads through the `design.version` frame that follows.
    pub fn steer(&mut self, tool: &str, args: Value, revalidate: bool) -> bool {
        match self.client.steer(tool, &args, revalidate) {
            Ok(v) => {
                self.toasts.success(s(&v["summary"]));
                for w in v["warnings"].as_array().into_iter().flatten() {
                    self.toasts.info(s(w));
                }
                true
            }
            Err(e) => {
                self.toasts.error(format!("Not applied: {e}"));
                false
            }
        }
    }

    /// Run the whole design with the engine alone: free, offline apart from the map you chose,
    /// and the same solvers the crew uses. Progress arrives in the conversation.
    pub fn autodesign(&mut self) {
        match self.client.autodesign() {
            Ok(v) => self.toasts.info(format!(
                "Designing automatically: {} engine steps, no AI",
                v["steps"].as_u64().unwrap_or(0)
            )),
            Err(e) => self.toasts.error(format!("Automatic design: {e}")),
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

    /// Opening a project starts the design engine, which takes seconds: the call runs on its
    /// own thread so the window keeps painting, and `poll_open` picks up the result.
    pub fn open_project(&mut self, area: &str, osm: Option<&str>, rulebook: &str) {
        if self.pending_open.is_some() {
            return;
        }
        self.status = "Starting the design engine…".into();
        let client = self.client.clone();
        let area = area.trim().to_string();
        let osm = osm
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string);
        let rulebook = rulebook.trim().to_string();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = tx.send(
                client
                    .open_project(&area, osm.as_deref(), &rulebook)
                    .map(|_| ())
                    .map_err(|e| e.to_string()),
            );
        });
        self.pending_open = Some(rx);
    }

    fn poll_open(&mut self) {
        let Some(rx) = &self.pending_open else {
            return;
        };
        let outcome = match rx.try_recv() {
            Ok(o) => o,
            Err(mpsc::TryRecvError::Empty) => return,
            Err(mpsc::TryRecvError::Disconnected) => Err("the open request was lost".to_string()),
        };
        self.pending_open = None;
        self.status.clear();
        match outcome {
            Ok(()) => {
                self.messages.clear();
                self.approvals.clear();
                self.selection.clear();
                self.changed.clear();
                self.explanations.clear();
                self.refresh_project();
                self.load_recents();
                self.toasts.success("Project opened");
            }
            Err(e) => self
                .toasts
                .error(format!("Could not open the project: {e}")),
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

    /// Delete the open project's design, versions and conversation and go back to the start page.
    pub fn reset_project(&mut self) {
        match self.client.reset_project() {
            Ok(_) => self
                .toasts
                .success("Project reset. Open the area again to start from a blank map."),
            Err(e) => {
                self.toasts.error(format!("reset: {e}"));
                return;
            }
        }
        self.project = None;
        self.design = None;
        self.streets = None;
        self.boq = None;
        self.validation = None;
        self.messages.clear();
        self.approvals.clear();
        self.chips.clear();
        self.selection.clear();
        self.changed.clear();
        self.versions.clear();
        self.backstage.clear();
        self.explanations.clear();
        self.stage = None;
        self.last_version_summary = None;
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
        project: s(&v["project"]),
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

/// One crew engine as the harness describes it: what it is and whether it can run here.
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
    /// free | free-tier | subscription | paid
    pub cost: String,
    pub cost_note: String,
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
                            cost: s(&e["cost"]),
                            cost_note: s(&e["costNote"]),
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

/// Everything the app shows about a design version, read on a worker thread.
struct Refresh {
    generation: u64,
    design: Result<Option<Design>, String>,
    streets: Option<StreetLayer>,
    boq: Option<Value>,
    validation: Option<Value>,
    versions: Option<Vec<Value>>,
    project: Option<Value>,
}

impl Refresh {
    fn load(client: &Client, dir: &std::path::Path, generation: u64, with_engine: bool) -> Refresh {
        let bundle = dir.join("design");
        let design = if bundle.exists() {
            Design::read_dir(&bundle)
                .map(Some)
                .map_err(|e| e.to_string())
        } else {
            Ok(None)
        };
        let streets_path = dir.join("twin").join("streets.geojson");
        let streets = streets_path
            .exists()
            .then(|| StreetLayer::read(&streets_path).ok())
            .flatten();
        let read_json = |name: &str| {
            std::fs::read_to_string(dir.join("deliverables").join(name))
                .ok()
                .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        };
        let versions = match with_engine.then(|| client.versions(60)) {
            Some(Ok(Value::Array(v))) => Some(v),
            _ => None,
        };
        Refresh {
            generation,
            design,
            streets,
            boq: read_json("boq.json"),
            validation: read_json("validation.json"),
            versions,
            project: with_engine.then(|| client.project().ok()).flatten(),
        }
    }
}

/// Elements added or altered from `old` to `new`, by id. Nothing on the first load of a project,
/// so an opened design is not shown as all-new.
fn changed_between(old: Option<&Design>, new: &Design) -> (Vec<String>, Vec<String>, Vec<String>) {
    let Some(old) = old else {
        return (Vec::new(), Vec::new(), Vec::new());
    };
    let before: HashMap<&str, &openftth_core::Element> = old
        .elements
        .iter()
        .map(|e| (e.element_uuid.as_str(), e))
        .collect();
    let (mut added, mut moved, mut updated) = (Vec::new(), Vec::new(), Vec::new());
    for e in &new.elements {
        match before.get(e.element_uuid.as_str()) {
            None => added.push(e.element_uuid.clone()),
            Some(o) if o.geometry != e.geometry => moved.push(e.element_uuid.clone()),
            Some(o) if o.props != e.props || o.openftth != e.openftth => {
                updated.push(e.element_uuid.clone())
            }
            Some(_) => {}
        }
    }
    (added, moved, updated)
}
