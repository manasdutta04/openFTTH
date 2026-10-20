//! The window shell: custom title bar with menus and window controls, crew panel, editor tab
//! strip, properties panel, status bar. The window has no OS decorations; dragging, resizing
//! and the window buttons are handled here.

use std::path::PathBuf;
use std::time::Duration;

use egui::{
    pos2, vec2, Align, Align2, CursorIcon, Frame, Id, Key, Layout, Margin, Order, Pos2, Rect,
    ResizeDirection, Sense, Stroke, Ui, UiBuilder, ViewportCommand,
};
use openftth_client::Client;

use crate::chat::{
    chat_panel, ChatAction, Composer, PROMPT_BOQ, PROMPT_DELIVER, PROMPT_DESIGN, PROMPT_SCENARIOS,
    PROMPT_VALIDATE,
};
use crate::icons::{self, Icon};
use crate::inspector::{inspector, is_excluded, is_movable, InspectorAction};
use crate::state::{AppState, Tab};
use crate::tabs::{backstage_tab, boq_tab, splice_tab, versions_tab, TabState};
use crate::theme::{self, font, Mode, Palette, Weight, R};
use crate::twin::TwinView;
use crate::welcome::{welcome, OpenForm};
use crate::widgets::{
    self, menu_check, menu_item, menu_separator, mono_text, pulse_dot, tag, text,
};

const TITLE_H: f32 = 34.0;
const CONTROL_W: f32 = 46.0;
const EDGE: f32 = 5.0;

/// The files the engine writes to `<project>/deliverables/`: menu label, file name, filter name.
const DELIVERABLES: [(&str, &str, &str); 8] = [
    ("Bill of quantities (CSV)", "boq.csv", "CSV"),
    ("Bill of quantities (JSON)", "boq.json", "JSON"),
    ("Splice schedule (CSV)", "splice_schedule.csv", "CSV"),
    ("Splice schedule (JSON)", "splice_schedule.json", "JSON"),
    ("GeoPackage", "design.gpkg", "GeoPackage"),
    ("Map image (PNG)", "map.png", "PNG image"),
    ("Validation report (JSON)", "validation.json", "JSON"),
    ("Design summary (JSON)", "summary.json", "JSON"),
];

pub struct OpenFtthApp {
    st: AppState,
    composer: Composer,
    tabs: TabState,
    twin: TwinView,
    form: OpenForm,
    mode: Mode,
    applied_mode: Option<Mode>,
    show_crew: bool,
    show_cursors: bool,
    follow_crew: bool,
    about: bool,
    engine_form: Option<EngineForm>,
    confirm_reset: bool,
    frame_ms: f32,
    /// The site the engineer is choosing a new position for (click on the map).
    placing: Option<Placing>,
    /// A change of the engineer's own awaiting its reason and confirmation.
    steer: Option<SteerDialog>,
    /// The project generation the map view was fitted for.
    seen_generation: u64,
}

struct EngineForm {
    kind: String,
    model: String,
    base_url: String,
    api_key: String,
}

#[derive(Clone)]
struct Placing {
    uuid: String,
    name: String,
}

enum SteerKind {
    Move { uuid: String, lon: f64, lat: f64 },
    Exclude { uuid: String },
    Forbid { osm_id: String },
}

/// The confirmation for a manual change: what will happen, the reason that goes on the
/// record, and whether the Checker should look again afterwards.
struct SteerDialog {
    kind: SteerKind,
    title: String,
    body: String,
    verb: &'static str,
    reason: String,
    revalidate: bool,
    focus: bool,
}

impl SteerDialog {
    fn new(kind: SteerKind, title: String, body: String, verb: &'static str) -> Self {
        Self {
            kind,
            title,
            body,
            verb,
            reason: String::new(),
            revalidate: true,
            focus: true,
        }
    }

    fn mv(uuid: &str, name: &str, lon: f64, lat: f64) -> Self {
        Self::new(
            SteerKind::Move { uuid: uuid.to_string(), lon, lat },
            format!("Move {name}"),
            format!(
                "New position {lat:.5}, {lon:.5}. The engine snaps it to the nearest street node, keeps the site's id and connections, and plans every route from it again: one version, with your reason on the record."
            ),
            "Move",
        )
    }

    fn exclude(uuid: &str, name: &str) -> Self {
        Self::new(
            SteerKind::Exclude { uuid: uuid.to_string() },
            format!("Exclude {name}"),
            "The building leaves the design: its homes and drops go, its FAT and cabinet lose the units, and the routes are planned again. A later re-import keeps it out.".to_string(),
            "Exclude",
        )
    }

    fn forbid(osm_id: &str, highway: &str) -> Self {
        Self::new(
            SteerKind::Forbid { osm_id: osm_id.to_string() },
            format!("Forbid digging on {highway} {osm_id}"),
            "The street leaves the route graph for good. Anything standing on it moves to the nearest remaining street node and every route is planned again around it.".to_string(),
            "Forbid digging",
        )
    }

    fn call(&self) -> (&'static str, serde_json::Value) {
        let reason = self.reason.trim();
        match &self.kind {
            SteerKind::Move { uuid, lon, lat } => (
                "move_site",
                serde_json::json!({ "element_uuid": uuid, "lon": lon, "lat": lat, "reason": reason }),
            ),
            SteerKind::Exclude { uuid } => (
                "exclude_building",
                serde_json::json!({ "element_uuid": uuid, "reason": reason }),
            ),
            SteerKind::Forbid { osm_id } => (
                "forbid_street",
                serde_json::json!({ "osm_id": osm_id, "reason": reason }),
            ),
        }
    }
}

