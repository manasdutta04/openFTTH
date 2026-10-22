//! Workspace tabs besides the twin: BOQ, splices, versions and backstage.

use std::collections::BTreeMap;

use egui::{pos2, vec2, Rect, Sense, Ui};
use egui_extras::{Column, TableBuilder};
use serde_json::Value;

use crate::icons::Icon;
use crate::state::{s, AppState};
use crate::theme::Weight;
use crate::widgets::{
    button, button_ex, caps, dot, fmt_int, fmt_metres, fmt_money, hairline, metric, mono_text,
    relative_time, short_id, tag, text, truncate, Kind,
};

#[derive(Default)]
pub struct TabState {
    pub splice_filter: String,
    pub backstage_filter: String,
}

fn header_cell(ui: &mut Ui, p: &crate::theme::Palette, t: &str) {
    ui.label(caps(t, p.text3));
}

fn search_field(ui: &mut Ui, p: &crate::theme::Palette, value: &mut String, width: f32) {
    let resp = ui.add(
        egui::TextEdit::singleline(value)
            .hint_text("Filter")
            .desired_width(width)
            .margin(egui::Margin {
                left: 22,
                right: 6,
                top: 3,
                bottom: 3,
            }),
    );
    let r = Rect::from_center_size(
        pos2(resp.rect.left() + 12.0, resp.rect.center().y),
        vec2(12.0, 12.0),
    );
    crate::icons::paint(ui.painter(), r, Icon::Search, p.text3);
}

// ---- BOQ --------------------------------------------------------------------------------------

