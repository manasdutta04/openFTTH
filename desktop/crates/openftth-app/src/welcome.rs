//! Before a project is open: an editor-style start page. Start actions on the left, recent
//! projects on the right.

use std::path::{Path, PathBuf};

use egui::{pos2, vec2, Align2, CursorIcon, Rect, Sense, Ui};

use crate::icons::{self, Icon};
use crate::state::AppState;
use crate::theme::{font, Weight, R};
use crate::widgets::{button_enabled, caps, pulse_dot, relative_time, text, Kind};

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
            ui.add_space(48.0);
            ui.vertical_centered(|ui| {
                ui.set_max_width(880.0);
                ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                    ui.horizontal(|ui| {
                        let (r, _) = ui.allocate_exact_size(vec2(40.0, 40.0), Sense::hover());
                        icons::paint_mark(ui.painter(), r);
                        ui.add_space(4.0);
                        ui.vertical(|ui| {
                            ui.label(text("openFTTH", 24.0, Weight::Semibold, p.text));
                            ui.label(text(
                                "Fibre access design with a crew",
                                13.0,
                                Weight::Regular,
                                p.text3,
                            ));
                        });
                    });
                    ui.add_space(28.0);
                    ui.columns(2, |cols| {
                        // `columns` justifies text; plain top-down keeps labels ragged-right.
                        cols[0].with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                            start_column(ui, st, form, &mut open)
                        });
                        cols[1].with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                            recent_column(ui, st, &mut open)
                        });
                    });
                });
            });
        });
    if let Some((area, osm, rulebook)) = open {
        st.open_project(&area, osm.as_deref(), &rulebook);
    }
}

fn start_column(
    ui: &mut Ui,
    st: &AppState,
    form: &mut OpenForm,
    open: &mut Option<(String, Option<String>, String)>,
) {
    let p = st.palette;
    ui.label(caps("Start", p.text3));
    ui.add_space(6.0);
    if link_row(
        ui,
        st,
        Icon::Folder,
        "Choose the design area",
        &form.area,
        "a polygon as GeoJSON or GeoPackage",
    ) {
        if let Some(path) = rfd::FileDialog::new()
            .add_filter("Area polygon", &["geojson", "json", "gpkg", "shp", "kml"])
            .pick_file()
        {
            form.area = path.display().to_string();
        }
    }
    if link_row(
        ui,
        st,
        Icon::Folder,
        "Choose the map extract",
        &form.osm,
        "a local OpenStreetMap .osm file",
    ) {
        if let Some(path) = rfd::FileDialog::new()
            .add_filter("OpenStreetMap", &["osm", "xml", "pbf"])
            .pick_file()
        {
            form.osm = path.display().to_string();
        }
    }
    ui.add_space(10.0);
    ui.label(caps("Rulebook", p.text3));
    ui.add_space(4.0);
    if st.rulebooks.is_empty() {
        ui.text_edit_singleline(&mut form.rulebook);
    } else {
        egui::ComboBox::from_id_salt("rulebook")
            .selected_text(&form.rulebook)
            .width(260.0)
            .show_ui(ui, |ui| {
                for rb in &st.rulebooks {
                    let label = match &rb.country {
                        Some(c) => format!("{}  ·  {}", rb.name, c),
                        None => rb.name.clone(),
                    };
                    ui.selectable_value(&mut form.rulebook, rb.name.clone(), label)
                        .on_hover_text(&rb.description);
                }
            });
        if let Some(rb) = st.rulebooks.iter().find(|r| r.name == form.rulebook) {
            ui.add(egui::Label::new(text(&rb.description, 11.5, Weight::Regular, p.text3)).wrap());
        }
    }
    ui.add_space(16.0);
    let ready = !form.area.trim().is_empty() && !form.rulebook.trim().is_empty();
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
            *open = Some((
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
}

fn recent_column(ui: &mut Ui, st: &AppState, open: &mut Option<(String, Option<String>, String)>) {
    let p = st.palette;
    ui.label(caps("Recent", p.text3));
    ui.add_space(6.0);
    if st.recents.is_empty() {
        ui.label(text(
            "Projects you open appear here.",
            12.0,
            Weight::Regular,
            p.text3,
        ));
    }
    for r in st.recents.iter().take(8) {
        let (rect, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 40.0), Sense::click());
        if resp.hovered() {
            ui.painter().rect_filled(rect, R as f32, p.bg2);
        }
        let painter = ui.painter();
        painter.text(
            pos2(rect.left() + 8.0, rect.top() + 12.0),
            Align2::LEFT_CENTER,
            file_stem(&r.area_path),
            font(13.0, Weight::Medium),
            p.text,
        );
        painter.text(
            pos2(rect.left() + 8.0, rect.bottom() - 11.0),
            Align2::LEFT_CENTER,
            shorten_path(&r.area_path, 52),
            font(11.5, Weight::Regular),
            p.text3,
        );
        painter.text(
            pos2(rect.right() - 8.0, rect.top() + 12.0),
            Align2::RIGHT_CENTER,
            &r.rulebook,
            font(11.5, Weight::Regular),
            p.text2,
        );
        painter.text(
            pos2(rect.right() - 8.0, rect.bottom() - 11.0),
            Align2::RIGHT_CENTER,
            relative_time(r.created_at),
            font(11.0, Weight::Regular),
            p.text3,
        );
        if resp
            .on_hover_cursor(CursorIcon::PointingHand)
            .on_hover_text(format!("{}\nproject {}", r.area_path, r.id))
            .clicked()
        {
            *open = Some((r.area_path.clone(), r.osm_path.clone(), r.rulebook.clone()));
        }
    }
}

/// A link-style action with the current choice beneath it. Returns true when clicked.
fn link_row(ui: &mut Ui, st: &AppState, icon: Icon, label: &str, value: &str, hint: &str) -> bool {
    let p = st.palette;
    let (rect, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 40.0), Sense::click());
    let hovered = resp.hovered();
    let painter = ui.painter();
    icons::paint(
        painter,
        icons::square(pos2(rect.left() + 10.0, rect.top() + 12.0), 14.0),
        icon,
        p.accent,
    );
    painter.text(
        pos2(rect.left() + 26.0, rect.top() + 12.0),
        Align2::LEFT_CENTER,
        label,
        font(13.0, Weight::Medium),
        if hovered { p.text } else { p.accent },
    );
    let detail = if value.trim().is_empty() {
        hint.to_string()
    } else {
        shorten_path(value, 60)
    };
    painter.text(
        pos2(rect.left() + 26.0, rect.bottom() - 11.0),
        Align2::LEFT_CENTER,
        detail,
        font(11.5, Weight::Regular),
        p.text3,
    );
    let _ = Rect::NOTHING;
    resp.on_hover_cursor(CursorIcon::PointingHand).clicked()
}

fn shorten_path(path: &str, max: usize) -> String {
    if path.chars().count() <= max {
        return path.to_string();
    }
    let tail: String = path
        .chars()
        .rev()
        .take(max - 1)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    format!("…{tail}")
}

fn file_stem(path: &str) -> String {
    Path::new(path)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or(path)
        .to_string()
}
