//! The window shell: top bar, chat column, workspace tabs, inspector, status bar, shortcuts.

use std::path::PathBuf;
use std::time::Duration;

use egui::{Key, RichText, Ui};
use openftth_client::Client;

use crate::chat::{chat_panel, Composer};
use crate::inspector::{inspector, InspectorAction};
use crate::state::{AppState, Tab};
use crate::tabs::{backstage_tab, boq_tab, splice_tab, versions_tab, TabState};
use crate::theme::{self, Mode};
use crate::twin::TwinView;
use crate::welcome::{welcome, OpenForm};
use crate::widgets::{dot, ghost_button, pill, pulse_dot};

pub struct OpenFtthApp {
    st: AppState,
    composer: Composer,
    tabs: TabState,
    twin: TwinView,
    form: OpenForm,
    mode: Mode,
    applied_mode: Option<Mode>,
}

impl OpenFtthApp {
    pub fn new(_cc: &eframe::CreationContext<'_>, client: Client) -> Self {
        let st = AppState::new(client);
        let fixture = repo_root().map(|r| r.join("fixtures").join("gent"));
        Self {
            st,
            composer: Composer::default(),
            tabs: TabState::default(),
            twin: TwinView::default(),
            form: OpenForm::new(fixture),
            mode: Mode::Light,
            applied_mode: None,
        }
    }

    fn top_bar(&mut self, ui: &mut Ui) {
        let p = self.st.palette;
        ui.horizontal(|ui| {
            ui.add_space(4.0);
            ui.label(
                RichText::new("openFTTH")
                    .size(18.0)
                    .strong()
                    .color(p.accent),
            );
            if let Some(pr) = &self.st.project {
                ui.separator();
                ui.label(RichText::new(file_stem(&pr.area_path)).strong())
                    .on_hover_text(format!(
                        "project {}
area {}
map {}",
                        pr.id,
                        pr.area_path,
                        pr.osm_path.as_deref().unwrap_or("—")
                    ));
                pill(ui, &pr.rulebook, p.accent, p.accent_soft);
                if let Some(stage) = &self.st.stage {
                    pill(ui, stage, p.text_muted, p.surface_alt);
                }
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let label = if self.mode == Mode::Light {
                    "Dark"
                } else {
                    "Light"
                };
                if ghost_button(ui, &p, label)
                    .on_hover_text("Switch theme")
                    .clicked()
                {
                    self.mode = if self.mode == Mode::Light {
                        Mode::Dark
                    } else {
                        Mode::Light
                    };
                }
                if self.st.project.is_some() && ghost_button(ui, &p, "Close project").clicked() {
                    self.st.close_project();
                }
                if self.st.connected {
                    dot(ui, p.success, 4.0);
                    ui.label(RichText::new("connected").small().color(p.text_muted));
                } else {
                    pulse_dot(ui, p.danger, 4.0);
                    ui.label(RichText::new("reconnecting…").small().color(p.danger));
                }
            });
        });
    }

    fn status_bar(&self, ui: &mut Ui) {
        let p = self.st.palette;
        ui.horizontal(|ui| {
            if let Some((lon, lat)) = self.st.cursor {
                ui.label(
                    RichText::new(format!("{lon:.5}, {lat:.5}"))
                        .monospace()
                        .small()
                        .color(p.text_muted),
                );
            }
            if let Some(h) = &self.st.hovered {
                ui.label(RichText::new(self.st.element_name(h)).small());
            }
            if let Some(v) = self.st.versions.first() {
                ui.separator();
                ui.label(
                    RichText::new(format!(
                        "head {}",
                        crate::widgets::short_id(v["version"].as_str().unwrap_or(""))
                    ))
                    .monospace()
                    .small()
                    .color(p.text_muted),
                );
            }
            if let Some(sum) = &self.st.last_version_summary {
                ui.label(RichText::new(sum).small().color(p.text_muted));
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                ui.label(
                    RichText::new(self.st.client.base())
                        .small()
                        .color(p.text_muted),
                );
                ui.label(
                    RichText::new("F fit · Esc clear selection · Ctrl+1…5 tabs")
                        .small()
                        .color(p.text_muted),
                );
            });
        });
    }

    fn workspace(&mut self, ui: &mut Ui) {
        let p = self.st.palette;
        ui.horizontal(|ui| {
            for (i, tab) in Tab::ALL.iter().enumerate() {
                let selected = self.st.tab == *tab;
                let text = RichText::new(tab.label()).color(if selected { p.accent } else { p.text });
                if ui.selectable_label(selected, text).on_hover_text(format!("Ctrl+{}", i + 1)).clicked() {
                    self.st.tab = *tab;
                    if *tab == Tab::Backstage {
                        self.st.refresh_backstage();
                    }
                }
            }
            if !self.st.changed.is_empty() {
                pill(ui, &format!("{} changed since last version", self.st.changed.len()), egui::Color32::WHITE, p.danger).on_hover_text("Red elements changed in the latest version. Versions → Clear highlight to hide.");
            }
        });
        ui.separator();
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
            Tab::Boq => boq_tab(ui, &self.st),
            Tab::Splice => splice_tab(ui, &self.st, &mut self.tabs),
            Tab::Versions => versions_tab(ui, &mut self.st),
            Tab::Backstage => backstage_tab(ui, &mut self.st, &mut self.tabs),
        }
    }

    fn shortcuts(&mut self, ui: &Ui) {
        let input = ui.input(|i| {
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
        let typing = ui.ctx().egui_wants_keyboard_input();
        if input.0 {
            self.st.selection.clear();
        }
        if input.1 {
            for (pressed, tab) in [
                (input.2, Tab::Twin),
                (input.3, Tab::Boq),
                (input.4, Tab::Splice),
                (input.5, Tab::Versions),
                (input.6, Tab::Backstage),
            ] {
                if pressed {
                    self.st.tab = tab;
                }
            }
        }
        if input.7 && !typing {
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

        egui::Panel::top("top").show(ui, |ui| {
            ui.add_space(6.0);
            self.top_bar(ui);
            ui.add_space(6.0);
        });
        egui::Panel::bottom("status").show(ui, |ui| {
            ui.add_space(2.0);
            self.status_bar(ui);
            ui.add_space(2.0);
        });

        if self.st.project.is_none() {
            egui::CentralPanel::default().show(ui, |ui| welcome(ui, &mut self.st, &mut self.form));
        } else {
            egui::Panel::left("chat")
                .resizable(true)
                .default_size(430.0)
                .min_size(340.0)
                .show(ui, |ui| {
                    ui.add_space(6.0);
                    chat_panel(ui, &mut self.st, &mut self.composer);
                });
            if !self.st.selection.is_empty() {
                egui::Panel::right("inspector")
                    .resizable(true)
                    .default_size(320.0)
                    .min_size(260.0)
                    .show(ui, |ui| {
                        ui.add_space(6.0);
                        egui::ScrollArea::vertical()
                            .auto_shrink([false, false])
                            .show(ui, |ui| match inspector(ui, &mut self.st) {
                                Some(InspectorAction::Ask(q)) => self.st.send(&q),
                                Some(InspectorAction::ZoomTo(id)) => {
                                    if let Some(e) =
                                        self.st.design.as_ref().and_then(|d| d.get(&id))
                                    {
                                        self.twin.focus(e);
                                        self.st.tab = Tab::Twin;
                                    }
                                }
                                None => {}
                            });
                    });
            }
            egui::CentralPanel::default().show(ui, |ui| self.workspace(ui));
        }

        let palette = self.st.palette;
        self.st.toasts.show(ui.ctx(), &palette);
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
