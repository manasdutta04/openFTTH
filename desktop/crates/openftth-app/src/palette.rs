//! The command palette (Ctrl+K): one search box for every action in the app and every named
//! element of the design. Arrow keys move, Enter runs, Esc closes.

use egui::{pos2, vec2, Align2, CursorIcon, Key, Rect, Sense};

use crate::icons::{self, Icon};
use crate::state::AppState;
use crate::theme::{font, Weight};

/// What a palette entry does when chosen.
#[derive(Clone, PartialEq)]
pub enum Command {
    DesignAutomatically,
    ImportData,
    ConnectAiApps,
    Tab(crate::state::Tab),
    FitDesign,
    ThemeDark,
    ThemeLight,
    CloseProject,
    NewProject,
    RevealFolder,
    About,
    Undo,
    Redo,
    Join,
    Tool(&'static str),
    Element(String),
}

struct Entry {
    title: String,
    hint: String,
    group: &'static str,
    command: Command,
}

#[derive(Default)]
pub struct CommandPalette {
    pub open: bool,
    query: String,
    selected: usize,
    focus: bool,
}

impl CommandPalette {
    pub fn toggle(&mut self) {
        self.open = !self.open;
        self.query.clear();
        self.selected = 0;
        self.focus = true;
    }

    /// Draw the palette when open; returns the command chosen this frame.
    pub fn show(&mut self, ctx: &egui::Context, st: &AppState) -> Option<Command> {
        if !self.open {
            return None;
        }
        let p = st.palette;
        let entries = self.matches(st);
        let (up, down, enter, esc) = ctx.input(|i| {
            (
                i.key_pressed(Key::ArrowUp),
                i.key_pressed(Key::ArrowDown),
                i.key_pressed(Key::Enter),
                i.key_pressed(Key::Escape),
            )
        });
        if esc {
            self.open = false;
            return None;
        }
        if down {
            self.selected = (self.selected + 1).min(entries.len().saturating_sub(1));
        }
        if up {
            self.selected = self.selected.saturating_sub(1);
        }
        let mut chosen: Option<Command> = None;
        if enter {
            chosen = entries.get(self.selected).map(|e| e.command.clone());
        }
        let screen = ctx.content_rect();
        // A dimmed backdrop: a click outside closes the palette.
        let backdrop = egui::Area::new(egui::Id::unique("palette-backdrop"))
            .order(egui::Order::Foreground)
            .fixed_pos(screen.min)
            .show(ctx, |ui| {
                let (r, resp) = ui.allocate_exact_size(screen.size(), Sense::click());
                ui.painter()
                    .rect_filled(r, 0.0, egui::Color32::from_black_alpha(90));
                resp.clicked()
            });
        if backdrop.inner {
            self.open = false;
            return None;
        }
        let w = 600.0_f32.min(screen.width() - 32.0);
        egui::Area::new(egui::Id::unique("palette"))
            .order(egui::Order::Tooltip)
            .fixed_pos(pos2(screen.center().x - w / 2.0, screen.top() + 90.0))
            .show(ctx, |ui| {
                egui::Frame::new()
                    .fill(p.bg1)
                    .stroke(egui::Stroke::new(1.0, p.line_strong))
                    .corner_radius(12.0)
                    .shadow(egui::epaint::Shadow {
                        offset: [0, 12],
                        blur: 32,
                        spread: 0,
                        color: egui::Color32::from_black_alpha(70),
                    })
                    .inner_margin(egui::Margin::same(10))
                    .show(ui, |ui| {
                        ui.set_width(w - 20.0);
                        ui.horizontal(|ui| {
                            let (r, _) = ui.allocate_exact_size(vec2(18.0, 28.0), Sense::hover());
                            icons::paint(ui.painter(), icons::square(r.center(), 15.0), Icon::Search, p.text3);
                            let field = ui.add(
                                egui::TextEdit::singleline(&mut self.query)
                                    .hint_text("Search actions and elements: design, export, FAT-012, an address…")
                                    .frame(egui::Frame::NONE)
                                    .font(font(15.0, Weight::Regular))
                                    .desired_width(f32::INFINITY),
                            );
                            if self.focus {
                                field.request_focus();
                                self.focus = false;
                            }
                            if field.changed() {
                                self.selected = 0;
                            }
                        });
                        ui.add_space(6.0);
                        crate::widgets::hairline(ui, &p);
                        ui.add_space(4.0);
                        if entries.is_empty() {
                            ui.add_space(10.0);
                            ui.label(crate::widgets::text("Nothing matches.", 12.5, Weight::Regular, p.text3));
                            ui.add_space(10.0);
                            return;
                        }
                        let mut last_group = "";
                        egui::ScrollArea::vertical().max_height(380.0).show(ui, |ui| {
                            for (i, e) in entries.iter().enumerate() {
                                if e.group != last_group {
                                    ui.add_space(4.0);
                                    ui.label(crate::widgets::caps(e.group, p.text3));
                                    ui.add_space(2.0);
                                    last_group = e.group;
                                }
                                let (r, resp) =
                                    ui.allocate_exact_size(vec2(ui.available_width(), 30.0), Sense::click());
                                let active = i == self.selected;
                                if active || resp.hovered() {
                                    ui.painter().rect_filled(r, 6.0, if active { p.accent_soft } else { p.bg3 });
                                }
                                if active && (up || down) {
                                    ui.scroll_to_rect(r, None);
                                }
                                ui.painter().text(
                                    pos2(r.left() + 10.0, r.center().y),
                                    Align2::LEFT_CENTER,
                                    &e.title,
                                    font(13.0, if active { Weight::Medium } else { Weight::Regular }),
                                    p.text,
                                );
                                ui.painter().text(
                                    pos2(r.right() - 10.0, r.center().y),
                                    Align2::RIGHT_CENTER,
                                    &e.hint,
                                    font(11.5, Weight::Regular),
                                    p.text3,
                                );
                                if resp.on_hover_cursor(CursorIcon::PointingHand).clicked() {
                                    chosen = Some(e.command.clone());
                                }
                            }
                        });
                        ui.add_space(4.0);
                        crate::widgets::hairline(ui, &p);
                        let hint = Rect::from_min_size(ui.cursor().min, vec2(ui.available_width(), 22.0));
                        ui.allocate_rect(hint, Sense::hover());
                        ui.painter().text(
                            hint.left_center() + vec2(4.0, 2.0),
                            Align2::LEFT_CENTER,
                            "↑↓ to move · Enter to run · Esc to close",
                            font(11.0, Weight::Regular),
                            p.text3,
                        );
                    });
            });
        if chosen.is_some() {
            self.open = false;
        }
        chosen
    }

