//! Controls painted to the design system: text helpers, caps labels, hairlines, panel headers,
//! quiet/prominent buttons, icon buttons, key-value rows, segmented progress and toasts.

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use egui::{
    pos2, vec2, Color32, CornerRadius, CursorIcon, FontId, Margin, Response, RichText, Sense,
    Stroke, Ui, Vec2,
};

use crate::icons::{self, Icon};
use crate::theme::{font, mono, Palette, Weight, R, R_FLOAT};

pub fn text(content: impl Into<String>, size: f32, weight: Weight, color: Color32) -> RichText {
    RichText::new(content).font(font(size, weight)).color(color)
}

pub fn mono_text(content: impl Into<String>, size: f32, color: Color32) -> RichText {
    RichText::new(content).font(mono(size)).color(color)
}

/// Uppercase section label with letter spacing.
pub fn caps(content: &str, color: Color32) -> RichText {
    RichText::new(content.to_uppercase())
        .font(font(10.5, Weight::Semibold))
        .color(color)
        .extra_letter_spacing(0.9)
}

pub fn hairline(ui: &mut Ui, p: &Palette) {
    let (rect, _) = ui.allocate_exact_size(vec2(ui.available_width(), 1.0), Sense::hover());
    ui.painter().hline(
        rect.left()..=rect.right(),
        rect.center().y,
        Stroke::new(1.0, p.line),
    );
}

/// Panel header strip: caps title on the left, anything on the right, hairline below.
pub fn panel_header(ui: &mut Ui, p: &Palette, title: &str, right: impl FnOnce(&mut Ui)) {
    ui.horizontal(|ui| {
        ui.set_height(26.0);
        ui.add_space(2.0);
        ui.label(caps(title, p.text3));
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), right);
    });
    hairline(ui, p);
}

pub fn section(ui: &mut Ui, p: &Palette, title: &str) {
    ui.add_space(8.0);
    ui.label(caps(title, p.text3));
    ui.add_space(2.0);
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Quiet,
    Prominent,
    Danger,
    Ghost,
}

/// A compact painted button.
pub fn button(ui: &mut Ui, p: &Palette, label: &str, kind: Kind) -> Response {
    button_ex(ui, p, None, label, kind, true)
}

pub fn button_enabled(
    ui: &mut Ui,
    p: &Palette,
    label: &str,
    kind: Kind,
    enabled: bool,
) -> Response {
    button_ex(ui, p, None, label, kind, enabled)
}

pub fn button_ex(
    ui: &mut Ui,
    p: &Palette,
    icon: Option<Icon>,
    label: &str,
    kind: Kind,
    enabled: bool,
) -> Response {
    let fg = match kind {
        Kind::Prominent => Color32::WHITE,
        Kind::Danger => p.red,
        _ => p.text,
    };
    let galley = ui
        .painter()
        .layout_no_wrap(label.to_string(), font(12.0, Weight::Medium), fg);
    let icon_w = if icon.is_some() { 16.0 } else { 0.0 };
    let pad = if label.is_empty() { 0.0 } else { 20.0 };
    let size = vec2(
        galley.size().x
            + pad
            + icon_w
            + if icon.is_some() && !label.is_empty() {
                4.0
            } else {
                6.0
            },
        24.0,
    );
    let (rect, response) = ui.allocate_exact_size(
        size,
        if enabled {
            Sense::click()
        } else {
            Sense::hover()
        },
    );
    let hovered = enabled && response.hovered();
    let down = response.is_pointer_button_down_on();
    let base = match kind {
        Kind::Prominent => p.accent,
        Kind::Ghost => Color32::TRANSPARENT,
        _ => p.bg2,
    };
    let mut fill = if hovered {
        match kind {
            Kind::Prominent => icons::lerp(p.accent, Color32::WHITE, 0.12),
            _ => p.bg3,
        }
    } else {
        base
    };
    if down {
        fill = fill.gamma_multiply(0.8);
    }
    let mut fg = fg;
    if !enabled {
        fill = fill.gamma_multiply(0.5);
        fg = fg.gamma_multiply(0.4);
    }
    let painter = ui.painter();
    painter.rect_filled(rect, R as f32, fill);
    if kind == Kind::Quiet || kind == Kind::Danger {
        painter.rect_stroke(
            rect,
            R as f32,
            Stroke::new(1.0, if hovered { p.line_strong } else { p.line }),
            egui::StrokeKind::Inside,
        );
    }
    let mut x = rect.left() + if label.is_empty() { 4.0 } else { 10.0 };
    if let Some(ic) = icon {
        icons::paint(
            painter,
            icons::square(pos2(x + 8.0, rect.center().y), 14.0),
            ic,
            fg,
        );
        x += icon_w + 2.0;
    }
    painter.galley(pos2(x, rect.center().y - galley.size().y / 2.0), galley, fg);
    if enabled {
        response.on_hover_cursor(CursorIcon::PointingHand)
    } else {
        response
    }
}

