//! 2D twin: streets, buildings, cables, equipment. Drag to pan, wheel to zoom, click to select,
//! hover for details. A floating toolbar, a layer list and a scale bar sit on the map.
//!
//! Geometry is projected to Mercator metres once per design and kept in a cache; each frame
//! only culls against the view and applies a scale and offset, so panning and resizing stay
//! smooth with thousands of elements. Detail follows the zoom: building outlines and homes
//! appear as you zoom in.

use std::collections::{HashMap, HashSet};

use egui::{pos2, vec2, Align2, CursorIcon, Pos2, Rect, Sense, Shape, Stroke, Ui, Vec2};
use openftth_core::{Bounds, Design, Element, Geometry, Mercator, StreetLayer};

use crate::icons::{self, Icon};
use crate::state::Layers;
use crate::theme::{font, mono, Palette, Weight};
use crate::widgets::{float_frame, icon_button_sized, text};

pub struct TwinView {
    center: [f64; 2],
    scale: f64,
    fitted: bool,
    cache: Option<GeomCache>,
    /// The layers legend is open (its header folds it away to keep the map clear).
    legend_open: bool,
    /// What was under the pointer when the context menu last opened.
    pub context: Option<MapContext>,
    /// Where a Shift-drag selection box started.
    boxing: Option<Pos2>,
    /// The map's area last frame: when a side panel opens or closes the left edge stays put,
    /// so what is under the pointer does not jump sideways.
    last_rect: Option<Rect>,
}

impl Default for TwinView {
    fn default() -> Self {
        Self {
            center: [0.0, 0.0],
            scale: 1.0,
            fitted: false,
            cache: None,
            legend_open: true,
            context: None,
            boxing: None,
            last_rect: None,
        }
    }
}

pub struct TwinResponse {
    pub clicked: Option<String>,
    pub clicked_empty: bool,
    pub hovered: Option<String>,
    pub cursor: Option<(f64, f64)>,
    /// Where a primary click landed, whatever was under it (placement mode uses this).
    pub click: Option<(f64, f64)>,
    /// The map's own response, for the context menu.
    pub response: egui::Response,
    /// What a Shift-drag box closed around (visible sites, homes and whole cables).
    pub boxed: Option<Vec<String>>,
    /// Shift was held: a click adds to or takes from the selection.
    pub shift: bool,
}

/// What the right mouse button hit: a position, maybe an element, maybe a street.
#[derive(Clone)]
pub struct MapContext {
    pub lonlat: (f64, f64),
    pub element: Option<String>,
    pub street: Option<StreetHit>,
}

#[derive(Clone)]
pub struct StreetHit {
    pub osm_id: String,
    pub highway: String,
    pub no_dig: bool,
}

// ---- cached geometry (Mercator metres) ----------------------------------------------------

#[derive(Clone, Copy)]
struct Bbox {
    min: [f64; 2],
    max: [f64; 2],
}

impl Bbox {
    fn of(pts: &[[f64; 2]]) -> Bbox {
        let mut b = Bbox {
            min: [f64::INFINITY, f64::INFINITY],
            max: [f64::NEG_INFINITY, f64::NEG_INFINITY],
        };
        for p in pts {
            b.min[0] = b.min[0].min(p[0]);
            b.min[1] = b.min[1].min(p[1]);
            b.max[0] = b.max[0].max(p[0]);
            b.max[1] = b.max[1].max(p[1]);
        }
        b
    }

    fn intersects(&self, o: &Bbox) -> bool {
        self.min[0] <= o.max[0]
            && self.max[0] >= o.min[0]
            && self.min[1] <= o.max[1]
            && self.max[1] >= o.min[1]
    }
}

struct CLine {
    pts: Vec<[f64; 2]>,
    bbox: Bbox,
    /// Street class (0 minor, 1 residential, 2 major) or cable kind (0 drop, 1 distribution, 2 feeder).
    class: u8,
    /// A street on the engineer's no-dig list (always false for cables).
    no_dig: bool,
    /// Index into `Design::elements` for cables, into `StreetLayer::segments` for streets.
    idx: usize,
}

struct CPoly {
    pts: Vec<[f64; 2]>,
    bbox: Bbox,
    idx: usize,
}

struct CPoint {
    xy: [f64; 2],
    /// 0 home, 1 chamber, 2 fat, 3 fdh, 4 pop
    kind: u8,
    idx: usize,
}

