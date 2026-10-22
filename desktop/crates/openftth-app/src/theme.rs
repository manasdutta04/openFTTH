//! Visual system: embedded UI fonts with real weights, a neutral professional palette in a
//! dark and a light variant, and a dense spacing scale. Everything the app draws uses these.

use std::sync::Arc;

use egui::{
    Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Margin, Stroke,
    TextStyle, Visuals,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Dark,
    Light,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Weight {
    Regular,
    Medium,
    Semibold,
}

pub fn font(size: f32, weight: Weight) -> FontId {
    let family = match weight {
        Weight::Regular => FontFamily::Proportional,
        Weight::Medium => FontFamily::Name("ui-medium".into()),
        Weight::Semibold => FontFamily::Name("ui-semibold".into()),
    };
    FontId::new(size, family)
}

pub fn mono(size: f32) -> FontId {
    FontId::new(size, FontFamily::Monospace)
}

/// Register the bundled fonts: Inter (regular, medium, semibold) and JetBrains Mono.
pub fn install_fonts(ctx: &egui::Context) {
    let mut fonts = FontDefinitions::default();
    fonts.font_data.insert(
        "inter".into(),
        Arc::new(FontData::from_static(include_bytes!(
            "../assets/fonts/Inter-Regular.ttf"
        ))),
    );
    fonts.font_data.insert(
        "inter-medium".into(),
        Arc::new(FontData::from_static(include_bytes!(
            "../assets/fonts/Inter-Medium.ttf"
        ))),
    );
    fonts.font_data.insert(
        "inter-semibold".into(),
        Arc::new(FontData::from_static(include_bytes!(
            "../assets/fonts/Inter-SemiBold.ttf"
        ))),
    );
    fonts.font_data.insert(
        "jbmono".into(),
        Arc::new(FontData::from_static(include_bytes!(
            "../assets/fonts/JetBrainsMono-Regular.ttf"
        ))),
    );
    let fallback: Vec<String> = fonts
        .families
        .get(&FontFamily::Proportional)
        .cloned()
        .unwrap_or_default();
    let with = |first: &str| {
        let mut v = vec![first.to_string()];
        v.extend(fallback.iter().cloned());
        v
    };
    fonts
        .families
        .insert(FontFamily::Proportional, with("inter"));
    fonts
        .families
        .insert(FontFamily::Name("ui-medium".into()), with("inter-medium"));
    fonts.families.insert(
        FontFamily::Name("ui-semibold".into()),
        with("inter-semibold"),
    );
    let mut mono_family = vec!["jbmono".to_string()];
    mono_family.extend(
        fonts
            .families
            .get(&FontFamily::Monospace)
            .cloned()
            .unwrap_or_default(),
    );
    fonts.families.insert(FontFamily::Monospace, mono_family);
    ctx.set_fonts(fonts);
}

#[derive(Clone, Copy, Debug)]
pub struct Palette {
    pub mode: Mode,
    /// Window background (deepest).
    pub bg0: Color32,
    /// Panels.
    pub bg1: Color32,
    /// Raised surfaces: inputs, floating panels, rows.
    pub bg2: Color32,
    /// Hover / pressed surfaces.
    pub bg3: Color32,
    pub line: Color32,
    pub line_strong: Color32,
    pub text: Color32,
    pub text2: Color32,
    pub text3: Color32,
    pub accent: Color32,
    pub accent_soft: Color32,
    pub green: Color32,
    pub amber: Color32,
    pub red: Color32,
    pub map_bg: Color32,
    pub map_street: Color32,
    pub map_street_major: Color32,
    pub map_building: Color32,
    pub map_building_edge: Color32,
}

impl Palette {
    pub fn dark() -> Self {
        Self {
            mode: Mode::Dark,
            bg0: Color32::from_rgb(0x14, 0x14, 0x17),
            bg1: Color32::from_rgb(0x1c, 0x1c, 0x20),
            bg2: Color32::from_rgb(0x24, 0x24, 0x29),
            bg3: Color32::from_rgb(0x2d, 0x2d, 0x33),
            line: Color32::from_rgb(0x30, 0x30, 0x37),
            line_strong: Color32::from_rgb(0x43, 0x43, 0x4b),
            text: Color32::from_rgb(0xe8, 0xe8, 0xec),
            text2: Color32::from_rgb(0xa6, 0xa6, 0xb0),
            text3: Color32::from_rgb(0x6f, 0x6f, 0x7a),
            accent: Color32::from_rgb(0x3f, 0x8c, 0xff),
            accent_soft: Color32::from_rgb(0x1f, 0x32, 0x52),
            green: Color32::from_rgb(0x3f, 0xb9, 0x50),
            amber: Color32::from_rgb(0xd9, 0x9e, 0x2b),
            red: Color32::from_rgb(0xf8, 0x51, 0x49),
            map_bg: Color32::from_rgb(0x19, 0x19, 0x1d),
            map_street: Color32::from_rgb(0x36, 0x36, 0x3e),
            map_street_major: Color32::from_rgb(0x4a, 0x4a, 0x54),
            map_building: Color32::from_rgb(0x25, 0x25, 0x2b),
            map_building_edge: Color32::from_rgb(0x2f, 0x2f, 0x36),
        }
    }

    pub fn light() -> Self {
        Self {
            mode: Mode::Light,
            bg0: Color32::from_rgb(0xef, 0xef, 0xf2),
            bg1: Color32::from_rgb(0xf9, 0xf9, 0xfb),
            bg2: Color32::WHITE,
            bg3: Color32::from_rgb(0xe9, 0xe9, 0xee),
            line: Color32::from_rgb(0xdf, 0xdf, 0xe5),
            line_strong: Color32::from_rgb(0xc9, 0xc9, 0xd1),
            text: Color32::from_rgb(0x1c, 0x1c, 0x21),
            text2: Color32::from_rgb(0x5c, 0x5c, 0x68),
            text3: Color32::from_rgb(0x8d, 0x8d, 0x99),
            accent: Color32::from_rgb(0x2f, 0x6f, 0xe6),
            accent_soft: Color32::from_rgb(0xdc, 0xe8, 0xff),
            green: Color32::from_rgb(0x1f, 0x8a, 0x3a),
            amber: Color32::from_rgb(0xb0, 0x73, 0x00),
            red: Color32::from_rgb(0xd0, 0x2f, 0x2f),
            map_bg: Color32::from_rgb(0xfa, 0xfa, 0xf8),
            map_street: Color32::from_rgb(0xcf, 0xd1, 0xd6),
            map_street_major: Color32::from_rgb(0xb3, 0xb7, 0xbe),
            map_building: Color32::from_rgb(0xe9, 0xe6, 0xdf),
            map_building_edge: Color32::from_rgb(0xd9, 0xd5, 0xcb),
        }
    }

    pub fn for_mode(mode: Mode) -> Self {
        match mode {
            Mode::Dark => Self::dark(),
            Mode::Light => Self::light(),
        }
    }

    /// Colour of a network element kind.
    pub fn kind(&self, kind: &str) -> Color32 {
        let dark = self.mode == Mode::Dark;
        match kind {
            "feeder" => Color32::from_rgb(0xc0, 0x5c, 0xff),
            "distribution" => Color32::from_rgb(0x3f, 0x8c, 0xff),
            "drop" => {
                if dark {
                    Color32::from_rgb(0x5a, 0xb8, 0x6a)
                } else {
                    Color32::from_rgb(0x3f, 0x9a, 0x4f)
                }
            }
            "home" => {
                if dark {
                    Color32::from_rgb(0x8c, 0xd2, 0x96)
                } else {
                    Color32::from_rgb(0x2e, 0x7d, 0x32)
                }
            }
            "fat" => Color32::from_rgb(0xff, 0x9f, 0x1c),
            "fdh" => Color32::from_rgb(0xff, 0x3d, 0x7f),
            "pop" => {
                if dark {
                    Color32::WHITE
                } else {
                    Color32::BLACK
                }
            }
            "existing_duct" | "existing_pole" | "existing_manhole" => {
                if dark {
                    Color32::from_rgb(0x8f, 0xa3, 0xb8)
                } else {
                    Color32::from_rgb(0x5b, 0x6b, 0x7c)
                }
            }
            "chamber" => {
                if dark {
                    Color32::from_rgb(0xb0, 0x8a, 0x6a)
                } else {
                    Color32::from_rgb(0x6d, 0x4c, 0x41)
                }
            }
            _ => self.text3,
        }
    }

    pub fn folk(&self, key: &str) -> Color32 {
        match key {
            "surveyor" => self.green,
            "planner" => self.accent,
            "checker" => self.amber,
            "drafter" => Color32::from_rgb(0xc0, 0x5c, 0xff),
            _ => self.text2,
        }
    }
}

pub const R: u8 = 4;
pub const R_FLOAT: u8 = 6;

pub fn apply(ctx: &egui::Context, p: &Palette) {
    let mut v = match p.mode {
        Mode::Dark => Visuals::dark(),
        Mode::Light => Visuals::light(),
    };
    v.panel_fill = p.bg1;
    v.window_fill = p.bg2;
    v.extreme_bg_color = p.bg2;
    v.faint_bg_color = p.bg2;
    v.code_bg_color = p.bg3;
    v.override_text_color = Some(p.text);
    v.hyperlink_color = p.accent;
    v.selection.bg_fill = p.accent_soft;
    v.selection.stroke = Stroke::new(1.0, p.accent);
    v.widgets.noninteractive.bg_fill = p.bg1;
    v.widgets.noninteractive.weak_bg_fill = p.bg1;
    v.widgets.noninteractive.bg_stroke = Stroke::new(1.0, p.line);
    v.widgets.noninteractive.fg_stroke = Stroke::new(1.0, p.text);
    v.widgets.inactive.bg_fill = p.bg2;
    v.widgets.inactive.weak_bg_fill = p.bg2;
    v.widgets.inactive.bg_stroke = Stroke::new(1.0, p.line);
    v.widgets.inactive.fg_stroke = Stroke::new(1.0, p.text);
    v.widgets.hovered.bg_fill = p.bg3;
    v.widgets.hovered.weak_bg_fill = p.bg3;
    v.widgets.hovered.bg_stroke = Stroke::new(1.0, p.line_strong);
    v.widgets.hovered.fg_stroke = Stroke::new(1.0, p.text);
    v.widgets.active.bg_fill = p.bg3;
    v.widgets.active.weak_bg_fill = p.bg3;
    v.widgets.active.bg_stroke = Stroke::new(1.0, p.accent);
    v.widgets.active.fg_stroke = Stroke::new(1.0, p.text);
    v.widgets.open.bg_fill = p.bg3;
    v.widgets.open.weak_bg_fill = p.bg3;
    v.widgets.open.bg_stroke = Stroke::new(1.0, p.line_strong);
    v.widgets.open.fg_stroke = Stroke::new(1.0, p.text);
    for w in [
        &mut v.widgets.noninteractive,
        &mut v.widgets.inactive,
        &mut v.widgets.hovered,
        &mut v.widgets.active,
        &mut v.widgets.open,
    ] {
        w.corner_radius = CornerRadius::same(R);
        w.expansion = 0.0;
    }
    v.window_corner_radius = CornerRadius::same(R_FLOAT);
    v.menu_corner_radius = CornerRadius::same(R_FLOAT);
    v.window_stroke = Stroke::new(1.0, p.line_strong);
    v.window_shadow.color = Color32::from_black_alpha(if p.mode == Mode::Dark { 120 } else { 40 });
    v.popup_shadow.color = Color32::from_black_alpha(if p.mode == Mode::Dark { 120 } else { 40 });
    v.striped = false;
    v.indent_has_left_vline = false;
    v.slider_trailing_fill = true;
    v.handle_shape = egui::style::HandleShape::Rect { aspect_ratio: 0.5 };
    ctx.set_visuals(v);

    ctx.all_styles_mut(|style| {
        style.spacing.item_spacing = egui::vec2(6.0, 4.0);
        style.spacing.button_padding = egui::vec2(10.0, 4.0);
        style.spacing.window_margin = Margin::same(10);
        style.spacing.menu_margin = Margin::same(6);
        style.spacing.indent = 14.0;
        style.spacing.interact_size = egui::vec2(36.0, 22.0);
        style.spacing.combo_width = 160.0;
        style.spacing.scroll.bar_width = 6.0;
        style.spacing.scroll.floating = true;
        style.spacing.scroll.floating_allocated_width = 0.0;
        style.spacing.tooltip_width = 320.0;
        style.text_styles = [
            (TextStyle::Heading, font(15.0, Weight::Semibold)),
            (TextStyle::Body, font(13.0, Weight::Regular)),
            (TextStyle::Monospace, mono(12.0)),
            (TextStyle::Button, font(12.5, Weight::Medium)),
            (TextStyle::Small, font(11.5, Weight::Regular)),
        ]
        .into();
        style.interaction.tooltip_delay = 0.35;
        style.interaction.selectable_labels = false;
        style.url_in_tooltip = false;
    });
}
