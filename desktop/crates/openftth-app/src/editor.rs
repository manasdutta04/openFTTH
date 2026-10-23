//! Editing on the map: a tool bar (select, add a FAT, cabinet or chamber, draw a cable, split
//! or join cables, reassign, measure, delete, undo, redo), an edit bar with the reason that goes
//! on the record, and the click handling that turns each into one engine edit. Every edit is an
//! engine tool call that snaps to the street twin; nothing here decides a number. Measuring
//! only reads the map: its distances are shown, never written to the design.

use egui::{pos2, vec2, Align2, Color32, CursorIcon, Pos2, Rect, Sense, Shape, Stroke, Ui, Vec2};
use openftth_core::geo::Mercator;
use serde_json::json;

use crate::icons::{self, Icon};
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
    Measure,
    AddPop,
    Zone,
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
            Tool::Measure => "Measure",
            Tool::AddPop => "Add POP",
            Tool::Zone => "Draw zone",
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
            Tool::Measure => "M",
            Tool::AddPop => "P",
            Tool::Zone => "K",
        }
    }

    fn icon(self) -> Icon {
        match self {
            Tool::Select => Icon::Select,
            Tool::AddFat => Icon::Fat,
            Tool::AddFdh => Icon::Cabinet,
            Tool::AddChamber => Icon::Chamber,
            Tool::AddPop => Icon::Pop,
            Tool::Draw => Icon::Draw,
            Tool::Split => Icon::Split,
            Tool::Reassign => Icon::Reassign,
            Tool::Measure => Icon::Measure,
            Tool::Zone => Icon::Zone,
        }
    }

    /// Tools that only read the map stay usable while the crew works.
    pub fn edits(self) -> bool {
        !matches!(self, Tool::Select | Tool::Measure)
    }

    pub fn from_key(name: &str) -> Option<Tool> {
        Some(match name {
            "fat" => Tool::AddFat,
            "fdh" => Tool::AddFdh,
            "chamber" => Tool::AddChamber,
            "pop" => Tool::AddPop,
            "draw" => Tool::Draw,
            "split" => Tool::Split,
            "reassign" => Tool::Reassign,
            "measure" => Tool::Measure,
            "zone" => Tool::Zone,
            "select" => Tool::Select,
            _ => return None,
        })
    }

    fn hint(self) -> &'static str {
        match self {
            Tool::Select => "",
            Tool::AddFat | Tool::AddFdh | Tool::AddChamber | Tool::AddPop => {
                "Click where it goes: it snaps to the nearest street node and is pinned"
            }
            Tool::Draw => "Click points along the route · Enter or double-click finishes · Backspace drops the last point",
            Tool::Split => "Click a cable where it should be split: a chamber is added there",
            Tool::Reassign => "Select homes or FATs (Shift-click, Shift-drag a box), then click the FAT or cabinet they go to",
            Tool::Measure => "Click points to measure in a straight line · Backspace drops the last point · nothing is written to the design",
            Tool::Zone => "Click the corners of the zone · Enter or double-click closes it · keep-out: no trench, no equipment · no-dig: no new trench",
        }
    }
}

/// What a tool bar button asks the app to do besides choosing a tool.
pub enum BarAction {
    Undo,
    Redo,
    Delete,
    Join,
}

/// The two selected cables, when exactly two cables are selected (what Join works on).
pub fn join_pair(st: &AppState) -> Option<(String, String)> {
    let [a, b] = st.selection.as_slice() else {
        return None;
    };
    let d = st.design.as_ref()?;
    let cable = |id: &str| {
        d.get(id)
            .is_some_and(|e| ["feeder", "distribution", "drop"].contains(&e.kind()))
    };
    (cable(a) && cable(b)).then(|| (a.clone(), b.clone()))
}

/// Straight-line distance in metres between two lon/lat points (haversine).
fn ground_m(a: (f64, f64), b: (f64, f64)) -> f64 {
    let r = 6_371_008.8;
    let (la1, la2) = (a.1.to_radians(), b.1.to_radians());
    let dla = la2 - la1;
    let dlo = (b.0 - a.0).to_radians();
    let h = (dla / 2.0).sin().powi(2) + la1.cos() * la2.cos() * (dlo / 2.0).sin().powi(2);
    2.0 * r * h.sqrt().asin()
}

