//! Line icons drawn with the painter on a 16 × 16 grid: crisp at any scale, no icon font.

use egui::epaint::{PathShape, PathStroke};
use std::cell::RefCell;
use std::collections::HashMap;

use egui::{
    pos2, vec2, Color32, ColorImage, Painter, Pos2, Rect, Stroke, TextureHandle, TextureOptions,
    Vec2,
};
use std::f32::consts::TAU;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Icon {
    Plus,
    Minus,
    Fit,
    Close,
    Stop,
    Search,
    Check,
    Warning,
    ChevronRight,
    ChevronDown,
    Folder,
    Refresh,
    Tool,
    Target,
    Maximize,
    Restore,
    ArrowUp,
    Mic,
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
    // Round caps and joins: a dot at every vertex, for a drawn-with-a-pen finish.
    let pen = |pts: Vec<Pos2>, width: f32| {
        for q in &pts {
            painter.circle_filled(*q, width / 2.0, color);
        }
        painter.add(PathShape::line(pts, PathStroke::new(width, color)));
    };
    match icon {
        Icon::ArrowUp => {
            let w2 = (1.9 * s).max(1.2);
            pen(vec![p(8.0, 13.2), p(8.0, 3.3)], w2);
            pen(vec![p(3.6, 7.7), p(8.0, 3.3), p(12.4, 7.7)], w2);
        }
        Icon::Mic => {
            let w2 = (1.6 * s).max(1.1);
            let body = Rect::from_min_max(p(5.9, 1.6), p(10.1, 9.8));
            painter.rect_stroke(
                body,
                2.1 * s,
                Stroke::new(w2, color),
                egui::StrokeKind::Middle,
            );
            let arc: Vec<Pos2> = (0..=16)
                .map(|i| {
                    let a = std::f32::consts::PI * i as f32 / 16.0;
                    p(8.0 + 4.7 * a.cos(), 7.4 + 4.7 * a.sin())
                })
                .collect();
            pen(arc, w2);
            pen(vec![p(8.0, 12.2), p(8.0, 14.6)], w2);
        }
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
        Icon::ChevronDown => line(vec![p(3.5, 6.0), p(8.0, 10.5), p(12.5, 6.0)]),
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
/// The openFTTH mark: two fibre strands flowing in from opposite corners and meeting in a
/// bright four-point spark, the fusion splice at the heart of every fibre network (and a nod to
/// the crew of agents). Cyan-to-violet strands on a deep navy tile with a soft glow. Geometry
/// lives on a 16-unit grid and is rasterised once; the window icon, the .exe icon and every mark
/// drawn inside the app use that same raster.
const MARK_STRANDS: [[(f32, f32); 3]; 2] = [
    [(2.55, 12.0), (4.4, 8.1), (6.55, 8.0)],
    [(9.45, 8.0), (11.6, 7.9), (13.45, 4.0)],
];
const MARK_STRAND_HALF: f32 = 0.92;
const MARK_SPARK: (f32, f32, f32) = (8.0, 8.0, 3.35);
const MARK_TILE_TOP: [f32; 3] = [30.0, 34.0, 68.0];
const MARK_TILE_BOTTOM: [f32; 3] = [12.0, 14.0, 32.0];
const MARK_CYAN: [f32; 3] = [34.0, 211.0, 238.0];
const MARK_VIOLET: [f32; 3] = [150.0, 104.0, 255.0];

/// The app mark in the title bar, the About box and the start page.
pub fn paint_mark(painter: &Painter, rect: Rect) {
    let r = rect.width().min(rect.height());
    let sq = Rect::from_center_size(rect.center(), Vec2::splat(r));
    let tex = APP_MARK.with(|cell| {
        cell.borrow_mut()
            .get_or_insert_with(|| {
                let icon = app_icon(128);
                let image = ColorImage::from_rgba_unmultiplied([128, 128], &icon.rgba);
                painter.ctx().load_texture("app-mark", image, TextureOptions::LINEAR)
            })
            .clone()
    });
    painter.image(
        tex.id(),
        sq,
        Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)),
        Color32::WHITE,
    );
}

thread_local! {
    static APP_MARK: RefCell<Option<TextureHandle>> = const { RefCell::new(None) };
}

