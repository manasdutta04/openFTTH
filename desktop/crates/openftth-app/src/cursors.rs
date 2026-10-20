//! Crew cursors: while a crew member (or the automatic design) works, its own pointer glides
//! across the map to what it touches, clicks there, and tours what the step changed, like a
//! colleague's cursor in a shared design tool. Positions live in Mercator metres so cursors
//! stay on their spot while the engineer pans and zooms.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

use egui::{vec2, Align2, Color32, Pos2, Rect, Shape, Stroke};
use openftth_core::{Design, Mercator, StreetLayer};

use crate::theme::{font, Palette, Weight};

/// Seconds a cursor stays after its last action before it fades away.
const LINGER: f32 = 6.0;
/// How long a click ripple lasts.
const RIPPLE: f32 = 0.45;
/// Pause on each stop of a tour.
const DWELL: Duration = Duration::from_millis(180);
/// Stiffness of the motion: higher arrives sooner. Critically damped, so it never overshoots.
const OMEGA: f64 = 7.5;

struct Actor {
    label: String,
    colour: Color32,
    pos: [f64; 2],
    vel: [f64; 2],
    stops: VecDeque<[f64; 2]>,
    dwell_until: Option<Instant>,
    last_active: Instant,
    ripples: Vec<([f64; 2], Instant)>,
    doing: String,
    /// A tool of this actor is running: the cursor stays and hovers while it thinks.
    working: bool,
}

#[derive(Default)]
pub struct AgentCursors {
    actors: HashMap<String, Actor>,
    /// The actor that acted last, for "Follow the crew".
    latest: Option<String>,
    /// An actor that started before the map had anything to point at: (key, label, colour,
    /// doing). It appears at the first point its step touches.
    pending: Option<(String, String, Color32, String)>,
}

impl AgentCursors {
    pub fn clear(&mut self) {
        self.actors.clear();
        self.latest = None;
        self.pending = None;
    }

    /// A tool started: the actor heads for where it acts (`at`), or keeps its place.
    pub fn started(
        &mut self,
        key: &str,
        label: &str,
        colour: Color32,
        doing: &str,
        at: Option<[f64; 2]>,
        fallback: Option<[f64; 2]>,
    ) {
        let now = Instant::now();
        self.latest = Some(key.to_string());
        if at.or(fallback).is_none() && !self.actors.contains_key(key) {
            // Nothing on the map yet: appear with the first thing this step changes.
            self.pending = Some((key.into(), label.into(), colour, doing.into()));
            return;
        }
        let a = self.actors.entry(key.to_string()).or_insert_with(|| {
            // A new cursor comes in from just beside where it is going.
            let start = at.or(fallback).unwrap_or([0.0, 0.0]);
            Actor {
                label: label.to_string(),
                colour,
                pos: [start[0] - 40.0, start[1] + 25.0],
                vel: [0.0, 0.0],
                stops: VecDeque::new(),
                dwell_until: None,
                last_active: now,
                ripples: Vec::new(),
                doing: String::new(),
                working: true,
            }
        });
        a.label = label.to_string();
        a.colour = colour;
        a.doing = doing.to_string();
        a.last_active = now;
        a.working = true;
        if let Some(p) = at.or(if a.stops.is_empty() { fallback } else { None }) {
            a.stops.clear();
            a.stops.push_back(p);
        }
        self.latest = Some(key.to_string());
    }

    /// A step committed: the actor that last worked tours what changed, nearest first.
    pub fn tour(&mut self, points: Vec<[f64; 2]>) {
        let Some(key) = self.latest.clone() else {
            return;
        };
        if let (Some(first), Some((k, label, colour, doing))) = (points.first(), self.pending.take()) {
            if k == key && !self.actors.contains_key(&key) {
                self.started(&key, &label, colour, &doing, None, Some(*first));
            }
        }
        let Some(a) = self.actors.get_mut(&key) else {
            return;
        };
        if a.last_active.elapsed().as_secs_f32() > 30.0 || points.is_empty() {
            return;
        }
        let mut left = points;
        let mut here = a.stops.back().copied().unwrap_or(a.pos);
        let mut route = Vec::new();
        while !left.is_empty() && route.len() < 10 {
            let (i, _) = left
                .iter()
                .enumerate()
                .map(|(i, p)| (i, (p[0] - here[0]).powi(2) + (p[1] - here[1]).powi(2)))
                .min_by(|x, y| x.1.total_cmp(&y.1))
                .expect("not empty");
            here = left.swap_remove(i);
            route.push(here);
        }
        a.stops.extend(route);
        a.last_active = Instant::now();
    }

