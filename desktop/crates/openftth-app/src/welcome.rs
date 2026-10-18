//! Before a project is open: inputs, rulebook, recent projects.

use std::path::PathBuf;

use egui::{Frame, Margin, Stroke, Ui};

use crate::icons::Icon;
use crate::state::AppState;
use crate::theme::{Weight, R};
use crate::widgets::{
    button, button_enabled, caps, hairline, icon_button_sized, pulse_dot, relative_time, tag, text,
    Kind,
};

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
    egui::ScrollArea::vertical()
        .auto_shrink([false, false])
        .show(ui, |ui| {
            ui.add_space(36.0);
            ui.vertical_centered(|ui| {
                ui.set_max_width(760.0);
                ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                    ui.label(text("Open a project", 18.0, Weight::Semibold, p.text));
                    ui.label(text(
                        "An area polygon and a local map extract. The crew does the rest.",
                        12.5,
                        Weight::Regular,
                        p.text3,
                    ));
                    ui.add_space(14.0);
                    ui.columns(2, |cols| {
                        let left = &mut cols[0];
                        Frame::new()
                            .fill(p.bg2)
                            .stroke(Stroke::new(1.0, p.line))
                            .corner_radius(R as f32)
                            .inner_margin(Margin::same(14))
                            .show(left, |ui| {
                                ui.label(caps("Design area", p.text3));
                                file_row(
                                    ui,
                                    st,
                                    &mut form.area,
                                    "Polygon (GeoJSON, GeoPackage)",
                                    &["geojson", "json", "gpkg", "shp", "kml"],
                                );
                                ui.add_space(8.0);
                                ui.label(caps("Map data", p.text3));
                                file_row(
                                    ui,
                                    st,
                                    &mut form.osm,
                                    "OpenStreetMap extract (.osm)",
                                    &["osm", "xml", "pbf"],
                                );
                                ui.add_space(8.0);
                                ui.label(caps("Rulebook", p.text3));
                                if st.rulebooks.is_empty() {
                                    ui.text_edit_singleline(&mut form.rulebook);
                                } else {
                                    egui::ComboBox::from_id_salt("rulebook")
                                        .selected_text(&form.rulebook)
                                        .width(ui.available_width())
                                        .show_ui(ui, |ui| {
                                            for rb in &st.rulebooks {
                                                let label = match &rb.country {
                                                    Some(c) => format!("{}  ·  {}", rb.name, c),
                                                    None => rb.name.clone(),
                                                };
                                                ui.selectable_value(
                                                    &mut form.rulebook,
                                                    rb.name.clone(),
                                                    label,
                                                )
                                                .on_hover_text(&rb.description);
                                            }
                                        });
                                    if let Some(rb) =
                                        st.rulebooks.iter().find(|r| r.name == form.rulebook)
                                    {
                                        ui.add(
                                            egui::Label::new(text(
                                                &rb.description,
                                                11.5,
                                                Weight::Regular,
                                                p.text3,
                                            ))
                                            .wrap(),
                                        );
                                    }
                                }
                                ui.add_space(12.0);
                                let ready = !form.area.trim().is_empty()
                                    && !form.rulebook.trim().is_empty();
                                ui.horizontal(|ui| {
                                    if button_enabled(
                                        ui,
                                        &p,
                                        "Open project",
                                        Kind::Prominent,
                                        ready && st.status.is_empty(),
                                    )
                                    .clicked()
                                    {
                                        open = Some((
                                            form.area.clone(),
                                            Some(form.osm.clone()).filter(|s| !s.trim().is_empty()),
                                            form.rulebook.clone(),
                                        ));
                                    }
                                    if !st.status.is_empty() {
                                        pulse_dot(ui, p.accent, 3.5);
                                        ui.label(text(&st.status, 11.5, Weight::Regular, p.text3));
                                    }
                                });
                            });
                        let right = &mut cols[1];
                        Frame::new()
                            .inner_margin(Margin::symmetric(8, 0))
                            .show(right, |ui| {
                                ui.label(caps("Recent", p.text3));
                                ui.add_space(4.0);
                                if st.recents.is_empty() {
                                    ui.label(text(
                                        "Projects you open appear here.",
                                        11.5,
                                        Weight::Regular,
                                        p.text3,
                                    ));
                                }
                                let recents = st.recents.clone();
                                for r in recents.iter().take(8) {
                                    ui.horizontal(|ui| {
                                        ui.set_min_height(34.0);
                                        ui.vertical(|ui| {
                                            ui.label(text(
                                                file_stem(&r.area_path),
                                                12.5,
                                                Weight::Medium,
                                                p.text,
                                            ))
                                            .on_hover_text(format!(
                                                "{}
project {}",
                                                r.area_path, r.id
                                            ));
                                            ui.horizontal(|ui| {
                                                tag(ui, &r.rulebook, p.text2, p.bg3);
                                                ui.label(text(
                                                    relative_time(r.created_at),
                                                    11.0,
                                                    Weight::Regular,
                                                    p.text3,
                                                ));
                                            });
                                        });
                                        ui.with_layout(
                                            egui::Layout::right_to_left(egui::Align::Center),
                                            |ui| {
                                                if button(ui, &p, "Open", Kind::Quiet).clicked() {
                                                    open = Some((
                                                        r.area_path.clone(),
                                                        r.osm_path.clone(),
                                                        r.rulebook.clone(),
                                                    ));
                                                }
                                            },
                                        );
                                    });
                                    hairline(ui, &p);
                                }
                            });
                    });
                });
            });
        });
    if let Some((area, osm, rulebook)) = open {
        st.open_project(&area, osm.as_deref(), &rulebook);
    }
}

fn file_row(ui: &mut Ui, st: &AppState, value: &mut String, hint: &str, exts: &[&str]) {
    let p = st.palette;
    ui.horizontal(|ui| {
        let w = ui.available_width() - 32.0;
        ui.add_sized([w, 24.0], egui::TextEdit::singleline(value).hint_text(hint));
        if icon_button_sized(ui, &p, Icon::Folder, "Browse", 24.0, true).clicked() {
            if let Some(path) = rfd::FileDialog::new().add_filter("Files", exts).pick_file() {
                *value = path.display().to_string();
            }
        }
    });
}

fn file_stem(path: &str) -> String {
    std::path::Path::new(path)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or(path)
        .to_string()
}
