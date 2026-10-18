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
    ChevronRight,
    Folder,
    Refresh,
    Tool,
    Target,
    Maximize,
    Restore,
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
        Icon::Maximize => closed(vec![p(3.0, 3.0), p(13.0, 3.0), p(13.0, 13.0), p(3.0, 13.0)]),
        Icon::Restore => {
            closed(vec![p(3.0, 5.5), p(10.5, 5.5), p(10.5, 13.0), p(3.0, 13.0)]);
            line(vec![
                p(5.5, 5.5),
                p(5.5, 3.0),
                p(13.0, 3.0),
                p(13.0, 10.5),
                p(10.5, 10.5),
            ]);
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

/// The glyph inside the app mark: a fibre loop with a drop into it.
fn mark_glyph(painter: &Painter, rect: Rect, color: Color32) {
    let s = rect.width().min(rect.height()) / 16.0;
    let p = |x: f32, y: f32| rect.center() - vec2(8.0, 8.0) * s + vec2(x, y) * s;
    let w = (1.9 * s).max(1.0);
    painter.circle_stroke(p(8.0, 8.5), 4.6 * s, Stroke::new(w, color));
    painter.circle_filled(p(8.0, 8.5), 1.6 * s, color);
    painter.add(PathShape::line(
        vec![p(8.0, 1.5), p(8.0, 5.0)],
        PathStroke::new(w, color),
    ));
}

/// The app mark as it appears in the title bar and About box: a rounded tile with the glyph.
pub fn paint_mark(painter: &Painter, rect: Rect, tile: Color32, glyph: Color32) {
    let r = rect.width().min(rect.height());
    let sq = Rect::from_center_size(rect.center(), Vec2::splat(r));
    painter.rect_filled(sq, r * 0.22, tile);
    mark_glyph(painter, sq.shrink(r * 0.12), glyph);
}

/// The same mark rasterised for the OS window icon and taskbar.
pub fn app_icon(size: u32) -> egui::IconData {
    let ss = 4u32;
    let n = size as f32;
    let mut rgba = Vec::with_capacity((size * size * 4) as usize);
    let tile = [0x3f_u8, 0x8c, 0xff];
    for y in 0..size {
        for x in 0..size {
            let mut cover = 0.0f32;
            let mut glyph = 0.0f32;
            for sy in 0..ss {
                for sx in 0..ss {
                    let gx = (x as f32 + (sx as f32 + 0.5) / ss as f32) / n * 16.0;
                    let gy = (y as f32 + (sy as f32 + 0.5) / ss as f32) / n * 16.0;
                    if in_rounded_square(gx, gy) {
                        cover += 1.0;
                        if in_glyph(gx, gy) {
                            glyph += 1.0;
                        }
                    }
                }
            }
            let total = (ss * ss) as f32;
            let a = cover / total;
            let g = if cover > 0.0 { glyph / cover } else { 0.0 };
            let ch = |c: u8| (c as f32 * (1.0 - g) + 255.0 * g).round() as u8;
            rgba.extend_from_slice(&[
                ch(tile[0]),
                ch(tile[1]),
                ch(tile[2]),
                (a * 255.0).round() as u8,
            ]);
        }
    }
    egui::IconData {
        rgba,
        width: size,
        height: size,
    }
}

fn in_rounded_square(x: f32, y: f32) -> bool {
    let (lo, hi, r) = (0.5, 15.5, 3.5);
    if x < lo || x > hi || y < lo || y > hi {
        return false;
    }
    let cx = x.clamp(lo + r, hi - r);
    let cy = y.clamp(lo + r, hi - r);
    (x - cx).powi(2) + (y - cy).powi(2) <= r * r
}

fn in_glyph(x: f32, y: f32) -> bool {
    // Mirrors `mark_glyph` on the 16-grid, inset by 12% like `paint_mark`.
    let scale = 0.76;
    let (cx, cy) = (8.0, 8.0 + 0.5 * scale);
    let d = ((x - cx).powi(2) + (y - cy).powi(2)).sqrt();
    let ring = (d - 4.6 * scale).abs() <= 0.95 * scale;
    let dot = d <= 1.6 * scale;
    let stem = (x - 8.0).abs() <= 0.95 * scale && y >= 8.0 - 6.5 * scale && y <= 8.0 - 3.0 * scale;
    ring || dot || stem
}

/// Window-control glyphs drawn on whole pixels with 1 px lines, like the OS's own caption buttons.
pub fn paint_window_glyph(painter: &Painter, center: Pos2, icon: Icon, color: Color32) {
    let px = painter.ctx().pixels_per_point();
    let snap = |v: f32| ((v * px).round() + 0.5) / px;
    let (cx, cy) = (snap(center.x), snap(center.y));
    let h = 5.0;
    let stroke = Stroke::new(1.0 / px.max(1.0) * px.round().max(1.0), color);
    match icon {
        Icon::Minus => {
            painter.line_segment([pos2(cx - h, cy), pos2(cx + h, cy)], stroke);
        }
        Icon::Maximize => {
            painter.rect_stroke(
                Rect::from_min_max(pos2(cx - h, cy - h), pos2(cx + h, cy + h)),
                1.0,
                stroke,
                egui::StrokeKind::Middle,
            );
        }
        Icon::Restore => {
            painter.rect_stroke(
                Rect::from_min_max(pos2(cx - h, cy - h + 2.0), pos2(cx + h - 2.0, cy + h)),
                1.0,
                stroke,
                egui::StrokeKind::Middle,
            );
            painter.add(PathShape::line(
                vec![
                    pos2(cx - h + 2.0, cy - h + 2.0),
                    pos2(cx - h + 2.0, cy - h),
                    pos2(cx + h, cy - h),
                    pos2(cx + h, cy + h - 2.0),
                    pos2(cx + h - 2.0, cy + h - 2.0),
                ],
                PathStroke::new(stroke.width, color),
            ));
        }
        _ => {
            painter.line_segment([pos2(cx - h, cy - h), pos2(cx + h, cy + h)], stroke);
            painter.line_segment([pos2(cx + h, cy - h), pos2(cx - h, cy + h)], stroke);
        }
    }
}