impl OpenFtthApp {
    pub fn new(cc: &eframe::CreationContext<'_>, client: Client) -> Self {
        theme::install_fonts(&cc.egui_ctx);
        // Crew frames arrive over SSE while the user is idle, so keep the window ticking.
        let ctx = cc.egui_ctx.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(120));
            ctx.request_repaint();
        });
        let st = AppState::new(client);
        let fixture = repo_root().map(|r| r.join("fixtures").join("gent"));
        Self {
            st,
            composer: Composer::default(),
            tabs: TabState::default(),
            twin: TwinView::default(),
            form: OpenForm::new(fixture),
            mode: Mode::Dark,
            applied_mode: None,
            show_crew: true,
            show_cursors: true,
            follow_crew: false,
            about: false,
            engine_form: None,
            confirm_reset: false,
            frame_ms: 0.0,
            placing: None,
            steer: None,
            seen_generation: 0,
        }
    }

    // ---- title bar --------------------------------------------------------------------------

    fn title_bar(&mut self, ui: &mut Ui) {
        let maximized = ui.input(|i| i.viewport().maximized.unwrap_or(false));
        let (bar, bar_resp) =
            ui.allocate_exact_size(vec2(ui.available_width(), TITLE_H), Sense::click_and_drag());
        if bar_resp.double_clicked() {
            ui.ctx()
                .send_viewport_cmd(ViewportCommand::Maximized(!maximized));
        } else if bar_resp.drag_started_by(egui::PointerButton::Primary) {
            ui.ctx().send_viewport_cmd(ViewportCommand::StartDrag);
        }

        // Centre: the project and its stage, as plain text.
        self.title_text(ui, bar);

        // Left: app mark and menus.
        let mut left = ui.new_child(
            UiBuilder::new()
                .max_rect(Rect::from_min_max(
                    bar.min,
                    pos2(bar.center().x - 140.0, bar.max.y),
                ))
                .layout(Layout::left_to_right(Align::Center)),
        );
        flat_buttons(&mut left);
        left.add_space(10.0);
        let (mark, _) = left.allocate_exact_size(vec2(18.0, 18.0), Sense::hover());
        icons::paint_mark(left.painter(), mark);
        left.add_space(8.0);
        self.menus(&mut left);

        // Right: window controls, then the engine the crew runs on.
        let mut right = ui.new_child(
            UiBuilder::new()
                .max_rect(Rect::from_min_max(
                    pos2(bar.center().x + 140.0, bar.min.y),
                    bar.max,
                ))
                .layout(Layout::right_to_left(Align::Center)),
        );
        right.spacing_mut().item_spacing.x = 0.0;
        self.window_control(&mut right, Icon::Close, "Close", true);
        self.window_control(
            &mut right,
            if maximized {
                Icon::Restore
            } else {
                Icon::Maximize
            },
            if maximized { "Restore" } else { "Maximize" },
            false,
        );
        self.window_control(&mut right, Icon::Minus, "Minimize", false);
    }

    fn title_text(&self, ui: &Ui, bar: Rect) {
        let p = self.st.palette;
        let painter = ui.painter();
        let label = match &self.st.project {
            Some(pr) => match &self.st.stage {
                Some(stage) => format!(
                    "{}  ·  {}  ·  {stage}",
                    file_stem(&pr.area_path),
                    pr.rulebook
                ),
                None => format!("{}  ·  {}", file_stem(&pr.area_path), pr.rulebook),
            },
            None => "openFTTH".to_string(),
        };
        let galley = painter.layout_no_wrap(label, font(12.0, Weight::Regular), p.text2);
        let rect = Rect::from_center_size(bar.center(), galley.size() + vec2(16.0, 8.0));
        painter.galley(rect.min + vec2(8.0, 4.0), galley, p.text2);
        if let Some(pr) = &self.st.project {
            ui.interact(rect, Id::unique("title-text"), Sense::hover())
                .on_hover_text(format!(
                    "{}
{}
project {}",
                    pr.area_path,
                    pr.osm_path.as_deref().unwrap_or("no map extract"),
                    pr.id
                ));
        }
    }

    fn open_engine_form(&mut self, kind: &str) {
        let e = self.st.engines.iter().find(|x| x.kind == kind).cloned();
        self.engine_form = Some(EngineForm {
            kind: kind.to_string(),
            model: e.as_ref().and_then(|x| x.model.clone()).unwrap_or_default(),
            base_url: e
                .as_ref()
                .and_then(|x| x.base_url.clone())
                .unwrap_or_default(),
            api_key: String::new(),
        });
    }

    fn engine_window(&mut self, ctx: &egui::Context) {
        let Some(form) = self.engine_form.as_mut() else {
            return;
        };
        let p = self.st.palette;
        let engines = self.st.engines.clone();
        let mut open = true;
        let mut apply: Option<(String, String, String, String)> = None;
        egui::Window::new("Crew engine")
            .open(&mut open)
            .collapsible(false)
            .resizable(false)
            .anchor(Align2::CENTER_CENTER, vec2(0.0, 0.0))
            .show(ctx, |ui| {
                ui.set_width(420.0);
                for e in &engines {
                    let selected = e.kind == form.kind;
                    let (rect, resp) =
                        ui.allocate_exact_size(vec2(ui.available_width(), 26.0), Sense::click());
                    if selected {
                        ui.painter().rect_filled(rect, R as f32, p.accent_soft);
                    } else if resp.hovered() {
                        ui.painter().rect_filled(rect, R as f32, p.bg3);
                    }
                    icons::paint_engine_mark(
                        ui.painter(),
                        Rect::from_center_size(
                            pos2(rect.left() + 18.0, rect.center().y),
                            vec2(16.0, 16.0),
                        ),
                        &e.kind,
                        &p,
                    );
                    if e.available {
                        ui.painter().circle_filled(
                            pos2(rect.left() + 28.0, rect.top() + 7.0),
                            2.5,
                            p.green,
                        );
                    }
                    ui.painter().text(
                        pos2(rect.left() + 36.0, rect.center().y),
                        Align2::LEFT_CENTER,
                        &e.label,
                        font(
                            12.5,
                            if selected {
                                Weight::Medium
                            } else {
                                Weight::Regular
                            },
                        ),
                        p.text,
                    );
                    if e.current {
                        ui.painter().text(
                            pos2(rect.right() - 8.0, rect.center().y),
                            Align2::RIGHT_CENTER,
                            "in use",
                            font(11.0, Weight::Regular),
                            p.text3,
                        );
                    }
                    if resp.on_hover_cursor(CursorIcon::PointingHand).clicked() && !selected {
                        form.kind = e.kind.clone();
                        form.model = e.model.clone().unwrap_or_default();
                        form.base_url = e.base_url.clone().unwrap_or_default();
                        form.api_key.clear();
                    }
                }
                let Some(e) = engines.iter().find(|x| x.kind == form.kind) else {
                    return;
                };
                ui.add_space(6.0);
                ui.add(
                    egui::Label::new(text(&e.description, 11.5, Weight::Regular, p.text2)).wrap(),
                );
                ui.add(
                    egui::Label::new(text(
                        &e.detail,
                        11.0,
                        Weight::Regular,
                        if e.available { p.green } else { p.amber },
                    ))
                    .wrap(),
                );
                ui.add_space(8.0);
                let needs = |n: &str| e.needs.iter().any(|x| x == n);
                if needs("model") || !e.models.is_empty() || e.kind == "claude" || e.kind == "codex"
                {
                    widgets::kv_edit(
                        ui,
                        &p,
                        "Model",
                        &mut form.model,
                        if e.kind == "claude" || e.kind == "codex" {
                            "default (leave empty)"
                        } else {
                            "model id"
                        },
                        &e.models,
                    );
                }
                if needs("baseUrl") || e.kind == "ollama" {
                    widgets::kv_edit(
                        ui,
                        &p,
                        "Endpoint",
                        &mut form.base_url,
                        "http://host:port/v1",
                        &[],
                    );
                }
                if needs("apiKey") || e.kind == "openai" {
                    ui.horizontal(|ui| {
                        ui.add_sized(
                            [90.0, 22.0],
                            egui::Label::new(text("API key", 12.0, Weight::Regular, p.text2)),
                        );
                        ui.add(
                            egui::TextEdit::singleline(&mut form.api_key)
                                .password(true)
                                .hint_text(if e.has_key {
                                    "a key is saved; type to replace"
                                } else {
                                    "paste a key"
                                })
                                .desired_width(f32::INFINITY),
                        );
                    });
                }
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    if widgets::button(ui, &p, "Use this engine", widgets::Kind::Prominent)
                        .clicked()
                    {
                        apply = Some((
                            form.kind.clone(),
                            form.model.clone(),
                            form.base_url.clone(),
                            form.api_key.clone(),
                        ));
                    }
                    ui.label(text(
                        "The crew restarts on its next turn.",
                        11.0,
                        Weight::Regular,
                        p.text3,
                    ));
                });
            });
        if let Some((kind, model, base_url, api_key)) = apply {
            let send_url = matches!(kind.as_str(), "openai" | "ollama");
            let send_key = !api_key.is_empty();
            self.st.set_engine(
                &kind,
                Some(&model),
                if send_url { Some(&base_url) } else { None },
                if send_key { Some(&api_key) } else { None },
            );
            open = false;
        }
        if !open {
            self.engine_form = None;
        }
    }

    fn window_control(&mut self, ui: &mut Ui, icon: Icon, tip: &str, danger: bool) {
        let p = self.st.palette;
        let (rect, resp) = ui.allocate_exact_size(vec2(CONTROL_W, TITLE_H), Sense::click());
        let hovered = resp.hovered();
        if hovered {
            ui.painter()
                .rect_filled(rect, 0.0, if danger { p.red } else { p.bg3 });
        }
        let color = if hovered && danger {
            egui::Color32::WHITE
        } else {
            p.text2
        };
        icons::paint_window_glyph(ui.painter(), rect.center(), icon, color);
        if resp
            .on_hover_cursor(CursorIcon::PointingHand)
            .on_hover_text(tip)
            .clicked()
        {
            let cmd = match icon {
                Icon::Close => ViewportCommand::Close,
                Icon::Minus => ViewportCommand::Minimized(true),
                Icon::Maximize => ViewportCommand::Maximized(true),
                _ => ViewportCommand::Maximized(false),
            };
            ui.ctx().send_viewport_cmd(cmd);
        }
    }

    fn menu_title(label: &str, p: &Palette) -> egui::RichText {
        text(label, 12.5, Weight::Regular, p.text)
    }

    fn menus(&mut self, ui: &mut Ui) {
        let p = self.st.palette;
        let open = self.st.project.is_some();
        let busy = self.st.busy();
        let has_selection = !self.st.selection.is_empty();

        ui.menu_button(Self::menu_title("File", &p), |ui| {
            widgets::menu_begin(ui, 260.0);
            if menu_item(ui, &p, "New project", None, true) {
                self.st.close_project();
            }
            ui.menu_button(Self::menu_title("Open recent", &p), |ui| {
                widgets::menu_begin(ui, 260.0);
                let recents = self.st.recents.clone();
                if recents.is_empty() {
                    menu_item(ui, &p, "No recent projects", None, false);
                }
                for r in recents.iter().take(10) {
                    let label = format!("{}  ·  {}", file_stem(&r.area_path), r.rulebook);
                    if menu_item(ui, &p, &label, None, true) {
                        self.st
                            .open_project(&r.area_path, r.osm_path.as_deref(), &r.rulebook);
                    }
                }
            })
            .response
            .on_hover_cursor(CursorIcon::PointingHand);
            menu_separator(ui, &p);
            ui.add_enabled_ui(open, |ui| {
                ui.menu_button(Self::menu_title("Export", &p), |ui| {
                    widgets::menu_begin(ui, 320.0);
                    self.export_menu(ui);
                })
                .response
                .on_hover_cursor(CursorIcon::PointingHand);
            });
            if menu_item(ui, &p, "Reveal project folder", None, open) {
                if let Some(pr) = &self.st.project {
                    reveal(&pr.dir.display().to_string());
                }
            }
            if menu_item(ui, &p, "Close project", None, open) {
                self.st.close_project();
            }
            if menu_item(ui, &p, "Reset project…", None, open && !busy) {
                self.confirm_reset = true;
            }
            menu_separator(ui, &p);
            if menu_item(ui, &p, "Exit", Some("Alt+F4"), true) {
                ui.ctx().send_viewport_cmd(ViewportCommand::Close);
            }
        })
        .response
        .on_hover_cursor(CursorIcon::PointingHand);

        ui.menu_button(Self::menu_title("Edit", &p), |ui| {
            widgets::menu_begin(ui, 260.0);
            if menu_item(ui, &p, "Copy selected ids", Some("Ctrl+C"), has_selection) {
                ui.ctx().copy_text(self.st.selection.join("\n"));
            }
            if menu_item(ui, &p, "Clear selection", Some("Esc"), has_selection) {
                self.st.selection.clear();
            }
            if menu_item(
                ui,
                &p,
                "Clear change highlight",
                None,
                !self.st.changed.is_empty(),
            ) {
                self.st.changed.clear();
            }
        })
        .response
        .on_hover_cursor(CursorIcon::PointingHand);

        ui.menu_button(Self::menu_title("View", &p), |ui| {
            widgets::menu_begin(ui, 260.0);
            for (i, tab) in Tab::ALL.iter().enumerate() {
                if menu_item(ui, &p, tab.label(), Some(&format!("Ctrl+{}", i + 1)), open) {
                    self.st.tab = *tab;
                    if *tab == Tab::Backstage {
                        self.st.refresh_backstage();
                    }
                }
            }
            menu_separator(ui, &p);
            if menu_item(ui, &p, "Fit design", Some("F"), open) {
                self.st.tab = Tab::Twin;
                self.twin.refit();
            }
            ui.menu_button(Self::menu_title("Layers", &p), |ui| {
                widgets::menu_begin(ui, 200.0);
                let l = &mut self.st.layers;
                for (label, on) in [
                    ("Feeder", &mut l.feeder),
                    ("Distribution", &mut l.distribution),
                    ("Drops", &mut l.drops),
                    ("FDH cabinets", &mut l.fdhs),
                    ("FATs", &mut l.fats),
                    ("Chambers", &mut l.chambers),
                    ("Homes", &mut l.homes),
                    ("Buildings", &mut l.buildings),
                    ("Streets", &mut l.streets),
                ] {
                    menu_check(ui, &p, label, on);
                }
            })
            .response
            .on_hover_cursor(CursorIcon::PointingHand);
            menu_separator(ui, &p);
            let mut crew = self.show_crew;
            if menu_check(ui, &p, "Crew panel", &mut crew) {
                self.show_crew = crew;
            }
            let mut cursors = self.show_cursors;
            if menu_check(ui, &p, "Show crew cursors", &mut cursors) {
                self.show_cursors = cursors;
            }
            let mut follow = self.follow_crew;
            if menu_check(ui, &p, "Follow the crew", &mut follow) {
                self.follow_crew = follow;
            }
            menu_separator(ui, &p);
            ui.menu_button(Self::menu_title("Theme", &p), |ui| {
                widgets::menu_begin(ui, 180.0);
                let mut dark = self.mode == Mode::Dark;
                let mut light = !dark;
                if menu_check(ui, &p, "Dark", &mut dark) {
                    self.mode = Mode::Dark;
                }
                if menu_check(ui, &p, "Light", &mut light) {
                    self.mode = Mode::Light;
                }
            })
            .response
            .on_hover_cursor(CursorIcon::PointingHand);
        })
        .response
        .on_hover_cursor(CursorIcon::PointingHand);

        ui.menu_button(Self::menu_title("Crew", &p), |ui| {
            widgets::menu_begin(ui, 260.0);
            let can = open && !busy;
            if menu_item(ui, &p, "Design automatically (free, no AI)", None, can) {
                self.st.autodesign();
            }
            if menu_item(ui, &p, "Ask the crew to design this area", None, can) {
                self.st.send(PROMPT_DESIGN);
            }
            if menu_item(ui, &p, "Validate the design", None, can) {
                self.st.send(PROMPT_VALIDATE);
            }
            if menu_item(ui, &p, "Generate scenarios", None, can) {
                self.st.send(PROMPT_SCENARIOS);
            }
            if menu_item(ui, &p, "Bill of quantities", None, can) {
                self.st.send(PROMPT_BOQ);
            }
            menu_separator(ui, &p);
            if menu_item(ui, &p, "Stop the current turn", None, busy) {
                self.st.interrupt();
            }
        })
        .response
        .on_hover_cursor(CursorIcon::PointingHand);

        ui.menu_button(Self::menu_title("Help", &p), |ui| {
            widgets::menu_begin(ui, 220.0);
            if menu_item(ui, &p, "Keyboard shortcuts", None, true) {
                self.st.toasts.info(
                    "Ctrl+1…5 tabs · F fit design · Esc clear selection · Enter send · Shift+Enter new line",
                );
            }
            if menu_item(ui, &p, "About openFTTH", None, true) {
                self.about = true;
            }
        })
        .response
        .on_hover_cursor(CursorIcon::PointingHand);
    }

    fn about_window(&mut self, ctx: &egui::Context) {
        if !self.about {
            return;
        }
        let p = self.st.palette;
        let mut open = true;
        egui::Window::new("About openFTTH")
            .open(&mut open)
            .collapsible(false)
            .resizable(false)
            .anchor(Align2::CENTER_CENTER, vec2(0.0, 0.0))
            .show(ctx, |ui| {
                ui.set_width(320.0);
                ui.horizontal(|ui| {
                    let (r, _) = ui.allocate_exact_size(vec2(28.0, 28.0), Sense::hover());
                    icons::paint_mark(ui.painter(), r);
                    ui.vertical(|ui| {
                        ui.label(text("openFTTH", 15.0, Weight::Semibold, p.text));
                        ui.label(text(
                            format!("version {}", env!("CARGO_PKG_VERSION")),
                            11.5,
                            Weight::Regular,
                            p.text3,
                        ));
                    });
                });
                ui.add_space(6.0);
                ui.label(text(
                    "Fibre access network design with a crew of agents and deterministic solvers. The crew never writes numbers into the design; solvers do, and record why.",
                    12.0,
                    Weight::Regular,
                    p.text2,
                ));
                ui.add_space(4.0);
                ui.label(text("MIT licence.", 11.5, Weight::Regular, p.text3));
            });
        self.about = open;
    }

    /// File → Export: every deliverable the engine has written, each one a native Save As
    /// that copies the file out of the project folder. Nothing here recomputes anything.
    fn export_menu(&mut self, ui: &mut Ui) {
        let p = self.st.palette;
        let Some(pr) = self.st.project.as_ref() else {
            menu_item(ui, &p, "No project open", None, false);
            return;
        };
        let dir = pr.dir.clone();
        let stem = file_stem(&pr.area_path);
        let deliver = dir.join("deliverables");
        let bundle = dir.join("design");
        let mut missing = 0;
        for (label, file, kind) in DELIVERABLES {
            let src = deliver.join(file);
            let exists = src.is_file();
            if !exists {
                missing += 1;
            }
            if menu_item(ui, &p, label, None, exists) {
                self.save_copy(&src, &format!("{stem}-{file}"), label, kind);
            }
        }
        menu_separator(ui, &p);
        if menu_item(
            ui,
            &p,
            "Design bundle (GeoJSON folder)…",
            None,
            bundle.is_dir(),
        ) {
            self.export_folder(&stem, &[("design", bundle.clone())]);
        }
        if menu_item(
            ui,
            &p,
            "Everything to a folder…",
            None,
            bundle.is_dir() || deliver.is_dir(),
        ) {
            let mut parts = vec![
                ("deliverables", deliver.clone()),
                ("design", bundle.clone()),
            ];
            for extra in ["decisions.jsonl", "rulebook.yaml"] {
                let path = dir.join(extra);
                if path.is_file() {
                    parts.push((extra, path));
                }
            }
            self.export_folder(&stem, &parts);
        }
        if missing > 0 {
            menu_separator(ui, &p);
            let busy = self.st.busy();
            if menu_item(
                ui,
                &p,
                "Produce the missing deliverables (ask the Drafter)",
                None,
                !busy,
            ) {
                self.st.send(PROMPT_DELIVER);
            }
        }
    }

    /// Save As for one deliverable: the file is copied, the project keeps its own.
    fn save_copy(&mut self, src: &std::path::Path, suggested: &str, label: &str, kind: &str) {
        let ext = src
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_string();
        let Some(dst) = rfd::FileDialog::new()
            .set_title(format!("Save {label}"))
            .set_file_name(suggested)
            .add_filter(kind, &[ext.as_str()])
            .save_file()
        else {
            return;
        };
        match std::fs::copy(src, &dst) {
            Ok(_) => self.st.toasts.success(format!("Saved {}", dst.display())),
            Err(e) => self.st.toasts.error(format!("Could not save: {e}")),
        }
    }

    /// Copy whole parts of the project (deliverables, the design bundle, the decision log)
    /// into a folder the engineer picks, under `<area>-openftth/`, then reveal it.
    fn export_folder(&mut self, stem: &str, parts: &[(&str, PathBuf)]) {
        let Some(folder) = rfd::FileDialog::new()
            .set_title("Export to folder")
            .pick_folder()
        else {
            return;
        };
        let target = folder.join(format!("{stem}-openftth"));
        let mut copied = 0usize;
        let mut failed: Option<String> = None;
        for (name, src) in parts {
            let dst = target.join(name);
            let res = if src.is_dir() {
                copy_dir(src, &dst)
            } else {
                std::fs::create_dir_all(&target).and_then(|_| std::fs::copy(src, &dst).map(|_| 1))
            };
            match res {
                Ok(n) => copied += n,
                Err(e) => failed = Some(format!("{name}: {e}")),
            }
        }
        match failed {
            Some(e) => self.st.toasts.error(format!("Export stopped: {e}")),
            None => {
                self.st
                    .toasts
                    .success(format!("Exported {copied} files to {}", target.display()));
                reveal(&target.display().to_string());
            }
        }
    }

    fn reset_window(&mut self, ctx: &egui::Context) {
        if !self.confirm_reset {
            return;
        }
        let p = self.st.palette;
        let name = self
            .st
            .project
            .as_ref()
            .map(|pr| file_stem(&pr.area_path))
            .unwrap_or_default();
        let mut open = true;
        let mut decision: Option<bool> = None;
        egui::Window::new("Reset project")
            .open(&mut open)
            .collapsible(false)
            .resizable(false)
            .anchor(Align2::CENTER_CENTER, vec2(0.0, 0.0))
            .show(ctx, |ui| {
                ui.set_width(360.0);
                ui.add(egui::Label::new(text(
                    format!("This deletes everything the crew produced for \"{name}\": the design, every version, the conversation and the runtime log. The area and map files on disk stay. You can open the area again and start from a blank map."),
                    12.5,
                    Weight::Regular,
                    p.text2,
                )).wrap());
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    if widgets::button(ui, &p, "Reset and start over", widgets::Kind::Danger).clicked() {
                        decision = Some(true);
                    }
                    if widgets::button(ui, &p, "Keep it", widgets::Kind::Quiet).clicked() {
                        decision = Some(false);
                    }
                });
            });
        if let Some(yes) = decision {
            if yes {
                self.st.reset_project();
            }
            open = false;
        }
        self.confirm_reset = open;
    }

    /// Thin zones along the window edge that start an OS resize (the window has no frame).
    fn resize_zones(&self, ctx: &egui::Context) {
        if ctx.input(|i| i.viewport().maximized.unwrap_or(false)) {
            return;
        }
        let r = ctx.viewport_rect();
        let (l, t, rt, b) = (r.left(), r.top(), r.right(), r.bottom());
        let zones: [(&str, Rect, ResizeDirection, CursorIcon); 8] = [
            (
                "n",
                Rect::from_min_max(pos2(l + EDGE, t), pos2(rt - EDGE, t + EDGE)),
                ResizeDirection::North,
                CursorIcon::ResizeNorth,
            ),
            (
                "s",
                Rect::from_min_max(pos2(l + EDGE, b - EDGE), pos2(rt - EDGE, b)),
                ResizeDirection::South,
                CursorIcon::ResizeSouth,
            ),
            (
                "w",
                Rect::from_min_max(pos2(l, t + EDGE), pos2(l + EDGE, b - EDGE)),
                ResizeDirection::West,
                CursorIcon::ResizeWest,
            ),
            (
                "e",
                Rect::from_min_max(pos2(rt - EDGE, t + EDGE), pos2(rt, b - EDGE)),
                ResizeDirection::East,
                CursorIcon::ResizeEast,
            ),
            (
                "nw",
                Rect::from_min_size(r.min, vec2(EDGE, EDGE)),
                ResizeDirection::NorthWest,
                CursorIcon::ResizeNwSe,
            ),
            (
                "ne",
                Rect::from_min_size(pos2(rt - EDGE, t), vec2(EDGE, EDGE)),
                ResizeDirection::NorthEast,
                CursorIcon::ResizeNeSw,
            ),
            (
                "sw",
                Rect::from_min_size(pos2(l, b - EDGE), vec2(EDGE, EDGE)),
                ResizeDirection::SouthWest,
                CursorIcon::ResizeNeSw,
            ),
            (
                "se",
                Rect::from_min_size(pos2(rt - EDGE, b - EDGE), vec2(EDGE, EDGE)),
                ResizeDirection::SouthEast,
                CursorIcon::ResizeNwSe,
            ),
        ];
        egui::Area::new(Id::unique("resize-zones"))
            .order(Order::Foreground)
            .fixed_pos(Pos2::ZERO)
            .show(ctx, |ui| {
                for (name, rect, dir, cursor) in zones {
                    let resp = ui.interact(rect, Id::unique(("resize", name)), Sense::drag());
                    if resp.hovered() || resp.dragged() {
                        ctx.set_cursor_icon(cursor);
                    }
                    if resp.drag_started() {
                        ctx.send_viewport_cmd(ViewportCommand::BeginResize(dir));
                    }
                }
            });
    }

    // ---- body -------------------------------------------------------------------------------

    fn status_bar(&self, ui: &mut Ui) {
        let p = self.st.palette;
        ui.horizontal(|ui| {
            ui.set_height(16.0);
            if !self.st.connected {
                pulse_dot(ui, p.red, 3.0);
                ui.label(text(
                    "reconnecting to the harness",
                    11.0,
                    Weight::Regular,
                    p.red,
                ));
                ui.add_space(8.0);
            }
            if let Some(pl) = &self.placing {
                ui.label(text(
                    format!("Click the new position for {}  ·  Esc cancels", pl.name),
                    11.5,
                    Weight::Medium,
                    p.accent,
                ));
                ui.add_space(10.0);
            }
            if let Some(wo) = &self.st.work_order {
                ui.label(text(
                    format!("{} open · {}", s_str(&wo["id"]), s_str(&wo["title"])),
                    11.0,
                    Weight::Medium,
                    p.amber,
                ))
                .on_hover_text(
                    "Changes belong to this work order until it is posted to the network of record",
                );
                ui.add_space(8.0);
            }
            if let Some((lon, lat)) = self.st.cursor {
                ui.label(mono_text(format!("{lon:.5}  {lat:.5}"), 11.0, p.text3));
            }
            if let Some(h) = &self.st.hovered {
                ui.add_space(8.0);
                ui.label(text(
                    self.st.element_name(h),
                    11.5,
                    Weight::Regular,
                    p.text2,
                ));
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if cfg!(debug_assertions) {
                    ui.label(mono_text(format!("{:.1} ms", self.frame_ms), 11.0, p.text3))
                        .on_hover_text("Frame CPU time (debug builds only)");
                }
                if let Some(v) = self.st.versions.first() {
                    ui.label(mono_text(
                        format!(
                            "head {}",
                            widgets::short_id(v["version"].as_str().unwrap_or(""))
                        ),
                        11.0,
                        p.text3,
                    ));
                    if let Some(sum) = &self.st.last_version_summary {
                        ui.label(text(
                            widgets::truncate(sum, 70),
                            11.0,
                            Weight::Regular,
                            p.text3,
                        ));
                    }
                }
            });
        });
    }

    fn tab_strip(&mut self, ui: &mut Ui) {
        let p = self.st.palette;
        ui.horizontal(|ui| {
            ui.set_height(30.0);
            ui.spacing_mut().item_spacing.x = 0.0;
            ui.add_space(6.0);
            for tab in Tab::ALL {
                let active = self.st.tab == tab;
                let galley = ui.painter().layout_no_wrap(
                    tab.label().to_string(),
                    font(
                        12.5,
                        if active {
                            Weight::Medium
                        } else {
                            Weight::Regular
                        },
                    ),
                    p.text,
                );
                let (rect, resp) =
                    ui.allocate_exact_size(vec2(galley.size().x + 24.0, 30.0), Sense::click());
                let color = if active || resp.hovered() {
                    p.text
                } else {
                    p.text2
                };
                ui.painter().galley(
                    pos2(rect.left() + 12.0, rect.center().y - galley.size().y / 2.0),
                    galley,
                    color,
                );
                if active {
                    ui.painter().rect_filled(
                        Rect::from_min_max(
                            pos2(rect.left() + 8.0, rect.bottom() - 2.0),
                            pos2(rect.right() - 8.0, rect.bottom()),
                        ),
                        1.0,
                        p.accent,
                    );
                }
                if resp.on_hover_cursor(CursorIcon::PointingHand).clicked() {
                    self.st.tab = tab;
                    if tab == Tab::Backstage {
                        self.st.refresh_backstage();
                    }
                }
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.add_space(10.0);
                if !self.st.changed.is_empty() {
                    let r = tag(
                        ui,
                        &format!("{} changed", self.st.changed.len()),
                        p.red,
                        p.red.gamma_multiply(0.18),
                    );
                    r.on_hover_text(
                        "Elements changed in the latest version are drawn in red. Edit → Clear change highlight.",
                    );
                }
                if self.st.tab == Tab::Twin {
                    if let Some(d) = &self.st.design {
                        let c = d.counts();
                        let line = [("home", "homes"), ("fat", "FATs"), ("fdh", "FDHs")]
                            .iter()
                            .filter_map(|(k, l)| c.get(*k).map(|n| format!("{n} {l}")))
                            .collect::<Vec<_>>()
                            .join("  ·  ");
                        ui.label(text(line, 11.0, Weight::Regular, p.text3));
                    }
                }
            });
        });
        widgets::hairline(ui, &p);
    }

    fn workspace(&mut self, ui: &mut Ui) {
        if self.seen_generation != self.st.project_generation {
            self.seen_generation = self.st.project_generation;
            self.twin = TwinView::default();
        }
        self.tab_strip(ui);
        match self.st.tab {
            Tab::Twin => {
                let placing = self.placing.clone();
                let reveal = if self.show_cursors {
                    self.st.cursors.reveal(self.twin.scale())
                } else {
                    crate::cursors::Reveal::default()
                };
                // The selection plus every element on the current fibre trace is highlighted.
                let mut lit = self.st.selection.clone();
                lit.extend(self.st.trace_ids());
                let st = &mut self.st;
                let resp = self.twin.show(
                    ui,
                    &st.palette,
                    st.design.as_ref(),
                    st.streets.as_ref(),
                    &mut st.layers,
                    &lit,
                    &st.changed,
                    placing.as_ref().map(|p| p.uuid.as_str()),
                    &reveal,
                );
                st.cursor = resp.cursor;
                st.hovered = resp.hovered;
                if let Some(pl) = placing {
                    if let Some((lon, lat)) = resp.click {
                        self.steer = Some(SteerDialog::mv(&pl.uuid, &pl.name, lon, lat));
                        self.placing = None;
                    }
                } else if let Some(id) = resp.clicked {
                    st.selection = vec![id];
                } else if resp.clicked_empty {
                    st.selection.clear();
                }
                let map = resp.response;
                if self.show_cursors {
                    let rect = map.rect;
                    let view = &self.twin;
                    let scale = view.scale();
                    let look = self
                        .st
                        .cursors
                        .show(ui, rect, scale, |m| view.to_screen(rect, m));
                    if let Some(m) = look {
                        self.twin.centre_on(m);
                    }
                    if self.follow_crew {
                        if let Some(m) = self.st.cursors.latest_position() {
                            let at = self.twin.to_screen(rect, m);
                            if !rect.shrink2(rect.size() * 0.22).contains(at) {
                                self.twin.ease_towards(m, 0.06);
                            }
                        }
                    }
                }
                self.map_context_menu(&map);
            }
            other => {
                Frame::new()
                    .inner_margin(Margin::symmetric(12, 8))
                    .show(ui, |ui| match other {
                        Tab::Boq => boq_tab(ui, &self.st),
                        Tab::Splice => splice_tab(ui, &self.st, &mut self.tabs),
                        Tab::Versions => versions_tab(ui, &mut self.st),
                        Tab::Backstage => backstage_tab(ui, &mut self.st, &mut self.tabs),
                        Tab::Twin => {}
                    });
            }
        }
    }

    /// Right click on the map: move the selected site here, forbid digging on the street
    /// under the pointer, exclude the building under it.
    fn map_context_menu(&mut self, map: &egui::Response) {
        let Some(ctx) = self.twin.context.clone() else {
            return;
        };
        let p = self.st.palette;
        let busy = self.st.busy();
        let design = self.st.design.as_ref();
        let movable = self
            .st
            .selection
            .first()
            .and_then(|id| design.and_then(|d| d.get(id)))
            .filter(|e| is_movable(e))
            .map(|e| (e.element_uuid.clone(), e.name()));
        let building = ctx
            .element
            .as_ref()
            .and_then(|id| design.and_then(|d| d.get(id)))
            .filter(|e| e.kind() == "building" && !is_excluded(e))
            .map(|e| (e.element_uuid.clone(), e.name()));
        let mut dialog: Option<SteerDialog> = None;
        map.context_menu(|ui| {
            widgets::menu_begin(ui, 300.0);
            if let Some((uuid, name)) = &movable {
                if menu_item(ui, &p, &format!("Move {name} here…"), None, !busy) {
                    dialog = Some(SteerDialog::mv(uuid, name, ctx.lonlat.0, ctx.lonlat.1));
                }
            }
            if let Some(s) = &ctx.street {
                let label = if s.no_dig {
                    format!("{} {} is on the no-dig list", s.highway, s.osm_id)
                } else {
                    format!("Forbid digging on {} {}…", s.highway, s.osm_id)
                };
                if menu_item(ui, &p, &label, None, !busy && !s.no_dig) {
                    dialog = Some(SteerDialog::forbid(&s.osm_id, &s.highway));
                }
            }
            if let Some((uuid, name)) = &building {
                if menu_item(
                    ui,
                    &p,
                    &format!("Exclude {name} from the design…"),
                    None,
                    !busy,
                ) {
                    dialog = Some(SteerDialog::exclude(uuid, name));
                }
            }
            if movable.is_none() && ctx.street.is_none() && building.is_none() {
                menu_item(ui, &p, "Select a site to move it here", None, false);
            }
            menu_separator(ui, &p);
            if menu_item(ui, &p, "Copy coordinates", None, true) {
                ui.ctx()
                    .copy_text(format!("{:.6}, {:.6}", ctx.lonlat.1, ctx.lonlat.0));
            }
        });
        if dialog.is_some() {
            self.steer = dialog;
            self.placing = None;
        }
    }

    /// The confirmation window for a manual change; the reason is required.
    fn steer_window(&mut self, ctx: &egui::Context) {
        let p = self.st.palette;
        let busy = self.st.busy();
        let Some(d) = self.steer.as_mut() else {
            return;
        };
        let mut open = true;
        let mut decision: Option<bool> = None;
        egui::Window::new(d.title.clone())
            .open(&mut open)
            .collapsible(false)
            .resizable(false)
            .anchor(Align2::CENTER_CENTER, vec2(0.0, 0.0))
            .show(ctx, |ui| {
                ui.set_width(400.0);
                ui.add(egui::Label::new(text(&d.body, 12.5, Weight::Regular, p.text2)).wrap());
                ui.add_space(10.0);
                ui.label(text("Reason", 11.5, Weight::Medium, p.text2));
                let edit = ui.add(
                    egui::TextEdit::multiline(&mut d.reason)
                        .desired_rows(2)
                        .desired_width(f32::INFINITY)
                        .hint_text("Goes on the record next to the engine's own decisions"),
                );
                if d.focus {
                    edit.request_focus();
                    d.focus = false;
                }
                ui.add_space(6.0);
                ui.checkbox(
                    &mut d.revalidate,
                    text(
                        "Ask the Checker to validate the design again",
                        12.0,
                        Weight::Regular,
                        p.text2,
                    ),
                );
                if busy {
                    ui.add_space(4.0);
                    ui.label(text(
                        "The crew is working; wait for the turn to finish.",
                        11.5,
                        Weight::Regular,
                        p.amber,
                    ));
                }
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    let can = !d.reason.trim().is_empty() && !busy;
                    if widgets::button_ex(ui, &p, None, d.verb, widgets::Kind::Prominent, can)
                        .clicked()
                    {
                        decision = Some(true);
                    }
                    if widgets::button(ui, &p, "Cancel", widgets::Kind::Quiet).clicked() {
                        decision = Some(false);
                    }
                });
            });
        match decision {
            Some(true) => {
                let (tool, args) = d.call();
                let revalidate = d.revalidate;
                if self.st.steer(tool, args, revalidate) {
                    self.steer = None;
                }
            }
            Some(false) => self.steer = None,
            None if !open => self.steer = None,
            None => {}
        }
    }

    fn shortcuts(&mut self, ui: &Ui) {
        let typing = ui.ctx().egui_wants_keyboard_input();
        let (esc, ctrl, c, n1, n2, n3, n4, n5, f) = ui.input(|i| {
            (
                i.key_pressed(Key::Escape),
                i.modifiers.ctrl,
                i.key_pressed(Key::C),
                i.key_pressed(Key::Num1),
                i.key_pressed(Key::Num2),
                i.key_pressed(Key::Num3),
                i.key_pressed(Key::Num4),
                i.key_pressed(Key::Num5),
                i.key_pressed(Key::F),
            )
        });
        if esc {
            if self.placing.is_some() {
                self.placing = None;
            } else if self.steer.is_some() {
                self.steer = None;
            } else {
                self.st.selection.clear();
            }
        }
        if ctrl {
            for (pressed, tab) in [
                (n1, Tab::Twin),
                (n2, Tab::Boq),
                (n3, Tab::Splice),
                (n4, Tab::Versions),
                (n5, Tab::Backstage),
            ] {
                if pressed {
                    self.st.tab = tab;
                }
            }
            if c && !typing && !self.st.selection.is_empty() {
                ui.ctx().copy_text(self.st.selection.join("\n"));
            }
        }
        if f && !typing {
            self.twin.refit();
        }
    }
}