pub struct Editor {
    /// Manual mode: the engineer edits the map by hand. Auto: the crew and the engine design it.
    pub manual: bool,
    pub tool: Tool,
    pub reason: String,
    pub cable_kind: &'static str,
    /// What a drawn zone forbids: `keep_out` or `no_dig`.
    pub zone_kind: &'static str,
    pub points: Vec<(f64, f64)>,
}

impl Default for Editor {
    fn default() -> Self {
        Self {
            manual: false,
            tool: Tool::Select,
            reason: "manual edit".into(),
            cable_kind: "distribution",
            zone_kind: "keep_out",
            points: Vec::new(),
        }
    }
}

impl Editor {
    pub fn set_tool(&mut self, tool: Tool) {
        if tool.edits() {
            self.manual = true;
        }
        self.tool = tool;
        self.points.clear();
    }

    /// Switch between Auto and Manual. Leaving Manual puts the tools away.
    pub fn set_manual(&mut self, on: bool) {
        self.manual = on;
        if !on {
            self.tool = Tool::Select;
            self.points.clear();
        }
    }

    /// The Auto / Manual switch at the map's top-left corner. Manual is locked while the crew
    /// works on the map.
    pub fn mode_switch(&mut self, ui: &mut Ui, rect: Rect, p: &Palette, st: &AppState) {
        let locked = st.crew_working();
        let mut pick: Option<bool> = None;
        egui::Area::new(egui::Id::unique("edit-mode"))
            .fixed_pos(rect.left_top() + vec2(10.0, 10.0))
            .order(egui::Order::Foreground)
            .show(ui.ctx(), |ui| {
                crate::widgets::float_frame(p)
                    .inner_margin(egui::Margin::same(3))
                    .show(ui, |ui| {
                        ui.horizontal(|ui| {
                            ui.spacing_mut().item_spacing.x = 2.0;
                            let auto = segment(ui, p, Icon::Auto, "Auto", !self.manual, true)
                                .on_hover_text(
                                    "Auto: the crew and the engine design the network (E switches)",
                                );
                            if auto.clicked() {
                                pick = Some(false);
                            }
                            let manual = segment(
                                ui,
                                p,
                                if locked { Icon::Lock } else { Icon::Manual },
                                "Manual",
                                self.manual,
                                !locked,
                            )
                            .on_hover_text(if locked {
                                "Locked while the crew works on the map"
                            } else {
                                "Manual: place sites and draw cables yourself (E switches)"
                            });
                            if manual.clicked() {
                                pick = Some(true);
                            }
                        });
                    });
            });
        if let Some(on) = pick {
            self.set_manual(on);
        }
    }

    /// While the crew works: say so over the map, with a way to stop it.
    pub fn lock_banner(&mut self, ui: &mut Ui, rect: Rect, p: &Palette, st: &mut AppState) {
        if !self.manual || !st.crew_working() {
            return;
        }
        let mut stop = false;
        egui::Area::new(egui::Id::unique("edit-lock"))
            .fixed_pos(pos2(rect.left() + 196.0, rect.top() + 10.0))
            .order(egui::Order::Foreground)
            .show(ui.ctx(), |ui| {
                crate::widgets::float_frame(p).show(ui, |ui| {
                    ui.horizontal(|ui| {
                        let (r, _) = ui.allocate_exact_size(vec2(18.0, 18.0), Sense::hover());
                        icons::paint(ui.painter(), r, Icon::Crew, p.amber);
                        ui.vertical(|ui| {
                            ui.label(crate::widgets::text(
                                "Manual editing is paused",
                                12.5,
                                Weight::Semibold,
                                p.text,
                            ));
                            ui.label(crate::widgets::text(
                                "The crew is working on the map. Your tools come back when it finishes.",
                                11.5,
                                Weight::Regular,
                                p.text3,
                            ));
                        });
                        ui.add_space(8.0);
                        if crate::widgets::button(ui, p, "Stop the crew", crate::widgets::Kind::Quiet)
                            .clicked()
                        {
                            stop = true;
                        }
                    });
                });
            });
        if stop {
            st.interrupt();
        }
    }

