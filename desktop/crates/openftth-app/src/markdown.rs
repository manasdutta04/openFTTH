//! A small Markdown renderer for chat replies: headings, paragraphs, bullet and numbered lists,
//! block quotes, fenced code, inline `code`, **bold** and *italic*. Enough for what the folks
//! write, without pulling a second UI toolkit version into the app.

use egui::text::LayoutJob;
use egui::{Color32, FontId, RichText, TextFormat, Ui};

use crate::theme::Palette;

pub fn render(ui: &mut Ui, p: &Palette, text: &str) {
    let mut lines = text.lines().peekable();
    let mut paragraph: Vec<String> = Vec::new();
    let flush = |ui: &mut Ui, paragraph: &mut Vec<String>| {
        if !paragraph.is_empty() {
            let joined = paragraph.join(" ");
            inline(ui, p, &joined, p.text, 14.5, false);
            paragraph.clear();
        }
    };
    while let Some(line) = lines.next() {
        let trimmed = line.trim_end();
        if trimmed.trim().is_empty() {
            flush(ui, &mut paragraph);
            ui.add_space(2.0);
            continue;
        }
        if let Some(rest) = trimmed.strip_prefix("```") {
            flush(ui, &mut paragraph);
            let _lang = rest.trim();
            let mut code = String::new();
            for l in lines.by_ref() {
                if l.trim_start().starts_with("```") {
                    break;
                }
                code.push_str(l);
                code.push('\n');
            }
            code_block(ui, p, code.trim_end_matches('\n'));
            continue;
        }
        if let Some(h) = heading(trimmed) {
            flush(ui, &mut paragraph);
            let (level, body) = h;
            let size = match level {
                1 => 19.0,
                2 => 17.0,
                _ => 15.5,
            };
            ui.add_space(4.0);
            inline(ui, p, body, p.text, size, true);
            continue;
        }
        if let Some(body) = trimmed.trim_start().strip_prefix("> ") {
            flush(ui, &mut paragraph);
            ui.horizontal(|ui| {
                let (r, _) = ui.allocate_exact_size(egui::vec2(3.0, 18.0), egui::Sense::hover());
                ui.painter().rect_filled(r, 1.0, p.accent);
                inline(ui, p, body, p.text_muted, 14.5, false);
            });
            continue;
        }
        if let Some(item) = bullet(trimmed) {
            flush(ui, &mut paragraph);
            ui.horizontal_wrapped(|ui| {
                ui.add_space(indent_of(trimmed));
                ui.label(RichText::new("•").color(p.accent));
                inline(ui, p, item, p.text, 14.5, false);
            });
            continue;
        }
        if let Some((num, item)) = numbered(trimmed) {
            flush(ui, &mut paragraph);
            ui.horizontal_wrapped(|ui| {
                ui.add_space(indent_of(trimmed));
                ui.label(RichText::new(format!("{num}.")).color(p.accent));
                inline(ui, p, item, p.text, 14.5, false);
            });
            continue;
        }
        paragraph.push(trimmed.trim().to_string());
    }
    flush(ui, &mut paragraph);
}

fn heading(line: &str) -> Option<(usize, &str)> {
    let hashes = line.chars().take_while(|c| *c == '#').count();
    if (1..=6).contains(&hashes) && line[hashes..].starts_with(' ') {
        Some((hashes, line[hashes..].trim()))
    } else {
        None
    }
}

fn bullet(line: &str) -> Option<&str> {
    let t = line.trim_start();
    for prefix in ["- ", "* ", "• "] {
        if let Some(rest) = t.strip_prefix(prefix) {
            return Some(rest);
        }
    }
    None
}

fn numbered(line: &str) -> Option<(u32, &str)> {
    let t = line.trim_start();
    let digits: String = t.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() || digits.len() > 3 {
        return None;
    }
    let rest = &t[digits.len()..];
    let rest = rest
        .strip_prefix(". ")
        .or_else(|| rest.strip_prefix(") "))?;
    Some((digits.parse().ok()?, rest))
}

fn indent_of(line: &str) -> f32 {
    (line.len() - line.trim_start().len()) as f32 * 4.0
}

fn code_block(ui: &mut Ui, p: &Palette, code: &str) {
    egui::Frame::new()
        .fill(p.surface_alt)
        .stroke(egui::Stroke::new(1.0, p.border))
        .corner_radius(egui::CornerRadius::same(6))
        .inner_margin(egui::Margin::same(8))
        .show(ui, |ui| {
            ui.add(egui::Label::new(RichText::new(code).monospace()).wrap());
        });
}

/// Render one line with inline **bold**, *italic* and `code`.
fn inline(ui: &mut Ui, p: &Palette, text: &str, color: Color32, size: f32, strong: bool) {
    let mut job = LayoutJob::default();
    job.wrap.max_width = ui.available_width();
    let base = TextFormat {
        font_id: FontId::proportional(size),
        color,
        ..Default::default()
    };
    let mut bold = strong;
    let mut italic = false;
    let mut buf = String::new();
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    let push = |job: &mut LayoutJob, buf: &mut String, bold: bool, italic: bool, code: bool| {
        if buf.is_empty() {
            return;
        }
        let mut fmt = base.clone();
        if code {
            fmt.font_id = FontId::monospace(size - 1.0);
            fmt.background = p.surface_alt;
            fmt.color = p.accent;
        } else {
            if bold {
                fmt.font_id = FontId::proportional(size);
                fmt.color = color;
                fmt.underline = egui::Stroke::NONE;
                // egui has no bold weight in the default font: emphasise with the accent colour.
                fmt.color = if color == p.text { p.text } else { color };
                fmt.extra_letter_spacing = 0.2;
            }
            if italic {
                fmt.italics = true;
            }
        }
        if bold && !code {
            // Simulate weight with a subtle stroke-like offset: egui draws text once, so we
            // use a slightly larger size and letter spacing instead.
            fmt.font_id = FontId::proportional(size + 0.5);
        }
        job.append(buf, 0.0, fmt);
        buf.clear();
    };
    while i < chars.len() {
        let c = chars[i];
        if c == '`' {
            push(&mut job, &mut buf, bold, italic, false);
            let mut j = i + 1;
            while j < chars.len() && chars[j] != '`' {
                buf.push(chars[j]);
                j += 1;
            }
            push(&mut job, &mut buf, false, false, true);
            i = j + 1;
            continue;
        }
        if c == '*' && i + 1 < chars.len() && chars[i + 1] == '*' {
            push(&mut job, &mut buf, bold, italic, false);
            bold = !bold;
            i += 2;
            continue;
        }
        if c == '*' || (c == '_' && (i == 0 || !chars[i - 1].is_alphanumeric())) {
            push(&mut job, &mut buf, bold, italic, false);
            italic = !italic;
            i += 1;
            continue;
        }
        buf.push(c);
        i += 1;
    }
    push(&mut job, &mut buf, bold, italic, false);
    ui.add(egui::Label::new(job).wrap());
}