/// A square icon-only button (toolbar, row actions).
pub fn icon_button(ui: &mut Ui, p: &Palette, icon: Icon, tooltip: &str) -> Response {
    icon_button_sized(ui, p, icon, tooltip, 24.0, false)
}

pub fn icon_button_sized(
    ui: &mut Ui,
    p: &Palette,
    icon: Icon,
    tooltip: &str,
    size: f32,
    raised: bool,
) -> Response {
    let (rect, response) = ui.allocate_exact_size(Vec2::splat(size), Sense::click());
    let hovered = response.hovered();
    let painter = ui.painter();
    if raised || hovered {
        painter.rect_filled(rect, R as f32, if hovered { p.bg3 } else { p.bg2 });
    }
    let color = if hovered { p.text } else { p.text2 };
    icons::paint(
        painter,
        icons::square(rect.center(), size * 0.6),
        icon,
        color,
    );
    let response = response.on_hover_cursor(CursorIcon::PointingHand);
    if tooltip.is_empty() {
        response
    } else {
        response.on_hover_text(tooltip)
    }
}

/// Small status disc.
pub fn dot(ui: &mut Ui, color: Color32, r: f32) {
    let (rect, _) = ui.allocate_exact_size(Vec2::splat(r * 2.0 + 2.0), Sense::hover());
    ui.painter().circle_filled(rect.center(), r, color);
}

pub fn pulse_dot(ui: &mut Ui, color: Color32, r: f32) {
    let t = ui.input(|i| i.time) as f32;
    let k = 0.6 + 0.4 * (t * 5.0).sin();
    let (rect, _) = ui.allocate_exact_size(Vec2::splat(r * 2.0 + 2.0), Sense::hover());
    ui.painter()
        .circle_filled(rect.center(), r, color.gamma_multiply(0.45 + 0.55 * k));
    ui.ctx().request_repaint_after(Duration::from_millis(40));
}

/// A small tinted tag.
pub fn tag(ui: &mut Ui, label: &str, fg: Color32, bg: Color32) -> Response {
    let galley = ui
        .painter()
        .layout_no_wrap(label.to_string(), font(10.5, Weight::Medium), fg);
    let size = vec2(galley.size().x + 10.0, 17.0);
    let (rect, response) = ui.allocate_exact_size(size, Sense::hover());
    ui.painter().rect_filled(rect, 3.0, bg);
    ui.painter().galley(
        pos2(rect.left() + 5.0, rect.center().y - galley.size().y / 2.0),
        galley,
        fg,
    );
    response
}

/// A key / value row in a properties grid.
pub fn kv(ui: &mut Ui, p: &Palette, key: &str, value: &str) {
    ui.horizontal(|ui| {
        ui.set_min_height(20.0);
        let (r, _) = ui.allocate_exact_size(vec2(108.0, 18.0), Sense::hover());
        ui.painter().text(
            pos2(r.left(), r.center().y),
            egui::Align2::LEFT_CENTER,
            key,
            font(11.5, Weight::Regular),
            p.text3,
        );
        ui.add(egui::Label::new(text(value, 12.0, Weight::Regular, p.text)).truncate());
    });
}

/// A raised floating panel (map overlays).
pub fn float_frame(p: &Palette) -> egui::Frame {
    egui::Frame::new()
        .fill(p.bg2.gamma_multiply(0.96))
        .stroke(Stroke::new(1.0, p.line_strong))
        .corner_radius(CornerRadius::same(R_FLOAT))
        .inner_margin(Margin::same(6))
        .shadow(egui::epaint::Shadow {
            offset: [0, 2],
            blur: 8,
            spread: 0,
            color: Color32::from_black_alpha(90),
        })
}

