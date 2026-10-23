//! File → Import data: bring a designer's own files (address register, streets, existing ducts,
//! poles, no-dig zones…) in any common GIS format and CRS. The engine lists the layers and
//! proposes a role and column mapping for each; the user corrects them, checks the data and
//! opens a project from the saved import profile.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use egui::{vec2, Align2, Color32, Ui};
use openftth_client::Client;
use serde_json::{json, Value};

use crate::state::AppState;
use crate::theme::{Palette, Weight};
use crate::widgets::{self, caps, text, Kind};

/// One layer of one file, and how it is bound into the project.
struct Layer {
    path: String,
    /// The layer name to bind (`None` for single-layer files such as CSV or GeoJSON).
    bind_layer: Option<String>,
    title: String,
    geometry: String,
    count: i64,
    crs: Option<String>,
    columns: Vec<String>,
    note: String,
    /// The role this layer plays, or empty to leave it out.
    role: String,
    bind_crs: String,
    x: String,
    y: String,
    map: BTreeMap<String, String>,
    values: Value,
}

struct Role {
    key: String,
    label: String,
    fields: Vec<String>,
}

enum Job {
    Inspect,
    Check,
}

pub struct ImportWizard {
    pub open: bool,
    files: Vec<String>,
    name: String,
    rulebook: String,
    layers: Vec<Layer>,
    roles: Vec<Role>,
    profile: Value,
    pending: Option<(Job, Receiver<Result<Value, String>>)>,
    report: Option<Value>,
    profile_path: Option<String>,
    error: Option<String>,
}

