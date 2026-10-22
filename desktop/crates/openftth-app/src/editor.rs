//! Editing on the map: a tool bar (select, add a FAT, cabinet or chamber, draw a cable, split
//! it, reassign, delete, undo, redo), an edit bar with the reason that goes on the record, and
//! the click handling that turns each into one engine edit. Every edit is an engine tool call
//! that snaps to the street twin; nothing here decides a number.

use egui::{pos2, vec2, Align2, Color32, CursorIcon, Pos2, Rect, Sense, Shape, Stroke, Ui};
use openftth_core::geo::Mercator;
use serde_json::json;

use crate::state::AppState;
use crate::theme::{font, Palette, Weight};
use crate::twin::{TwinResponse, TwinView};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Tool {
    Select,
    AddFat,
    AddFdh,
    AddChamber,
    Draw,
    Split,
    Reassign,
}

impl Tool {
    fn label(self) -> &'static str {
        match self {
            Tool::Select => "Select",
            Tool::AddFat => "Add FAT",
            Tool::AddFdh => "Add cabinet",
            Tool::AddChamber => "Add chamber",
            Tool::Draw => "Draw cable",
            Tool::Split => "Split cable",
            Tool::Reassign => "Reassign",
        }
    }

    fn key(self) -> &'static str {
        match self {
            Tool::Select => "V",
            Tool::AddFat => "T",
            Tool::AddFdh => "H",
            Tool::AddChamber => "C",
            Tool::Draw => "D",
            Tool::Split => "S",
            Tool::Reassign => "R",
        }
    }

    fn hint(self) -> &'static str {
        match self {
            Tool::Select => "",
            Tool::AddFat | Tool::AddFdh | Tool::AddChamber => {
                "Click where it goes: it snaps to the nearest street node and is pinned"
            }
            Tool::Draw => "Click points along the route · Enter or double-click finishes · Backspace drops the last point",
            Tool::Split => "Click a cable where it should be split: a chamber is added there",
            Tool::Reassign => "Select a home or a FAT, then click the FAT or cabinet it should go to",
        }
    }
}

/// What a tool bar button asks the app to do besides choosing a tool.
pub enum BarAction {
    Undo,
    Redo,
    Delete,
}

pub struct Editor {
    pub tool: Tool,
    pub reason: String,
    pub cable_kind: &'static str,
    pub points: Vec<(f64, f64)>,
}

impl Default for Editor {
    fn default() -> Self {
        Self {
            tool: Tool::Select,
            reason: "manual edit".into(),
            cable_kind: "distribution",
            points: Vec::new(),
        }
    }
}

impl Editor {
    pub fn set_tool(&mut self, tool: Tool) {
        self.tool = tool;
        self.points.clear();
    }

    fn reason(&self) -> String {
        let r = self.reason.trim();
        if r.is_empty() {
            "manual edit".into()
        } else {
            r.to_string()
        }
    }

    /// The tool bar down the left edge of the map.
    pub fn toolbar(
        &mut self,
        ui: &mut Ui,
        rect: Rect,
        p: &Palette,
        st: &AppState,
    ) -> Option<BarAction> {
        let mut action = None;
        let busy = st.busy() || st.editing();
        egui::Area::new(egui::Id::unique("edit-toolbar"))
            .fixed_pos(rect.left_top() + vec2(10.0, 10.0))
            .order(egui::Order::Foreground)
            .show(ui.ctx(), |ui| {
                crate::widgets::float_frame(p).show(ui, |ui| {
                    ui.spacing_mut().item_spacing.y = 2.0;
                    // The divider takes the available width; keep the bar as narrow as a button.
                    ui.set_max_width(32.0);
                    for tool in [
                        Tool::Select,
                        Tool::AddFat,
                        Tool::AddFdh,
                        Tool::AddChamber,
                        Tool::Draw,
                        Tool::Split,
                        Tool::Reassign,
                    ] {
                        let active = self.tool == tool;
                        let resp = tool_button(
                            ui,
                            p,
                            active,
                            !busy || tool == Tool::Select,
                            |pa, r, c| glyph(pa, r, tool, c),
                        )
                        .on_hover_text(format!(
                            "{} ({})",
                            tool.label(),
                            tool.key()
                        ));
                        if resp.clicked() {
                            self.set_tool(if active { Tool::Select } else { tool });
                        }
                    }
                    crate::widgets::hairline(ui, p);
                    let has_sel = !st.selection.is_empty();
                    if tool_button(ui, p, false, has_sel && !busy, trash)
                        .on_hover_text("Delete the selection (Del)")
                        .clicked()
                    {
                        action = Some(BarAction::Delete);
                    }
                    if tool_button(ui, p, false, st.can_undo() && !busy, |pa, r, c| {
                        undo_glyph(pa, r, c, false)
                    })
                    .on_hover_text(undo_hint(st, "undo", "Undo (Ctrl+Z)"))
                    .clicked()
                    {
                        action = Some(BarAction::Undo);
                    }
                    if tool_button(ui, p, false, st.can_redo() && !busy, |pa, r, c| {
                        undo_glyph(pa, r, c, true)
                    })
                    .on_hover_text(undo_hint(st, "redo", "Redo (Ctrl+Y)"))
                    .clicked()
                    {
                        action = Some(BarAction::Redo);
                    }
                });
            });
        action
    }