/// A metric block: large value, small label, no border.
pub fn metric(ui: &mut Ui, p: &Palette, value: &str, label: &str, color: Option<Color32>) {
    egui::Frame::new()
        .fill(p.bg2)
        .corner_radius(CornerRadius::same(R))
        .inner_margin(Margin::symmetric(12, 8))
        .show(ui, |ui| {
            ui.set_min_width(112.0);
            ui.vertical(|ui| {
                ui.label(text(value, 17.0, Weight::Semibold, color.unwrap_or(p.text)));
                ui.label(text(label, 10.5, Weight::Regular, p.text3));
            });
        });
}

pub fn relative_time(at_ms: i64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    let s = ((now - at_ms) / 1000).max(0);
    match s {
        0..=9 => "now".into(),
        10..=59 => format!("{s}s"),
        60..=3599 => format!("{}m", s / 60),
        3600..=86399 => format!("{}h", s / 3600),
        _ => format!("{}d", s / 86400),
    }
}

/// Wall-clock time of a timestamp in the user's local time zone.
pub fn clock_time(at_ms: i64) -> String {
    use chrono::TimeZone;
    match chrono::Local.timestamp_millis_opt(at_ms).single() {
        Some(t) => t.format("%H:%M").to_string(),
        None => String::new(),
    }
}

pub fn short_id(id: &str) -> &str {
    &id[..id.len().min(7)]
}

pub fn fmt_int(n: i64) -> String {
    let digits = n.abs().to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    format!("{}{}", if n < 0 { "-" } else { "" }, out)
}

pub fn fmt_money(v: f64, currency: &str) -> String {
    format!("{} {currency}", fmt_int(v.round() as i64))
        .trim_end()
        .to_string()
}

pub fn fmt_metres(m: f64) -> String {
    if m >= 1000.0 {
        format!("{:.1} km", m / 1000.0)
    } else {
        format!("{m:.0} m")
    }
}

pub fn truncate(text: &str, n: usize) -> String {
    if text.chars().count() <= n {
        text.to_string()
    } else {
        format!("{}…", text.chars().take(n).collect::<String>())
    }
}

#[allow(dead_code)]
pub fn label_font(size: f32) -> FontId {
    font(size, Weight::Regular)
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
        let secs = if kind == ToastKind::Error { 8 } else { 4 };
        self.items.push(Toast {
            kind,
            text: text.into(),
            until: Instant::now() + Duration::from_secs(secs),
        });
        if self.items.len() > 4 {
            self.items.remove(0);
        }
    }
    pub fn error(&mut self, t: impl Into<String>) {
        self.push(ToastKind::Error, t);
    }
    pub fn info(&mut self, t: impl Into<String>) {
        self.push(ToastKind::Info, t);
    }
    pub fn success(&mut self, t: impl Into<String>) {
        self.push(ToastKind::Success, t);
    }

    pub fn show(&mut self, ctx: &egui::Context, p: &Palette) {
        let now = Instant::now();
        self.items.retain(|t| t.until > now);
        if self.items.is_empty() {
            return;
        }
        egui::Area::new(egui::Id::unique("toasts"))
            .anchor(egui::Align2::RIGHT_BOTTOM, vec2(-14.0, -32.0))
            .order(egui::Order::Foreground)
            .show(ctx, |ui| {
                for t in &self.items {
                    let bar = match t.kind {
                        ToastKind::Info => p.accent,
                        ToastKind::Success => p.green,
                        ToastKind::Error => p.red,
                    };
                    float_frame(p)
                        .inner_margin(Margin::symmetric(10, 8))
                        .show(ui, |ui| {
                            ui.set_max_width(340.0);
                            ui.horizontal(|ui| {
                                let (r, _) =
                                    ui.allocate_exact_size(vec2(3.0, 16.0), Sense::hover());
                                ui.painter().rect_filled(r, 1.0, bar);
                                ui.add(
                                    egui::Label::new(text(&t.text, 12.0, Weight::Regular, p.text))
                                        .wrap(),
                                );
                            });
                        });
                }
            });
        ctx.request_repaint_after(Duration::from_millis(400));
    }
}

