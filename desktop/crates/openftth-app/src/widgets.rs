//! Small reusable pieces: status dots, pills, KPI tiles, cards, toasts, time formatting.

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use egui::{Color32, CornerRadius, Frame, Margin, Response, RichText, Stroke, Ui, Vec2};

use crate::theme::{Palette, RADIUS, RADIUS_SM};

/// A coloured disc (the default font has no bullet glyph).
pub fn dot(ui: &mut Ui, color: Color32, r: f32) {
    let (rect, _) = ui.allocate_exact_size(Vec2::splat(r * 2.0 + 2.0), egui::Sense::hover());
    ui.painter().circle_filled(rect.center(), r, color);
}

/// A pulsing disc for "working" states.
pub fn pulse_dot(ui: &mut Ui, color: Color32, r: f32) {
    let t = ui.input(|i| i.time) as f32;
    let k = 0.65 + 0.35 * (t * 4.0).sin();
    let (rect, _) = ui.allocate_exact_size(Vec2::splat(r * 2.0 + 2.0), egui::Sense::hover());
    ui.painter()
        .circle_filled(rect.center(), r * k, color.gamma_multiply(0.6 + 0.4 * k));
    ui.ctx().request_repaint_after(Duration::from_millis(40));
}

/// A small rounded label with a tinted background.
pub fn pill(ui: &mut Ui, text: &str, fg: Color32, bg: Color32) -> Response {
    Frame::new()
        .fill(bg)
        .corner_radius(CornerRadius::same(10))
        .inner_margin(Margin::symmetric(8, 2))
        .show(ui, |ui| ui.label(RichText::new(text).small().color(fg)))
        .response
}

/// A card: surface background, hairline border, rounded corners, inner padding.
pub fn card<R>(ui: &mut Ui, p: &Palette, add: impl FnOnce(&mut Ui) -> R) -> R {
    Frame::new()
        .fill(p.surface)
        .stroke(Stroke::new(1.0, p.border))
        .corner_radius(CornerRadius::same(RADIUS))
        .inner_margin(Margin::same(12))
        .show(ui, add)
        .inner
}

/// A tinted card (approvals, warnings).
pub fn tinted_card<R>(
    ui: &mut Ui,
    fill: Color32,
    border: Color32,
    add: impl FnOnce(&mut Ui) -> R,
) -> R {
    Frame::new()
        .fill(fill)
        .stroke(Stroke::new(1.0, border))
        .corner_radius(CornerRadius::same(RADIUS))
        .inner_margin(Margin::same(12))
        .show(ui, add)
        .inner
}

/// A KPI tile: big value, small caption.
pub fn kpi(ui: &mut Ui, p: &Palette, value: &str, caption: &str, accent: Option<Color32>) {
    Frame::new()
        .fill(p.surface)
        .stroke(Stroke::new(1.0, p.border))
        .corner_radius(CornerRadius::same(RADIUS))
        .inner_margin(Margin::symmetric(14, 10))
        .show(ui, |ui| {
            ui.set_min_width(120.0);
            ui.vertical(|ui| {
                ui.label(
                    RichText::new(value)
                        .size(20.0)
                        .strong()
                        .color(accent.unwrap_or(p.text)),
                );
                ui.label(RichText::new(caption).small().color(p.text_muted));
            });
        });
}

pub fn section_title(ui: &mut Ui, p: &Palette, text: &str) {
    ui.add_space(2.0);
    ui.label(
        RichText::new(text.to_uppercase())
            .small()
            .strong()
            .color(p.text_muted),
    );
}

/// A primary (accent) button.
pub fn primary_button(ui: &mut Ui, p: &Palette, text: &str) -> Response {
    ui.add(
        egui::Button::new(RichText::new(text).color(Color32::WHITE).strong())
            .fill(p.accent)
            .corner_radius(CornerRadius::same(RADIUS_SM)),
    )
}

/// A quiet secondary button.
pub fn ghost_button(ui: &mut Ui, p: &Palette, text: &str) -> Response {
    ui.add(
        egui::Button::new(RichText::new(text).color(p.text))
            .fill(p.surface_alt)
            .stroke(Stroke::new(1.0, p.border))
            .corner_radius(CornerRadius::same(RADIUS_SM)),
    )
}

/// Short relative time for a unix-ms timestamp.
pub fn relative_time(at_ms: i64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    let s = ((now - at_ms) / 1000).max(0);
    match s {
        0..=9 => "just now".into(),
        10..=59 => format!("{s}s ago"),
        60..=3599 => format!("{}m ago", s / 60),
        3600..=86399 => format!("{}h ago", s / 3600),
        _ => format!("{}d ago", s / 86400),
    }
}

/// Clock time (HH:MM) for a unix-ms timestamp, in local time if the offset is known.
pub fn clock_time(at_ms: i64) -> String {
    let secs = at_ms / 1000;
    let day = secs.rem_euclid(86400);
    format!("{:02}:{:02}", day / 3600, (day % 3600) / 60)
}

pub fn short_id(id: &str) -> &str {
    &id[..id.len().min(8)]
}

pub fn fmt_money(v: f64, currency: &str) -> String {
    let n = v.round() as i64;
    let digits = n.abs().to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    format!("{}{} {}", if n < 0 { "-" } else { "" }, out, currency)
}

pub fn fmt_metres(m: f64) -> String {
    if m >= 1000.0 {
        format!("{:.1} km", m / 1000.0)
    } else {
        format!("{m:.0} m")
    }
}

// ---- toasts -------------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ToastKind {
    Info,
    Success,
    Error,
}

pub struct Toast {
    pub kind: ToastKind,
    pub text: String,
    pub until: Instant,
}

#[derive(Default)]
pub struct Toasts {
    items: Vec<Toast>,
}

impl Toasts {
    pub fn push(&mut self, kind: ToastKind, text: impl Into<String>) {
        let secs = match kind {
            ToastKind::Error => 8,
            _ => 4,
        };
        self.items.push(Toast {
            kind,
            text: text.into(),
            until: Instant::now() + Duration::from_secs(secs),
        });
        if self.items.len() > 5 {
            self.items.remove(0);
        }
    }

    pub fn error(&mut self, text: impl Into<String>) {
        self.push(ToastKind::Error, text);
    }

    pub fn info(&mut self, text: impl Into<String>) {
        self.push(ToastKind::Info, text);
    }

    pub fn success(&mut self, text: impl Into<String>) {
        self.push(ToastKind::Success, text);
    }

    pub fn show(&mut self, ctx: &egui::Context, p: &Palette) {
        let now = Instant::now();
        self.items.retain(|t| t.until > now);
        if self.items.is_empty() {
            return;
        }
        egui::Area::new(egui::Id::unique("toasts"))
            .anchor(egui::Align2::RIGHT_BOTTOM, egui::vec2(-16.0, -40.0))
            .order(egui::Order::Foreground)
            .show(ctx, |ui| {
                for t in &self.items {
                    let (fg, bg) = match t.kind {
                        ToastKind::Info => (p.text, p.surface),
                        ToastKind::Success => (Color32::WHITE, p.success),
                        ToastKind::Error => (Color32::WHITE, p.danger),
                    };
                    Frame::new()
                        .fill(bg)
                        .stroke(Stroke::new(1.0, p.border))
                        .corner_radius(CornerRadius::same(RADIUS))
                        .inner_margin(Margin::symmetric(12, 8))
                        .show(ui, |ui| {
                            ui.set_max_width(360.0);
                            ui.label(RichText::new(&t.text).color(fg));
                        });
                }
            });
        ctx.request_repaint_after(Duration::from_millis(500));
    }
}