/// The mark rasterised at `size` px (RGBA, straight alpha), 6 × 6 supersampled.
pub fn app_icon(size: u32) -> egui::IconData {
    let ss = 6u32;
    let n = size as f32;
    let mut rgba = Vec::with_capacity((size * size * 4) as usize);
    for y in 0..size {
        for x in 0..size {
            let mut acc = [0.0f32; 3];
            let mut cover = 0.0f32;
            for sy in 0..ss {
                for sx in 0..ss {
                    let gx = (x as f32 + (sx as f32 + 0.5) / ss as f32) / n * 16.0;
                    let gy = (y as f32 + (sy as f32 + 0.5) / ss as f32) / n * 16.0;
                    if let Some(c) = mark_sample(gx, gy) {
                        cover += 1.0;
                        for k in 0..3 {
                            acc[k] += c[k];
                        }
                    }
                }
            }
            let total = (ss * ss) as f32;
            let px = |k: usize| {
                if cover > 0.0 {
                    (acc[k] / cover).round().clamp(0.0, 255.0) as u8
                } else {
                    0
                }
            };
            rgba.extend_from_slice(&[px(0), px(1), px(2), (cover / total * 255.0).round() as u8]);
        }
    }
    egui::IconData {
        rgba,
        width: size,
        height: size,
    }
}

/// Colour of the mark at a point of the 16-grid, or None outside the tile.
fn mark_sample(x: f32, y: f32) -> Option<[f32; 3]> {
    if !in_rounded_square(x, y) {
        return None;
    }
    let mix = |a: [f32; 3], b: [f32; 3], t: f32| {
        let t = t.clamp(0.0, 1.0);
        [
            a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t,
        ]
    };
    let (sx, sy, sa) = MARK_SPARK;
    let (dx, dy) = ((x - sx).abs(), (y - sy).abs());
    // The spark: an astroid, four slender points.
    if (dx / sa).powf(2.0 / 3.0) + (dy / sa).powf(2.0 / 3.0) <= 1.0 {
        return Some([255.0, 255.0, 255.0]);
    }
    for [a, c, b] in MARK_STRANDS {
        if bezier_distance(a, c, b, x, y) <= MARK_STRAND_HALF {
            // Light runs cyan on the lower left to violet on the upper right.
            let t = ((x - 2.5) + (12.0 - y)) / 19.0;
            return Some(mix(MARK_CYAN, MARK_VIOLET, t));
        }
    }
    let mut c = mix(MARK_TILE_TOP, MARK_TILE_BOTTOM, y / 16.0);
    // The splice glows: a soft halo around the spark, tinted by both strands.
    let d = ((x - sx).powi(2) + (y - sy).powi(2)).sqrt();
    let halo = (1.0 - d / 5.6).clamp(0.0, 1.0).powi(2) * 0.55;
    let tint = mix(MARK_CYAN, MARK_VIOLET, 0.5);
    for k in 0..3 {
        c[k] += (tint[k] - c[k]) * halo;
    }
    // A hairline of light along the top edge, so the tile reads as a surface.
    let rim = (1.0 - (y - 0.55) / 1.2).clamp(0.0, 1.0) * 0.22;
    for v in &mut c {
        *v += (255.0 - *v) * rim;
    }
    Some(c)
}

fn in_rounded_square(x: f32, y: f32) -> bool {
    let (lo, hi, r) = (0.55, 15.45, 3.7);
    if x < lo || x > hi || y < lo || y > hi {
        return false;
    }
    let cx = x.clamp(lo + r, hi - r);
    let cy = y.clamp(lo + r, hi - r);
    (x - cx).powi(2) + (y - cy).powi(2) <= r * r
}

