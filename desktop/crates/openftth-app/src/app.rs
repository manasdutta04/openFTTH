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
    chat_panel, Composer, PROMPT_BOQ, PROMPT_DESIGN, PROMPT_SCENARIOS, PROMPT_VALIDATE,
};
use crate::icons::{self, Icon};
use crate::inspector::{inspector, InspectorAction};
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

pub struct OpenFtthApp {
    st: AppState,
    composer: Composer,
    tabs: TabState,
    twin: TwinView,
    form: OpenForm,
    mode: Mode,
    applied_mode: Option<Mode>,
    show_crew: bool,
    about: bool,
    engine_form: Option<EngineForm>,
}

struct EngineForm {
    kind: String,
    model: String,
    base_url: String,
    api_key: String,
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
            about: false,
            engine_form: None,
        }
    }

    // ---- title bar --------------------------------------------------------------------------

    fn title_bar(&mut self, ui: &mut Ui) {
        let p = self.st.palette;
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
        icons::paint_mark(left.painter(), mark, p.accent, egui::Color32::WHITE);
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
        right.add_space(12.0);
        flat_buttons(&mut right);
        self.engine_picker(&mut right);
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

    /// The engine pill: which runtime the crew is on, and a menu to switch it.
    fn engine_picker(&mut self, ui: &mut Ui) {
        let p = self.st.palette;
        let (label, ok) = match &self.st.engine {
            Some(e) => (
                match &e.model {
                    Some(m) if !m.is_empty() => {
                        format!("{}  ·  {}", e.label, widgets::truncate(m, 28))
                    }
                    _ => e.label.clone(),
                },
                self.st
                    .engines
                    .iter()
                    .find(|x| x.kind == e.kind)
                    .map(|x| x.available)
                    .unwrap_or(true),
            ),
            None => ("Engine".to_string(), false),
        };
        let title = text(
            label,
            12.0,
            Weight::Regular,
            if ok { p.text } else { p.amber },
        );
        let resp = ui
            .menu_button(title, |ui| {
                widgets::menu_begin(ui, 300.0);
                ui.label(widgets::caps("Crew engine", p.text3));
                ui.add_space(2.0);
                let engines = self.st.engines.clone();
                let mut pick: Option<(String, Option<String>)> = None;
                let mut configure: Option<String> = None;
                for e in &engines {
                    let detail = if e.available {
                        match &e.model {
                            Some(m) if !m.is_empty() => m.clone(),
                            _ => "ready".to_string(),
                        }
                    } else {
                        e.detail.clone()
                    };
                    let needs_form = e.needs.iter().any(|n| n == "apiKey" || n == "baseUrl");
                    if e.kind == "ollama" && !e.models.is_empty() {
                        let models = e.models.clone();
                        ui.menu_button(Self::menu_title(&e.label, &p), |ui| {
                            widgets::menu_begin(ui, 260.0);
                            for m in &models {
                                let mut on = e.current && e.model.as_deref() == Some(m);
                                if widgets::menu_check(ui, &p, m, &mut on) && on {
                                    pick = Some((e.kind.clone(), Some(m.clone())));
                                    ui.close();
                                }
                            }
                        })
                        .response
                        .on_hover_cursor(CursorIcon::PointingHand);
                        continue;
                    }
                    let row = widgets::menu_item_detail(ui, &p, &e.label, &detail, e.current, true);
                    if row {
                        if needs_form || (!e.available) {
                            configure = Some(e.kind.clone());
                        } else {
                            pick = Some((e.kind.clone(), None));
                        }
                    }
                }
                widgets::menu_separator(ui, &p);
                if menu_item(ui, &p, "Engine settings…", None, true) {
                    configure = Some(
                        self.st
                            .engine
                            .as_ref()
                            .map(|e| e.kind.clone())
                            .unwrap_or_else(|| "claude".into()),
                    );
                }
                if let Some((kind, model)) = pick {
                    self.st.set_engine(&kind, model.as_deref(), None, None);
                }
                if let Some(kind) = configure {
                    self.open_engine_form(&kind);
                }
            })
            .response;
        resp.on_hover_cursor(CursorIcon::PointingHand)
            .on_hover_text("The AI runtime the crew runs on. Click to switch.");
        let (dot_rect, _) = ui.allocate_exact_size(vec2(10.0, 10.0), Sense::hover());
        ui.painter()
            .circle_filled(dot_rect.center(), 3.0, if ok { p.green } else { p.amber });
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
                    ui.painter().circle_filled(
                        pos2(rect.left() + 12.0, rect.center().y),
                        3.0,
                        if e.available { p.green } else { p.text3 },
                    );
                    ui.painter().text(
                        pos2(rect.left() + 24.0, rect.center().y),
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
                        "Folks restart on their next turn.",
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
            if menu_item(ui, &p, "Reveal project folder", None, open) {
                if let Some(pr) = &self.st.project {
                    reveal(&pr.dir.display().to_string());
                }
            }
            if menu_item(ui, &p, "Close project", None, open) {
                self.st.close_project();
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
            if menu_item(ui, &p, "Design this area", None, can) {
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
                    icons::paint_mark(ui.painter(), r, p.accent, egui::Color32::WHITE);
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
        self.tab_strip(ui);
        match self.st.tab {
            Tab::Twin => {
                let st = &mut self.st;
                let resp = self.twin.show(
                    ui,
                    &st.palette,
                    st.design.as_ref(),
                    st.streets.as_ref(),
                    &mut st.layers,
                    &st.selection,
                    &st.changed,
                );
                st.cursor = resp.cursor;
                st.hovered = resp.hovered;
                if let Some(id) = resp.clicked {
                    st.selection = vec![id];
                } else if resp.clicked_empty {
                    st.selection.clear();
                }
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
            self.st.selection.clear();
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
    fn ui(&mut self, ui: &mut Ui, _frame: &mut eframe::Frame) {
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
                        chat_panel(ui, &mut self.st, &mut self.composer);
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
                        None => {}
                    });
            }
            egui::CentralPanel::default()
                .frame(Frame::new().fill(p.bg1).inner_margin(Margin::ZERO))
                .show(ui, |ui| self.workspace(ui));
        }

        self.about_window(ui.ctx());
        self.engine_window(ui.ctx());
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