pub fn boq_tab(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    let Some(boq) = &st.boq else {
        empty(
            ui,
            st,
            "No bill of quantities",
            "Ask the Drafter for the BOQ once the design is routed.",
        );
        return;
    };
    let currency = boq["currency"].as_str().unwrap_or("EUR");
    ui.horizontal_wrapped(|ui| {
        metric(
            ui,
            &p,
            &fmt_money(boq["grand_total"].as_f64().unwrap_or(0.0), currency),
            "total",
            Some(p.text),
        );
        metric(
            ui,
            &p,
            &fmt_money(boq["cost_per_unit"].as_f64().unwrap_or(0.0), currency),
            "per unit",
            None,
        );
        metric(
            ui,
            &p,
            &boq["units_served"].as_i64().unwrap_or(0).to_string(),
            "units served",
            None,
        );
        if let Some(d) = &st.design {
            let trench: f64 = d
                .by_kind("feeder")
                .chain(d.by_kind("distribution"))
                .filter_map(|c| c.props.get("length_m").and_then(|v| v.as_f64()))
                .sum();
            metric(ui, &p, &fmt_metres(trench), "feeder + distribution", None);
        }
        if let Some(v) = &st.validation {
            let n: u64 = v["summary"]
                .as_object()
                .map(|m| m.values().filter_map(|x| x.as_u64()).sum())
                .unwrap_or(0);
            metric(
                ui,
                &p,
                &n.to_string(),
                "rule violations",
                Some(if n == 0 { p.green } else { p.red }),
            );
            let warnings: u64 = v["warnings_summary"]
                .as_object()
                .map(|m| m.values().filter_map(|x| x.as_u64()).sum())
                .unwrap_or(0);
            if warnings > 0 {
                metric(
                    ui,
                    &p,
                    &warnings.to_string(),
                    "exceptions to accept",
                    Some(p.amber),
                );
            }
            if let Some(w) = v["power_budget"]["worst_db"].as_f64() {
                let budget = v["power_budget"]["budget_db"].as_f64().unwrap_or(28.0);
                metric(
                    ui,
                    &p,
                    &format!("{w:.1} / {budget:.0} dB"),
                    "worst ODN loss",
                    Some(if w <= budget { p.green } else { p.red }),
                );
            }
        }
    });
    ui.add_space(10.0);
    if let Some(totals) = boq["totals"].as_object() {
        let grand = boq["grand_total"].as_f64().unwrap_or(1.0).max(1.0);
        let (rect, _) = ui.allocate_exact_size(vec2(ui.available_width(), 10.0), Sense::hover());
        let colors = [p.accent, p.kind("fat"), p.green, p.kind("feeder"), p.text3];
        let mut x = rect.left();
        for (i, (_, total)) in totals.iter().enumerate() {
            let w = (total.as_f64().unwrap_or(0.0) / grand) as f32 * rect.width();
            ui.painter().rect_filled(
                Rect::from_min_size(pos2(x, rect.top()), vec2((w - 1.0).max(0.0), rect.height())),
                2.0,
                colors[i % colors.len()],
            );
            x += w;
        }
        ui.horizontal_wrapped(|ui| {
            for (i, (cat, total)) in totals.iter().enumerate() {
                dot(ui, colors[i % colors.len()], 3.5);
                let v = total.as_f64().unwrap_or(0.0);
                ui.label(text(
                    format!(
                        "{cat} {} · {:.0}%",
                        fmt_money(v, currency),
                        v / grand * 100.0
                    ),
                    11.5,
                    Weight::Regular,
                    p.text2,
                ));
                ui.add_space(8.0);
            }
        });
    }
    ui.add_space(8.0);
    let lines = boq["lines"].as_array().cloned().unwrap_or_default();
    let mut by_cat: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for l in lines {
        by_cat.entry(s(&l["category"])).or_default().push(l);
    }
    egui::ScrollArea::vertical()
        .id_salt("boq")
        .auto_shrink([false, false])
        .show(ui, |ui| {
            for (cat, rows) in &by_cat {
                let total: f64 = rows.iter().filter_map(|r| r["total"].as_f64()).sum();
                ui.add_space(6.0);
                ui.horizontal(|ui| {
                    ui.label(text(capitalize(cat), 12.5, Weight::Semibold, p.text));
                    ui.label(mono_text(fmt_money(total, currency), 11.5, p.text2));
                });
                hairline(ui, &p);
                TableBuilder::new(ui)
                    .id_salt(format!("boq-{cat}"))
                    .striped(false)
                    .column(Column::remainder().at_least(180.0))
                    .column(Column::auto().at_least(80.0))
                    .column(Column::auto().at_least(44.0))
                    .column(Column::auto().at_least(90.0))
                    .column(Column::auto().at_least(110.0))
                    .header(20.0, |mut h| {
                        for t in ["Item", "Qty", "Unit", "Unit cost", "Total"] {
                            h.col(|ui| header_cell(ui, &p, t));
                        }
                    })
                    .body(|body| {
                        body.rows(22.0, rows.len(), |mut row| {
                            let r = &rows[row.index()];
                            row.col(|ui| {
                                let l = ui.label(text(
                                    s(&r["item"]).replace('_', " "),
                                    12.5,
                                    Weight::Regular,
                                    p.text,
                                ));
                                if let Some(note) = r["note"].as_str() {
                                    l.on_hover_text(note);
                                }
                            });
                            row.col(|ui| {
                                ui.label(mono_text(
                                    fmt_int(r["qty"].as_f64().unwrap_or(0.0).round() as i64),
                                    12.0,
                                    p.text,
                                ));
                            });
                            row.col(|ui| {
                                ui.label(text(s(&r["unit"]), 12.0, Weight::Regular, p.text3));
                            });
                            row.col(|ui| {
                                ui.label(mono_text(
                                    unit_price(r["unit_cost"].as_f64().unwrap_or(0.0)),
                                    12.0,
                                    p.text2,
                                ));
                            });
                            row.col(|ui| {
                                ui.label(mono_text(
                                    fmt_money(r["total"].as_f64().unwrap_or(0.0), ""),
                                    12.0,
                                    p.text,
                                ));
                            });
                        });
                    });
            }
            if let Some(a) = boq["assumptions"].as_array() {
                if !a.is_empty() {
                    ui.add_space(8.0);
                    ui.label(caps("Assumptions", p.text3));
                    for x in a {
                        ui.label(text(s(x), 11.5, Weight::Regular, p.text3));
                    }
                }
            }
        });
}

// ---- Splices ----------------------------------------------------------------------------------

