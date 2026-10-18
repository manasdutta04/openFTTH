//! Visual design system: one palette, one type scale, consistent spacing and rounding, in a
//! light and a dark variant. Every colour the app draws comes from here.

use egui::{Color32, CornerRadius, FontFamily, FontId, Margin, Stroke, TextStyle, Visuals};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Light,
    Dark,
}

#[derive(Clone, Copy, Debug)]
pub struct Palette {
    pub mode: Mode,
    pub accent: Color32,
    pub accent_soft: Color32,
    pub success: Color32,
    pub warning: Color32,
    pub danger: Color32,
    pub info: Color32,
    pub canvas: Color32,
    pub surface: Color32,
    pub surface_alt: Color32,
    pub border: Color32,
    pub text: Color32,
    pub text_muted: Color32,
    pub bubble_user: Color32,
    pub bubble_folk: Color32,
    pub approval_bg: Color32,
    pub approval_border: Color32,
    pub map_bg: Color32,
    pub map_street: Color32,
    pub map_street_major: Color32,
    pub map_building: Color32,
    pub map_building_edge: Color32,
}

impl Palette {
    pub fn light() -> Self {
        Self {
            mode: Mode::Light,
            accent: Color32::from_rgb(0x0f, 0x76, 0x6e),
            accent_soft: Color32::from_rgb(0xd8, 0xef, 0xec),
            success: Color32::from_rgb(0x2e, 0x7d, 0x32),
            warning: Color32::from_rgb(0xc7, 0x7a, 0x00),
            danger: Color32::from_rgb(0xc6, 0x28, 0x28),
            info: Color32::from_rgb(0x1e, 0x6f, 0xd9),
            canvas: Color32::from_rgb(0xf4, 0xf5, 0xf7),
            surface: Color32::WHITE,
            surface_alt: Color32::from_rgb(0xf0, 0xf2, 0xf5),
            border: Color32::from_rgb(0xdd, 0xe1, 0xe6),
            text: Color32::from_rgb(0x1b, 0x1f, 0x24),
            text_muted: Color32::from_rgb(0x6b, 0x72, 0x7b),
            bubble_user: Color32::from_rgb(0xe1, 0xf0, 0xee),
            bubble_folk: Color32::from_rgb(0xf6, 0xf7, 0xf9),
            approval_bg: Color32::from_rgb(0xff, 0xf6, 0xe0),
            approval_border: Color32::from_rgb(0xf2, 0xb8, 0x3c),
            map_bg: Color32::from_rgb(0xfb, 0xfb, 0xf8),
            map_street: Color32::from_rgb(0xc9, 0xcc, 0xd1),
            map_street_major: Color32::from_rgb(0xae, 0xb3, 0xba),
            map_building: Color32::from_rgb(0xe8, 0xe4, 0xdb),
            map_building_edge: Color32::from_rgb(0xd6, 0xd1, 0xc6),
        }
    }

    pub fn dark() -> Self {
        Self {
            mode: Mode::Dark,
            accent: Color32::from_rgb(0x4f, 0xc3, 0xb8),
            accent_soft: Color32::from_rgb(0x15, 0x3b, 0x38),
            success: Color32::from_rgb(0x66, 0xbb, 0x6a),
            warning: Color32::from_rgb(0xf5, 0xb0, 0x41),
            danger: Color32::from_rgb(0xef, 0x53, 0x50),
            info: Color32::from_rgb(0x64, 0xa8, 0xff),
            canvas: Color32::from_rgb(0x12, 0x14, 0x17),
            surface: Color32::from_rgb(0x1b, 0x1e, 0x23),
            surface_alt: Color32::from_rgb(0x22, 0x26, 0x2c),
            border: Color32::from_rgb(0x30, 0x35, 0x3d),
            text: Color32::from_rgb(0xe8, 0xea, 0xee),
            text_muted: Color32::from_rgb(0x98, 0x9f, 0xa9),
            bubble_user: Color32::from_rgb(0x18, 0x36, 0x33),
            bubble_folk: Color32::from_rgb(0x22, 0x26, 0x2c),
            approval_bg: Color32::from_rgb(0x3a, 0x2d, 0x10),
            approval_border: Color32::from_rgb(0xd9, 0xa4, 0x3a),
            map_bg: Color32::from_rgb(0x16, 0x18, 0x1c),
            map_street: Color32::from_rgb(0x3c, 0x42, 0x4b),
            map_street_major: Color32::from_rgb(0x55, 0x5d, 0x68),
            map_building: Color32::from_rgb(0x26, 0x2a, 0x30),
            map_building_edge: Color32::from_rgb(0x32, 0x37, 0x3e),
        }
    }

    pub fn for_mode(mode: Mode) -> Self {
        match mode {
            Mode::Light => Self::light(),
            Mode::Dark => Self::dark(),
        }
    }

