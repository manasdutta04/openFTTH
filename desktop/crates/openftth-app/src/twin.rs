//! 2D twin: streets, buildings, cables, equipment. Drag to pan, wheel to zoom, click to select,
//! hover for details. A floating toolbar, a layer list and a scale bar sit on the map.

use egui::{pos2, vec2, Align2, Color32, CursorIcon, Pos2, Rect, Sense, Shape, Stroke, Ui, Vec2};
use openftth_core::{Bounds, Design, Element, Geometry, Mercator, StreetLayer};

use crate::icons::{self, Icon};
use crate::state::Layers;
use crate::theme::{font, mono, Palette, Weight};
use crate::widgets::{caps, float_frame, icon_button_sized, text};

pub struct TwinView {
    center: [f64; 2],
    scale: f64,
    fitted: bool,
}

impl Default for TwinView {
    fn default() -> Self {
        Self {
            center: [0.0, 0.0],
            scale: 1.0,
            fitted: false,
        }
    }
}

pub struct TwinResponse {
    pub clicked: Option<String>,
    pub clicked_empty: bool,
    pub hovered: Option<String>,
    pub cursor: Option<(f64, f64)>,
}

struct Hit {
    dist: f32,
    uuid: String,
}

impl TwinView {
    pub fn fit(&mut self, bounds: &Bounds, rect: Rect) {
        if bounds.is_empty() || rect.width() < 1.0 {
            return;
        }
        let size = bounds.size();
        let sx = rect.width() as f64 / size[0].max(1.0);
        let sy = rect.height() as f64 / size[1].max(1.0);
        self.scale = (sx.min(sy) * 0.92).max(1e-6);
        self.center = bounds.center();
        self.fitted = true;
    }

    pub fn refit(&mut self) {
        self.fitted = false;
    }

    pub fn zoom_by(&mut self, factor: f64) {
        self.scale = (self.scale * factor).clamp(1e-6, 1e3);
    }

    pub fn focus(&mut self, e: &Element) {
        if let Some([lon, lat]) = e.geometry.anchor() {
            self.center = Mercator::project(lon, lat);
            if self.scale < 0.6 {
                self.scale = 1.0;
            }
        }
    }

    fn to_screen(&self, rect: Rect, p: [f64; 2]) -> Pos2 {
        let c = rect.center();
        Pos2::new(
            c.x + ((p[0] - self.center[0]) * self.scale) as f32,
            c.y - ((p[1] - self.center[1]) * self.scale) as f32,
        )
    }

    fn ll(&self, rect: Rect, lonlat: [f64; 2]) -> Pos2 {
        self.to_screen(rect, Mercator::project(lonlat[0], lonlat[1]))
    }