    fn matches(&self, st: &AppState) -> Vec<Entry> {
        let q = self.query.trim().to_lowercase();
        let open = st.project.is_some();
        let mut out: Vec<Entry> = Vec::new();
        let mut action = |title: &str, hint: &str, command: Command, enabled: bool| {
            if enabled {
                out.push(Entry {
                    title: title.into(),
                    hint: hint.into(),
                    group: "Actions",
                    command,
                });
            }
        };
        use crate::state::Tab;
        action(
            "Design automatically",
            "free, no AI",
            Command::DesignAutomatically,
            open && !st.busy(),
        );
        action("Import data…", "any GIS format", Command::ImportData, true);
        action("Connect AI apps…", "MCP link", Command::ConnectAiApps, true);
        action("Go to the map", "Ctrl+1", Command::Tab(Tab::Twin), open);
        action(
            "Go to the bill of quantities",
            "Ctrl+2",
            Command::Tab(Tab::Boq),
            open,
        );
        action("Go to splices", "Ctrl+3", Command::Tab(Tab::Splice), open);
        action(
            "Go to versions",
            "Ctrl+4",
            Command::Tab(Tab::Versions),
            open,
        );
        action(
            "Go to backstage",
            "Ctrl+5",
            Command::Tab(Tab::Backstage),
            open,
        );
        action("Fit the design", "F", Command::FitDesign, open);
        action(
            "Undo the last edit",
            "Ctrl+Z",
            Command::Undo,
            open && st.can_undo(),
        );
        action("Redo", "Ctrl+Y", Command::Redo, open && st.can_redo());
        action("Add a FAT", "T", Command::Tool("fat"), open);
        action("Add a cabinet (FDH)", "H", Command::Tool("fdh"), open);
        action("Add a chamber", "C", Command::Tool("chamber"), open);
        action("Draw a cable", "D", Command::Tool("draw"), open);
        action("Split a cable", "S", Command::Tool("split"), open);
        action(
            "Reassign a home or FAT",
            "R",
            Command::Tool("reassign"),
            open,
        );
        action("Measure a distance", "M", Command::Tool("measure"), open);
        action(
            "Join the two selected cables",
            "J",
            Command::Join,
            open && crate::editor::join_pair(st).is_some(),
        );
        action(
            "Go to the attribute table",
            "Ctrl+6",
            Command::Tab(Tab::Table),
            open,
        );
        action(
            "Reveal project folder",
            "exports, design files",
            Command::RevealFolder,
            open,
        );
        action("Dark theme", "", Command::ThemeDark, true);
        action("Light theme", "", Command::ThemeLight, true);
        action("New project", "start page", Command::NewProject, true);
        action("Close project", "", Command::CloseProject, open);
        action("About openFTTH", "", Command::About, true);
        let mut entries: Vec<Entry> = out
            .into_iter()
            .filter(|e| q.is_empty() || e.title.to_lowercase().contains(&q))
            .collect();
        // Elements by name once a query is typed: FATs, cabinets, POPs, chambers, crossings, homes.
        if q.chars().count() >= 2 {
            if let Some(d) = &st.design {
                let mut found: Vec<Entry> = d
                    .elements
                    .iter()
                    .filter(|e| {
                        e.kind() != "drop" && e.kind() != "building" && e.kind() != "service_area"
                    })
                    .filter_map(|e| {
                        let name = e.name();
                        name.to_lowercase().contains(&q).then(|| Entry {
                            hint: e.kind().replace('_', " "),
                            title: name,
                            group: "Elements",
                            command: Command::Element(e.element_uuid.clone()),
                        })
                    })
                    .take(40)
                    .collect();
                found.sort_by(|a, b| {
                    a.title
                        .len()
                        .cmp(&b.title.len())
                        .then(a.title.cmp(&b.title))
                });
                entries.extend(found.into_iter().take(20));
            }
        }
        entries
    }
}