impl eframe::App for OpenFtthApp {
    fn ui(&mut self, ui: &mut Ui, frame: &mut eframe::Frame) {
        if cfg!(debug_assertions) {
            if let Some(cpu) = frame.info().cpu_usage {
                self.frame_ms = self.frame_ms * 0.9 + cpu * 1000.0 * 0.1;
            }
        }
        if self.applied_mode != Some(self.mode) {
            self.st.set_mode(self.mode);
            theme::apply(ui.ctx(), &self.st.palette);
            self.applied_mode = Some(self.mode);
        }
        self.st.drain_frames();
        self.shortcuts(ui);
        let p = self.st.palette;

        egui::Panel::top("title")
            .resizable(false)
            .show_separator_line(false)
            .frame(Frame::new().fill(p.bg0).inner_margin(Margin::ZERO))
            .show(ui, |ui| self.title_bar(ui));
        egui::Panel::bottom("status")
            .resizable(false)
            .show_separator_line(false)
            .frame(
                Frame::new()
                    .fill(p.bg0)
                    .inner_margin(Margin::symmetric(10, 3)),
            )
            .show(ui, |ui| self.status_bar(ui));

        if self.st.project.is_none() {
            egui::CentralPanel::default()
                .frame(Frame::new().fill(p.bg1))
                .show(ui, |ui| welcome(ui, &mut self.st, &mut self.form));
        } else {
            if self.show_crew {
                egui::Panel::left("crew")
                    .resizable(true)
                    .default_size(380.0)
                    .min_size(320.0)
                    .show_separator_line(true)
                    .frame(Frame::new().fill(p.bg1).inner_margin(Margin::ZERO))
                    .show(ui, |ui| {
                        if let Some(ChatAction::ConfigureEngine(kind)) =
                            chat_panel(ui, &mut self.st, &mut self.composer)
                        {
                            self.open_engine_form(&kind);
                        }
                    });
            }
            if !self.st.selection.is_empty() {
                egui::Panel::right("properties")
                    .resizable(true)
                    .default_size(300.0)
                    .min_size(240.0)
                    .show_separator_line(true)
                    .frame(Frame::new().fill(p.bg1).inner_margin(Margin::ZERO))
                    .show(ui, |ui| match inspector(ui, &mut self.st) {
                        Some(InspectorAction::Ask(q)) => self.st.send(&q),
                        Some(InspectorAction::ZoomTo(id)) => {
                            if let Some(e) = self.st.design.as_ref().and_then(|d| d.get(&id)) {
                                self.twin.focus(e);
                                self.st.tab = Tab::Twin;
                            }
                        }
                        Some(InspectorAction::Move(id)) => {
                            let name = self.st.element_name(&id);
                            self.placing = Some(Placing { uuid: id, name });
                            self.st.tab = Tab::Twin;
                        }
                        Some(InspectorAction::Exclude(id)) => {
                            let name = self.st.element_name(&id);
                            self.steer = Some(SteerDialog::exclude(&id, &name));
                        }
                        None => {}
                    });
            }
            egui::CentralPanel::default()
                .frame(Frame::new().fill(p.bg1).inner_margin(Margin::ZERO))
                .show(ui, |ui| self.workspace(ui));
        }

        self.about_window(ui.ctx());
        self.engine_window(ui.ctx());
        self.reset_window(ui.ctx());
        self.steer_window(ui.ctx());
        self.st.toasts.show(ui.ctx(), &p);
        self.resize_zones(ui.ctx());
        // A hairline around the frameless window so it reads as a window over other apps.
        if !ui.input(|i| i.viewport().maximized.unwrap_or(false)) {
            ui.ctx()
                .layer_painter(egui::LayerId::new(
                    Order::Foreground,
                    Id::unique("window-edge"),
                ))
                .rect_stroke(
                    ui.ctx().viewport_rect().shrink(0.5),
                    0.0,
                    Stroke::new(1.0, p.line_strong),
                    egui::StrokeKind::Inside,
                );
        }
        ui.ctx()
            .request_repaint_after(Duration::from_millis(if self.st.busy() {
                100
            } else {
                300
            }));
    }
}