// ---- menus --------------------------------------------------------------------------------

/// Call first inside a menu closure: fixes the width and the spacing the popup inherited.
pub fn menu_begin(ui: &mut Ui, width: f32) {
    ui.set_min_width(width);
    ui.set_max_width(width);
    ui.spacing_mut().button_padding = egui::vec2(10.0, 4.0);
    ui.spacing_mut().item_spacing = egui::vec2(6.0, 2.0);
}

/// One row in a menu: label left, shortcut right. Closes the menu when clicked.
pub fn menu_item(
    ui: &mut Ui,
    p: &Palette,
    label: &str,
    shortcut: Option<&str>,
    enabled: bool,
) -> bool {
    let w = ui.available_width().clamp(180.0, 360.0);
    let (rect, resp) = ui.allocate_exact_size(
        egui::vec2(w, 24.0),
        if enabled {
            egui::Sense::click()
        } else {
            egui::Sense::hover()
        },
    );
    if enabled && resp.hovered() {
        ui.painter().rect_filled(rect, R as f32, p.accent_soft);
    }
    let color = if enabled { p.text } else { p.text3 };
    ui.painter().text(
        egui::pos2(rect.left() + 10.0, rect.center().y),
        egui::Align2::LEFT_CENTER,
        label,
        font(12.5, Weight::Regular),
        color,
    );
    if let Some(s) = shortcut {
        ui.painter().text(
            egui::pos2(rect.right() - 10.0, rect.center().y),
            egui::Align2::RIGHT_CENTER,
            s,
            font(11.5, Weight::Regular),
            p.text3,
        );
    }
    let clicked = enabled && resp.on_hover_cursor(CursorIcon::PointingHand).clicked();
    if clicked {
        ui.close();
    }
    clicked
}

/// A toggle row with a check mark; stays open so several can be flipped. Returns true on change.
pub fn menu_check(ui: &mut Ui, p: &Palette, label: &str, on: &mut bool) -> bool {
    let w = ui.available_width().clamp(160.0, 360.0);
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(w, 24.0), egui::Sense::click());
    if resp.hovered() {
        ui.painter().rect_filled(rect, R as f32, p.accent_soft);
    }
    if *on {
        icons::paint(
            ui.painter(),
            icons::square(egui::pos2(rect.left() + 14.0, rect.center().y), 12.0),
            Icon::Check,
            p.text,
        );
    }
    ui.painter().text(
        egui::pos2(rect.left() + 28.0, rect.center().y),
        egui::Align2::LEFT_CENTER,
        label,
        font(12.5, Weight::Regular),
        p.text,
    );
    if resp.on_hover_cursor(CursorIcon::PointingHand).clicked() {
        *on = !*on;
        return true;
    }
    false
}

pub fn menu_separator(ui: &mut Ui, p: &Palette) {
    let (r, _) =
        ui.allocate_exact_size(egui::vec2(ui.available_width(), 7.0), egui::Sense::hover());
    ui.painter()
        .hline(r.x_range(), r.center().y, egui::Stroke::new(1.0, p.line));
}

/// A labelled text field; with suggestions it becomes an editable combo.
pub fn kv_edit(
    ui: &mut Ui,
    p: &Palette,
    key: &str,
    value: &mut String,
    hint: &str,
    suggestions: &[String],
) {
    ui.horizontal(|ui| {
        ui.add_sized(
            [90.0, 22.0],
            egui::Label::new(text(key, 12.0, Weight::Regular, p.text2)),
        );
        let w = ui.available_width() - if suggestions.is_empty() { 0.0 } else { 30.0 };
        ui.add_sized([w, 22.0], egui::TextEdit::singleline(value).hint_text(hint));
        if !suggestions.is_empty() {
            ui.menu_button(text("▾", 12.0, Weight::Regular, p.text2), |ui| {
                menu_begin(ui, 260.0);
                for s in suggestions {
                    if menu_item(ui, p, s, None, true) {
                        *value = s.clone();
                    }
                }
            })
            .response
            .on_hover_cursor(CursorIcon::PointingHand);
        }
    });
}