    /// The bar across the top of the map while a tool is active: what to do, the cable type,
    /// and the reason that goes on the record with every edit.
    pub fn edit_bar(&mut self, ui: &mut Ui, rect: Rect, p: &Palette, st: &mut AppState) {
        if self.tool == Tool::Select {
            return;
        }
        let mut finish = false;
        let mut cancel = false;
        egui::Area::new(egui::Id::unique("edit-bar"))
            .fixed_pos(pos2(rect.center().x, rect.top() + 10.0))
            .pivot(Align2::CENTER_TOP)
            .order(egui::Order::Foreground)
            .show(ui.ctx(), |ui| {
                crate::widgets::float_frame(p).show(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.label(crate::widgets::text(
                            self.tool.label(),
                            12.5,
                            Weight::Semibold,
                            p.text,
                        ));
                        if self.tool == Tool::Draw {
                            for kind in ["feeder", "distribution", "drop"] {
                                let on = self.cable_kind == kind;
                                if ui
                                    .selectable_label(
                                        on,
                                        crate::widgets::text(
                                            kind,
                                            12.0,
                                            Weight::Medium,
                                            if on { p.accent } else { p.text2 },
                                        ),
                                    )
                                    .clicked()
                                {
                                    self.cable_kind = kind;
                                }
                            }
                        }
                        ui.add_space(6.0);
                        ui.label(crate::widgets::text(
                            "Reason",
                            11.5,
                            Weight::Medium,
                            p.text3,
                        ));
                        ui.add(egui::TextEdit::singleline(&mut self.reason).desired_width(180.0));
                        if self.tool == Tool::Draw {
                            let n = self.points.len();
                            if crate::widgets::button_enabled(
                                ui,
                                p,
                                &format!("Finish ({n})"),
                                crate::widgets::Kind::Prominent,
                                n >= 2,
                            )
                            .clicked()
                            {
                                finish = true;
                            }
                        }
                        if crate::widgets::button(ui, p, "Done", crate::widgets::Kind::Quiet)
                            .clicked()
                        {
                            cancel = true;
                        }
                        if st.editing() {
                            ui.spinner();
                        }
                    });
                    ui.label(crate::widgets::text(
                        self.tool.hint(),
                        11.5,
                        Weight::Regular,
                        p.text3,
                    ));
                });
            });
        if finish {
            self.finish_cable(st);
        }
        if cancel {
            self.set_tool(Tool::Select);
        }
    }

    /// Send the cable being drawn.
    pub fn finish_cable(&mut self, st: &mut AppState) {
        if self.points.len() < 2 {
            return;
        }
        let pts: Vec<[f64; 2]> = self.points.iter().map(|(lon, lat)| [*lon, *lat]).collect();
        st.edit(
            "draw_cable",
            json!({ "kind": self.cable_kind, "points": pts, "reason": self.reason() }),
        );
        self.points.clear();
    }

    /// A click on the map with the current tool. Returns true when the tool used the click (so
    /// it does not also change the selection).
    pub fn on_map(&mut self, resp: &TwinResponse, st: &mut AppState) -> bool {
        if self.tool == Tool::Select {
            return false;
        }
        if resp.response.hovered() {
            resp.response.ctx.set_cursor_icon(CursorIcon::Crosshair);
        }
        if self.tool == Tool::Draw && resp.response.double_clicked() {
            self.finish_cable(st);
            return true;
        }
        let Some((lon, lat)) = resp.click else {
            return false;
        };
        if st.editing() || st.busy() {
            st.toasts.info("Wait for the last change to finish");
            return true;
        }
        let reason = self.reason();
        let kind_of = |id: &str| {
            st.design
                .as_ref()
                .and_then(|d| d.get(id))
                .map(|e| e.kind().to_string())
                .unwrap_or_default()
        };
        match self.tool {
            Tool::AddFat | Tool::AddFdh | Tool::AddChamber => {
                let kind = match self.tool {
                    Tool::AddFat => "fat",
                    Tool::AddFdh => "fdh",
                    _ => "chamber",
                };
                st.edit(
                    "add_site",
                    json!({ "kind": kind, "lon": lon, "lat": lat, "reason": reason }),
                );
            }
            Tool::Draw => self.points.push((lon, lat)),
            Tool::Split => match resp.clicked.as_deref() {
                Some(id) if ["feeder", "distribution", "drop"].contains(&kind_of(id).as_str()) => {
                    st.edit(
                        "split_cable",
                        json!({ "element_uuid": id, "lon": lon, "lat": lat, "reason": reason }),
                    );
                }
                _ => st.toasts.info("Click on a cable to split it"),
            },
            Tool::Reassign => {
                let from = st.selection.first().cloned();
                match (from, resp.clicked.clone()) {
                    (Some(from), Some(to)) if from != to => {
                        let pair = (kind_of(&from), kind_of(&to));
                        if pair == ("home".into(), "fat".into())
                            || pair == ("fat".into(), "fdh".into())
                        {
                            st.edit(
                                "reassign",
                                json!({ "element_uuid": from, "to_uuid": to, "reason": reason }),
                            );
                        } else {
                            st.toasts
                                .info("Reassign a home to a FAT, or a FAT to a cabinet");
                        }
                    }
                    (None, Some(id)) => st.selection = vec![id],
                    _ => st
                        .toasts
                        .info("Select a home or a FAT first, then click where it goes"),
                }
            }
            Tool::Select => {}
        }
        true
    }

    /// The cable being drawn: its points and a rubber band to the pointer.
    pub fn overlay(&self, ui: &Ui, rect: Rect, view: &TwinView, p: &Palette) {
        if self.tool != Tool::Draw || self.points.is_empty() {
            return;
        }
        let painter = ui.painter_at(rect);
        let mut pts: Vec<Pos2> = self
            .points
            .iter()
            .map(|(lon, lat)| view.to_screen(rect, Mercator::project(*lon, *lat)))
            .collect();
        if let Some(cur) = ui.ctx().pointer_hover_pos().filter(|q| rect.contains(*q)) {
            pts.push(cur);
        }
        let color = p.kind(self.cable_kind);
        painter.extend(Shape::dashed_line(&pts, Stroke::new(2.5, color), 8.0, 5.0));
        for (i, q) in pts.iter().enumerate().take(self.points.len()) {
            painter.circle_filled(*q, 4.5, color);
            painter.circle_stroke(*q, 4.5, Stroke::new(1.5, p.map_bg));
            if i == 0 {
                painter.text(
                    *q + vec2(8.0, -8.0),
                    Align2::LEFT_BOTTOM,
                    "start",
                    font(10.5, Weight::Medium),
                    p.text2,
                );
            }
        }
    }
}

