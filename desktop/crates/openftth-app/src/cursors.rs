//! Crew cursors: while a crew member (or the automatic design) works, its own pointer moves over
//! the map and narrates what it does. For every committed step it visits the major elements
//! (the POP, cabinets, feeder cables and the longest distribution cables) at their exact
//! positions, drawing each cable along its route; everything small (homes, drops, FATs,
//! chambers, buildings) is swept in from where the cursor stands. Positions live in Mercator
//! metres, so cursors stay on their spot while the engineer pans and zooms.

use std::collections::{HashMap, HashSet, VecDeque};
use std::time::{Duration, Instant};

use egui::{vec2, Align2, Color32, Pos2, Rect, Shape, Stroke};
use openftth_core::{Design, Element, Geometry, Mercator, StreetLayer};

use crate::theme::{font, Weight};

/// Seconds a cursor stays after its last action before it fades away.
const LINGER: f32 = 6.0;
/// How long a click ripple lasts.
const RIPPLE: f32 = 0.45;
/// Pause on a major point, so the narration can be read.
const DWELL: Duration = Duration::from_millis(420);
/// Stiffness of the motion: higher arrives sooner. Critically damped, so it never overshoots.
const OMEGA: f64 = 8.0;
/// Screen speed of the sweep that plots the small elements, px/s.
const SWEEP_PX_S: f32 = 1300.0;
/// Fade-in of one swept element, s.
const FADE: f32 = 0.28;
/// Most major stops queued per actor; older ones are plotted straight away.
const MAX_STOPS: usize = 12;
/// Distribution cables drawn by hand per step (the longest); the rest are swept in.
const DRAWN_DISTRIBUTION: usize = 4;
/// A hidden major element appears on its own after this long, whatever the cursor does.
const MAJOR_TIMEOUT: f32 = 20.0;

/// What the map should hide or draw partly this frame: element id → opacity (0..1), and for a
/// cable being drawn, id → the share of its length drawn so far.
#[derive(Default)]
pub struct Reveal {
    pub alpha: HashMap<String, f32>,
    pub partial: HashMap<String, f32>,
}

enum Path {
    Point,
    /// A cable: its vertices in Mercator metres, from the end the cursor starts at.
    Line(Vec<[f64; 2]>),
}

struct Stop {
    at: [f64; 2],
    path: Path,
    uuid: String,
    note: String,
}

struct Hidden {
    pos: [f64; 2],
    created: Instant,
    origin: [f64; 2],
    /// Waits for the cursor (major), or is swept in from `origin` (minor).
    major: bool,
}

struct Actor {
    label: String,
    colour: Color32,
    pos: [f64; 2],
    vel: [f64; 2],
    stops: VecDeque<Stop>,
    dwell_until: Option<Instant>,
    /// Drawing a cable: metres along the current stop's line.
    tracing: Option<f64>,
    last_active: Instant,
    ripples: Vec<([f64; 2], Instant)>,
    /// A tool of this actor is running: the cursor stays and hovers while it thinks.
    working: bool,
    thought: String,
    thought_at: Instant,
}

#[derive(Default)]
pub struct AgentCursors {
    actors: HashMap<String, Actor>,
    /// The actor that acted last: it owns the next committed step.
    latest: Option<String>,
    /// An actor that started before the map had anything to point at: (key, label, colour,
    /// thought). It appears at the first point its step touches.
    pending: Option<(String, String, Color32, String)>,
    hidden: HashMap<String, Hidden>,
    /// Cables being drawn: id → share drawn.
    drawing: HashMap<String, f32>,
}

impl AgentCursors {
    pub fn clear(&mut self) {
        *self = AgentCursors::default();
    }