/// A menu row with an engine mark, label, detail on the right and a check when current.
pub fn menu_row_mark(
    ui: &mut Ui,
    p: &Palette,
    kind: &str,
    label: &str,
    detail: &str,
    current: bool,
) -> bool {
    let w = ui.available_width().clamp(220.0, 360.0);
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(w, 28.0), egui::Sense::click());
    if resp.hovered() {
        ui.painter().rect_filled(rect, R as f32, p.accent_soft);
    }
    icons::paint_engine_mark(
        ui.painter(),
        egui::Rect::from_center_size(
            egui::pos2(rect.left() + 18.0, rect.center().y),
            egui::vec2(16.0, 16.0),
        ),
        kind,
        p,
    );
    ui.painter().text(
        egui::pos2(rect.left() + 34.0, rect.center().y),
        egui::Align2::LEFT_CENTER,
        label,
        font(
            12.5,
            if current {
                Weight::Medium
            } else {
                Weight::Regular
            },
        ),
        p.text,
    );
    let label_w = ui
        .painter()
        .layout_no_wrap(label.to_string(), font(12.5, Weight::Regular), p.text)
        .size()
        .x;
    let right = rect.right() - 10.0;
    let mut right_edge = right;
    if current {
        icons::paint(
            ui.painter(),
            icons::square(egui::pos2(right - 6.0, rect.center().y), 12.0),
            Icon::Check,
            p.text,
        );
        right_edge = right - 18.0;
    }
    let room = right_edge - (rect.left() + 34.0 + label_w + 12.0);
    if room > 40.0 {
        ui.painter().text(
            egui::pos2(right_edge, rect.center().y),
            egui::Align2::RIGHT_CENTER,
            truncate(detail, (room / 6.0) as usize),
            font(11.0, Weight::Regular),
            p.text3,
        );
    }
    let clicked = resp.on_hover_cursor(CursorIcon::PointingHand).clicked();
    if clicked {
        ui.close();
    }
    clicked
}

// ---- crew header --------------------------------------------------------------------------

/// A crew member as the header shows it.
pub struct Member {
    pub key: String,
    pub name: String,
    pub title: String,
    pub state: String,
}

/// Overlapping discs, one per crew member, laid out right to left from the cursor. Idle ones are
/// muted; a working one is drawn on top in full colour with a pulsing ring. Returns the name of
/// a clicked member (to address them in the composer).
pub fn avatar_stack(ui: &mut Ui, p: &Palette, members: &[Member]) -> Option<String> {
    let size = 22.0;
    let step = 18.5;
    let n = members.len();
    if n == 0 {
        return None;
    }
    let width = size + step * (n as f32 - 1.0);
    let (rect, _) = ui.allocate_exact_size(egui::vec2(width, size + 4.0), egui::Sense::hover());
    let painter = ui.painter().clone();
    let t = ui.input(|i| i.time) as f32;
    let mut clicked = None;
    // Idle members first, working ones last so they sit on top.
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by_key(|&i| members[i].state != "idle");
    let pointer = ui.input(|i| i.pointer.hover_pos());
    let mut hovered: Option<usize> = None;
    // The topmost disc under the pointer wins (later in the draw order is on top).
    for &i in &order {
        let c = egui::pos2(rect.left() + size / 2.0 + step * i as f32, rect.center().y);
        if pointer
            .map(|q| q.distance(c) <= size / 2.0)
            .unwrap_or(false)
        {
            hovered = Some(i);
        }
    }
    for &i in &order {
        let m = &members[i];
        let c = egui::pos2(rect.left() + size / 2.0 + step * i as f32, rect.center().y);
        let active = m.state != "idle";
        let colour = p.folk(&m.key);
        let lift = if hovered == Some(i) { 1.0 } else { 0.0 };
        let c = c - egui::vec2(0.0, lift);
        painter.circle_filled(c, size / 2.0 + 2.0, p.bg1);
        if active {
            let pulse = 0.5 + 0.5 * (t * 4.0).sin();
            painter.circle_stroke(
                c,
                size / 2.0 + 2.5 + pulse * 1.5,
                egui::Stroke::new(1.5, colour.gamma_multiply(0.4 + 0.4 * pulse)),
            );
            ui.ctx()
                .request_repaint_after(std::time::Duration::from_millis(40));
        }
        let fill = if active || hovered == Some(i) {
            colour
        } else {
            colour.gamma_multiply(0.42)
        };
        painter.circle_filled(c, size / 2.0, fill);
        let initial: String = m
            .name
            .chars()
            .next()
            .map(|ch| ch.to_uppercase().to_string())
            .unwrap_or_default();
        painter.text(
            c + egui::vec2(0.0, 0.5),
            egui::Align2::CENTER_CENTER,
            initial,
            font(10.5, Weight::Semibold),
            if active || hovered == Some(i) {
                Color32::WHITE
            } else {
                Color32::WHITE.gamma_multiply(0.7)
            },
        );
    }
    if let Some(i) = hovered {
        let m = &members[i];
        let c = egui::pos2(rect.left() + size / 2.0 + step * i as f32, rect.center().y);
        let hit = egui::Rect::from_center_size(c, egui::vec2(size, size));
        let resp = ui
            .interact(hit, ui.scope_id().with(("avatar", i)), egui::Sense::click())
            .on_hover_cursor(egui::CursorIcon::PointingHand)
            .on_hover_text(format!(
                "{} · {} · {}\nClick to address them",
                m.name, m.title, m.state
            ));
        if resp.clicked() {
            clicked = Some(m.name.clone());
        }
    }
    clicked
}