/// Distance from a point to a quadratic Bézier, by its polyline of 24 segments.
fn bezier_distance(a: (f32, f32), c: (f32, f32), b: (f32, f32), x: f32, y: f32) -> f32 {
    let at = |t: f32| {
        let u = 1.0 - t;
        (
            u * u * a.0 + 2.0 * u * t * c.0 + t * t * b.0,
            u * u * a.1 + 2.0 * u * t * c.1 + t * t * b.1,
        )
    };
    let mut best = f32::INFINITY;
    let mut prev = at(0.0);
    for i in 1..=24 {
        let cur = at(i as f32 / 24.0);
        let (dx, dy) = (cur.0 - prev.0, cur.1 - prev.1);
        let len2 = dx * dx + dy * dy;
        let t = if len2 > 0.0 {
            (((x - prev.0) * dx + (y - prev.1) * dy) / len2).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let (px, py) = (prev.0 + t * dx, prev.1 + t * dy);
        best = best.min(((x - px).powi(2) + (y - py).powi(2)).sqrt());
        prev = cur;
    }
    best
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

// ---- engine marks ---------------------------------------------------------------------------

const MARK_SIZE: usize = 96;

/// Alpha masks rasterised from the engine icons by `tools/rasterize_marks.py`.
const MARKS: &[(&str, &[u8])] = &[
    ("claude", include_bytes!("../assets/engines/claude.a8")),
    ("codex", include_bytes!("../assets/engines/codex.a8")),
    ("hermes", include_bytes!("../assets/engines/hermes.a8")),
    ("ollama", include_bytes!("../assets/engines/ollama.a8")),
    (
        "openrouter",
        include_bytes!("../assets/engines/openrouter.a8"),
    ),
    ("openai", include_bytes!("../assets/engines/openai.a8")),
];

thread_local! {
    static MARK_TEXTURES: RefCell<HashMap<&'static str, TextureHandle>> = RefCell::new(HashMap::new());
}

fn mark_texture(ctx: &egui::Context, kind: &str) -> Option<TextureHandle> {
    let (name, mask) = MARKS.iter().find(|(k, _)| *k == kind)?;
    MARK_TEXTURES.with(|cache| {
        let mut cache = cache.borrow_mut();
        if let Some(t) = cache.get(name) {
            return Some(t.clone());
        }
        let pixels: Vec<Color32> = mask.iter().map(|a| Color32::from_white_alpha(*a)).collect();
        let image = ColorImage {
            size: [MARK_SIZE, MARK_SIZE],
            source_size: Vec2::splat(MARK_SIZE as f32),
            pixels,
        };
        let handle = ctx.load_texture(format!("engine-mark-{name}"), image, TextureOptions::LINEAR);
        cache.insert(name, handle.clone());
        Some(handle)
    })
}

/// An engine's mark. Claude keeps its own hue; every other mark takes the text colour.
pub fn paint_engine_mark(painter: &Painter, rect: Rect, kind: &str, p: &crate::theme::Palette) {
    let r = rect.width().min(rect.height());
    let sq = Rect::from_center_size(rect.center(), Vec2::splat(r));
    let tint = match kind {
        "claude" => Color32::from_rgb(0xd9, 0x77, 0x57),
        _ => p.text,
    };
    match mark_texture(painter.ctx(), kind) {
        Some(tex) => {
            painter.image(
                tex.id(),
                sq,
                Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)),
                tint,
            );
        }
        None => {
            painter.rect_filled(sq, r * 0.25, p.text3);
            painter.text(
                sq.center(),
                egui::Align2::CENTER_CENTER,
                "?",
                crate::theme::font(r * 0.62, crate::theme::Weight::Semibold),
                Color32::WHITE,
            );
        }
    }
}

#[cfg(test)]
mod ico {
    /// Writes `assets/openftth.ico` (16–256 px, 32-bit) from the same rasteriser as the
    /// window icon, for the .exe resource. Run with `cargo test -p openftth-app -- --ignored`.
    #[test]
    #[ignore]
    fn write_app_ico() {
        let sizes = [16u32, 24, 32, 48, 64, 128, 256];
        let images: Vec<Vec<u8>> = sizes.iter().map(|&s| dib(s)).collect();
        let mut out = Vec::new();
        out.extend_from_slice(&[0, 0, 1, 0]);
        out.extend_from_slice(&(sizes.len() as u16).to_le_bytes());
        let mut offset = 6 + 16 * sizes.len() as u32;
        for (s, img) in sizes.iter().zip(&images) {
            let b = if *s >= 256 { 0 } else { *s as u8 };
            out.extend_from_slice(&[b, b, 0, 0, 1, 0, 32, 0]);
            out.extend_from_slice(&(img.len() as u32).to_le_bytes());
            out.extend_from_slice(&offset.to_le_bytes());
            offset += img.len() as u32;
        }
        for img in &images {
            out.extend_from_slice(img);
        }
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("assets/openftth.ico");
        std::fs::write(path, out).unwrap();
    }

    /// One icon entry: BITMAPINFOHEADER, bottom-up BGRA rows, and an all-zero AND mask.
    fn dib(size: u32) -> Vec<u8> {
        let icon = super::app_icon(size);
        let mut v = Vec::new();
        v.extend_from_slice(&40u32.to_le_bytes());
        v.extend_from_slice(&(size as i32).to_le_bytes());
        v.extend_from_slice(&((size * 2) as i32).to_le_bytes());
        v.extend_from_slice(&1u16.to_le_bytes());
        v.extend_from_slice(&32u16.to_le_bytes());
        v.extend_from_slice(&[0u8; 24]);
        for y in (0..size).rev() {
            for x in 0..size {
                let i = ((y * size + x) * 4) as usize;
                let px = &icon.rgba[i..i + 4];
                v.extend_from_slice(&[px[2], px[1], px[0], px[3]]);
            }
        }
        let mask_row = size.div_ceil(32) * 4;
        v.extend(std::iter::repeat_n(0u8, (mask_row * size) as usize));
        v
    }
}
