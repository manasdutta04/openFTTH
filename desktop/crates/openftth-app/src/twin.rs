//! 2D twin view drawn with egui's painter: streets, buildings, cables, equipment. Pan with
//! drag, zoom with the wheel, click to select an element (its id goes into the design context).

use egui::{Color32, Pos2, Rect, Sense, Shape, Stroke, Vec2};
use openftth_core::{Bounds, Design, Geometry, Mercator, StreetLayer};

pub struct TwinView {
    /// Mercator metres at the view centre.
    center: [f64; 2],
    /// Pixels per metre.
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

const COLORS: &[(&str, Color32)] = &[
    ("feeder", Color32::from_rgb(0x8e, 0x24, 0xaa)),
    ("distribution", Color32::from_rgb(0x1e, 0x88, 0xe5)),
    ("drop", Color32::from_rgb(0x43, 0xa0, 0x47)),
    ("home", Color32::from_rgb(0x2e, 0x7d, 0x32)),
    ("fat", Color32::from_rgb(0xfb, 0x8c, 0x00)),
    ("fdh", Color32::from_rgb(0xd8, 0x1b, 0x60)),
    ("pop", Color32::BLACK),
    ("chamber", Color32::from_rgb(0x6d, 0x4c, 0x41)),
];

fn color(kind: &str) -> Color32 {
    COLORS
        .iter()
        .find(|(k, _)| *k == kind)
        .map(|(_, c)| *c)
        .unwrap_or(Color32::GRAY)
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

    fn to_screen(&self, rect: Rect, p: [f64; 2]) -> Pos2 {
        let c = rect.center();
        Pos2::new(
            c.x + ((p[0] - self.center[0]) * self.scale) as f32,
            c.y - ((p[1] - self.center[1]) * self.scale) as f32,
        )
    }

    fn lonlat_to_screen(&self, rect: Rect, lonlat: [f64; 2]) -> Pos2 {
        self.to_screen(rect, Mercator::project(lonlat[0], lonlat[1]))
    }

    pub fn screen_to_lonlat(&self, rect: Rect, pos: Pos2) -> (f64, f64) {
        let c = rect.center();
        let x = self.center[0] + ((pos.x - c.x) as f64) / self.scale;
        let y = self.center[1] - ((pos.y - c.y) as f64) / self.scale;
        Mercator::unproject(x, y)
    }

    /// Draw and handle input. Returns the uuid of an element clicked this frame, if any.
    pub fn show(
        &mut self,
        ui: &mut egui::Ui,
        design: Option<&Design>,
        streets: Option<&StreetLayer>,
        selection: &[String],
        changed: &[String],
    ) -> TwinResponse {
        let (response, painter) = ui.allocate_painter(ui.available_size(), Sense::click_and_drag());
        let rect = response.rect;
        painter.rect_filled(rect, 0.0, Color32::from_rgb(0xfa, 0xfa, 0xf7));

        if !self.fitted {
            let mut b = design.map(Design::bounds).unwrap_or_else(Bounds::empty);
            if b.is_empty() {
                if let Some(s) = streets {
                    b = s.bounds();
                }
            }
            self.fit(&b, rect);
        }

        // input: drag to pan, wheel to zoom around the cursor
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
                    self.scale = (self.scale * factor).clamp(1e-6, 1e3);
                    let after_pos = self.to_screen(rect, before);
                    self.center[0] += (after_pos.x - pos.x) as f64 / self.scale;
                    self.center[1] -= (after_pos.y - pos.y) as f64 / self.scale;
                }
            }
        }

        if let Some(s) = streets {
            for seg in &s.segments {
                let pts: Vec<Pos2> = seg
                    .line
                    .iter()
                    .map(|p| self.lonlat_to_screen(rect, *p))
                    .collect();
                let w = match seg.highway.as_str() {
                    "primary" | "secondary" | "trunk" => 2.4,
                    "tertiary" | "residential" | "living_street" => 1.4,
                    _ => 0.8,
                };
                painter.add(Shape::line(pts, Stroke::new(w, Color32::from_gray(0xb8))));
            }
        }

        let click_pos = if response.clicked() {
            response.interact_pointer_pos()
        } else {
            None
        };
        let mut best: Option<(f32, String)> = None;

        if let Some(d) = design {
            // buildings first
            for e in d.by_kind("building") {
                match &e.geometry {
                    Geometry::Polygon(rings) => {
                        self.draw_polygon(&painter, rect, rings, e.element_uuid.as_str(), changed)
                    }
                    Geometry::MultiPolygon(polys) => {
                        for rings in polys {
                            self.draw_polygon(
                                &painter,
                                rect,
                                rings,
                                e.element_uuid.as_str(),
                                changed,
                            );
                        }
                    }
                    _ => {}
                }
            }
            // cables: drops thin, distribution medium, feeder thick
            for (kind, w) in [("drop", 0.9f32), ("distribution", 2.2), ("feeder", 3.6)] {
                for e in d.by_kind(kind) {
                    if let Geometry::LineString(line) = &e.geometry {
                        let pts: Vec<Pos2> = line
                            .iter()
                            .map(|p| self.lonlat_to_screen(rect, *p))
                            .collect();
                        let mut c = color(kind);
                        if changed.iter().any(|u| u == &e.element_uuid) {
                            c = Color32::from_rgb(0xe5, 0x39, 0x35);
                        }
                        let selected = selection.iter().any(|u| u == &e.element_uuid);
                        painter.add(Shape::line(
                            pts.clone(),
                            Stroke::new(if selected { w + 2.0 } else { w }, c),
                        ));
                        if let Some(cp) = click_pos {
                            let dist = polyline_distance(&pts, cp);
                            if dist < 8.0 && best.as_ref().map(|(b, _)| dist < *b).unwrap_or(true) {
                                best = Some((dist, e.element_uuid.clone()));
                            }
                        }
                    }
                }
            }
            // points
            for (kind, r) in [
                ("home", 2.0f32),
                ("chamber", 3.0),
                ("fat", 5.0),
                ("fdh", 7.0),
                ("pop", 9.0),
            ] {
                for e in d.by_kind(kind) {
                    if let Some(a) = e.geometry.anchor() {
                        let p = self.lonlat_to_screen(rect, a);
                        let selected = selection.iter().any(|u| u == &e.element_uuid);
                        let mut c = color(kind);
                        if changed.iter().any(|u| u == &e.element_uuid) {
                            c = Color32::from_rgb(0xe5, 0x39, 0x35);
                        }
                        match kind {
                            "fat" => painter.add(Shape::convex_polygon(
                                vec![
                                    p + Vec2::new(0.0, -r),
                                    p + Vec2::new(r, r),
                                    p + Vec2::new(-r, r),
                                ],
                                c,
                                Stroke::new(1.0, Color32::WHITE),
                            )),
                            "fdh" | "chamber" => painter.rect_filled(
                                Rect::from_center_size(p, Vec2::splat(r * 2.0)),
                                1.0,
                                c,
                            ),
                            _ => painter.circle_filled(p, r, c),
                        };
                        if selected {
                            painter.circle_stroke(
                                p,
                                r + 4.0,
                                Stroke::new(2.0, Color32::from_rgb(0x21, 0x96, 0xf3)),
                            );
                        }
                        if let Some(cp) = click_pos {
                            let dist = p.distance(cp);
                            if dist < r + 6.0
                                && best.as_ref().map(|(b, _)| dist < *b).unwrap_or(true)
                            {
                                best = Some((dist, e.element_uuid.clone()));
                            }
                        }
                    }
                }
            }
        }
        let clicked: Option<String> = if click_pos.is_some() {
            best.map(|(_, u)| u)
        } else {
            None
        };
        let clicked_empty = click_pos.is_some() && clicked.is_none();
        let cursor = response.hover_pos().map(|p| self.screen_to_lonlat(rect, p));
        TwinResponse {
            clicked,
            cursor,
            clicked_empty,
        }
    }

    fn draw_polygon(
        &self,
        painter: &egui::Painter,
        rect: Rect,
        rings: &[Vec<[f64; 2]>],
        uuid: &str,
        changed: &[String],
    ) {
        let Some(outer) = rings.first() else { return };
        let pts: Vec<Pos2> = outer
            .iter()
            .map(|p| self.lonlat_to_screen(rect, *p))
            .collect();
        let fill = if changed.iter().any(|u| u == uuid) {
            Color32::from_rgb(0xff, 0xcd, 0xd2)
        } else {
            Color32::from_rgb(0xe6, 0xe2, 0xd8)
        };
        painter.add(Shape::convex_polygon(pts.clone(), fill, Stroke::NONE));
        painter.add(Shape::closed_line(
            pts,
            Stroke::new(0.5, Color32::from_gray(0xc8)),
        ));
    }
}

pub struct TwinResponse {
    pub clicked: Option<String>,
    pub clicked_empty: bool,
    pub cursor: Option<(f64, f64)>,
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
        let proj = a + ab * t;
        best = best.min(proj.distance(p));
    }
    best
}