    /// A tool started: the actor says what it is about to do and heads for where it acts.
    pub fn started(
        &mut self,
        key: &str,
        label: &str,
        colour: Color32,
        thought: &str,
        at: Option<[f64; 2]>,
        fallback: Option<[f64; 2]>,
    ) {
        let now = Instant::now();
        self.latest = Some(key.to_string());
        if at.or(fallback).is_none() && !self.actors.contains_key(key) {
            self.pending = Some((key.into(), label.into(), colour, thought.into()));
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
                tracing: None,
                last_active: now,
                ripples: Vec::new(),
                working: true,
                thought: String::new(),
                thought_at: now,
            }
        });
        a.label = label.to_string();
        a.colour = colour;
        a.last_active = now;
        a.working = true;
        // While touring, the cursor keeps narrating the element it is on.
        if a.stops.iter().all(|s| s.uuid.is_empty()) {
            a.thought = thought.to_string();
            a.thought_at = now;
        }
        // Go where the tool acts, unless a tour is still under way.
        if a.stops.is_empty() {
            if let Some(p) = at.or(fallback) {
                a.stops.push_back(Stop {
                    at: p,
                    path: Path::Point,
                    uuid: String::new(),
                    note: String::new(),
                });
            }
        }
    }

    /// A tool finished: the actor reports the engine's own one-line result.
    pub fn step_done(&mut self, key: &str, summary: &str) {
        if let Some(a) = self.actors.get_mut(key) {
            if !summary.is_empty() && a.stops.iter().all(|s| s.uuid.is_empty()) {
                a.thought = format!("Done: {summary}");
                a.thought_at = Instant::now();
            }
        }
    }

    /// The actor has nothing running any more.
    pub fn finished(&mut self, key: &str) {
        if let Some(a) = self.actors.get_mut(key) {
            a.working = false;
            a.last_active = Instant::now();
        }
        if self.pending.as_ref().map(|p| p.0 == key).unwrap_or(false) {
            self.pending = None;
        }
    }

    /// A step committed. The actor that worked last visits the major elements it added or
    /// changed; new small elements are hidden and swept in from the cursor.
    pub fn version(&mut self, design: &Design, added: &[String], modified: &[String]) {
        let Some(key) = self.latest.clone() else {
            return;
        };
        let now = Instant::now();
        let is_new: HashSet<&str> = added.iter().map(String::as_str).collect();
        let mut majors: Vec<(&Element, bool)> = Vec::new();
        let mut minors: Vec<&Element> = Vec::new();
        for id in added.iter().chain(modified) {
            if let Some(e) = design.get(id) {
                let new = is_new.contains(id.as_str());
                if is_major(e) {
                    majors.push((e, new));
                } else if new {
                    minors.push(e);
                }
            }
        }
        // Distribution: only the longest few are drawn by hand; the rest are swept in.
        let mut longest: Vec<(&Element, bool)> = majors
            .iter()
            .filter(|(e, _)| e.kind() == "distribution")
            .copied()
            .collect();
        longest.sort_by(|a, b| length_m(b.0).total_cmp(&length_m(a.0)));
        let drawn: HashSet<&str> = longest
            .iter()
            .take(DRAWN_DISTRIBUTION)
            .map(|(e, _)| e.element_uuid.as_str())
            .collect();
        for (e, new) in longest.iter().skip(DRAWN_DISTRIBUTION) {
            if *new {
                minors.push(e);
            }
        }
        majors
            .retain(|(e, _)| e.kind() != "distribution" || drawn.contains(e.element_uuid.as_str()));
        if majors.is_empty() && minors.is_empty() {
            return;
        }
        // Wake the actor (creating it at the first element if the map was empty when it began).
        if !self.actors.contains_key(&key) {
            let first = majors
                .first()
                .map(|(e, _)| *e)
                .or(minors.first().copied())
                .and_then(element_point);
            match (first, self.pending.take()) {
                (Some(p), Some((k, label, colour, thought))) if k == key => {
                    self.started(&key, &label, colour, &thought, None, Some(p));
                }
                _ => return,
            }
        }
        let a = self.actors.get_mut(&key).expect("actor exists");
        if a.last_active.elapsed().as_secs_f32() > 30.0 && !a.working {
            return;
        }
        a.last_active = now;
        let origin = a.stops.back().map(|s| s.at).unwrap_or(a.pos);
        for e in minors {
            if let Some(pos) = element_point(e) {
                self.hidden.insert(
                    e.element_uuid.clone(),
                    Hidden {
                        pos,
                        created: now,
                        origin,
                        major: false,
                    },
                );
            }
        }
        // Nearest-neighbour route over the majors, from where the cursor will be; a cable is
        // entered from whichever end is nearer.
        let mut left: Vec<(Stop, bool)> = majors
            .into_iter()
            .filter_map(|(e, new)| major_stop(e, new).map(|s| (s, new)))
            .collect();
        let mut here = origin;
        while !left.is_empty() {
            let (i, flip) = left
                .iter()
                .enumerate()
                .map(|(i, (s, _))| {
                    let d0 = dist2(s.at, here);
                    match &s.path {
                        Path::Line(pts) => {
                            let d1 = dist2(*pts.last().expect("line has points"), here);
                            (i, d0.min(d1), d1 < d0)
                        }
                        Path::Point => (i, d0, false),
                    }
                })
                .min_by(|x, y| x.1.total_cmp(&y.1))
                .map(|(i, _, flip)| (i, flip))
                .expect("not empty");
            let (mut stop, new) = left.swap_remove(i);
            if let (true, Path::Line(pts)) = (flip, &mut stop.path) {
                pts.reverse();
                stop.at = pts[0];
            }
            here = match &stop.path {
                Path::Line(pts) => *pts.last().expect("line has points"),
                Path::Point => stop.at,
            };
            if new {
                self.hidden.insert(
                    stop.uuid.clone(),
                    Hidden {
                        pos: stop.at,
                        created: now,
                        origin: stop.at,
                        major: true,
                    },
                );
            }
            a.stops.push_back(stop);
        }
        // Never fall far behind the engine: plot what the cursor will not reach in time.
        while a.stops.len() > MAX_STOPS {
            if let Some(s) = a.stops.pop_front() {
                self.hidden.remove(&s.uuid);
                self.drawing.remove(&s.uuid);
                a.tracing = None;
                a.dwell_until = None;
            }
        }
    }

    pub fn latest_position(&self) -> Option<[f64; 2]> {
        let a = self.actors.get(self.latest.as_ref()?)?;
        (a.last_active.elapsed().as_secs_f32() < LINGER).then_some(a.pos)
    }

    /// What the map hides or draws partly this frame. `scale` is pixels per metre.
    pub fn reveal(&mut self, scale: f64) -> Reveal {
        let mut out = Reveal::default();
        let now = Instant::now();
        self.hidden.retain(|id, h| {
            let age = now.duration_since(h.created).as_secs_f32();
            if h.major {
                if age > MAJOR_TIMEOUT {
                    return false;
                }
                out.alpha.insert(id.clone(), 0.0);
                return true;
            }
            let px = (dist2(h.pos, h.origin).sqrt() * scale) as f32;
            let delay = (px / SWEEP_PX_S).min(1.6);
            let a = ((age - delay) / FADE).clamp(0.0, 1.0);
            if a >= 1.0 {
                return false;
            }
            out.alpha.insert(id.clone(), a);
            true
        });
        for (id, f) in &self.drawing {
            out.partial.insert(id.clone(), *f);
            out.alpha.remove(id);
        }
        out
    }

    /// Advance the motion and draw every cursor over the map. `to_screen` maps Mercator metres
    /// to the screen; `scale` is pixels per metre. Returns a map point to centre on when the
    /// engineer clicks an off-screen marker.
    pub fn show(
        &mut self,
        ui: &mut egui::Ui,
        rect: Rect,
        scale: f64,
        to_screen: impl Fn([f64; 2]) -> Pos2,
    ) -> Option<[f64; 2]> {
        let dt = ui.input(|i| i.stable_dt).clamp(0.0, 0.05) as f64;
        let time = ui.input(|i| i.time) as f32;
        let now = Instant::now();
        let painter = ui.painter_at(rect);
        let mut animating = !self.hidden.is_empty() || !self.drawing.is_empty();
        let mut centre_on = None;
        let mut keys: Vec<String> = self.actors.keys().cloned().collect();
        keys.sort();
        for key in keys {
            let a = self.actors.get_mut(&key).expect("key from map");
            step(a, dt, scale, now, &mut self.hidden, &mut self.drawing);
            if a.working || !a.stops.is_empty() {
                a.last_active = now;
                animating = true;
            }
            a.ripples
                .retain(|(_, t)| t.elapsed().as_secs_f32() < RIPPLE);
            if !a.ripples.is_empty() {
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
            if idle > LINGER {
                animating = true;
            }
            for (at, t) in &a.ripples {
                let k = t.elapsed().as_secs_f32() / RIPPLE;
                painter.circle_stroke(
                    to_screen(*at),
                    5.0 + 20.0 * k,
                    Stroke::new(2.0 * (1.0 - k), a.colour.gamma_multiply((1.0 - k) * alpha)),
                );
            }
            let mut tip = to_screen(a.pos);
            if a.working && a.stops.is_empty() && a.dwell_until.is_none() {
                // Thinking: a slow, small hover around the spot, like a hand resting on a mouse.
                tip += vec2((time * 1.3).sin() * 5.0, (time * 1.7).cos() * 3.5);
            }
            if rect.shrink(6.0).contains(tip) {
                pointer(&painter, tip, a.colour, alpha);
                let thought_alpha = if a.working || !a.stops.is_empty() {
                    alpha
                } else {
                    let age = a.thought_at.elapsed().as_secs_f32();
                    alpha * (1.0 - (age - 5.0).max(0.0)).clamp(0.0, 1.0)
                };
                label(
                    &painter,
                    rect,
                    tip + vec2(14.0, 18.0),
                    &a.label,
                    &a.thought,
                    a.colour,
                    (alpha, thought_alpha),
                );
            } else if let Some(hit) =
                edge_marker(ui, &painter, rect, tip, &a.label, a.colour, alpha)
            {
                if hit {
                    centre_on = Some(a.pos);
                }
            }
        }
        if animating {
            ui.ctx().request_repaint();
        }
        centre_on
    }
}

