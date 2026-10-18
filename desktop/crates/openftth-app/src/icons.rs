//! Line icons drawn with the painter on a 16 × 16 grid: crisp at any scale, no icon font.

use egui::epaint::{PathShape, PathStroke};
use egui::{pos2, vec2, Color32, Painter, Pos2, Rect, Stroke, Vec2};
use std::f32::consts::TAU;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Icon {
    Plus,
    Minus,
    Fit,
    Close,
    Send,
    Stop,
    Search,
    Check,
    Warning,
    Sun,
    Moon,
    ChevronRight,
    Folder,
    Refresh,
    Tool,
    Target,
    Mark,
}

pub fn paint(painter: &Painter, rect: Rect, icon: Icon, color: Color32) {
    let s = rect.width().min(rect.height()) / 16.0;
    let o = rect.center() - vec2(8.0, 8.0) * s;
    let p = |x: f32, y: f32| o + vec2(x, y) * s;
    let w = (1.4 * s).max(1.0);
    let stroke = Stroke::new(w, color);
    let line = |pts: Vec<Pos2>| {
        painter.add(PathShape::line(pts, PathStroke::new(w, color)));
    };
    let closed = |pts: Vec<Pos2>| {
        painter.add(PathShape::closed_line(pts, PathStroke::new(w, color)));
    };
    match icon {
        Icon::Plus => {
            line(vec![p(8.0, 3.0), p(8.0, 13.0)]);
            line(vec![p(3.0, 8.0), p(13.0, 8.0)]);
        }
        Icon::Minus => line(vec![p(3.0, 8.0), p(13.0, 8.0)]),
        Icon::Fit => {
            line(vec![p(2.5, 6.0), p(2.5, 2.5), p(6.0, 2.5)]);
            line(vec![p(10.0, 2.5), p(13.5, 2.5), p(13.5, 6.0)]);
            line(vec![p(13.5, 10.0), p(13.5, 13.5), p(10.0, 13.5)]);
            line(vec![p(6.0, 13.5), p(2.5, 13.5), p(2.5, 10.0)]);
        }
        Icon::Close => {
            line(vec![p(4.0, 4.0), p(12.0, 12.0)]);
            line(vec![p(12.0, 4.0), p(4.0, 12.0)]);
        }
        Icon::Send => {
            closed(vec![p(2.5, 8.0), p(13.5, 2.5), p(10.0, 13.5), p(8.0, 9.0)]);
            line(vec![p(8.0, 9.0), p(13.5, 2.5)]);
        }
        Icon::Stop => {
            painter.rect_filled(
                Rect::from_min_max(p(4.0, 4.0), p(12.0, 12.0)),
                1.5 * s,
                color,
            );
        }
        Icon::Search => {
            painter.circle_stroke(p(7.0, 7.0), 4.2 * s, stroke);
            line(vec![p(10.2, 10.2), p(13.8, 13.8)]);
        }
        Icon::Check => line(vec![p(3.2, 8.4), p(6.6, 11.6), p(12.8, 4.6)]),
        Icon::Warning => {
            closed(vec![p(8.0, 2.2), p(14.2, 13.4), p(1.8, 13.4)]);
            line(vec![p(8.0, 6.4), p(8.0, 9.6)]);
            painter.circle_filled(p(8.0, 11.5), 0.9 * s, color);
        }
        Icon::Sun => {
            painter.circle_stroke(p(8.0, 8.0), 3.0 * s, stroke);
            for i in 0..8 {
                let a = i as f32 / 8.0 * TAU;
                let d = Vec2::angled(a);
                painter.line_segment(
                    [p(8.0, 8.0) + d * 5.0 * s, p(8.0, 8.0) + d * 6.8 * s],
                    stroke,
                );
            }
        }
        Icon::Moon => {
            let mut pts = Vec::new();
            for i in 0..=20 {
                let a = -0.4 * TAU / 4.0 * 2.0 + i as f32 / 20.0 * (TAU * 0.72);
                pts.push(p(8.0, 8.0) + Vec2::angled(a) * 5.5 * s);
            }
            for i in (0..=20).rev() {
                let a = -0.4 * TAU / 4.0 * 2.0 + i as f32 / 20.0 * (TAU * 0.72);
                pts.push(p(9.6, 6.8) + Vec2::angled(a) * 4.2 * s);
            }
            painter.add(PathShape::closed_line(pts, PathStroke::new(w, color)));
        }
        Icon::ChevronRight => line(vec![p(6.0, 3.5), p(10.5, 8.0), p(6.0, 12.5)]),
        Icon::Folder => {
            closed(vec![
                p(2.0, 4.0),
                p(6.0, 4.0),
                p(7.5, 5.8),
                p(14.0, 5.8),
                p(14.0, 13.0),
                p(2.0, 13.0),
            ]);
        }
        Icon::Refresh => {
            let mut pts = Vec::new();
            for i in 0..=18 {
                let a = 0.3 + i as f32 / 18.0 * (TAU * 0.75);
                pts.push(p(8.0, 8.0) + Vec2::angled(a) * 5.0 * s);
            }
            line(pts);
            line(vec![p(11.0, 2.8), p(12.6, 5.2), p(10.0, 5.6)]);
        }
        Icon::Tool => {
            line(vec![p(9.8, 2.6), p(13.4, 6.2), p(11.0, 8.6), p(7.4, 5.0)]);
            line(vec![p(7.4, 5.0), p(2.6, 9.8), p(6.2, 13.4), p(11.0, 8.6)]);
        }
        Icon::Target => {
            painter.circle_stroke(p(8.0, 8.0), 5.0 * s, stroke);
            painter.circle_filled(p(8.0, 8.0), 1.6 * s, color);
            line(vec![p(8.0, 1.5), p(8.0, 4.0)]);
            line(vec![p(8.0, 12.0), p(8.0, 14.5)]);
            line(vec![p(1.5, 8.0), p(4.0, 8.0)]);
            line(vec![p(12.0, 8.0), p(14.5, 8.0)]);
        }
        Icon::Mark => {
            // The app mark: a fibre loop.
            painter.circle_stroke(p(8.0, 8.0), 5.2 * s, Stroke::new(w * 1.4, color));
            painter.circle_filled(p(8.0, 8.0), 1.8 * s, color);
            line(vec![p(8.0, 2.8), p(8.0, 6.2)]);
        }
    }
}

pub fn lerp(a: Color32, b: Color32, t: f32) -> Color32 {
    let t = t.clamp(0.0, 1.0);
    let f = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t).round() as u8;
    Color32::from_rgba_premultiplied(
        f(a.r(), b.r()),
        f(a.g(), b.g()),
        f(a.b(), b.b()),
        f(a.a(), b.a()),
    )
}

pub fn square(center: Pos2, size: f32) -> Rect {
    Rect::from_center_size(center, Vec2::splat(size))
}

#[allow(dead_code)]
pub fn at(x: f32, y: f32) -> Pos2 {
    pos2(x, y)
}
