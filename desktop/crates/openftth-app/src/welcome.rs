//! The screen before a project is open: pick the inputs, choose a rulebook, or reopen a
//! recent project.

use std::path::PathBuf;

use egui::{RichText, Ui};

use crate::state::AppState;
use crate::widgets::{card, ghost_button, pill, primary_button, relative_time, section_title};

pub struct OpenForm {
    pub area: String,
    pub osm: String,
    pub rulebook: String,
}

impl OpenForm {
    pub fn new(repo_fixture: Option<PathBuf>) -> Self {
        Self {
            area: repo_fixture
                .as_ref()
                .map(|p| p.join("area.geojson").display().to_string())
                .unwrap_or_default(),
            osm: repo_fixture
                .as_ref()
                .map(|p| p.join("gent.osm").display().to_string())
                .unwrap_or_default(),
            rulebook: "generic-gpon".into(),
        }
    }
}

pub fn welcome(ui: &mut Ui, st: &mut AppState, form: &mut OpenForm) {
    let p = st.palette;
    let mut open: Option<(String, Option<String>, String)> = None;
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        ui.add_space(28.0);
        ui.vertical_centered(|ui| {
            ui.label(RichText::new("Design a fibre network with your crew").size(26.0).strong());
            ui.label(RichText::new("Give it an area and the map data; the crew places equipment, routes cables, checks the rules and prices it. You approve the risky steps.").color(p.text_muted));
        });
        ui.add_space(20.0);
        let width = ui.available_width().min(880.0);
        ui.vertical_centered(|ui| {
            ui.set_max_width(width);
            ui.columns(2, |cols| {
                // ---- new project ---------------------------------------------------------------
                card(&mut cols[0], &p, |ui| {
                    section_title(ui, &p, "New project");
                    ui.add_space(4.0);
                    ui.label(RichText::new("Design area").small().color(p.text_muted));
                    ui.horizontal(|ui| {
                        ui.add(egui::TextEdit::singleline(&mut form.area).hint_text("polygon · GeoJSON or GeoPackage").desired_width(ui.available_width() - 90.0));
                        if ghost_button(ui, &p, "Browse…").clicked() {
                            if let Some(path) = rfd::FileDialog::new().set_title("Design area polygon").add_filter("Geo files", &["geojson", "json", "gpkg", "shp", "kml"]).pick_file() {
                                form.area = path.display().to_string();
                            }
                        }
                    });
                    ui.add_space(4.0);
                    ui.label(RichText::new("Map data").small().color(p.text_muted));
                    ui.horizontal(|ui| {
                        ui.add(egui::TextEdit::singleline(&mut form.osm).hint_text("OpenStreetMap extract · .osm").desired_width(ui.available_width() - 90.0));
                        if ghost_button(ui, &p, "Browse…").clicked() {
                            if let Some(path) = rfd::FileDialog::new().set_title("OpenStreetMap extract").add_filter("OSM", &["osm", "xml", "pbf"]).pick_file() {
                                form.osm = path.display().to_string();
                            }
                        }
                    });
                    ui.add_space(4.0);
                    ui.label(RichText::new("Rulebook").small().color(p.text_muted));
                    if st.rulebooks.is_empty() {
                        ui.text_edit_singleline(&mut form.rulebook);
                    } else {
                        egui::ComboBox::from_id_salt("rulebook").selected_text(&form.rulebook).width(ui.available_width()).show_ui(ui, |ui| {
                            for rb in &st.rulebooks {
                                let label = match &rb.country {
                                    Some(c) => format!("{} · {}", rb.name, c),
                                    None => rb.name.clone(),
                                };
                                ui.selectable_value(&mut form.rulebook, rb.name.clone(), label).on_hover_text(&rb.description);
                            }
                        });
                        if let Some(rb) = st.rulebooks.iter().find(|r| r.name == form.rulebook) {
                            ui.label(RichText::new(&rb.description).small().color(p.text_muted));
                        }
                    }
                    ui.add_space(10.0);
                    let ready = !form.area.trim().is_empty() && !form.rulebook.trim().is_empty();
                    ui.horizontal(|ui| {
                        if ui.add_enabled_ui(ready, |ui| primary_button(ui, &p, "Open project")).inner.clicked() {
                            open = Some((form.area.clone(), Some(form.osm.clone()).filter(|s| !s.trim().is_empty()), form.rulebook.clone()));
                        }
                        if !st.status.is_empty() {
                            crate::widgets::pulse_dot(ui, p.accent, 4.0);
                            ui.label(RichText::new(&st.status).small().color(p.text_muted));
                        }
                    });
                    ui.label(RichText::new("Opening starts the design engine for this area (a few seconds).").small().color(p.text_muted));
                });
                // ---- recents -------------------------------------------------------------------
                card(&mut cols[1], &p, |ui| {
                    section_title(ui, &p, "Recent projects");
                    ui.add_space(4.0);
                    if st.recents.is_empty() {
                        ui.label(RichText::new("Projects you open appear here.").small().color(p.text_muted));
                    }
                    let recents = st.recents.clone();
                    for r in recents.iter().take(8) {
                        ui.horizontal(|ui| {
                            ui.vertical(|ui| {
                                ui.label(RichText::new(file_stem(&r.area_path)).strong())
                                    .on_hover_text(format!("{}
{}", r.id, r.area_path));
                                ui.horizontal(|ui| {
                                    pill(ui, &r.rulebook, p.accent, p.accent_soft);
                                    ui.label(RichText::new(relative_time(r.created_at)).small().color(p.text_muted));
                                });
                            });
                            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                if ghost_button(ui, &p, "Open").clicked() {
                                    open = Some((r.area_path.clone(), r.osm_path.clone(), r.rulebook.clone()));
                                }
                            });
                        });
                        ui.separator();
                    }
                });
            });
        });
        ui.add_space(16.0);
        ui.vertical_centered(|ui| {
            ui.set_max_width(width);
            card(ui, &p, |ui| {
                section_title(ui, &p, "How it works");
                ui.columns(4, |c| {
                    for (i, (title, body)) in [
                        ("1 · Ingest", "Streets, buildings and addresses become a digital twin of the area."),
                        ("2 · Solve", "Deterministic solvers place the POP, FDH cabinets and FATs and route every cable."),
                        ("3 · Validate", "Power budget, capacities and distances are checked against the rulebook."),
                        ("4 · Deliver", "BOQ, splice schedule and a GeoPackage you can open in any GIS."),
                    ]
                    .iter()
                    .enumerate()
                    {
                        c[i].label(RichText::new(*title).strong());
                        c[i].label(RichText::new(*body).small().color(p.text_muted));
                    }
                });
            });
        });
    });
    if let Some((area, osm, rulebook)) = open {
        st.open_project(&area, osm.as_deref(), &rulebook);
    }
}

fn file_stem(path: &str) -> String {
    std::path::Path::new(path)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or(path)
        .to_string()
}