    /// A tool of this actor finished (or the actor went idle).
    pub fn finished(&mut self, key: &str) {
        if let Some(a) = self.actors.get_mut(key) {
            a.working = false;
            a.last_active = Instant::now();
        }
        if self.pending.as_ref().map(|p| p.0 == key).unwrap_or(false) {
            self.pending = None;
        }
    }

    pub fn latest_position(&self) -> Option<[f64; 2]> {
        let a = self.actors.get(self.latest.as_ref()?)?;
        (a.last_active.elapsed().as_secs_f32() < LINGER).then_some(a.pos)
    }

    /// Advance the motion and draw every cursor over the map. `to_screen` maps Mercator metres
    /// to the screen; `scale` is pixels per metre. Returns a map point to centre on when the
    /// engineer clicks an off-screen marker.
    pub fn show(
        &mut self,
        ui: &mut egui::Ui,
        rect: Rect,
        scale: f64,
        p: &Palette,
        to_screen: impl Fn([f64; 2]) -> Pos2,
    ) -> Option<[f64; 2]> {
        let dt = ui.input(|i| i.stable_dt).clamp(0.0, 0.05) as f64;
        let now = Instant::now();
        let painter = ui.painter_at(rect);
        let mut animating = false;
        let mut centre_on = None;
        let mut keys: Vec<String> = self.actors.keys().cloned().collect();
        keys.sort();
        for key in keys {
            let a = self.actors.get_mut(&key).expect("key from map");
            // Motion toward the current stop.
            if let Some(target) = a.stops.front().copied() {
                if a.dwell_until.map(|t| now >= t).unwrap_or(false) {
                    a.dwell_until = None;
                    a.stops.pop_front();
                } else if a.dwell_until.is_none() {
                    for ((pos, vel), goal) in a.pos.iter_mut().zip(a.vel.iter_mut()).zip(target) {
                        let acc = -OMEGA * OMEGA * (*pos - goal) - 2.0 * OMEGA * *vel;
                        *vel += acc * dt;
                        *pos += *vel * dt;
                    }
                    let off = ((a.pos[0] - target[0]).powi(2) + (a.pos[1] - target[1]).powi(2))
                        .sqrt()
                        * scale;
                    let speed = (a.vel[0].powi(2) + a.vel[1].powi(2)).sqrt() * scale;
                    if off < 1.5 && speed < 30.0 {
                        a.pos = target;
                        a.vel = [0.0, 0.0];
                        a.dwell_until = Some(now + DWELL);
                        a.ripples.push((target, now));
                    }
                }
                animating = true;
                a.last_active = now;
            }
            a.ripples
                .retain(|(_, t)| t.elapsed().as_secs_f32() < RIPPLE);
            if !a.ripples.is_empty() {
                animating = true;
            }
            if a.working {
                a.last_active = now;
                animating = true;
            }
            let idle = a.last_active.elapsed().as_secs_f32();
            let alpha = if idle < LINGER {
                1.0
            } else {
                (1.0 - (idle - LINGER)).clamp(0.0, 1.0)
            };
            if alpha <= 0.0 {
                continue;
            }
            if idle > LINGER && alpha > 0.0 {
                animating = true;
            }
            for (at, t) in &a.ripples {
                let k = t.elapsed().as_secs_f32() / RIPPLE;
                let c = to_screen(*at);
                painter.circle_stroke(
                    c,
                    4.0 + 18.0 * k,
                    Stroke::new(2.0 * (1.0 - k), a.colour.gamma_multiply((1.0 - k) * alpha)),
                );
                painter.circle_filled(c, 3.0 * (1.0 - k), a.colour.gamma_multiply(alpha));
            }
            let mut tip = to_screen(a.pos);
            if a.working && a.stops.is_empty() && a.dwell_until.is_none() {
                // Thinking: a slow, small hover around the spot, like a hand resting on a mouse.
                let t = ui.input(|i| i.time) as f32;
                tip += vec2((t * 1.3).sin() * 5.0, (t * 1.7).cos() * 3.5);
            }
            let label = if a.doing.is_empty() {
                a.label.clone()
            } else {
                format!("{} · {}", a.label, a.doing)
            };
            if rect.shrink(6.0).contains(tip) {
                pointer(&painter, tip, a.colour, alpha);
                name_pill(&painter, tip + vec2(13.0, 17.0), &label, a.colour, alpha);
            } else if let Some(hit) =
                edge_marker(ui, &painter, rect, tip, &a.label, a.colour, alpha)
            {
                if hit {
                    centre_on = Some(a.pos);
                }
            }
        }
        let _ = p;
        if animating {
            ui.ctx().request_repaint();
        }
        centre_on
    }
}