struct GeomCache {
    design_key: (usize, usize),
    streets_key: (usize, usize),
    streets: Vec<CLine>,
    buildings: Vec<CPoly>,
    /// Keep-out and no-dig zones (the bool: keep-out).
    zones: Vec<(CPoly, bool)>,
    cables: Vec<CLine>,
    points: Vec<CPoint>,
    uuid_index: HashMap<String, usize>,
}

/// Indexed by `CLine::class`; existing ducts are drawn first, under the designed cables, and
/// crossings last, over them.
const CABLE_KINDS: [&str; 5] = [
    "drop",
    "distribution",
    "feeder",
    "existing_duct",
    "crossing",
];
const POINT_KINDS: [&str; 7] = [
    "home",
    "chamber",
    "fat",
    "fdh",
    "pop",
    "existing_pole",
    "existing_manhole",
];

fn design_key(d: Option<&Design>) -> (usize, usize) {
    d.map(|d| (d.elements.as_ptr() as usize, d.elements.len()))
        .unwrap_or((0, 0))
}

fn streets_key(s: Option<&StreetLayer>) -> (usize, usize) {
    s.map(|s| (s.segments.as_ptr() as usize, s.segments.len()))
        .unwrap_or((0, 0))
}

fn project_line(line: &[[f64; 2]]) -> Vec<[f64; 2]> {
    line.iter().map(|q| Mercator::project(q[0], q[1])).collect()
}

impl GeomCache {
    fn build(design: Option<&Design>, streets: Option<&StreetLayer>) -> GeomCache {
        let mut c = GeomCache {
            design_key: design_key(design),
            streets_key: streets_key(streets),
            streets: Vec::new(),
            buildings: Vec::new(),
            zones: Vec::new(),
            cables: Vec::new(),
            points: Vec::new(),
            uuid_index: HashMap::new(),
        };
        if let Some(s) = streets {
            for (i, seg) in s.segments.iter().enumerate() {
                let class = match seg.highway.as_str() {
                    "primary" | "secondary" | "trunk" | "primary_link" | "secondary_link" => 2,
                    "tertiary" | "residential" | "living_street" | "unclassified" => 1,
                    _ => 0,
                };
                let pts = project_line(&seg.line);
                if pts.len() < 2 {
                    continue;
                }
                let bbox = Bbox::of(&pts);
                c.streets.push(CLine {
                    pts,
                    bbox,
                    class,
                    no_dig: seg.no_dig,
                    idx: i,
                });
            }
        }
        if let Some(d) = design {
            for (i, e) in d.elements.iter().enumerate() {
                c.uuid_index.insert(e.element_uuid.clone(), i);
                let kind = e.kind();
                if kind == "zone" {
                    let keep_out =
                        e.openftth.get("zone").and_then(|v| v.as_str()) == Some("keep_out");
                    let rings: Vec<&Vec<[f64; 2]>> = match &e.geometry {
                        Geometry::Polygon(r) => r.first().into_iter().collect(),
                        Geometry::MultiPolygon(m) => m.iter().filter_map(|r| r.first()).collect(),
                        _ => Vec::new(),
                    };
                    for ring in rings {
                        let pts = project_line(ring);
                        if pts.len() >= 3 {
                            let bbox = Bbox::of(&pts);
                            c.zones.push((CPoly { pts, bbox, idx: i }, keep_out));
                        }
                    }
                } else if kind == "building" {
                    let rings: Vec<&Vec<[f64; 2]>> = match &e.geometry {
                        Geometry::Polygon(r) => r.first().into_iter().collect(),
                        Geometry::MultiPolygon(m) => m.iter().filter_map(|r| r.first()).collect(),
                        _ => Vec::new(),
                    };
                    for ring in rings {
                        let pts = project_line(ring);
                        if pts.len() < 3 {
                            continue;
                        }
                        let bbox = Bbox::of(&pts);
                        c.buildings.push(CPoly { pts, bbox, idx: i });
                    }
                } else if let Some(class) = CABLE_KINDS.iter().position(|k| *k == kind) {
                    if let Geometry::LineString(line) = &e.geometry {
                        let pts = project_line(line);
                        if pts.len() < 2 {
                            continue;
                        }
                        let bbox = Bbox::of(&pts);
                        c.cables.push(CLine {
                            pts,
                            bbox,
                            class: class as u8,
                            no_dig: false,
                            idx: i,
                        });
                    }
                } else if let Some(kind_idx) = POINT_KINDS.iter().position(|k| *k == kind) {
                    if let Some(a) = e.geometry.anchor() {
                        c.points.push(CPoint {
                            xy: Mercator::project(a[0], a[1]),
                            kind: kind_idx as u8,
                            idx: i,
                        });
                    }
                }
            }
        }
        c
    }
}