/// One frame of an actor's motion: glide to the next stop, draw a cable along its route, or
/// pause on a point; on arrival, click and show the element.
fn step(
    a: &mut Actor,
    dt: f64,
    scale: f64,
    now: Instant,
    hidden: &mut HashMap<String, Hidden>,
    drawing: &mut HashMap<String, f32>,
) {
    let Some(stop) = a.stops.front() else {
        return;
    };
    if let Some(until) = a.dwell_until {
        if now >= until {
            a.dwell_until = None;
            a.stops.pop_front();
        }
        return;
    }
    if let Some(done) = a.tracing {
        let Path::Line(pts) = &stop.path else {
            a.tracing = None;
            return;
        };
        let total = polyline_len(pts);
        // A cable is drawn in 0.4 to 1.6 s, at a steady hand-like screen speed.
        let speed = (900.0 / scale).clamp(total / 1.6, (total / 0.4).max(total / 1.6));
        let d = (done + speed * dt).min(total);
        a.pos = point_along(pts, d);
        let share = if total > 0.0 { (d / total) as f32 } else { 1.0 };
        drawing.insert(stop.uuid.clone(), share);
        if d >= total {
            drawing.remove(&stop.uuid);
            hidden.remove(&stop.uuid);
            a.ripples.push((a.pos, now));
            a.tracing = None;
            a.dwell_until = Some(now + Duration::from_millis(120));
        } else {
            a.tracing = Some(d);
        }
        return;
    }
    let target = stop.at;
    for ((pos, vel), goal) in a.pos.iter_mut().zip(a.vel.iter_mut()).zip(target) {
        let acc = -OMEGA * OMEGA * (*pos - goal) - 2.0 * OMEGA * *vel;
        *vel += acc * dt;
        *pos += *vel * dt;
    }
    let off = dist2(a.pos, target).sqrt() * scale;
    let speed = (a.vel[0].powi(2) + a.vel[1].powi(2)).sqrt() * scale;
    if off < 1.0 && speed < 40.0 {
        // Arrived: the pointer's tip sits exactly on the element.
        a.pos = target;
        a.vel = [0.0, 0.0];
        if !stop.note.is_empty() {
            a.thought = stop.note.clone();
            a.thought_at = now;
        }
        match stop.path {
            Path::Line(_) => {
                a.tracing = Some(0.0);
                hidden.remove(&stop.uuid);
                drawing.insert(stop.uuid.clone(), 0.0);
            }
            Path::Point => {
                a.ripples.push((target, now));
                if let Some(h) = hidden.get_mut(&stop.uuid) {
                    // Pops in where the cursor clicked.
                    h.major = false;
                    h.created = now;
                    h.origin = h.pos;
                }
                let dwell = if stop.uuid.is_empty() {
                    Duration::from_millis(60)
                } else {
                    DWELL
                };
                a.dwell_until = Some(now + dwell);
            }
        }
    }
}