pub fn splice_tab(ui: &mut Ui, st: &AppState, tabs: &mut TabState) {
    let p = st.palette;
    let Some(d) = &st.design else {
        empty(
            ui,
            st,
            "No design",
            "Open a project and design the area first.",
        );
        return;
    };
    let Some(conns) = d.tables.get("fiber_connection") else {
        empty(
            ui,
            st,
            "Fibres not assigned",
            "Ask the Planner to run size_cables and assign_fibers.",
        );
        return;
    };
    let points: BTreeMap<&str, &serde_json::Map<String, Value>> = d
        .tables
        .get("splice_point")
        .map(|rows| {
            rows.iter()
                .filter_map(|r| r.get("uuid").and_then(|u| u.as_str()).map(|u| (u, r)))
                .collect()
        })
        .unwrap_or_default();
    let name_of = |u: &str| st.element_name(u);
    ui.horizontal(|ui| {
        ui.label(text(
            format!(
                "{} connections in {} splice points",
                conns.len(),
                points.len()
            ),
            12.5,
            Weight::Medium,
            p.text,
        ));
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            search_field(ui, &p, &mut tabs.splice_filter, 220.0);
        });
    });
    ui.add_space(4.0);
    let filter = tabs.splice_filter.trim().to_lowercase();
    struct Row {
        closure: String,
        kind: String,
        from: String,
        to: String,
        loss: f64,
    }
    let side = |c: &serde_json::Map<String, Value>, side: &str| -> String {
        if let Some(cable) = c
            .get(&format!("{side}_cable_uuid"))
            .and_then(|v| v.as_str())
        {
            format!(
                "{} T{}:{}",
                name_of(cable),
                s(&c[&format!("{side}_tube")]),
                s(&c[&format!("{side}_fiber")])
            )
        } else {
            let el = c
                .get(&format!("{side}_element_uuid"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            format!(
                "{} port {} {}",
                name_of(el),
                s(&c[&format!("{side}_port")]),
                s(&c[&format!("{side}_side")])
            )
            .trim()
            .to_string()
        }
    };
    let mut rows: Vec<Row> = conns
        .iter()
        .map(|c| {
            let pid = c
                .get("splice_point_uuid")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let closure = points
                .get(pid)
                .and_then(|pt| pt.get("element_uuid"))
                .and_then(|v| v.as_str())
                .map(name_of)
                .unwrap_or_else(|| short_id(pid).to_string());
            Row {
                closure,
                kind: s(&c["connection_type"]),
                from: side(c, "source"),
                to: side(c, "dest"),
                loss: c.get("loss_db").and_then(|v| v.as_f64()).unwrap_or(0.0),
            }
        })
        .filter(|r| {
            filter.is_empty()
                || r.closure.to_lowercase().contains(&filter)
                || r.from.to_lowercase().contains(&filter)
                || r.to.to_lowercase().contains(&filter)
                || r.kind.contains(&filter)
        })
        .collect();
    rows.sort_by(|a, b| {
        natural_cmp(&a.closure, &b.closure)
            .then_with(|| a.kind.cmp(&b.kind))
            .then_with(|| natural_cmp(&a.from, &b.from))
    });
    // The table scrolls itself, so only the rows in view are laid out: every row is listed.
    let shown = rows.len();
    TableBuilder::new(ui)
        .id_salt("splices")
        .vscroll(true)
        .auto_shrink([false, false])
        .striped(false)
        .column(Column::auto().at_least(120.0))
        .column(Column::auto().at_least(64.0))
        .column(Column::remainder().at_least(200.0))
        .column(Column::remainder().at_least(200.0))
        .column(Column::auto().at_least(56.0))
        .header(20.0, |mut h| {
            for t in ["Closure", "Type", "From", "To", "Loss dB"] {
                h.col(|ui| header_cell(ui, &p, t));
            }
        })
        .body(|body| {
            body.rows(20.0, shown, |mut row| {
                let r = &rows[row.index()];
                row.col(|ui| {
                    ui.label(text(&r.closure, 12.0, Weight::Regular, p.text));
                });
                row.col(|ui| {
                    let fg = match r.kind.as_str() {
                        "splitter" => p.kind("fdh"),
                        "patch" => p.text,
                        _ => p.text2,
                    };
                    tag(ui, &r.kind, fg, p.bg3);
                });
                row.col(|ui| {
                    ui.label(mono_text(&r.from, 11.5, p.text2));
                });
                row.col(|ui| {
                    ui.label(mono_text(&r.to, 11.5, p.text2));
                });
                row.col(|ui| {
                    ui.label(mono_text(format!("{:.1}", r.loss), 11.5, p.text3));
                });
            });
        });
}

// ---- Versions ---------------------------------------------------------------------------------

pub fn versions_tab(ui: &mut Ui, st: &mut AppState) {
    let p = st.palette;
    if st.versions.is_empty() {
        empty(
            ui,
            st,
            "No versions",
            "Every engine tool call becomes a version.",
        );
        return;
    }
    ui.horizontal(|ui| {
        ui.label(text(
            format!("{} versions", st.versions.len()),
            12.5,
            Weight::Medium,
            p.text,
        ));
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if button_ex(ui, &p, Some(Icon::Refresh), "", Kind::Quiet, true)
                .on_hover_text("Refresh")
                .clicked()
            {
                st.refresh_versions();
            }
            if !st.changed.is_empty() && button(ui, &p, "Clear highlight", Kind::Quiet).clicked() {
                st.changed.clear();
            }
        });
    });
    ui.add_space(4.0);
    let approved: Vec<String> = st
        .project
        .as_ref()
        .map(|pr| pr.approved.clone())
        .unwrap_or_default();
    let versions = st.versions.clone();
    let record = st.network_of_record.clone();
    let mut approve: Option<String> = None;
    let mut compare: Option<String> = None;
    egui::ScrollArea::vertical().id_salt("versions").auto_shrink([false, false]).show(ui, |ui| {
        for (i, v) in versions.iter().enumerate() {
            let ver = s(&v["version"]);
            let (stage, rest) = split_subject(&s(&v["subject"]));
            ui.horizontal(|ui| {
                ui.set_min_height(30.0);
                ui.label(mono_text(short_id(&ver), 11.5, p.text3));
                if i == 0 {
                    tag(ui, "head", egui::Color32::WHITE, p.accent);
                }
                if approved.contains(&ver) {
                    tag(ui, "approved", egui::Color32::WHITE, p.green);
                }
                if record.as_deref() == Some(ver.as_str()) {
                    tag(ui, "network of record", egui::Color32::WHITE, p.green.gamma_multiply(0.8))
                        .on_hover_text("The version posted by the last work order");
                }
                if !stage.is_empty() {
                    tag(ui, &stage, p.text2, p.bg3);
                }
                ui.add(egui::Label::new(text(rest, 12.5, Weight::Regular, p.text)).truncate());
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if i == 0 && !approved.contains(&ver) && button(ui, &p, "Approve", Kind::Quiet).on_hover_text("Tag this version as approved by an engineer").clicked() {
                        approve = Some(ver.clone());
                    }
                    if i > 0 && button(ui, &p, "Changes since", Kind::Ghost).on_hover_text("Highlight on the map what changed between this version and head").clicked() {
                        compare = Some(ver.clone());
                    }
                    if let Some(t) = v["time"].as_i64() {
                        ui.label(text(relative_time(t * 1000), 11.0, Weight::Regular, p.text3));
                    }
                });
            });
            hairline(ui, &p);
        }
    });
    if let Some(v) = approve {
        st.approve_version(&v);
    }
    if let Some(v) = compare {
        st.compare_with_head(&v);
    }
}