/// The classic arrow pointer, filled in the actor's colour with a white rim and a soft shadow.
fn pointer(painter: &egui::Painter, tip: Pos2, colour: Color32, alpha: f32) {
    let s = 1.05;
    let pt = |x: f32, y: f32| tip + vec2(x, y) * s;
    let outline = vec![
        pt(0.0, 0.0),
        pt(0.0, 16.0),
        pt(4.3, 12.3),
        pt(7.1, 18.6),
        pt(9.7, 17.4),
        pt(7.0, 11.3),
        pt(12.4, 11.3),
    ];
    let head = vec![pt(0.0, 0.0), pt(0.0, 16.0), pt(12.4, 11.3)];
    let tail = vec![pt(4.3, 12.3), pt(7.1, 18.6), pt(9.7, 17.4), pt(7.0, 11.3)];
    let shadow = |pts: &Vec<Pos2>| pts.iter().map(|q| *q + vec2(1.2, 1.8)).collect::<Vec<_>>();
    let dark = Color32::from_black_alpha((90.0 * alpha) as u8);
    painter.add(Shape::convex_polygon(shadow(&head), dark, Stroke::NONE));
    painter.add(Shape::convex_polygon(shadow(&tail), dark, Stroke::NONE));
    painter.add(Shape::closed_line(
        outline,
        Stroke::new(3.0, Color32::WHITE.gamma_multiply(alpha)),
    ));
    let fill = colour.gamma_multiply(alpha);
    painter.add(Shape::convex_polygon(head, fill, Stroke::NONE));
    painter.add(Shape::convex_polygon(tail, fill, Stroke::NONE));
}

fn name_pill(painter: &egui::Painter, at: Pos2, label: &str, colour: Color32, alpha: f32) {
    let galley = painter.layout_no_wrap(
        label.to_string(),
        font(11.0, Weight::Medium),
        Color32::WHITE.gamma_multiply(alpha),
    );
    let r = Rect::from_min_size(at, galley.size() + vec2(14.0, 8.0));
    painter.rect_filled(
        r.translate(vec2(0.0, 1.5)),
        9.0,
        Color32::from_black_alpha((60.0 * alpha) as u8),
    );
    painter.rect_filled(r, 9.0, colour.gamma_multiply(alpha));
    painter.galley(r.min + vec2(7.0, 4.0), galley, Color32::WHITE);
}

