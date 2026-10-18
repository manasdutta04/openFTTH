//! The window shell: top bar, crew panel, editor tab strip, properties panel, status bar.

use std::path::PathBuf;
use std::time::Duration;

use egui::{pos2, vec2, Frame, Key, Margin, Sense, Stroke, Ui};
use openftth_client::Client;

use crate::chat::{chat_panel, Composer};
use crate::icons::{self, Icon};
use crate::inspector::{inspector, InspectorAction};
use crate::state::{AppState, Tab};
use crate::tabs::{backstage_tab, boq_tab, splice_tab, versions_tab, TabState};
use crate::theme::{self, font, Mode, Weight};
use crate::twin::TwinView;
use crate::welcome::{welcome, OpenForm};
use crate::widgets::{
    self, button, dot, icon_button, mono_text, pulse_dot, tag, text, vline, Kind,
};

pub struct OpenFtthApp {
    st: AppState,
    composer: Composer,
    tabs: TabState,
    twin: TwinView,
    form: OpenForm,
    mode: Mode,
    applied_mode: Option<Mode>,
    frames: u8,
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
            frames: 0,
        }
    }

    fn top_bar(&mut self, ui: &mut Ui) {
        let p = self.st.palette;
        ui.horizontal(|ui| {
            ui.set_height(24.0);
            let (r, _) = ui.allocate_exact_size(vec2(18.0, 18.0), Sense::hover());
            icons::paint(ui.painter(), r, Icon::Mark, p.accent);
            ui.label(text("openFTTH", 13.0, Weight::Semibold, p.text));
            if let Some(pr) = &self.st.project {
                ui.add_space(6.0);
                vline(ui, &p, 16.0);
                ui.add_space(6.0);
                ui.label(text(file_stem(&pr.area_path), 12.5, Weight::Medium, p.text))
                    .on_hover_text(format!(
                        "{}\n{}\nproject {}",
                        pr.area_path,
                        pr.osm_path.as_deref().unwrap_or("no map extract"),
                        pr.id
                    ));
                ui.label(text(&pr.rulebook, 12.0, Weight::Regular, p.text2));
                if let Some(stage) = &self.st.stage {
                    tag(ui, stage, p.text2, p.bg3);
                }
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if icon_button(
                    ui,
                    &p,
                    if self.mode == Mode::Dark {
                        Icon::Sun
                    } else {
                        Icon::Moon
                    },
                    "Theme",
                )
                .clicked()
                {
                    self.mode = if self.mode == Mode::Dark {
                        Mode::Light
                    } else {
                        Mode::Dark
                    };
                }
                if self.st.project.is_some()
                    && button(ui, &p, "Close project", Kind::Ghost).clicked()
                {
                    self.st.close_project();
                }
                ui.add_space(4.0);
                if self.st.connected {
                    ui.label(text("connected", 11.0, Weight::Regular, p.text3));
                    dot(ui, p.green, 3.5);
                } else {
                    ui.label(text("reconnecting", 11.0, Weight::Regular, p.red));
                    pulse_dot(ui, p.red, 3.5);
                }
            });
        });
    }

    fn status_bar(&self, ui: &mut Ui) {
        let p = self.st.palette;
        ui.horizontal(|ui| {
            ui.set_height(16.0);
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
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
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
                let galley = ui.painter().layout_no_wrap(tab.label().to_string(), font(12.5, if active { Weight::Medium } else { Weight::Regular }), if active { p.text } else { p.text2 });
                let (rect, resp) = ui.allocate_exact_size(vec2(galley.size().x + 24.0, 30.0), Sense::click());
                let color = if active || resp.hovered() { p.text } else { p.text2 };
                ui.painter().galley(pos2(rect.left() + 12.0, rect.center().y - galley.size().y / 2.0), galley, color);
                if active {
                    ui.painter().rect_filled(egui::Rect::from_min_max(pos2(rect.left() + 8.0, rect.bottom() - 2.0), pos2(rect.right() - 8.0, rect.bottom())), 1.0, p.accent);
                }
                if resp.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    self.st.tab = tab;
                    if tab == Tab::Backstage {
                        self.st.refresh_backstage();
                    }
                }
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                ui.add_space(10.0);
                if !self.st.changed.is_empty() {
                    let r = tag(ui, &format!("{} changed", self.st.changed.len()), p.red, p.red.gamma_multiply(0.18));
                    r.on_hover_text("Elements changed in the latest version are drawn in red. Versions → Clear.");
                }
                if self.st.tab == Tab::Twin {
                    if let Some(d) = &self.st.design {
                        let c = d.counts();
                        let line = [("home", "homes"), ("fat", "FATs"), ("fdh", "FDHs")].iter().filter_map(|(k, l)| c.get(*k).map(|n| format!("{n} {l}"))).collect::<Vec<_>>().join("  ·  ");
                        ui.label(text(line, 11.0, Weight::Regular, p.text3));
                    }
                }
            });
        });
        widgets::hairline(ui, &p);
    }

    fn workspace(&mut self, ui: &mut Ui) {
        self.tab_strip(ui);
        let p = self.st.palette;
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
                let _ = p;
            }
        }
    }

    fn shortcuts(&mut self, ui: &Ui) {
        let typing = ui.ctx().egui_wants_keyboard_input();
        let (esc, ctrl, n1, n2, n3, n4, n5, f) = ui.input(|i| {
            (
                i.key_pressed(Key::Escape),
                i.modifiers.ctrl,
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
        if self.frames < 3 {
            self.frames += 1;
            if self.frames == 2 {
                ui.ctx()
                    .send_viewport_cmd(egui::ViewportCommand::Maximized(true));
            }
        }
        self.st.drain_frames();
        self.shortcuts(ui);
        let p = self.st.palette;

        egui::Panel::top("top")
            .resizable(false)
            .show_separator_line(false)
            .frame(
                Frame::new()
                    .fill(p.bg0)
                    .inner_margin(Margin::symmetric(10, 6))
                    .stroke(Stroke::NONE),
            )
            .show(ui, |ui| self.top_bar(ui));
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
            egui::Panel::left("crew")
                .resizable(true)
                .default_size(380.0)
                .min_size(320.0)
                .show_separator_line(true)
                .frame(Frame::new().fill(p.bg1).inner_margin(Margin::ZERO))
                .show(ui, |ui| {
                    chat_panel(ui, &mut self.st, &mut self.composer);
                });
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

        self.st.toasts.show(ui.ctx(), &p);
        ui.ctx()
            .request_repaint_after(Duration::from_millis(if self.st.busy() {
                100
            } else {
                300
            }));
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