fn split_subject(subject: &str) -> (String, String) {
    if let Some(rest) = subject.strip_prefix('[') {
        if let Some(end) = rest.find(']') {
            return (rest[..end].to_string(), rest[end + 1..].trim().to_string());
        }
    }
    (String::new(), subject.to_string())
}

// ---- Backstage --------------------------------------------------------------------------------

pub fn backstage_tab(ui: &mut Ui, st: &mut AppState, tabs: &mut TabState) {
    let p = st.palette;
    ui.horizontal(|ui| {
        ui.label(text(
            format!("{} runtime events", st.backstage.len()),
            12.5,
            Weight::Medium,
            p.text,
        ));
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if button_ex(ui, &p, Some(Icon::Refresh), "", Kind::Quiet, true)
                .on_hover_text("Reload")
                .clicked()
            {
                st.refresh_backstage();
            }
            search_field(ui, &p, &mut tabs.backstage_filter, 200.0);
        });
    });
    ui.add_space(4.0);
    if st.backstage.is_empty() {
        ui.label(text(
            "Tool calls, requests and errors appear here as the crew works.",
            11.5,
            Weight::Regular,
            p.text3,
        ));
        return;
    }
    let filter = tabs.backstage_filter.trim().to_lowercase();
    let rows: Vec<&Value> = st
        .backstage
        .iter()
        .filter(|e| filter.is_empty() || e.to_string().to_lowercase().contains(&filter))
        .collect();
    egui::ScrollArea::vertical()
        .id_salt("backstage")
        .auto_shrink([false, false])
        .stick_to_bottom(true)
        .show(ui, |ui| {
            for e in rows {
                let event = s(&e["event"]);
                let c = match event.as_str() {
                    "error" => p.red,
                    "tool.started" => p.accent,
                    "tool.done" => p.green,
                    _ => p.text3,
                };
                ui.horizontal(|ui| {
                    ui.label(mono_text(
                        crate::widgets::clock_time(e["at"].as_i64().unwrap_or(0)),
                        11.0,
                        p.text3,
                    ));
                    dot(ui, c, 3.0);
                    ui.label(text(
                        st.folk_name(e["folk"].as_str().unwrap_or("·")),
                        11.5,
                        Weight::Medium,
                        p.text2,
                    ));
                    ui.label(text(&event, 11.5, Weight::Regular, c));
                    let raw = e["detail"].to_string();
                    let d = &e["detail"];
                    // Say it as a sentence: the tool and what it did, or the error; the raw
                    // record stays one hover away.
                    let tool = d["tool"].as_str().map(|t| t.replace('_', " "));
                    let said = match (event.as_str(), tool) {
                        ("tool.started", Some(t)) => Some((t, String::new())),
                        ("tool.done", Some(t)) => {
                            Some((t, d["summary"].as_str().unwrap_or_default().to_string()))
                        }
                        ("error", t) => Some((
                            t.unwrap_or_default(),
                            d["message"].as_str().unwrap_or_default().to_string(),
                        )),
                        _ => None,
                    };
                    let resp = match said {
                        Some((t, what)) => {
                            ui.label(text(t, 12.0, Weight::Medium, p.text));
                            ui.add(
                                egui::Label::new(text(what, 12.0, Weight::Regular, p.text2))
                                    .truncate(),
                            )
                        }
                        None => ui.add(
                            egui::Label::new(mono_text(truncate(&raw, 140), 11.0, p.text3))
                                .truncate(),
                        ),
                    };
                    resp.on_hover_text(raw);
                });
            }
        });
}