fn is_major(e: &Element) -> bool {
    matches!(e.kind(), "pop" | "fdh" | "feeder" | "distribution")
}

fn length_m(e: &Element) -> f64 {
    e.props
        .get("length_m")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0)
}

/// A stop for a major element, narrated from the element's own data.
fn major_stop(e: &Element, new: bool) -> Option<Stop> {
    let name = e.name();
    let num = |k: &str| {
        e.props
            .get(k)
            .or_else(|| e.openftth.get(k))
            .and_then(|v| v.as_f64())
    };
    let verb = if new { "Placing" } else { "Updating" };
    let note = match e.kind() {
        "pop" => format!("{} the POP here", if new { "Placing" } else { "Moving" }),
        "fdh" => match (
            num("units").or_else(|| num("total_connections")),
            num("port_capacity"),
        ) {
            (Some(u), Some(c)) => format!("{verb} cabinet {name} · {u:.0} units on {c:.0} ports"),
            _ => format!("{verb} cabinet {name}"),
        },
        "feeder" | "distribution" => {
            let what = if e.kind() == "feeder" {
                "feeder"
            } else {
                "distribution cable"
            };
            match (num("length_m"), num("fiber_count")) {
                (Some(l), Some(f)) => format!("Drawing {what} {name} · {l:.0} m, {f:.0} fibres"),
                (Some(l), None) => format!("Drawing {what} {name} · {l:.0} m"),
                _ => format!("Drawing {what} {name}"),
            }
        }
        _ => name.clone(),
    };
    match e.kind() {
        "feeder" | "distribution" => {
            let pts: Vec<[f64; 2]> = e
                .geometry
                .coords()
                .iter()
                .map(|q| Mercator::project(q[0], q[1]))
                .collect();
            (pts.len() >= 2).then(|| Stop {
                at: pts[0],
                path: Path::Line(pts),
                uuid: e.element_uuid.clone(),
                note,
            })
        }
        _ => element_point(e).map(|at| Stop {
            at,
            path: Path::Point,
            uuid: e.element_uuid.clone(),
            note,
        }),
    }
}