/// A cursor outside the view: a marker on the edge pointing toward it. Returns whether it was
/// clicked (to bring that part of the map into view).
fn edge_marker(
    ui: &mut egui::Ui,
    painter: &egui::Painter,
    rect: Rect,
    tip: Pos2,
    label: &str,
    colour: Color32,
    alpha: f32,
) -> Option<bool> {
    let inner = rect.shrink(18.0);
    let c = rect.center();
    let d = tip - c;
    if d.length() < 1.0 {
        return None;
    }
    let tx = if d.x.abs() > 1e-3 {
        (inner.width() / 2.0) / d.x.abs()
    } else {
        f32::INFINITY
    };
    let ty = if d.y.abs() > 1e-3 {
        (inner.height() / 2.0) / d.y.abs()
    } else {
        f32::INFINITY
    };
    let at = c + d * tx.min(ty);
    let dir = d.normalized();
    let fill = colour.gamma_multiply(alpha);
    painter.add(Shape::convex_polygon(
        vec![
            at + dir * 15.0,
            at + dir.rot90() * 6.0 + dir * 7.0,
            at - dir.rot90() * 6.0 + dir * 7.0,
        ],
        fill,
        Stroke::NONE,
    ));
    painter.circle_filled(at, 11.0, fill);
    painter.circle_stroke(
        at,
        11.0,
        Stroke::new(1.5, Color32::WHITE.gamma_multiply(alpha)),
    );
    let initial: String = label
        .chars()
        .next()
        .map(|c| c.to_string())
        .unwrap_or_default();
    painter.text(
        at,
        Align2::CENTER_CENTER,
        initial,
        font(11.0, Weight::Semibold),
        Color32::WHITE.gamma_multiply(alpha),
    );
    let resp = ui
        .interact(
            Rect::from_center_size(at, vec2(24.0, 24.0)),
            ui.scope_id().with(("edge-cursor", label)),
            egui::Sense::click(),
        )
        .on_hover_cursor(egui::CursorIcon::PointingHand)
        .on_hover_text(format!("{label} is working over here. Click to look."));
    Some(resp.clicked())
}

/// Where a tool acts, in Mercator metres, from its `target` (element, coordinates or street).
pub fn locate(
    target: &serde_json::Value,
    design: Option<&Design>,
    streets: Option<&StreetLayer>,
) -> Option<[f64; 2]> {
    if let (Some(lon), Some(lat)) = (target["lon"].as_f64(), target["lat"].as_f64()) {
        return Some(Mercator::project(lon, lat));
    }
    let ids = target["element_uuid"]
        .as_str()
        .map(|s| vec![s.to_string()])
        .or_else(|| {
            target["element_uuids"].as_array().map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
        })
        .unwrap_or_default();
    if let Some(d) = design {
        for id in &ids {
            if let Some(p) = d.get(id).and_then(element_point) {
                return Some(p);
            }
        }
    }
    if let (Some(osm), Some(s)) = (target["osm_id"].as_str(), streets) {
        if let Some(seg) = s.segments.iter().find(|g| g.osm_id == osm) {
            let mid = seg.line[seg.line.len() / 2];
            return Some(Mercator::project(mid[0], mid[1]));
        }
    }
    None
}

/// A point to visit for an element: its location, or the middle of a line.
pub fn element_point(e: &openftth_core::Element) -> Option<[f64; 2]> {
    let pts = e.geometry.coords();
    let q = if e.kind() == "building" || pts.len() < 3 {
        *pts.first()?
    } else {
        pts[pts.len() / 2]
    };
    Some(Mercator::project(q[0], q[1]))
}

/// Points worth visiting among changed elements: sites first, then cables, never more than 40.
pub fn tour_points(design: &Design, changed: &[String]) -> Vec<[f64; 2]> {
    let rank = |k: &str| match k {
        "pop" | "fdh" => 0,
        "fat" => 1,
        "chamber" => 2,
        "feeder" | "distribution" => 3,
        "home" | "drop" => 4,
        _ => 5,
    };
    let mut items: Vec<(u8, [f64; 2])> = changed
        .iter()
        .filter_map(|id| design.get(id))
        .filter_map(|e| element_point(e).map(|p| (rank(e.kind()), p)))
        .collect();
    items.sort_by_key(|(r, _)| *r);
    let best = items.first().map(|(r, _)| *r).unwrap_or(5);
    let pool: Vec<[f64; 2]> = items
        .iter()
        .filter(|(r, _)| *r <= best.max(1))
        .map(|(_, p)| *p)
        .collect();
    let step = (pool.len() / 40).max(1);
    pool.into_iter().step_by(step).take(40).collect()
}

/// A fallback spot when a tool names no place: the POP, else the middle of the design.
pub fn working_spot(design: Option<&Design>) -> Option<[f64; 2]> {
    let d = design?;
    if let Some(p) = d.by_kind("pop").next().and_then(element_point) {
        return Some(p);
    }
    let b = d.bounds();
    (!b.is_empty()).then(|| b.center())
}