fn empty(ui: &mut Ui, st: &AppState, title: &str, hint: &str) {
    let p = st.palette;
    ui.add_space(40.0);
    ui.vertical_centered(|ui| {
        ui.label(text(title, 13.5, Weight::Medium, p.text2));
        ui.label(text(hint, 12.0, Weight::Regular, p.text3));
    });
}

/// Orders "FAT-2" before "FAT-10": digit runs compare by value, the rest by text.
fn natural_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    let chunks = |s: &str| -> Vec<(bool, String)> {
        let mut out: Vec<(bool, String)> = Vec::new();
        for ch in s.chars() {
            let digit = ch.is_ascii_digit();
            match out.last_mut() {
                Some((d, buf)) if *d == digit => buf.push(ch),
                _ => out.push((digit, ch.to_string())),
            }
        }
        out
    };
    let (ca, cb) = (chunks(a), chunks(b));
    for (x, y) in ca.iter().zip(cb.iter()) {
        let ord = if x.0 && y.0 {
            let (nx, ny) = (
                x.1.parse::<u64>().unwrap_or(0),
                y.1.parse::<u64>().unwrap_or(0),
            );
            nx.cmp(&ny).then_with(|| x.1.cmp(&y.1))
        } else {
            x.1.cmp(&y.1)
        };
        if ord != std::cmp::Ordering::Equal {
            return ord;
        }
    }
    ca.len().cmp(&cb.len())
}

fn capitalize(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

/// A unit price: whole numbers with thousands separators, cents only below 100.
fn unit_price(v: f64) -> String {
    if v >= 100.0 {
        fmt_int(v.round() as i64)
    } else {
        format!("{v:.2}")
    }
}