fn undo_hint(st: &AppState, which: &str, fallback: &str) -> String {
    st.edits
        .as_ref()
        .and_then(|e| {
            e[which]["summary"]
                .as_str()
                .map(|s| format!("{fallback}: {s}"))
        })
        .unwrap_or_else(|| fallback.to_string())
}

fn tool_button(
    ui: &mut Ui,
    p: &Palette,
    active: bool,
    enabled: bool,
    draw: impl FnOnce(&egui::Painter, Rect, Color32),
) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(
        vec2(32.0, 32.0),
        if enabled {
            Sense::click()
        } else {
            Sense::hover()
        },
    );
    let painter = ui.painter();
    if active {
        painter.rect_filled(rect, 6.0, p.accent_soft);
    } else if enabled && resp.hovered() {
        painter.rect_filled(rect, 6.0, p.bg3);
    }
    let c = if !enabled {
        p.text3.gamma_multiply(0.5)
    } else if active {
        p.accent
    } else {
        p.text2
    };
    draw(painter, rect.shrink(8.0), c);
    if enabled {
        resp.on_hover_cursor(CursorIcon::PointingHand)
    } else {
        resp
    }
}

/// Each tool's mark, drawn in the 16 px square `r`.
fn glyph(pa: &egui::Painter, r: Rect, tool: Tool, c: Color32) {
    let s = Stroke::new(1.6, c);
    let at = |x: f32, y: f32| pos2(r.left() + x * r.width(), r.top() + y * r.height());
    match tool {
        Tool::Select => {
            pa.add(Shape::convex_polygon(
                vec![
                    at(0.2, 0.05),
                    at(0.85, 0.6),
                    at(0.52, 0.62),
                    at(0.68, 0.95),
                    at(0.55, 1.0),
                    at(0.4, 0.68),
                    at(0.2, 0.85),
                ],
                Color32::TRANSPARENT,
                s,
            ));
        }
        Tool::AddFat => {
            pa.add(Shape::convex_polygon(
                vec![at(0.5, 0.1), at(0.95, 0.9), at(0.05, 0.9)],
                Color32::TRANSPARENT,
                s,
            ));
        }
        Tool::AddFdh => {
            pa.rect_filled(Rect::from_min_max(at(0.15, 0.15), at(0.85, 0.85)), 2.0, c);
        }
        Tool::AddChamber => {
            pa.rect_stroke(
                Rect::from_min_max(at(0.15, 0.15), at(0.85, 0.85)),
                1.0,
                s,
                egui::StrokeKind::Middle,
            );
            pa.line_segment([at(0.15, 0.5), at(0.85, 0.5)], s);
        }
        Tool::Draw => {
            pa.add(Shape::line(
                vec![at(0.05, 0.9), at(0.35, 0.45), at(0.6, 0.6), at(0.95, 0.1)],
                s,
            ));
            for q in [at(0.05, 0.9), at(0.35, 0.45), at(0.6, 0.6), at(0.95, 0.1)] {
                pa.circle_filled(q, 1.8, c);
            }
        }
        Tool::Split => {
            pa.line_segment([at(0.05, 0.5), at(0.38, 0.5)], s);
            pa.line_segment([at(0.62, 0.5), at(0.95, 0.5)], s);
            pa.line_segment(
                [at(0.5, 0.1), at(0.5, 0.9)],
                Stroke::new(1.2, c.gamma_multiply(0.7)),
            );
        }
        Tool::Reassign => {
            pa.circle_stroke(at(0.22, 0.75), 2.6, s);
            pa.add(Shape::line(
                vec![at(0.3, 0.62), at(0.55, 0.3), at(0.88, 0.25)],
                s,
            ));
            pa.add(Shape::line(
                vec![at(0.7, 0.1), at(0.88, 0.25), at(0.72, 0.42)],
                s,
            ));
        }
    }
}