    pub fn screen_to_lonlat(&self, rect: Rect, pos: Pos2) -> (f64, f64) {
        let c = rect.center();
        let x = self.center[0] + ((pos.x - c.x) as f64) / self.scale;
        let y = self.center[1] - ((pos.y - c.y) as f64) / self.scale;
        Mercator::unproject(x, y)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn show(
        &mut self,
        ui: &mut Ui,
        p: &Palette,
        design: Option<&Design>,
        streets: Option<&StreetLayer>,
        layers: &mut Layers,
        selection: &[String],
        changed: &[String],
    ) -> TwinResponse {
        let (response, painter) = ui.allocate_painter(ui.available_size(), Sense::click_and_drag());
        let rect = response.rect;
        painter.rect_filled(rect, 0.0, p.map_bg);

        if !self.fitted {
            let mut b = design.map(Design::bounds).unwrap_or_else(Bounds::empty);
            if b.is_empty() {
                if let Some(s) = streets {
                    b = s.bounds();
                }
            }
            self.fit(&b, rect);
        }

        if response.dragged() {
            let d = response.drag_delta();
            self.center[0] -= d.x as f64 / self.scale;
            self.center[1] += d.y as f64 / self.scale;
        }
        if response.hovered() {
            let zoom = ui.input(|i| i.zoom_delta());
            let scroll = ui.input(|i| i.smooth_scroll_delta.y);
            let factor = (zoom as f64) * (1.0 + scroll as f64 * 0.0015);
            if (factor - 1.0).abs() > 1e-4 {
                if let Some(pos) = response.hover_pos() {
                    let (lon, lat) = self.screen_to_lonlat(rect, pos);
                    let before = Mercator::project(lon, lat);
                    self.zoom_by(factor);
                    let after = self.to_screen(rect, before);
                    self.center[0] += (after.x - pos.x) as f64 / self.scale;
                    self.center[1] -= (after.y - pos.y) as f64 / self.scale;
                }
            }
        }
        let hover_pos = response.hover_pos().filter(|_| response.hovered());
        let click_pos = if response.clicked() {
            response.interact_pointer_pos()
        } else {
            None
        };
        let probe = click_pos.or(hover_pos);
        let mut best: Option<Hit> = None;
        let mut consider = |dist: f32, tol: f32, uuid: &str| {
            if dist <= tol && best.as_ref().map(|b| dist < b.dist).unwrap_or(true) {
                best = Some(Hit {
                    dist,
                    uuid: uuid.to_string(),
                });
            }
        };

        if layers.streets {
            if let Some(s) = streets {
                for seg in &s.segments {
                    let pts: Vec<Pos2> = seg.line.iter().map(|q| self.ll(rect, *q)).collect();
                    let (w, c) = match seg.highway.as_str() {
                        "primary" | "secondary" | "trunk" => (2.4, p.map_street_major),
                        "tertiary" | "residential" | "living_street" => (1.4, p.map_street),
                        _ => (0.8, p.map_street),
                    };
                    painter.add(Shape::line(pts, Stroke::new(w, c)));
                }
            }
        }

        if let Some(d) = design {
            let is_changed = |u: &str| changed.iter().any(|c| c == u);
            let is_selected = |u: &str| selection.iter().any(|c| c == u);
            if layers.buildings {
                for e in d.by_kind("building") {
                    let fill = if is_changed(&e.element_uuid) {
                        p.red.gamma_multiply(0.3)
                    } else {
                        p.map_building
                    };
                    match &e.geometry {
                        Geometry::Polygon(rings) => {
                            self.polygon(&painter, rect, rings, fill, p.map_building_edge)
                        }
                        Geometry::MultiPolygon(polys) => {
                            for rings in polys {
                                self.polygon(&painter, rect, rings, fill, p.map_building_edge);
                            }
                        }
                        _ => {}
                    }
                }
            }
            for (kind, w, on) in [
                ("drop", 0.9f32, layers.drops),
                ("distribution", 2.2, layers.distribution),
                ("feeder", 3.4, layers.feeder),
            ] {
                if !on {
                    continue;
                }
                for e in d.by_kind(kind) {
                    if let Geometry::LineString(line) = &e.geometry {
                        let pts: Vec<Pos2> = line.iter().map(|q| self.ll(rect, *q)).collect();
                        let c = if is_changed(&e.element_uuid) {
                            p.red
                        } else {
                            p.kind(kind)
                        };
                        if is_selected(&e.element_uuid) {
                            painter.add(Shape::line(
                                pts.clone(),
                                Stroke::new(w + 6.0, p.accent.gamma_multiply(0.35)),
                            ));
                        }
                        painter.add(Shape::line(pts.clone(), Stroke::new(w, c)));
                        if let Some(q) = probe {
                            consider(polyline_distance(&pts, q), 7.0, &e.element_uuid);
                        }
                    }
                }
            }
            for (kind, r, on) in [
                ("home", 2.0f32, layers.homes),
                ("chamber", 3.0, layers.chambers),
                ("fat", 5.0, layers.fats),
                ("fdh", 7.0, layers.fdhs),
                ("pop", 9.0, true),
            ] {
                if !on {
                    continue;
                }
                for e in d.by_kind(kind) {
                    let Some(a) = e.geometry.anchor() else {
                        continue;
                    };
                    let pos = self.ll(rect, a);
                    if !rect.expand(20.0).contains(pos) {
                        continue;
                    }
                    let c = if is_changed(&e.element_uuid) {
                        p.red
                    } else {
                        p.kind(kind)
                    };
                    if is_selected(&e.element_uuid) {
                        painter.circle_filled(pos, r + 8.0, p.accent.gamma_multiply(0.25));
                        painter.circle_stroke(pos, r + 5.0, Stroke::new(1.5, p.accent));
                    }
                    let edge = Stroke::new(1.0, p.map_bg);
                    match kind {
                        "fat" => {
                            painter.add(Shape::convex_polygon(
                                vec![
                                    pos + Vec2::new(0.0, -r - 1.0),
                                    pos + Vec2::new(r + 1.0, r),
                                    pos + Vec2::new(-r - 1.0, r),
                                ],
                                c,
                                edge,
                            ));
                        }
                        "fdh" => {
                            painter.add(Shape::rect_filled(
                                Rect::from_center_size(pos, Vec2::splat(r * 2.0)),
                                2.0,
                                c,
                            ));
                        }
                        "chamber" => {
                            painter.add(Shape::rect_filled(
                                Rect::from_center_size(pos, Vec2::splat(r * 2.0)),
                                1.0,
                                c,
                            ));
                        }
                        "pop" => {
                            painter.circle_filled(pos, r, c);
                            painter.circle_stroke(pos, r, Stroke::new(2.0, p.map_bg));
                            painter.text(
                                pos,
                                Align2::CENTER_CENTER,
                                "P",
                                font(10.0, Weight::Semibold),
                                p.map_bg,
                            );
                        }
                        _ => {
                            painter.add(Shape::circle_filled(pos, r, c));
                        }
                    }
                    if let Some(q) = probe {
                        consider(pos.distance(q), r + 6.0, &e.element_uuid);
                    }
                }
            }
        }

        self.scale_bar(&painter, rect, p);
        // floating toolbar
        let mut refit = false;
        let mut zoom: Option<f64> = None;
        egui::Area::new(egui::Id::unique("twin-toolbar"))
            .fixed_pos(rect.right_top() + vec2(-10.0, 10.0))
            .pivot(Align2::RIGHT_TOP)
            .order(egui::Order::Foreground)
            .show(ui.ctx(), |ui| {
                float_frame(p)
                    .inner_margin(egui::Margin::same(3))
                    .show(ui, |ui| {
                        ui.spacing_mut().item_spacing.y = 2.0;
                        if icon_button_sized(ui, p, Icon::Plus, "Zoom in", 26.0, false).clicked() {
                            zoom = Some(1.5);
                        }
                        if icon_button_sized(ui, p, Icon::Minus, "Zoom out", 26.0, false).clicked()
                        {
                            zoom = Some(1.0 / 1.5);
                        }
                        if icon_button_sized(ui, p, Icon::Fit, "Fit the design  (F)", 26.0, false)
                            .clicked()
                        {
                            refit = true;
                        }
                    });
            });
        if let Some(z) = zoom {
            self.zoom_by(z);
        }
        if refit {
            self.refit();
        }
        self.layer_list(ui, rect, p, layers, design);

        let hovered = if click_pos.is_none() {
            best.as_ref().map(|h| h.uuid.clone())
        } else {
            None
        };
        let clicked = if click_pos.is_some() {
            best.map(|h| h.uuid)
        } else {
            None
        };
        let clicked_empty = click_pos.is_some() && clicked.is_none();
        let cursor = hover_pos.map(|q| self.screen_to_lonlat(rect, q));
        if let (Some(d), Some(h)) = (design, hovered.as_deref()) {
            if let Some(e) = d.get(h) {
                let lines = hover_summary(e);
                response.clone().on_hover_ui_at_pointer(|ui| {
                    ui.label(text(e.name(), 12.0, Weight::Semibold, p.text));
                    for l in lines {
                        ui.label(text(l, 11.5, Weight::Regular, p.text2));
                    }
                });
            }
        }
        if response.dragged() {
            ui.ctx().set_cursor_icon(CursorIcon::Grabbing);
        } else if hovered.is_some() {
            ui.ctx().set_cursor_icon(CursorIcon::PointingHand);
        } else if response.hovered() {
            ui.ctx().set_cursor_icon(CursorIcon::Crosshair);
        }
        TwinResponse {
            clicked,
            clicked_empty,
            hovered,
            cursor,
        }
    }

    fn polygon(
        &self,
        painter: &egui::Painter,
        rect: Rect,
        rings: &[Vec<[f64; 2]>],
        fill: Color32,
        edge: Color32,
    ) {
        let Some(outer) = rings.first() else { return };
        let pts: Vec<Pos2> = outer.iter().map(|q| self.ll(rect, *q)).collect();
        if pts.iter().all(|q| !rect.expand(50.0).contains(*q)) {
            return;
        }
        painter.add(Shape::convex_polygon(pts.clone(), fill, Stroke::NONE));
        painter.add(Shape::closed_line(pts, Stroke::new(0.6, edge)));
    }

    fn scale_bar(&self, painter: &egui::Painter, rect: Rect, p: &Palette) {
        let metres = 110.0 / self.scale;
        let nice = [
            1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0,
        ];
        let m = nice
            .iter()
            .copied()
            .min_by(|a, b| (a - metres).abs().partial_cmp(&(b - metres).abs()).unwrap())
            .unwrap_or(100.0);
        let px = (m * self.scale) as f32;
        let origin = rect.left_bottom() + vec2(12.0, -12.0);
        let c = p.text2;
        painter.line_segment([origin, origin + vec2(px, 0.0)], Stroke::new(1.5, c));
        painter.line_segment([origin, origin + vec2(0.0, -4.0)], Stroke::new(1.5, c));
        painter.line_segment(
            [origin + vec2(px, 0.0), origin + vec2(px, -4.0)],
            Stroke::new(1.5, c),
        );
        let label = if m >= 1000.0 {
            format!("{} km", m / 1000.0)
        } else {
            format!("{m} m")
        };
        painter.text(
            origin + vec2(px + 6.0, 0.0),
            Align2::LEFT_BOTTOM,
            label,
            mono(10.5),
            c,
        );
    }

    fn layer_list(
        &self,
        ui: &mut Ui,
        rect: Rect,
        p: &Palette,
        layers: &mut Layers,
        design: Option<&Design>,
    ) {
        let counts = design.map(Design::counts).unwrap_or_default();
        egui::Area::new(egui::Id::unique("twin-layers"))
            .fixed_pos(rect.right_bottom() - vec2(10.0, 10.0))
            .pivot(Align2::RIGHT_BOTTOM)
            .order(egui::Order::Foreground)
            .show(ui.ctx(), |ui| {
                float_frame(p).show(ui, |ui| {
                    ui.set_width(150.0);
                    ui.spacing_mut().item_spacing.y = 0.0;
                    ui.horizontal(|ui| {
                        ui.add_space(2.0);
                        ui.label(caps("Layers", p.text3));
                    });
                    ui.add_space(4.0);
                    let rows: [(&str, &str, &mut bool); 9] = [
                        ("Feeder", "feeder", &mut layers.feeder),
                        ("Distribution", "distribution", &mut layers.distribution),
                        ("Drops", "drop", &mut layers.drops),
                        ("FDH cabinets", "fdh", &mut layers.fdhs),
                        ("FATs", "fat", &mut layers.fats),
                        ("Chambers", "chamber", &mut layers.chambers),
                        ("Homes", "home", &mut layers.homes),
                        ("Buildings", "building", &mut layers.buildings),
                        ("Streets", "street", &mut layers.streets),
                    ];
                    for (label, kind, on) in rows {
                        let (r, resp) = ui
                            .allocate_exact_size(vec2(ui.available_width(), 20.0), Sense::click());
                        if resp.hovered() {
                            ui.painter().rect_filled(r, 3.0, p.bg3);
                        }
                        if resp.on_hover_cursor(CursorIcon::PointingHand).clicked() {
                            *on = !*on;
                        }
                        let alpha = if *on { 1.0 } else { 0.35 };
                        let c = match kind {
                            "building" => p.map_building_edge,
                            "street" => p.map_street_major,
                            k => p.kind(k),
                        }
                        .gamma_multiply(alpha);
                        let sw = Rect::from_center_size(
                            pos2(r.left() + 12.0, r.center().y),
                            vec2(14.0, 10.0),
                        );
                        let painter = ui.painter();
                        match kind {
                            "feeder" | "distribution" | "drop" | "street" => {
                                let w = match kind {
                                    "feeder" => 3.0,
                                    "distribution" => 2.2,
                                    "street" => 2.0,
                                    _ => 1.0,
                                };
                                painter.line_segment(
                                    [sw.left_center(), sw.right_center()],
                                    Stroke::new(w, c),
                                );
                            }
                            "fat" => {
                                painter.add(Shape::convex_polygon(
                                    vec![sw.center_top(), sw.right_bottom(), sw.left_bottom()],
                                    c,
                                    Stroke::NONE,
                                ));
                            }
                            "fdh" | "chamber" => {
                                painter.rect_filled(
                                    Rect::from_center_size(sw.center(), Vec2::splat(8.0)),
                                    1.0,
                                    c,
                                );
                            }
                            "building" => {
                                painter.rect_filled(
                                    Rect::from_center_size(sw.center(), vec2(12.0, 8.0)),
                                    1.0,
                                    p.map_building.gamma_multiply(alpha),
                                );
                            }
                            _ => {
                                painter.circle_filled(sw.center(), 3.0, c);
                            }
                        }
                        painter.text(
                            pos2(r.left() + 26.0, r.center().y),
                            Align2::LEFT_CENTER,
                            label,
                            font(11.5, Weight::Regular),
                            if *on { p.text } else { p.text3 },
                        );
                        if let Some(n) = counts.get(kind) {
                            painter.text(
                                pos2(r.right() - 4.0, r.center().y),
                                Align2::RIGHT_CENTER,
                                n.to_string(),
                                mono(10.5),
                                p.text3,
                            );
                        }
                    }
                });
            });
        let _ = icons::lerp;
    }
}

fn hover_summary(e: &Element) -> Vec<String> {
    let mut out = vec![format!("{} · {}", e.kind(), e.placement)];
    let o = &e.openftth;
    let pr = &e.props;
    let get_i =
        |m: &serde_json::Map<String, serde_json::Value>, k: &str| m.get(k).and_then(|v| v.as_i64());
    let get_f =
        |m: &serde_json::Map<String, serde_json::Value>, k: &str| m.get(k).and_then(|v| v.as_f64());
    match e.kind() {
        "home" => {
            if let Some(u) = get_i(o, "units") {
                out.push(format!("{u} unit(s)"));
            }
            if let Some(d) = get_f(o, "drop_m") {
                out.push(format!("drop {d:.0} m"));
            }
            if let Some(l) = get_f(o, "odn_loss_db") {
                out.push(format!("ODN loss {l:.1} dB"));
            }
            if let Some(pon) = o.get("pon_id").and_then(|v| v.as_str()) {
                out.push(format!("PON {pon}"));
            }
        }
        "fat" | "fdh" => {
            if let (Some(u), Some(c)) = (get_i(o, "units"), get_i(pr, "port_capacity")) {
                out.push(format!("{u} / {c} units"));
            }
            if let Some(n) = get_i(o, "pons") {
                out.push(format!("{n} PONs"));
            }
        }
        "feeder" | "distribution" => {
            if let (Some(f), Some(l)) = (get_i(pr, "fiber_count"), get_f(pr, "length_m")) {
                let n = get_i(pr, "cable_count").unwrap_or(1);
                out.push(if n > 1 {
                    format!("{n} × {f}F · {l:.0} m")
                } else {
                    format!("{f}F · {l:.0} m")
                });
            }
            if let Some(d) = get_i(o, "downstream_units") {
                out.push(format!("{d} units downstream"));
            }
        }
        "drop" => {
            if let Some(l) = get_f(pr, "length_m") {
                out.push(format!("{l:.0} m"));
            }
        }
        "chamber" => {
            if let Some(t) = pr.get("manhole_type").and_then(|v| v.as_str()) {
                out.push(format!("{t} chamber"));
            }
            if o.get("closure").and_then(|v| v.as_bool()) == Some(true) {
                if let Some(sz) = get_i(o, "size") {
                    out.push(format!("closure {sz}F"));
                }
            }
        }
        _ => {}
    }
    out
}

fn polyline_distance(pts: &[Pos2], p: Pos2) -> f32 {
    let mut best = f32::INFINITY;
    for w in pts.windows(2) {
        let (a, b) = (w[0], w[1]);
        let ab = b - a;
        let len2 = ab.length_sq();
        let t = if len2 > 0.0 {
            ((p - a).dot(ab) / len2).clamp(0.0, 1.0)
        } else {
            0.0
        };
        best = best.min((a + ab * t).distance(p));
    }
    best
}