struct Hit {
    dist: f32,
    idx: usize,
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

    pub fn scale(&self) -> f64 {
        self.scale
    }

    /// Glide the view toward a point (Mercator metres); `k` is the share of the way per call.
    pub fn ease_towards(&mut self, m: [f64; 2], k: f64) {
        self.center[0] += (m[0] - self.center[0]) * k;
        self.center[1] += (m[1] - self.center[1]) * k;
    }

    pub fn centre_on(&mut self, m: [f64; 2]) {
        self.center = m;
    }

    pub(crate) fn to_screen(&self, rect: Rect, p: [f64; 2]) -> Pos2 {
        let c = rect.center();
        Pos2::new(
            c.x + ((p[0] - self.center[0]) * self.scale) as f32,
            c.y - ((p[1] - self.center[1]) * self.scale) as f32,
        )
    }

    fn screen_to_mercator(&self, rect: Rect, pos: Pos2) -> [f64; 2] {
        let c = rect.center();
        [
            self.center[0] + ((pos.x - c.x) as f64) / self.scale,
            self.center[1] - ((pos.y - c.y) as f64) / self.scale,
        ]
    }

    pub fn screen_to_lonlat(&self, rect: Rect, pos: Pos2) -> (f64, f64) {
        let m = self.screen_to_mercator(rect, pos);
        Mercator::unproject(m[0], m[1])
    }

    fn ensure_cache(&mut self, design: Option<&Design>, streets: Option<&StreetLayer>) {
        let fresh = self
            .cache
            .as_ref()
            .map(|c| c.design_key == design_key(design) && c.streets_key == streets_key(streets))
            .unwrap_or(false);
        if !fresh {
            self.cache = Some(GeomCache::build(design, streets));
        }
    }