impl ImportWizard {
    pub fn new() -> Self {
        Self {
            open: false,
            files: Vec::new(),
            name: String::new(),
            rulebook: "generic-gpon".into(),
            layers: Vec::new(),
            roles: Vec::new(),
            profile: Value::Null,
            pending: None,
            report: None,
            profile_path: None,
            error: None,
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, st: &mut AppState) {
        if !self.open {
            return;
        }
        self.poll();
        if self.pending.is_some() {
            ctx.request_repaint_after(Duration::from_millis(120));
        }
        let p = st.palette;
        let mut open = true;
        let mut create = false;
        egui::Window::new("Import data")
            .open(&mut open)
            .collapsible(false)
            .resizable(false)
            .anchor(Align2::CENTER_CENTER, vec2(0.0, 0.0))
            .show(ctx, |ui| {
                ui.set_width(660.0);
                ui.add(
                    egui::Label::new(text(
                        "Add the project's files: a design area, an address register, streets or an OpenStreetMap extract, and any existing ducts, poles, manholes or no-dig zones. GeoPackage, Shapefile, GeoJSON, KML, FlatGeobuf, DXF and CSV (x/y or WKT) in any CRS.",
                        12.0,
                        Weight::Regular,
                        p.text2,
                    ))
                    .wrap(),
                );
                ui.add_space(8.0);
                let busy = self.pending.is_some();
                ui.horizontal(|ui| {
                    if widgets::button_enabled(ui, &p, "Add files…", Kind::Quiet, !busy).clicked() {
                        self.add_files(&st.client);
                    }
                    if !self.files.is_empty()
                        && widgets::button_enabled(ui, &p, "Clear", Kind::Ghost, !busy).clicked()
                    {
                        *self = Self { open: true, ..Self::new() };
                    }
                    if let Some((job, _)) = &self.pending {
                        ui.add_space(6.0);
                        ui.spinner();
                        let what = match job {
                            Job::Inspect => "Reading the files…",
                            Job::Check => "Reading the data and checking it…",
                        };
                        ui.label(text(what, 12.0, Weight::Regular, p.text3));
                    }
                });
                if let Some(e) = &self.error {
                    ui.add_space(6.0);
                    ui.add(egui::Label::new(text(e, 12.0, Weight::Regular, p.red)).wrap());
                }
                if !self.layers.is_empty() {
                    ui.add_space(10.0);
                    ui.label(caps("Layers", p.text3));
                    ui.add_space(4.0);
                    let mut changed = false;
                    egui::ScrollArea::vertical()
                        .max_height(360.0)
                        .auto_shrink([false, true])
                        .show(ui, |ui| {
                            for (i, l) in self.layers.iter_mut().enumerate() {
                                changed |= layer_card(ui, &p, i, l, &self.roles);
                                ui.add_space(6.0);
                            }
                        });
                    if changed {
                        self.report = None;
                        self.profile_path = None;
                    }
                    ui.add_space(8.0);
                    self.footer(ui, st, &mut create);
                }
            });
        if create {
            if let Some(path) = self.profile_path.clone() {
                st.open_project(&path, None, &self.rulebook);
                open = false;
            }
        }
        self.open = open;
    }

    fn footer(&mut self, ui: &mut Ui, st: &mut AppState, create: &mut bool) {
        let p = st.palette;
        let busy = self.pending.is_some();
        ui.horizontal(|ui| {
            ui.label(text("Project name", 12.0, Weight::Medium, p.text2));
            ui.add(egui::TextEdit::singleline(&mut self.name).desired_width(200.0));
            ui.add_space(8.0);
            ui.label(text("Rulebook", 12.0, Weight::Medium, p.text2));
            egui::ComboBox::from_id_salt("import-rulebook")
                .selected_text(&self.rulebook)
                .width(170.0)
                .show_ui(ui, |ui| {
                    for rb in &st.rulebooks {
                        ui.selectable_value(&mut self.rulebook, rb.name.clone(), &rb.name)
                            .on_hover_text(&rb.description);
                    }
                });
        });
        if let Some(r) = &self.report {
            ui.add_space(8.0);
            report_view(ui, &p, r);
        }
        ui.add_space(10.0);
        let has_area = self.layers.iter().any(|l| l.role == "area");
        ui.horizontal(|ui| {
            let ready = has_area && !self.name.trim().is_empty() && !busy;
            if widgets::button_enabled(ui, &p, "Check data", Kind::Quiet, ready).clicked() {
                self.check(&st.client);
            }
            let ok = self.profile_path.is_some()
                && self.report.as_ref().is_some_and(|r| r["level"] != "error");
            if widgets::button_enabled(ui, &p, "Create project", Kind::Prominent, ok && !busy)
                .clicked()
            {
                *create = true;
            }
            if !has_area {
                ui.label(text(
                    "Give one layer the role Design area.",
                    11.5,
                    Weight::Regular,
                    p.amber,
                ));
            }
        });
    }

    fn add_files(&mut self, client: &Client) {
        let Some(paths) = rfd::FileDialog::new()
            .add_filter(
                "GIS data",
                &[
                    "gpkg", "shp", "geojson", "json", "kml", "fgb", "csv", "txt", "dxf", "gml",
                    "osm", "pbf", "tab",
                ],
            )
            .pick_files()
        else {
            return;
        };
        for p in paths {
            let s = p.display().to_string();
            if !self.files.contains(&s) {
                self.files.push(s);
            }
        }
        if self.name.is_empty() {
            self.name = self
                .files
                .first()
                .and_then(|f| Path::new(f).file_stem())
                .map(|s| s.to_string_lossy().to_string())
                .unwrap_or_default();
        }
        let client = client.clone();
        let files = self.files.clone();
        self.run(Job::Inspect, move || client.import_inspect(&files));
    }

    fn check(&mut self, client: &Client) {
        let mut profile = self.profile.clone();
        profile["name"] = json!(self.name.trim());
        profile["bindings"] = Value::Array(self.layers.iter().filter_map(binding).collect());
        let client = client.clone();
        let name = self.name.trim().to_string();
        let rulebook = self.rulebook.clone();
        self.run(Job::Check, move || {
            client.import_profile(&name, &profile, &rulebook)
        });
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

    fn poll(&mut self) {
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
            (Job::Inspect, Ok(v)) => self.take_inspection(&v),
            (Job::Check, Ok(v)) => {
                self.profile_path = v["path"].as_str().map(str::to_string);
                self.report = Some(v["report"].clone());
            }
        }
    }

    fn take_inspection(&mut self, v: &Value) {
        self.report = None;
        self.profile_path = None;
        self.profile = v["profile"].clone();
        self.roles = v["roles"]
            .as_object()
            .map(|m| {
                m.iter()
                    .map(|(k, r)| Role {
                        key: k.clone(),
                        label: r["label"].as_str().unwrap_or(k).to_string(),
                        fields: strings(&r["fields"]),
                    })
                    .collect()
            })
            .unwrap_or_default();
        let bindings = self.profile["bindings"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let infos = v["layers"].as_array().cloned().unwrap_or_default();
        self.layers = infos
            .iter()
            .map(|info| {
                let path = info["path"].as_str().unwrap_or_default().to_string();
                let layer = info["layer"].as_str().unwrap_or_default().to_string();
                let single = infos.iter().filter(|o| o["path"] == info["path"]).count() == 1;
                let b = bindings.iter().find(|b| {
                    b["path"].as_str() == Some(path.as_str())
                        && (b["layer"].is_null() || b["layer"].as_str() == Some(layer.as_str()))
                });
                let file = Path::new(&path)
                    .file_name()
                    .map(|f| f.to_string_lossy().to_string())
                    .unwrap_or_else(|| path.clone());
                let title = if single || file.starts_with(&layer) {
                    file
                } else {
                    format!("{file}  /  {layer}")
                };
                let s = |key: &str| {
                    b.and_then(|b| b[key].as_str())
                        .unwrap_or_default()
                        .to_string()
                };
                Layer {
                    bind_layer: b
                        .map(|b| b["layer"].as_str().map(str::to_string))
                        .unwrap_or(if single { None } else { Some(layer.clone()) }),
                    path,
                    title,
                    geometry: info["geometry"].as_str().unwrap_or("?").to_string(),
                    count: info["count"].as_i64().unwrap_or(0),
                    crs: info["crs"].as_str().map(str::to_string),
                    columns: strings(&info["columns"]),
                    note: info["note"].as_str().unwrap_or_default().to_string(),
                    role: s("role"),
                    bind_crs: s("crs"),
                    x: s("x"),
                    y: s("y"),
                    map: b
                        .and_then(|b| b["columns"].as_object())
                        .map(|m| {
                            m.iter()
                                .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string())))
                                .collect()
                        })
                        .unwrap_or_default(),
                    values: b.map(|b| b["values"].clone()).unwrap_or(json!({})),
                }
            })
            .collect();
    }
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .map(|a| {
            a.iter()
                .filter_map(|s| s.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

fn opt(s: &str) -> Value {
    if s.trim().is_empty() {
        Value::Null
    } else {
        json!(s.trim())
    }
}

/// The profile binding for a layer, or `None` when it is left out.
fn binding(l: &Layer) -> Option<Value> {
    if l.role.is_empty() {
        return None;
    }
    let columns: BTreeMap<_, _> = l.map.iter().filter(|(_, v)| !v.is_empty()).collect();
    Some(json!({
        "path": l.path,
        "layer": l.bind_layer,
        "role": l.role,
        "crs": opt(&l.bind_crs),
        "x": opt(&l.x),
        "y": opt(&l.y),
        "columns": columns,
        "values": if l.values.is_object() { l.values.clone() } else { json!({}) },
    }))
}

/// One layer: what it holds, the role it plays and which columns carry what. Returns whether
/// anything changed.
fn layer_card(ui: &mut Ui, p: &Palette, i: usize, l: &mut Layer, roles: &[Role]) -> bool {
    let mut changed = false;
    let fill = if l.role.is_empty() { p.bg1 } else { p.bg2 };
    egui::Frame::new()
        .fill(fill)
        .stroke(egui::Stroke::new(1.0, p.line))
        .corner_radius(crate::theme::R as f32)
        .inner_margin(egui::Margin::same(8))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                ui.label(text(&l.title, 12.5, Weight::Semibold, p.text));
                ui.label(text(
                    format!("{} × {}", l.geometry, widgets::fmt_int(l.count)),
                    11.5,
                    Weight::Regular,
                    p.text3,
                ));
                match &l.crs {
                    Some(c) => ui.label(widgets::mono_text(c, 11.0, p.text3)),
                    None => ui.label(text("no CRS in the file", 11.5, Weight::Regular, p.amber)),
                };
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let label = roles
                        .iter()
                        .find(|r| r.key == l.role)
                        .map(|r| r.label.as_str())
                        .unwrap_or("Leave out");
                    egui::ComboBox::from_id_salt(("import-role", i))
                        .selected_text(widgets::truncate(label, 34))
                        .width(230.0)
                        .show_ui(ui, |ui| {
                            changed |= ui
                                .selectable_value(&mut l.role, String::new(), "Leave out")
                                .changed();
                            for r in roles {
                                changed |= ui
                                    .selectable_value(&mut l.role, r.key.clone(), &r.label)
                                    .changed();
                            }
                        });
                });
            });
            if !l.note.is_empty() {
                ui.label(text(&l.note, 11.5, Weight::Regular, p.text3));
            }
            if l.role.is_empty() {
                return;
            }
            let fields = roles
                .iter()
                .find(|r| r.key == l.role)
                .map(|r| r.fields.clone())
                .unwrap_or_default();
            let table = l.geometry == "None" || !l.x.is_empty();
            if l.crs.is_none() && l.bind_crs.trim().is_empty() {
                ui.label(text(
                    "Type the coordinate system these coordinates are in, for example EPSG:31370 (Belgian Lambert 72) or EPSG:4326 (longitude/latitude).",
                    11.5,
                    Weight::Regular,
                    p.amber,
                ));
            }
            // Label and picker pairs, three to a row, so every label stays beside its picker.
            egui::Grid::new(("import-cols", i))
                .num_columns(6)
                .spacing(vec2(8.0, 6.0))
                .show(ui, |ui| {
                    let mut n = 0;
                    let mut next = |ui: &mut Ui| {
                        n += 1;
                        if n % 3 == 0 {
                            ui.end_row();
                        }
                    };
                    if table {
                        changed |= column_pick(ui, p, ("x", i), "x", &mut l.x, &l.columns);
                        next(ui);
                        changed |= column_pick(ui, p, ("y", i), "y", &mut l.y, &l.columns);
                        next(ui);
                    }
                    if l.crs.is_none() {
                        ui.label(text("CRS", 11.5, Weight::Medium, p.text2));
                        changed |= ui
                            .add(
                                egui::TextEdit::singleline(&mut l.bind_crs)
                                    .hint_text("EPSG:…")
                                    .desired_width(110.0),
                            )
                            .changed();
                        next(ui);
                    }
                    for f in &fields {
                        let mut v = l.map.get(f).cloned().unwrap_or_default();
                        if column_pick(ui, p, (f.as_str(), i), f, &mut v, &l.columns) {
                            l.map.insert(f.clone(), v);
                            changed = true;
                        }
                        next(ui);
                    }
                });
        });
    changed
}