/// Stage progress: one slim segment per stage, done ones filled, the current one accented (with
/// a moving sheen while work is under way), the rest quiet. A caption names the current stage.
pub fn stage_bar(ui: &mut Ui, p: &Palette, steps: &[&str], current: Option<usize>, working: bool) {
    let n = steps.len().max(1);
    let caption = match current {
        Some(c) => {
            let mut name = steps[c.min(n - 1)].to_string();
            if let Some(first) = name.get_mut(0..1) {
                first.make_ascii_uppercase();
            }
            format!("{name}  ·  {} of {n}", c.min(n - 1) + 1)
        }
        None => "Not started".to_string(),
    };
    let galley = ui
        .painter()
        .layout_no_wrap(caption, font(11.0, Weight::Medium), p.text2);
    let width = ui.available_width();
    let (rect, _) = ui.allocate_exact_size(egui::vec2(width, 14.0), egui::Sense::hover());
    let bar_w = (width - galley.size().x - 12.0).max(60.0);
    let gap = 3.0;
    let seg_w = (bar_w - gap * (n as f32 - 1.0)) / n as f32;
    let y = rect.center().y;
    let painter = ui.painter();
    let t = ui.input(|i| i.time) as f32;
    let pointer = ui.input(|i| i.pointer.hover_pos());
    for (i, name) in steps.iter().enumerate() {
        let x0 = rect.left() + i as f32 * (seg_w + gap);
        let seg = egui::Rect::from_min_size(egui::pos2(x0, y - 1.5), egui::vec2(seg_w, 3.0));
        let fill = match current {
            Some(c) if i < c => p.accent.gamma_multiply(0.75),
            Some(c) if i == c => p.accent,
            _ => p.bg3,
        };
        painter.rect_filled(seg, 1.5, fill);
        if working && current == Some(i) {
            // A soft highlight sweeping along the current segment.
            let phase = (t * 0.9).fract();
            let sx = seg.left() + seg.width() * phase;
            let sheen = egui::Rect::from_center_size(
                egui::pos2(sx, y),
                egui::vec2((seg.width() * 0.35).min(18.0), 3.0),
            )
            .intersect(seg);
            painter.rect_filled(sheen, 1.5, Color32::WHITE.gamma_multiply(0.55));
            ui.ctx()
                .request_repaint_after(std::time::Duration::from_millis(30));
        }
        let hit = seg.expand2(egui::vec2(gap / 2.0, 6.0));
        if pointer.map(|q| hit.contains(q)).unwrap_or(false) {
            let mut label = name.to_string();
            if let Some(first) = label.get_mut(0..1) {
                first.make_ascii_uppercase();
            }
            ui.interact(hit, ui.scope_id().with(("stage", i)), egui::Sense::hover())
                .on_hover_text(label);
        }
    }
    painter.galley(
        egui::pos2(rect.right() - galley.size().x, y - galley.size().y / 2.0),
        galley,
        p.text2,
    );
}