    /// Draw the map and answer what the pointer did. `placing` is the element the engineer is
    /// choosing a new position for: the cursor stays a crosshair, nothing is hit-tested, and
    /// a primary click anywhere is reported as a position.
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
        placing: Option<&str>,
        issues: &HashMap<String, Vec<String>>,
        reveal: &crate::cursors::Reveal,
    ) -> TwinResponse {
        let (response, painter) = ui.allocate_painter(ui.available_size(), Sense::click_and_drag());
        let rect = response.rect;
        painter.rect_filled(rect, 0.0, p.map_bg);
        if let Some(prev) = self.last_rect.replace(rect) {
            let narrower = prev.width() - rect.width();
            if self.fitted && narrower.abs() > 0.5 && (prev.left() - rect.left()).abs() < 0.5 {
                self.center[0] -= (narrower / 2.0) as f64 / self.scale;
            }
        }

        if !self.fitted {
            let mut b = design.map(Design::bounds).unwrap_or_else(Bounds::empty);
            if b.is_empty() {
                if let Some(s) = streets {
                    b = s.bounds();
                }
            }
            self.fit(&b, rect);
        }
        self.ensure_cache(design, streets);

        // Shift and drag draws a selection box; a plain drag pans.
        let shift = ui.input(|i| i.modifiers.shift);
        if response.drag_started() && shift {
            self.boxing = ui.input(|i| i.pointer.press_origin());
        }
        if self.boxing.is_none() && response.dragged() {
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
                    let before = self.screen_to_mercator(rect, pos);
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
        // While placing, nothing under the pointer is a target: the click is a position.
        let probe = if placing.is_some() {
            None
        } else {
            click_pos.or(hover_pos)
        };
        let right_click = if response.secondary_clicked() {
            response.interact_pointer_pos()
        } else {
            None
        };

        // The view in Mercator metres, with a margin so strokes at the edge still draw.
        let margin = 40.0;
        let view = {
            let a = self.screen_to_mercator(rect, rect.left_bottom() + vec2(-margin, margin));
            let b = self.screen_to_mercator(rect, rect.right_top() + vec2(margin, -margin));
            Bbox { min: a, max: b }
        };
        let scale = self.scale;
        let centre = self.center;
        let rc = rect.center();
        let tf = move |q: &[f64; 2]| -> Pos2 {
            Pos2::new(
                rc.x + ((q[0] - centre[0]) * scale) as f32,
                rc.y - ((q[1] - centre[1]) * scale) as f32,
            )
        };

        let cache = self.cache.as_ref().expect("cache built above");
        let elements: &[Element] = design.map(|d| d.elements.as_slice()).unwrap_or(&[]);
        let changed_set: HashSet<&str> = changed.iter().map(String::as_str).collect();
        let selected_set: HashSet<&str> = selection.iter().map(String::as_str).collect();
        let issue_set: HashSet<&str> = if layers.issues {
            issues.keys().map(String::as_str).collect()
        } else {
            HashSet::new()
        };
        let uuid_of = |idx: usize| elements[idx].element_uuid.as_str();
        let mut shapes: Vec<Shape> = Vec::with_capacity(4096);
        let mut best: Option<Hit> = None;
        let mut consider = |dist: f32, tol: f32, idx: usize| {
            if dist <= tol && best.as_ref().map(|b| dist < b.dist).unwrap_or(true) {
                best = Some(Hit { dist, idx });
            }
        };

        // Streets: minor streets fade out when zoomed far out; no-dig streets are always
        // drawn, dashed in red. A right click remembers the nearest street for the menu.
        let mut street_hit: Option<(f32, usize)> = None;
        if layers.streets {
            for s in &cache.streets {
                if !s.bbox.intersects(&view) || (s.class == 0 && scale < 0.2 && !s.no_dig) {
                    continue;
                }
                let (w, c): (f32, _) = match s.class {
                    2 => (2.4, p.map_street_major),
                    1 => (1.4, p.map_street),
                    _ => (0.8, p.map_street),
                };
                let pts: Vec<Pos2> = s.pts.iter().map(tf).collect();
                if let Some(q) = right_click {
                    let d = polyline_distance(&pts, q);
                    if d <= 8.0 && street_hit.map(|(bd, _)| d < bd).unwrap_or(true) {
                        street_hit = Some((d, s.idx));
                    }
                }
                if s.no_dig {
                    shapes.extend(Shape::dashed_line(
                        &pts,
                        Stroke::new(w.max(1.6), p.red),
                        6.0,
                        4.0,
                    ));
                } else {
                    shapes.push(Shape::line(pts, Stroke::new(w, c)));
                }
            }
        }

        // Buildings: fills from medium zoom, outlines only when close.
        if layers.buildings && scale >= 0.25 {
            let edge = if scale >= 1.2 {
                Stroke::new(0.6, p.map_building_edge)
            } else {
                Stroke::NONE
            };
            for b in &cache.buildings {
                if !b.bbox.intersects(&view) {
                    continue;
                }
                let shown = reveal.alpha.get(uuid_of(b.idx)).copied().unwrap_or(1.0);
                if shown <= 0.0 {
                    continue;
                }
                let fill = if changed_set.contains(uuid_of(b.idx)) {
                    p.red.gamma_multiply(0.3)
                } else {
                    p.map_building
                }
                .gamma_multiply(shown);
                let pts: Vec<Pos2> = b.pts.iter().map(tf).collect();
                shapes.push(Shape::convex_polygon(pts.clone(), fill, Stroke::NONE));
                if edge != Stroke::NONE {
                    shapes.push(Shape::closed_line(pts, edge));
                }
            }
        }

        // Zones: a thick dashed outline (red keep-out, amber no-dig); a click on it selects it.
        if layers.zones {
            for (z, keep_out) in &cache.zones {
                if !z.bbox.intersects(&view) {
                    continue;
                }
                let c = if *keep_out { p.red } else { p.amber };
                let mut pts: Vec<Pos2> = z.pts.iter().map(tf).collect();
                if pts.first() != pts.last() {
                    pts.push(pts[0]);
                }
                if selected_set.contains(uuid_of(z.idx)) {
                    shapes.push(Shape::line(
                        pts.clone(),
                        Stroke::new(7.0, p.accent.gamma_multiply(0.35)),
                    ));
                }
                if let Some(q) = probe {
                    consider(polyline_distance(&pts, q), 6.0, z.idx);
                }
                shapes.extend(Shape::dashed_line(&pts, Stroke::new(2.4, c), 9.0, 5.0));
            }
        }

        // Cables: drops are the thinnest and the most numerous, so they go first.
        let cable_on = [
            layers.drops,
            layers.distribution,
            layers.feeder,
            layers.existing,
            layers.crossings,
        ];
        let cable_w = [0.9f32, 2.2, 3.4, 1.6, 6.0];
        for cls in [3u8, 0, 1, 2, 4] {
            if !cable_on[cls as usize] || (cls == 0 && scale < 0.35) {
                continue;
            }
            let w = cable_w[cls as usize];
            let base = p.kind(CABLE_KINDS[cls as usize]);
            for c in cache.cables.iter().filter(|c| c.class == cls) {
                if !c.bbox.intersects(&view) {
                    continue;
                }
                let uuid = uuid_of(c.idx);
                let shown = reveal.alpha.get(uuid).copied().unwrap_or(1.0);
                if shown <= 0.0 {
                    continue;
                }
                let mut pts: Vec<Pos2> = c.pts.iter().map(tf).collect();
                if let Some(share) = reveal.partial.get(uuid) {
                    // Being drawn by a crew cursor: only the part it has drawn so far.
                    pts = crate::cursors::polyline_prefix(&pts, *share);
                    if pts.len() < 2 {
                        continue;
                    }
                }
                if issue_set.contains(uuid) {
                    shapes.push(Shape::line(
                        pts.clone(),
                        Stroke::new(w + 7.0, p.red.gamma_multiply(0.35)),
                    ));
                }
                if selected_set.contains(uuid) {
                    shapes.push(Shape::line(
                        pts.clone(),
                        Stroke::new(w + 6.0, p.accent.gamma_multiply(0.35)),
                    ));
                }
                let color = if changed_set.contains(uuid) {
                    p.red
                } else {
                    base
                }
                .gamma_multiply(shown);
                if let Some(q) = probe {
                    // A bore shares its line with the cable through it; the bore is what a click
                    // on it means, so it wins the tie.
                    let bias = if cls == 4 { 3.0 } else { 0.0 };
                    consider(polyline_distance(&pts, q) - bias, 7.0, c.idx);
                }
                if cls == 3 {
                    shapes.extend(Shape::dashed_line(&pts, Stroke::new(w, color), 6.0, 3.0));
                } else if cls == 4 {
                    // A bore: a wide band with a dark core, and a cap at each pit.
                    shapes.push(Shape::line(pts.clone(), Stroke::new(w, color)));
                    shapes.push(Shape::line(pts.clone(), Stroke::new(1.5, p.map_bg)));
                    for end in [pts[0], pts[pts.len() - 1]] {
                        shapes.push(Shape::circle_filled(end, 3.5, color));
                    }
                } else {
                    shapes.push(Shape::line(pts, Stroke::new(w, color)));
                }
            }
        }

        // Equipment and homes.
        let point_on = [
            layers.homes,
            layers.chambers,
            layers.fats,
            layers.fdhs,
            true,
            layers.existing,
            layers.existing,
        ];
        let radii = [2.0f32, 3.0, 5.0, 7.0, 9.0, 3.0, 3.5];
        let edge = Stroke::new(1.0, p.map_bg);
        for pt in &cache.points {
            let k = pt.kind as usize;
            if !point_on[k] || (k == 0 && scale < 0.15) {
                continue;
            }
            if pt.xy[0] < view.min[0]
                || pt.xy[0] > view.max[0]
                || pt.xy[1] < view.min[1]
                || pt.xy[1] > view.max[1]
            {
                continue;
            }
            let pos = tf(&pt.xy);
            let uuid = uuid_of(pt.idx);
            let shown = reveal.alpha.get(uuid).copied().unwrap_or(1.0);
            if shown <= 0.0 {
                continue;
            }
            // Swept-in elements grow into place as they fade in.
            let grow = 0.4 + 0.6 * shown;
            let r = if k == 0 && scale < 0.5 { 1.2 } else { radii[k] } * grow;
            let c = if changed_set.contains(uuid) {
                p.red
            } else {
                p.kind(POINT_KINDS[k])
            }
            .gamma_multiply(shown);
            if issue_set.contains(uuid) {
                shapes.push(Shape::circle_stroke(pos, r + 4.0, Stroke::new(2.0, p.red)));
            }
            if selected_set.contains(uuid) {
                shapes.push(Shape::circle_filled(
                    pos,
                    r + 8.0,
                    p.accent.gamma_multiply(0.25),
                ));
                shapes.push(Shape::circle_stroke(
                    pos,
                    r + 5.0,
                    Stroke::new(1.5, p.accent),
                ));
            }
            match k {
                2 => shapes.push(Shape::convex_polygon(
                    vec![
                        pos + Vec2::new(0.0, -r - 1.0),
                        pos + Vec2::new(r + 1.0, r),
                        pos + Vec2::new(-r - 1.0, r),
                    ],
                    c,
                    edge,
                )),
                3 => shapes.push(Shape::rect_filled(
                    Rect::from_center_size(pos, Vec2::splat(r * 2.0)),
                    2.0,
                    c,
                )),
                1 => shapes.push(Shape::rect_filled(
                    Rect::from_center_size(pos, Vec2::splat(r * 2.0)),
                    1.0,
                    c,
                )),
                5 => shapes.push(Shape::circle_stroke(pos, r, Stroke::new(1.5, c))),
                6 => shapes.push(Shape::rect_stroke(
                    Rect::from_center_size(pos, Vec2::splat(r * 2.0)),
                    0.0,
                    Stroke::new(1.5, c),
                    egui::StrokeKind::Middle,
                )),
                4 => {
                    shapes.push(Shape::circle_filled(pos, r, c));
                    shapes.push(Shape::circle_stroke(pos, r, Stroke::new(2.0, p.map_bg)));
                }
                _ => shapes.push(Shape::circle_filled(pos, r, c)),
            }
            if let Some(q) = probe {
                // A site within its own radius wins over the cable passing under it, which
                // would otherwise always be the nearer hit: it competes at a negative distance.
                let d = pos.distance(q);
                if d <= r + 6.0 {
                    consider(d - (r + 6.5), f32::INFINITY, pt.idx);
                }
            }
        }
        // The selection box, and what it closed around once the button is let go.
        let mut boxed = None;
        if let Some(start) = self.boxing {
            let now = ui.input(|i| i.pointer.latest_pos()).unwrap_or(start);
            let sel = Rect::from_two_pos(start, now);
            shapes.push(Shape::rect_filled(sel, 0.0, p.accent.gamma_multiply(0.08)));
            shapes.push(Shape::rect_stroke(
                sel,
                0.0,
                Stroke::new(1.0, p.accent),
                egui::StrokeKind::Middle,
            ));
            if !ui.input(|i| i.pointer.primary_down()) {
                let mut ids: Vec<String> = Vec::new();
                for pt in &cache.points {
                    let k = pt.kind as usize;
                    if point_on[k] && !(k == 0 && scale < 0.15) && sel.contains(tf(&pt.xy)) {
                        ids.push(uuid_of(pt.idx).to_string());
                    }
                }
                for c in &cache.cables {
                    if cable_on[c.class as usize] && c.pts.iter().all(|q| sel.contains(tf(q))) {
                        ids.push(uuid_of(c.idx).to_string());
                    }
                }
                boxed = Some(ids);
                self.boxing = None;
            }
        }
        // Placement: a ring under the pointer and a dashed line from where the site stands.
        if let (Some(uuid), Some(q)) = (placing, hover_pos) {
            let from = cache
                .uuid_index
                .get(uuid)
                .and_then(|&i| cache.points.iter().find(|pt| pt.idx == i))
                .map(|pt| tf(&pt.xy));
            if let Some(a) = from {
                shapes.extend(Shape::dashed_line(
                    &[a, q],
                    Stroke::new(1.2, p.accent),
                    5.0,
                    4.0,
                ));
            }
            shapes.push(Shape::circle_stroke(q, 9.0, Stroke::new(1.5, p.accent)));
            shapes.push(Shape::circle_filled(q, 2.0, p.accent));
        }
        painter.extend(shapes);
        // The POP label needs the font layout, so it is drawn after the batch. With several
        // POPs each also carries its name beside it, so a catchment can be told from another.
        let several = cache.points.iter().filter(|pt| pt.kind == 4).count() > 1;
        for pt in cache.points.iter().filter(|pt| pt.kind == 4) {
            let pos = tf(&pt.xy);
            if rect.contains(pos) {
                painter.text(
                    pos,
                    Align2::CENTER_CENTER,
                    "P",
                    font(10.0, Weight::Semibold),
                    p.map_bg,
                );
                if several {
                    painter.text(
                        pos + Vec2::new(13.0, 0.0),
                        Align2::LEFT_CENTER,
                        elements[pt.idx].name(),
                        font(11.0, Weight::Semibold),
                        p.text,
                    );
                }
            }
        }

        self.scale_bar(&painter, rect, p);
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
        self.layer_list(ui, rect, p, layers, design, issues.len());

        let hit_uuid = best.map(|h| elements[h.idx].element_uuid.clone());
        let hovered = if click_pos.is_none() {
            hit_uuid.clone()
        } else {
            None
        };
        let clicked = if click_pos.is_some() {
            hit_uuid.clone()
        } else {
            None
        };
        let clicked_empty = click_pos.is_some() && clicked.is_none() && placing.is_none();
        let cursor = hover_pos.map(|q| self.screen_to_lonlat(rect, q));
        if let Some(q) = right_click {
            let street = street_hit
                .and_then(|(_, i)| streets.and_then(|s| s.segments.get(i)))
                .map(|seg| StreetHit {
                    osm_id: seg.osm_id.clone(),
                    highway: seg.highway.clone(),
                    no_dig: seg.no_dig,
                });
            self.context = Some(MapContext {
                lonlat: self.screen_to_lonlat(rect, q),
                element: hit_uuid,
                street,
            });
        }
        if let (Some(d), Some(h)) = (design, hovered.as_deref()) {
            if let Some(e) = d.get(h) {
                let lines = hover_summary(e);
                response.clone().on_hover_ui_at_pointer(|ui| {
                    ui.label(text(e.name(), 12.0, Weight::Semibold, p.text));
                    for l in lines {
                        ui.label(text(l, 11.5, Weight::Regular, p.text2));
                    }
                    for m in issues.get(h).into_iter().flatten().take(4) {
                        ui.label(text(m, 11.5, Weight::Regular, p.red));
                    }
                });
            }
        }
        if self.boxing.is_some() {
            ui.ctx().set_cursor_icon(CursorIcon::Crosshair);
        } else if response.dragged() {
            ui.ctx().set_cursor_icon(CursorIcon::Grabbing);
        } else if hovered.is_some() && placing.is_none() {
            ui.ctx().set_cursor_icon(CursorIcon::PointingHand);
        } else if response.hovered() {
            ui.ctx().set_cursor_icon(CursorIcon::Crosshair);
        }
        TwinResponse {
            clicked,
            clicked_empty,
            hovered,
            cursor,
            click: click_pos.map(|q| self.screen_to_lonlat(rect, q)),
            response,
            boxed,
            shift,
        }
    }

    fn scale_bar(&self, painter: &egui::Painter, rect: Rect, p: &Palette) {
        // A Mercator metre is 1 / cos(latitude) ground metres; the bar shows ground metres.
        let lat = Mercator::unproject(self.center[0], self.center[1]).1;
        let ground = lat.to_radians().cos();
        let metres = 110.0 / self.scale * ground;
        let nice = [
            1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0,
        ];
        let m = nice
            .iter()
            .copied()
            .min_by(|a, b| (a - metres).abs().partial_cmp(&(b - metres).abs()).unwrap())
            .unwrap_or(100.0);
        let px = (m / ground * self.scale) as f32;
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
        &mut self,
        ui: &mut Ui,
        rect: Rect,
        p: &Palette,
        layers: &mut Layers,
        design: Option<&Design>,
        issue_count: usize,
    ) {
        let mut counts = design.map(Design::counts).unwrap_or_default();
        if issue_count > 0 {
            counts.insert("issue".into(), issue_count);
        }
        egui::Area::new(egui::Id::unique("twin-layers"))
            .fixed_pos(rect.right_bottom() - vec2(10.0, 10.0))
            .pivot(Align2::RIGHT_BOTTOM)
            .order(egui::Order::Foreground)
            .show(ui.ctx(), |ui| {
                float_frame(p).show(ui, |ui| {
                    ui.set_width(150.0);
                    ui.spacing_mut().item_spacing.y = 0.0;
                    let (hr, head) =
                        ui.allocate_exact_size(vec2(ui.available_width(), 18.0), Sense::click());
                    let painter = ui.painter();
                    painter.text(
                        pos2(hr.left() + 2.0, hr.center().y),
                        Align2::LEFT_CENTER,
                        "LAYERS",
                        font(10.5, Weight::Semibold),
                        if head.hovered() { p.text2 } else { p.text3 },
                    );
                    icons::paint(
                        painter,
                        icons::square(pos2(hr.right() - 8.0, hr.center().y), 12.0),
                        if self.legend_open {
                            icons::Icon::ChevronDown
                        } else {
                            icons::Icon::ChevronRight
                        },
                        p.text3,
                    );
                    if head
                        .on_hover_cursor(CursorIcon::PointingHand)
                        .on_hover_text(if self.legend_open {
                            "Fold the legend away"
                        } else {
                            "Show the legend"
                        })
                        .clicked()
                    {
                        self.legend_open = !self.legend_open;
                    }
                    if !self.legend_open {
                        return;
                    }
                    ui.add_space(4.0);
                    let rows: [(&str, &str, &mut bool); 13] = [
                        ("Feeder", "feeder", &mut layers.feeder),
                        ("Distribution", "distribution", &mut layers.distribution),
                        ("Drops", "drop", &mut layers.drops),
                        ("FDH cabinets", "fdh", &mut layers.fdhs),
                        ("FATs", "fat", &mut layers.fats),
                        ("Chambers", "chamber", &mut layers.chambers),
                        ("Homes", "home", &mut layers.homes),
                        ("Buildings", "building", &mut layers.buildings),
                        ("Streets", "street", &mut layers.streets),
                        ("Existing network", "existing_duct", &mut layers.existing),
                        ("Crossings", "crossing", &mut layers.crossings),
                        ("Rule issues", "issue", &mut layers.issues),
                        ("Zones", "zone", &mut layers.zones),
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
                            "street" => p.text3,
                            "issue" => p.red,
                            "zone" => p.amber,
                            k => p.kind(k),
                        }
                        .gamma_multiply(alpha);
                        let sw = Rect::from_center_size(
                            pos2(r.left() + 12.0, r.center().y),
                            vec2(14.0, 10.0),
                        );
                        let painter = ui.painter();
                        match kind {
                            "crossing" => {
                                painter.line_segment(
                                    [sw.left_center(), sw.right_center()],
                                    Stroke::new(5.0, c),
                                );
                                painter.line_segment(
                                    [sw.left_center(), sw.right_center()],
                                    Stroke::new(1.2, p.map_bg),
                                );
                            }
                            "issue" => {
                                painter.circle_stroke(sw.center(), 4.0, Stroke::new(1.8, c));
                            }
                            "zone" => {
                                painter.extend(Shape::dashed_line(
                                    &[
                                        sw.left_top(),
                                        sw.right_top(),
                                        sw.right_bottom(),
                                        sw.left_bottom(),
                                        sw.left_top(),
                                    ],
                                    Stroke::new(1.4, c),
                                    3.0,
                                    2.0,
                                ));
                            }
                            "existing_duct" => {
                                painter.extend(Shape::dashed_line(
                                    &[sw.left_center(), sw.right_center()],
                                    Stroke::new(1.6, c),
                                    4.0,
                                    2.0,
                                ));
                            }
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
                if o.get("p2p").and_then(|v| v.as_bool()) == Some(true) {
                    out.push(format!("{n} dedicated fibres, no splitters"));
                } else {
                    out.push(format!("{n} PONs"));
                }
            }
            if let Some(n) = get_i(o, "splitters") {
                out.push(format!("{n} FAT splitter(s)"));
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
        "crossing" => {
            if let Some(n) = pr.get("name").and_then(|v| v.as_str()) {
                out.push(n.to_string());
            }
            if let (Some(l), Some(c)) = (get_f(pr, "length_m"), get_f(o, "cost")) {
                out.push(format!("{l:.0} m directional drill · {c:.0} EUR"));
            }
            match (get_f(o, "detour_m"), get_f(o, "detour_cost")) {
                (Some(m), Some(c)) => out.push(format!("going round: {m:.0} m, {c:.0} EUR")),
                _ => out.push("no way round on the streets".to_string()),
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