fn column_pick(
    ui: &mut Ui,
    p: &Palette,
    salt: (&str, usize),
    label: &str,
    value: &mut String,
    columns: &[String],
) -> bool {
    let mut changed = false;
    ui.label(text(label.replace('_', " "), 11.5, Weight::Medium, p.text2));
    egui::ComboBox::from_id_salt(("import-col", salt.0, salt.1))
        .selected_text(if value.is_empty() {
            "—"
        } else {
            value.as_str()
        })
        .width(110.0)
        .show_ui(ui, |ui| {
            changed |= ui.selectable_value(value, String::new(), "—").changed();
            for c in columns {
                changed |= ui.selectable_value(value, c.clone(), c).changed();
            }
        });
    changed
}

/// What a count in the quality report is called, in report order: (key, one, many).
const COUNT_NAMES: [(&str, &str, &str); 11] = [
    ("homes", "home", "homes"),
    ("units", "unit", "units"),
    ("addresses", "address", "addresses"),
    ("streets", "street segment", "street segments"),
    ("buildings", "building", "buildings"),
    ("existing_ducts", "existing duct", "existing ducts"),
    ("poles", "pole", "poles"),
    ("manholes", "manhole", "manholes"),
    ("no_dig_zones", "no-dig zone", "no-dig zones"),
    (
        "osm_streets",
        "OpenStreetMap street",
        "OpenStreetMap streets",
    ),
    (
        "osm_buildings",
        "OpenStreetMap building",
        "OpenStreetMap buildings",
    ),
];