fn s_str(v: &serde_json::Value) -> String {
    v.as_str().map(str::to_string).unwrap_or_default()
}

/// Copy a directory tree; returns the number of files copied.
fn copy_dir(src: &std::path::Path, dst: &std::path::Path) -> std::io::Result<usize> {
    std::fs::create_dir_all(dst)?;
    let mut n = 0;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let to = dst.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            n += copy_dir(&entry.path(), &to)?;
        } else {
            std::fs::copy(entry.path(), &to)?;
            n += 1;
        }
    }
    Ok(n)
}

fn reveal(dir: &str) {
    #[cfg(target_os = "windows")]
    {
        let _ = std::process::Command::new("explorer").arg(dir).spawn();
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = dir;
    }
}

fn file_stem(path: &str) -> String {
    std::path::Path::new(path)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or(path)
        .to_string()
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

/// Title-bar buttons: no frame until hovered, like an editor's menu bar.
fn flat_buttons(ui: &mut Ui) {
    let v = ui.visuals_mut();
    v.widgets.inactive.weak_bg_fill = egui::Color32::TRANSPARENT;
    v.widgets.inactive.bg_stroke = Stroke::NONE;
    v.widgets.hovered.bg_stroke = Stroke::NONE;
    v.widgets.open.bg_stroke = Stroke::NONE;
    v.widgets.active.bg_stroke = Stroke::NONE;
    ui.spacing_mut().button_padding = vec2(8.0, 5.0);
    ui.spacing_mut().item_spacing.x = 0.0;
}