    /// The thin accent frame round the map that says Manual mode is on.
    pub fn frame(&self, ui: &Ui, rect: Rect, p: &Palette, st: &AppState) {
        if !self.manual {
            return;
        }
        let c = if st.crew_working() {
            p.text3.gamma_multiply(0.5)
        } else {
            p.accent.gamma_multiply(0.8)
        };
        ui.painter_at(rect).rect_stroke(
            rect.shrink(1.0),
            0.0,
            Stroke::new(2.0, c),
            egui::StrokeKind::Inside,
        );
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
        if !self.manual {
            return None;
        }
        let locked = st.crew_working();
        let busy = locked || st.editing();
        egui::Area::new(egui::Id::unique("edit-toolbar"))
            .fixed_pos(rect.left_top() + vec2(10.0, 52.0))
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
                        Tool::AddPop,
                        Tool::Draw,
                        Tool::Split,
                        Tool::Reassign,
                        Tool::Measure,
                        Tool::Zone,
                    ] {
                        let active = self.tool == tool;
                        let resp =
                            tool_button(ui, p, active, !busy || !tool.edits(), |pa, r, c| {
                                icons::paint(pa, r, tool.icon(), c)
                            })
                            .on_hover_text(
                                if locked && tool.edits() {
                                    format!("{}: paused while the crew works", tool.label())
                                } else {
                                    format!("{} ({})", tool.label(), tool.key())
                                },
                            );
                        if resp.clicked() {
                            self.set_tool(if active { Tool::Select } else { tool });
                        }
                    }
                    crate::widgets::hairline(ui, p);
                    let has_sel = !st.selection.is_empty();
                    if tool_button(ui, p, false, has_sel && !busy, |pa, r, c| {
                        icons::paint(pa, r, Icon::Delete, c)
                    })
                    .on_hover_text("Delete the selection (Del)")
                    .clicked()
                    {
                        action = Some(BarAction::Delete);
                    }
                    if tool_button(
                        ui,
                        p,
                        false,
                        join_pair(st).is_some() && !busy,
                        |pa, r, c| icons::paint(pa, r, Icon::Join, c),
                    )
                    .on_hover_text("Join the two selected cables (J)")
                    .clicked()
                    {
                        action = Some(BarAction::Join);
                    }
                    if tool_button(ui, p, false, st.can_undo() && !busy, |pa, r, c| {
                        icons::paint(pa, r, Icon::Undo, c)
                    })
                    .on_hover_text(undo_hint(st, "undo", "Undo (Ctrl+Z)"))
                    .clicked()
                    {
                        action = Some(BarAction::Undo);
                    }
                    if tool_button(ui, p, false, st.can_redo() && !busy, |pa, r, c| {
                        icons::paint(pa, r, Icon::Redo, c)
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
        if self.tool == Tool::Select || !self.manual || st.crew_working() {
            return;
        }
        let mut finish = false;
        let mut cancel = false;
        // Beside the mode switch, not centred: a side panel opening must not move it under the pointer.
        egui::Area::new(egui::Id::unique("edit-bar"))
            .fixed_pos(pos2(rect.left() + 196.0, rect.top() + 10.0))
            .pivot(Align2::LEFT_TOP)
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
                        if self.tool == Tool::Zone {
                            for (kind, label) in [("keep_out", "keep-out"), ("no_dig", "no-dig")] {
                                let on = self.zone_kind == kind;
                                if ui
                                    .selectable_label(
                                        on,
                                        crate::widgets::text(
                                            label,
                                            12.0,
                                            Weight::Medium,
                                            if on { p.accent } else { p.text2 },
                                        ),
                                    )
                                    .clicked()
                                {
                                    self.zone_kind = kind;
                                }
                            }
                        }
                        ui.add_space(6.0);
                        if self.tool == Tool::Measure {
                            let total: f64 =
                                self.points.windows(2).map(|w| ground_m(w[0], w[1])).sum();
                            ui.label(crate::widgets::text(
                                format!("Total {}", crate::widgets::fmt_metres(total)),
                                12.5,
                                Weight::Medium,
                                p.text,
                            ));
                            if crate::widgets::button_enabled(
                                ui,
                                p,
                                "Clear",
                                crate::widgets::Kind::Quiet,
                                !self.points.is_empty(),
                            )
                            .clicked()
                            {
                                self.points.clear();
                            }
                        } else {
                            ui.label(crate::widgets::text(
                                "Reason",
                                11.5,
                                Weight::Medium,
                                p.text3,
                            ));
                            ui.add(
                                egui::TextEdit::singleline(&mut self.reason).desired_width(180.0),
                            );
                        }
                        if matches!(self.tool, Tool::Draw | Tool::Zone) {
                            let n = self.points.len();
                            let need = if self.tool == Tool::Zone { 3 } else { 2 };
                            if crate::widgets::button_enabled(
                                ui,
                                p,
                                &format!("Finish ({n})"),
                                crate::widgets::Kind::Prominent,
                                n >= need,
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
        if self.tool == Tool::Zone {
            self.finish_zone(st);
            return;
        }
        if self.points.len() < 2 {
            return;
        }
        if st.editing() || st.busy() {
            // Keep the points: the cable is sent once the last change has been applied.
            st.toasts
                .info("The last change is still being applied; press Enter again in a moment");
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
        if self.tool == Tool::Select || !self.manual {
            return false;
        }
        if self.tool.edits() && st.crew_working() {
            return false;
        }
        if resp.response.hovered() {
            resp.response.ctx.set_cursor_icon(CursorIcon::Crosshair);
        }
        if matches!(self.tool, Tool::Draw | Tool::Zone) && resp.response.double_clicked() {
            self.finish_cable(st);
            return true;
        }
        if self.tool == Tool::Measure {
            if let Some(q) = resp.click {
                self.points.push(q);
                return true;
            }
            return false;
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
            Tool::AddFat | Tool::AddFdh | Tool::AddChamber | Tool::AddPop => {
                let kind = match self.tool {
                    Tool::AddFat => "fat",
                    Tool::AddFdh => "fdh",
                    Tool::AddPop => "pop",
                    _ => "chamber",
                };
                st.edit(
                    "add_site",
                    json!({ "kind": kind, "lon": lon, "lat": lat, "reason": reason }),
                );
            }
            Tool::Draw | Tool::Zone => self.points.push((lon, lat)),
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
                // A FAT is where selected homes go, a cabinet where selected FATs go; a click on
                // anything else selects it like the Select tool does (Shift adds to it).
                let Some(to) = resp.clicked.clone() else {
                    return false;
                };
                let want = match kind_of(&to).as_str() {
                    "fat" => "home",
                    "fdh" => "fat",
                    _ => "",
                };
                let movers: Vec<String> = st
                    .selection
                    .iter()
                    .filter(|id| !want.is_empty() && kind_of(id) == want)
                    .cloned()
                    .collect();
                if movers.is_empty() {
                    return false;
                }
                let args = if let [one] = movers.as_slice() {
                    json!({ "element_uuid": one, "to_uuid": to, "reason": reason })
                } else {
                    json!({ "element_uuids": movers, "to_uuid": to, "reason": reason })
                };
                st.edit("reassign", args);
            }
            Tool::Select | Tool::Measure => {}
        }
        true
    }

    /// The cable being drawn: its points and a rubber band to the pointer.
    pub fn overlay(&self, ui: &Ui, rect: Rect, view: &TwinView, p: &Palette) {
        if self.tool == Tool::Measure {
            self.measure_overlay(ui, rect, view, p);
            return;
        }
        if !matches!(self.tool, Tool::Draw | Tool::Zone) || self.points.is_empty() {
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
        let color = if self.tool == Tool::Zone {
            zone_colour(p, self.zone_kind)
        } else {
            p.kind(self.cable_kind)
        };
        if self.tool == Tool::Zone && pts.len() >= 3 {
            pts.push(pts[0]); // the zone closes back on its first corner
        }
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

impl Editor {
    /// The measured line: each leg's length at its middle and the running total at the pointer.
    fn measure_overlay(&self, ui: &Ui, rect: Rect, view: &TwinView, p: &Palette) {
        if self.points.is_empty() {
            return;
        }
        let painter = ui.painter_at(rect);
        let mut ll = self.points.clone();
        let hover = ui.ctx().pointer_hover_pos().filter(|q| rect.contains(*q));
        if let Some(q) = hover {
            ll.push(view.screen_to_lonlat(rect, q));
        }
        let pts: Vec<Pos2> = ll
            .iter()
            .map(|(lon, lat)| view.to_screen(rect, Mercator::project(*lon, *lat)))
            .collect();
        painter.add(Shape::line(pts.clone(), Stroke::new(2.0, p.text)));
        let label = |at: Pos2, s: String| {
            let galley = painter.layout_no_wrap(s, font(11.0, Weight::Medium), p.text);
            let r = Rect::from_center_size(at, galley.size() + vec2(8.0, 4.0));
            painter.rect_filled(r, 3.0, p.bg1);
            painter.galley(r.min + vec2(4.0, 2.0), galley, p.text);
        };
        let mut total = 0.0;
        for (i, w) in ll.windows(2).enumerate() {
            let m = ground_m(w[0], w[1]);
            total += m;
            if ll.len() > 2 {
                label(pts[i].lerp(pts[i + 1], 0.5), crate::widgets::fmt_metres(m));
            }
        }
        for q in pts.iter().take(self.points.len()) {
            painter.circle_filled(*q, 3.5, p.text);
            painter.circle_stroke(*q, 3.5, Stroke::new(1.2, p.map_bg));
        }
        if ll.len() > 1 {
            label(
                pts[pts.len() - 1] + vec2(0.0, -16.0),
                format!("{} total", crate::widgets::fmt_metres(total)),
            );
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

/// One half of the Auto / Manual switch.
fn segment(
    ui: &mut Ui,
    p: &Palette,
    icon: Icon,
    label: &str,
    on: bool,
    enabled: bool,
) -> egui::Response {
    let galley =
        ui.painter()
            .layout_no_wrap(label.to_string(), font(12.0, Weight::Semibold), p.text);
    let size = vec2(galley.size().x + 38.0, 26.0);
    let (rect, resp) = ui.allocate_exact_size(
        size,
        if enabled {
            Sense::click()
        } else {
            Sense::hover()
        },
    );
    let painter = ui.painter();
    if on {
        painter.rect_filled(rect, 5.0, p.accent_soft);
    } else if enabled && resp.hovered() {
        painter.rect_filled(rect, 5.0, p.bg3);
    }
    let c = if !enabled {
        p.text3.gamma_multiply(0.6)
    } else if on {
        p.accent
    } else {
        p.text2
    };
    let ir = Rect::from_center_size(pos2(rect.left() + 17.0, rect.center().y), Vec2::splat(16.0));
    icons::paint(painter, ir, icon, c);
    painter.text(
        pos2(rect.left() + 30.0, rect.center().y),
        Align2::LEFT_CENTER,
        label,
        font(12.0, Weight::Semibold),
        c,
    );
    if enabled {
        resp.on_hover_cursor(CursorIcon::PointingHand)
    } else {
        resp
    }
}

/// A zone's colour on the map: red for keep-out, amber for no-dig.
pub fn zone_colour(p: &Palette, kind: &str) -> Color32 {
    if kind == "keep_out" {
        p.red
    } else {
        p.amber
    }
}

impl Editor {
    /// Send the zone being drawn (three corners or more).
    pub fn finish_zone(&mut self, st: &mut AppState) {
        if self.points.len() < 3 {
            st.toasts.info("A zone needs at least three corners");
            return;
        }
        if st.editing() || st.busy() {
            st.toasts
                .info("The last change is still being applied; press Enter again in a moment");
            return;
        }
        let pts: Vec<[f64; 2]> = self.points.iter().map(|(lon, lat)| [*lon, *lat]).collect();
        st.edit(
            "add_zone",
            json!({ "kind": self.zone_kind, "points": pts, "reason": self.reason() }),
        );
        self.points.clear();
    }
}
