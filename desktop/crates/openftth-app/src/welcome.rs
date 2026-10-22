//! Before a project is open: the start page. Three ways in as cards (your own data,
//! OpenStreetMap, other AI apps), then the OpenStreetMap form beside the recent projects.

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

/// What the start page asks the app to open.
pub enum WelcomeAction {
    Import,
    Connect,
}

/// One way in: an icon, a title, what it does, and a status line at the bottom.
fn start_card(
    ui: &mut Ui,
    st: &AppState,
    w: f32,
    icon: Icon,
    title: &str,
    body: &str,
    status: (&str, egui::Color32),
) -> egui::Response {
    let p = st.palette;
    let (rect, resp) = ui.allocate_exact_size(vec2(w, 132.0), Sense::click());
    let hovered = resp.hovered();
    let painter = ui.painter();
    painter.rect_filled(rect, 10.0, if hovered { p.bg3 } else { p.bg2 });
    painter.rect_stroke(
        rect,
        10.0,
        egui::Stroke::new(1.0, if hovered { p.accent } else { p.line }),
        egui::StrokeKind::Inside,
    );
    let badge = Rect::from_min_size(rect.min + vec2(16.0, 16.0), vec2(30.0, 30.0));
    painter.rect_filled(badge, 8.0, p.accent_soft);
    icons::paint(painter, icons::square(badge.center(), 16.0), icon, p.accent);
    painter.text(
        pos2(rect.left() + 58.0, rect.top() + 31.0),
        Align2::LEFT_CENTER,
        title,
        font(14.0, Weight::Semibold),
        p.text,
    );
    let galley = painter.layout(
        body.to_string(),
        font(12.0, Weight::Regular),
        p.text2,
        w - 32.0,
    );
    painter.galley(pos2(rect.left() + 16.0, rect.top() + 56.0), galley, p.text2);
    painter.circle_filled(
        pos2(rect.left() + 20.0, rect.bottom() - 18.0),
        3.0,
        status.1,
    );
    painter.text(
        pos2(rect.left() + 30.0, rect.bottom() - 18.0),
        Align2::LEFT_CENTER,
        status.0,
        font(11.5, Weight::Medium),
        p.text3,
    );
    resp.on_hover_cursor(CursorIcon::PointingHand)
}

pub fn welcome(ui: &mut Ui, st: &mut AppState, form: &mut OpenForm) -> Option<WelcomeAction> {
    let p = st.palette;
    let mut action: Option<WelcomeAction> = None;
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
                    ui.add_space(26.0);
                    let card_gap = 14.0;
                    let card_w = ((ui.available_width() - 2.0 * card_gap) / 3.0).max(200.0);
                    let listening = st.mcp.as_ref().is_some_and(|m| m["listening"] == true);
                    ui.horizontal_top(|ui| {
                        ui.spacing_mut().item_spacing.x = card_gap;
                        if start_card(
                            ui,
                            st,
                            card_w,
                            Icon::Plus,
                            "Import your own data",
                            "Address register, streets, existing ducts, poles and no-dig zones, in any GIS format and CRS.",
                            ("GeoPackage · Shapefile · CSV · DXF · KML", p.text3),
                        )
                        .clicked()
                        {
                            action = Some(WelcomeAction::Import);
                        }
                        if start_card(
                            ui,
                            st,
                            card_w,
                            Icon::Folder,
                            "Start from OpenStreetMap",
                            "A design area and a map extract: streets and buildings come from the extract.",
                            if form.area.trim().is_empty() {
                                ("choose the area below", p.text3)
                            } else {
                                ("area chosen · open it below", p.green)
                            },
                        )
                        .clicked()
                        {
                            if let Some(path) = rfd::FileDialog::new()
                                .add_filter("Area polygon", &["geojson", "json", "gpkg", "shp", "kml"])
                                .pick_file()
                            {
                                form.area = path.display().to_string();
                            }
                        }
                        if start_card(
                            ui,
                            st,
                            card_w,
                            Icon::Link,
                            "Connect AI apps",
                            "Claude, Cursor or VS Code design, check and export through one local MCP link.",
                            if listening {
                                ("MCP link on", p.green)
                            } else {
                                ("MCP link off", p.text3)
                            },
                        )
                        .clicked()
                        {
                            action = Some(WelcomeAction::Connect);
                        }
                    });
                    ui.add_space(30.0);
                    // Two columns of fixed width, so a row's click area never reaches into the
                    // other column (a Recent click used to land on "Choose the design area").
                    let gap = 32.0;
                    let col_w = ((ui.available_width() - gap) / 2.0).max(240.0);
                    ui.horizontal_top(|ui| {
                        ui.spacing_mut().item_spacing.x = gap;
                        ui.allocate_ui_with_layout(
                            vec2(col_w, 0.0),
                            egui::Layout::top_down(egui::Align::Min),
                            |ui| {
                                ui.set_width(col_w);
                                start_column(ui, st, form, &mut open, &mut action)
                            },
                        );
                        ui.allocate_ui_with_layout(
                            vec2(col_w, 0.0),
                            egui::Layout::top_down(egui::Align::Min),
                            |ui| {
                                ui.set_width(col_w);
                                recent_column(ui, st, &mut open, &mut action)
                            },
                        );
                    });
                });
            });
        });
    if let Some((area, osm, rulebook)) = open {
        st.open_project(&area, osm.as_deref(), &rulebook);
    }
    action
}

fn start_column(
    ui: &mut Ui,
    st: &AppState,
    form: &mut OpenForm,
    open: &mut Option<(String, Option<String>, String)>,
    action: &mut Option<WelcomeAction>,
) {
    let p = st.palette;
    let _ = action;
    ui.label(caps("New from OpenStreetMap", p.text3));
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

fn recent_column(
    ui: &mut Ui,
    st: &AppState,
    open: &mut Option<(String, Option<String>, String)>,
    action: &mut Option<WelcomeAction>,
) {
    let p = st.palette;
    let _ = action;
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
        let imported = r.area_path.ends_with(".yaml") || r.area_path.ends_with(".yml");
        icons::paint(
            painter,
            icons::square(pos2(rect.left() + 14.0, rect.top() + 12.0), 13.0),
            if imported { Icon::Plus } else { Icon::Folder },
            p.text3,
        );
        painter.text(
            pos2(rect.left() + 28.0, rect.top() + 12.0),
            Align2::LEFT_CENTER,
            file_stem(&r.area_path),
            font(13.0, Weight::Medium),
            p.text,
        );
        painter.text(
            pos2(rect.left() + 28.0, rect.bottom() - 11.0),
            Align2::LEFT_CENTER,
            if imported {
                "imported data".to_string()
            } else {
                format!(
                    "OpenStreetMap · {}",
                    r.osm_path
                        .as_deref()
                        .map(file_stem)
                        .unwrap_or_else(|| "no extract".into())
                )
            },
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
