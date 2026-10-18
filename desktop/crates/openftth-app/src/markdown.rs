//! A small Markdown renderer for chat replies: headings, paragraphs, bullet and numbered lists,
//! block quotes, fenced code, inline `code`, **bold** and *italic*.

use egui::text::LayoutJob;
use egui::{Color32, TextFormat, Ui};

use crate::theme::{font, mono, Palette, Weight};

pub fn render(ui: &mut Ui, p: &Palette, text: &str) {
    let mut lines = text.lines().peekable();
    let mut paragraph: Vec<String> = Vec::new();
    let flush = |ui: &mut Ui, paragraph: &mut Vec<String>| {
        if !paragraph.is_empty() {
            let joined = paragraph.join(" ");
            inline(ui, p, &joined, p.text, 13.0, Weight::Regular);
            paragraph.clear();
        }
    };
    while let Some(line) = lines.next() {
        let trimmed = line.trim_end();
        if trimmed.trim().is_empty() {
            flush(ui, &mut paragraph);
            ui.add_space(3.0);
            continue;
        }
        if trimmed.starts_with("```") {
            flush(ui, &mut paragraph);
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
        if let Some((level, body)) = heading(trimmed) {
            flush(ui, &mut paragraph);
            let size = match level {
                1 => 15.0,
                2 => 14.0,
                _ => 13.0,
            };
            ui.add_space(4.0);
            inline(ui, p, body, p.text, size, Weight::Semibold);
            continue;
        }
        if let Some(body) = trimmed.trim_start().strip_prefix("> ") {
            flush(ui, &mut paragraph);
            ui.horizontal(|ui| {
                let (r, _) = ui.allocate_exact_size(egui::vec2(2.0, 16.0), egui::Sense::hover());
                ui.painter().rect_filled(r, 1.0, p.line_strong);
                inline(ui, p, body, p.text2, 13.0, Weight::Regular);
            });
            continue;
        }
        if let Some(item) = bullet(trimmed) {
            flush(ui, &mut paragraph);
            ui.horizontal_wrapped(|ui| {
                ui.add_space(indent_of(trimmed));
                ui.label(
                    egui::RichText::new("•")
                        .font(font(13.0, Weight::Regular))
                        .color(p.text3),
                );
                inline(ui, p, item, p.text, 13.0, Weight::Regular);
            });
            continue;
        }
        if let Some((num, item)) = numbered(trimmed) {
            flush(ui, &mut paragraph);
            ui.horizontal_wrapped(|ui| {
                ui.add_space(indent_of(trimmed));
                ui.label(
                    egui::RichText::new(format!("{num}."))
                        .font(font(13.0, Weight::Regular))
                        .color(p.text3),
                );
                inline(ui, p, item, p.text, 13.0, Weight::Regular);
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
    ["- ", "* ", "• "]
        .iter()
        .find_map(|prefix| t.strip_prefix(prefix))
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
        .fill(p.bg3)
        .corner_radius(egui::CornerRadius::same(4))
        .inner_margin(egui::Margin::same(8))
        .show(ui, |ui| {
            ui.add(
                egui::Label::new(egui::RichText::new(code).font(mono(11.5)).color(p.text)).wrap(),
            );
        });
}

/// One line with inline **bold**, *italic* and `code`.
fn inline(ui: &mut Ui, p: &Palette, text: &str, color: Color32, size: f32, weight: Weight) {
    let mut job = LayoutJob::default();
    job.wrap.max_width = ui.available_width();
    let mut bold = weight == Weight::Semibold;
    let mut italic = false;
    let mut buf = String::new();
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    let push = |job: &mut LayoutJob, buf: &mut String, bold: bool, italic: bool, code: bool| {
        if buf.is_empty() {
            return;
        }
        let mut fmt = TextFormat {
            font_id: font(size, if bold { Weight::Semibold } else { weight }),
            color,
            ..Default::default()
        };
        if code {
            fmt.font_id = mono(size - 1.0);
            fmt.background = p.bg3;
            fmt.color = p.text;
        }
        fmt.italics = italic && !code;
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