fn dist2(a: [f64; 2], b: [f64; 2]) -> f64 {
    (a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2)
}

fn polyline_len(pts: &[[f64; 2]]) -> f64 {
    pts.windows(2).map(|w| dist2(w[0], w[1]).sqrt()).sum()
}

fn point_along(pts: &[[f64; 2]], d: f64) -> [f64; 2] {
    let mut left = d;
    for w in pts.windows(2) {
        let seg = dist2(w[0], w[1]).sqrt();
        if left <= seg && seg > 0.0 {
            let t = left / seg;
            return [
                w[0][0] + (w[1][0] - w[0][0]) * t,
                w[0][1] + (w[1][1] - w[0][1]) * t,
            ];
        }
        left -= seg;
    }
    *pts.last().unwrap_or(&[0.0, 0.0])
}

/// The prefix of a polyline covering `share` of its length (for cables being drawn).
pub fn polyline_prefix(pts: &[Pos2], share: f32) -> Vec<Pos2> {
    let total: f32 = pts.windows(2).map(|w| w[0].distance(w[1])).sum();
    let mut left = total * share.clamp(0.0, 1.0);
    let mut out = Vec::new();
    for w in pts.windows(2) {
        if out.is_empty() {
            out.push(w[0]);
        }
        let seg = w[0].distance(w[1]);
        if left >= seg {
            out.push(w[1]);
            left -= seg;
        } else {
            if seg > 0.0 {
                out.push(w[0] + (w[1] - w[0]) * (left / seg));
            }
            break;
        }
    }
    out
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

/// The name pill, and under it what the actor is doing, in a soft bubble. `alphas` are the
/// cursor's and the narration's opacity.
fn label(
    painter: &egui::Painter,
    clip: Rect,
    at: Pos2,
    name: &str,
    thought: &str,
    colour: Color32,
    alphas: (f32, f32),
) {
    let (alpha, thought_alpha) = alphas;
    let name_g = painter.layout_no_wrap(
        name.to_string(),
        font(11.0, Weight::Semibold),
        Color32::WHITE.gamma_multiply(alpha),
    );
    let thought_g = (!thought.is_empty() && thought_alpha > 0.0).then(|| {
        painter.layout(
            thought.to_string(),
            font(11.5, Weight::Regular),
            Color32::from_gray(235).gamma_multiply(thought_alpha),
            250.0,
        )
    });
    let pill = Rect::from_min_size(at, name_g.size() + vec2(14.0, 7.0));
    let bubble = thought_g.as_ref().map(|g| {
        Rect::from_min_size(
            pill.left_bottom() + vec2(0.0, 4.0),
            g.size() + vec2(16.0, 10.0),
        )
    });
    // Keep the label inside the map: flip it to the left of the pointer near the right edge.
    let width = pill.width().max(bubble.map(|b| b.width()).unwrap_or(0.0));
    let shift = if at.x + width > clip.right() - 8.0 {
        -(width + 28.0)
    } else {
        0.0
    };
    let pill = pill.translate(vec2(shift, 0.0));
    painter.rect_filled(
        pill.translate(vec2(0.0, 1.5)),
        9.0,
        Color32::from_black_alpha((60.0 * alpha) as u8),
    );
    painter.rect_filled(pill, 9.0, colour.gamma_multiply(alpha));
    painter.galley(pill.min + vec2(7.0, 3.5), name_g, Color32::WHITE);
    if let (Some(g), Some(b)) = (thought_g, bubble) {
        let b = b.translate(vec2(shift, 0.0));
        painter.rect_filled(
            b.translate(vec2(0.0, 2.0)),
            8.0,
            Color32::from_black_alpha((70.0 * thought_alpha) as u8),
        );
        painter.rect_filled(
            b,
            8.0,
            Color32::from_rgb(24, 25, 30).gamma_multiply(0.96 * thought_alpha),
        );
        painter.rect_stroke(
            b,
            8.0,
            Stroke::new(1.0, colour.gamma_multiply(0.55 * thought_alpha)),
            egui::StrokeKind::Inside,
        );
        painter.galley(b.min + vec2(8.0, 5.0), g, Color32::WHITE);
    }
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

/// The exact point the map draws an element at: a point element's position, a line's middle
/// vertex, a polygon's first vertex.
pub fn element_point(e: &Element) -> Option<[f64; 2]> {
    let pts = e.geometry.coords();
    let q = match e.geometry {
        Geometry::LineString(_) => *pts.get(pts.len() / 2)?,
        _ => *pts.first()?,
    };
    Some(Mercator::project(q[0], q[1]))
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