/// The data-quality report: counts, then each finding with its level.
fn report_view(ui: &mut Ui, p: &Palette, r: &Value) {
    let level = r["level"].as_str().unwrap_or("ok");
    let colour = |l: &str| -> Color32 {
        match l {
            "error" => p.red,
            "warning" => p.amber,
            "ok" => p.green,
            _ => p.text3,
        }
    };
    ui.horizontal(|ui| {
        widgets::dot(ui, colour(level), 4.0);
        let head = match level {
            "error" => "The data cannot be designed yet",
            "warning" => "The data can be designed; look at the warnings",
            _ => "The data is ready to design",
        };
        ui.label(text(head, 12.5, Weight::Semibold, p.text));
    });
    if let Some(counts) = r["counts"].as_object() {
        ui.horizontal_wrapped(|ui| {
            for (key, one, many) in COUNT_NAMES {
                let Some(n) = counts.get(key).and_then(Value::as_i64) else {
                    continue;
                };
                if n == 0 && !matches!(key, "homes" | "units") {
                    continue;
                }
                let name = if n == 1 { one } else { many };
                ui.label(text(
                    format!("{} {name}", widgets::fmt_int(n)),
                    11.5,
                    Weight::Regular,
                    p.text2,
                ));
                ui.add_space(8.0);
            }
        });
    }
    for i in r["issues"].as_array().into_iter().flatten() {
        let l = i["level"].as_str().unwrap_or("info");
        ui.horizontal(|ui| {
            widgets::dot(ui, colour(l), 3.0);
            ui.vertical(|ui| {
                ui.add(
                    egui::Label::new(text(
                        i["message"].as_str().unwrap_or_default(),
                        11.5,
                        Weight::Regular,
                        p.text2,
                    ))
                    .wrap(),
                );
                // What to do about it, from the engine's own check.
                if let Some(fix) = i["fix"].as_str().filter(|f| !f.is_empty()) {
                    ui.add(egui::Label::new(text(fix, 11.0, Weight::Regular, p.text3)).wrap());
                }
            });
        });
    }
}