    /// Colour of a network element kind on the map and in legends.
    pub fn kind(&self, kind: &str) -> Color32 {
        match kind {
            "feeder" => Color32::from_rgb(0x8e, 0x24, 0xaa),
            "distribution" => Color32::from_rgb(0x1e, 0x88, 0xe5),
            "drop" => Color32::from_rgb(0x43, 0xa0, 0x47),
            "home" => Color32::from_rgb(0x2e, 0x7d, 0x32),
            "fat" => Color32::from_rgb(0xfb, 0x8c, 0x00),
            "fdh" => Color32::from_rgb(0xd8, 0x1b, 0x60),
            "pop" => {
                if self.mode == Mode::Dark {
                    Color32::WHITE
                } else {
                    Color32::BLACK
                }
            }
            "chamber" => Color32::from_rgb(0x6d, 0x4c, 0x41),
            _ => self.text_muted,
        }
    }

    /// Colour of a crew member by key.
    pub fn folk(&self, key: &str) -> Color32 {
        match key {
            "surveyor" => Color32::from_rgb(0x43, 0xa0, 0x47),
            "planner" => Color32::from_rgb(0x1e, 0x88, 0xe5),
            "checker" => Color32::from_rgb(0xfb, 0x8c, 0x00),
            "drafter" => Color32::from_rgb(0x8e, 0x24, 0xaa),
            _ => self.accent,
        }
    }
}

pub const RADIUS: u8 = 8;
pub const RADIUS_SM: u8 = 5;

/// Apply the design system to an egui context.
pub fn apply(ctx: &egui::Context, palette: &Palette) {
    let mut visuals = match palette.mode {
        Mode::Light => Visuals::light(),
        Mode::Dark => Visuals::dark(),
    };
    visuals.panel_fill = palette.canvas;
    visuals.window_fill = palette.surface;
    visuals.extreme_bg_color = palette.surface;
    visuals.faint_bg_color = palette.surface_alt;
    visuals.override_text_color = Some(palette.text);
    visuals.hyperlink_color = palette.accent;
    visuals.selection.bg_fill = palette.accent_soft;
    visuals.selection.stroke = Stroke::new(1.0, palette.accent);
    visuals.widgets.noninteractive.bg_fill = palette.surface;
    visuals.widgets.noninteractive.bg_stroke = Stroke::new(1.0, palette.border);
    visuals.widgets.noninteractive.fg_stroke = Stroke::new(1.0, palette.text);
    visuals.widgets.inactive.bg_fill = palette.surface_alt;
    visuals.widgets.inactive.weak_bg_fill = palette.surface_alt;
    visuals.widgets.inactive.bg_stroke = Stroke::new(1.0, palette.border);
    visuals.widgets.inactive.fg_stroke = Stroke::new(1.0, palette.text);
    visuals.widgets.hovered.bg_fill = palette.accent_soft;
    visuals.widgets.hovered.weak_bg_fill = palette.accent_soft;
    visuals.widgets.hovered.bg_stroke = Stroke::new(1.0, palette.accent);
    visuals.widgets.hovered.fg_stroke = Stroke::new(1.5, palette.text);
    visuals.widgets.active.bg_fill = palette.accent;
    visuals.widgets.active.weak_bg_fill = palette.accent;
    visuals.widgets.active.fg_stroke = Stroke::new(1.5, palette.text);
    visuals.widgets.open.bg_fill = palette.surface_alt;
    visuals.widgets.open.bg_stroke = Stroke::new(1.0, palette.accent);
    for w in [
        &mut visuals.widgets.noninteractive,
        &mut visuals.widgets.inactive,
        &mut visuals.widgets.hovered,
        &mut visuals.widgets.active,
        &mut visuals.widgets.open,
    ] {
        w.corner_radius = CornerRadius::same(RADIUS_SM);
        w.expansion = 0.0;
    }
    visuals.window_corner_radius = CornerRadius::same(RADIUS);
    visuals.menu_corner_radius = CornerRadius::same(RADIUS);
    visuals.window_stroke = Stroke::new(1.0, palette.border);
    visuals.window_shadow.color =
        Color32::from_black_alpha(if palette.mode == Mode::Dark { 90 } else { 30 });
    visuals.popup_shadow.color =
        Color32::from_black_alpha(if palette.mode == Mode::Dark { 90 } else { 30 });
    visuals.striped = true;
    visuals.slider_trailing_fill = true;
    ctx.set_visuals(visuals);

    ctx.all_styles_mut(|style| {
        style.spacing.item_spacing = egui::vec2(8.0, 6.0);
        style.spacing.button_padding = egui::vec2(12.0, 6.0);
        style.spacing.window_margin = Margin::same(12);
        style.spacing.menu_margin = Margin::same(8);
        style.spacing.indent = 18.0;
        style.spacing.interact_size = egui::vec2(40.0, 24.0);
        style.spacing.scroll.bar_width = 8.0;
        style.spacing.scroll.floating = true;
        style.text_styles = [
            (
                TextStyle::Heading,
                FontId::new(22.0, FontFamily::Proportional),
            ),
            (TextStyle::Body, FontId::new(14.5, FontFamily::Proportional)),
            (
                TextStyle::Monospace,
                FontId::new(13.0, FontFamily::Monospace),
            ),
            (
                TextStyle::Button,
                FontId::new(14.0, FontFamily::Proportional),
            ),
            (
                TextStyle::Small,
                FontId::new(12.0, FontFamily::Proportional),
            ),
        ]
        .into();
        style.url_in_tooltip = true;
        style.interaction.tooltip_delay = 0.25;
    });
}