fn trash(pa: &egui::Painter, r: Rect, c: Color32) {
    let s = Stroke::new(1.5, c);
    let at = |x: f32, y: f32| pos2(r.left() + x * r.width(), r.top() + y * r.height());
    pa.line_segment([at(0.1, 0.22), at(0.9, 0.22)], s);
    pa.line_segment([at(0.38, 0.08), at(0.62, 0.08)], s);
    pa.add(Shape::line(
        vec![at(0.2, 0.22), at(0.27, 0.95), at(0.73, 0.95), at(0.8, 0.22)],
        s,
    ));
}

fn undo_glyph(pa: &egui::Painter, r: Rect, c: Color32, redo: bool) {
    let s = Stroke::new(1.6, c);
    let flip = |x: f32| if redo { 1.0 - x } else { x };
    let at = |x: f32, y: f32| pos2(r.left() + flip(x) * r.width(), r.top() + y * r.height());
    let arc: Vec<Pos2> = (0..=12)
        .map(|i| {
            let t = std::f32::consts::PI * (i as f32 / 12.0);
            at(0.55 + 0.32 * t.cos(), 0.55 + 0.3 * t.sin() - 0.0)
        })
        .collect();
    pa.add(Shape::line(arc, s));
    pa.add(Shape::line(
        vec![at(0.1, 0.35), at(0.23, 0.58), at(0.42, 0.42)],
        s,
    ));
}
