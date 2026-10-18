//! Workspace tabs besides the twin: BOQ, splice schedule, versions and backstage.

use std::collections::BTreeMap;

use egui::{RichText, Ui};
use egui_extras::{Column, TableBuilder};
use serde_json::Value;

use crate::state::{s, AppState};
use crate::widgets::{
    card, fmt_metres, fmt_money, ghost_button, kpi, pill, relative_time, section_title, short_id,
};

#[derive(Default)]
pub struct TabState {
    pub splice_filter: String,
    pub backstage_filter: String,
}

// ---- BOQ --------------------------------------------------------------------------------------

pub fn boq_tab(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    let Some(boq) = &st.boq else {
        empty(
            ui,
            st,
            "No bill of quantities yet",
            "Ask the Drafter for the BOQ once the design is routed.",
        );
        return;
    };
    let currency = boq["currency"].as_str().unwrap_or("EUR");
    ui.horizontal_wrapped(|ui| {
        kpi(
            ui,
            &p,
            &fmt_money(boq["grand_total"].as_f64().unwrap_or(0.0), currency),
            "total",
            Some(p.accent),
        );
        kpi(
            ui,
            &p,
            &fmt_money(boq["cost_per_unit"].as_f64().unwrap_or(0.0), currency),
            "per unit",
            None,
        );
        kpi(
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
            kpi(ui, &p, &fmt_metres(trench), "feeder + distribution", None);
        }
        if let Some(v) = &st.validation {
            let n: usize = v["summary"]
                .as_object()
                .map(|m| m.values().filter_map(|x| x.as_u64()).sum::<u64>() as usize)
                .unwrap_or(0);
            kpi(
                ui,
                &p,
                &n.to_string(),
                "rule violations",
                Some(if n == 0 { p.success } else { p.danger }),
            );
            if let Some(w) = v["power_budget"]["worst_db"].as_f64() {
                let budget = v["power_budget"]["budget_db"].as_f64().unwrap_or(28.0);
                kpi(
                    ui,
                    &p,
                    &format!("{w:.1} / {budget:.0} dB"),
                    "worst ODN loss",
                    Some(if w <= budget { p.success } else { p.danger }),
                );
            }
        }
    });
    ui.add_space(8.0);
    if let Some(totals) = boq["totals"].as_object() {
        let grand = boq["grand_total"].as_f64().unwrap_or(1.0).max(1.0);
        card(ui, &p, |ui| {
            section_title(ui, &p, "By category");
            let (rect, _) = ui
                .allocate_exact_size(egui::vec2(ui.available_width(), 18.0), egui::Sense::hover());
            let mut x = rect.left();
            let colors = [p.accent, p.info, p.warning, p.kind("feeder"), p.text_muted];
            for (i, (cat, total)) in totals.iter().enumerate() {
                let w = (total.as_f64().unwrap_or(0.0) / grand) as f32 * rect.width();
                let r = egui::Rect::from_min_size(
                    egui::pos2(x, rect.top()),
                    egui::vec2(w.max(0.0), rect.height()),
                );
                ui.painter().rect_filled(r, 2.0, colors[i % colors.len()]);
                let _ = cat;
                x += w;
            }
            ui.horizontal_wrapped(|ui| {
                for (i, (cat, total)) in totals.iter().enumerate() {
                    crate::widgets::dot(ui, colors[i % colors.len()], 4.0);
                    ui.label(
                        RichText::new(format!(
                            "{cat} {} ({:.0}%)",
                            fmt_money(total.as_f64().unwrap_or(0.0), currency),
                            total.as_f64().unwrap_or(0.0) / grand * 100.0
                        ))
                        .small(),
                    );
                }
            });
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
                egui::CollapsingHeader::new(
                    RichText::new(format!(
                        "{}  ·  {}",
                        capitalize(cat),
                        fmt_money(total, currency)
                    ))
                    .strong(),
                )
                .default_open(true)
                .show(ui, |ui| {
                    TableBuilder::new(ui)
                        .id_salt(format!("boq-{cat}"))
                        .striped(true)
                        .column(Column::remainder().at_least(180.0))
                        .column(Column::auto().at_least(80.0))
                        .column(Column::auto().at_least(50.0))
                        .column(Column::auto().at_least(90.0))
                        .column(Column::auto().at_least(110.0))
                        .header(22.0, |mut h| {
                            for t in ["Item", "Qty", "Unit", "Unit cost", "Total"] {
                                h.col(|ui| {
                                    ui.label(RichText::new(t).small().strong().color(p.text_muted));
                                });
                            }
                        })
                        .body(|body| {
                            body.rows(22.0, rows.len(), |mut row| {
                                let r = &rows[row.index()];
                                row.col(|ui| {
                                    let label = ui.label(s(&r["item"]));
                                    if let Some(note) = r["note"].as_str() {
                                        label.on_hover_text(note);
                                    }
                                });
                                row.col(|ui| {
                                    ui.label(format!("{:.0}", r["qty"].as_f64().unwrap_or(0.0)));
                                });
                                row.col(|ui| {
                                    ui.label(RichText::new(s(&r["unit"])).color(p.text_muted));
                                });
                                row.col(|ui| {
                                    ui.label(format!(
                                        "{:.2}",
                                        r["unit_cost"].as_f64().unwrap_or(0.0)
                                    ));
                                });
                                row.col(|ui| {
                                    ui.label(
                                        RichText::new(fmt_money(
                                            r["total"].as_f64().unwrap_or(0.0),
                                            "",
                                        ))
                                        .strong(),
                                    );
                                });
                            });
                        });
                });
            }
            if let Some(a) = boq["assumptions"].as_array() {
                if !a.is_empty() {
                    ui.add_space(6.0);
                    section_title(ui, &p, "Assumptions");
                    for x in a {
                        ui.label(
                            RichText::new(format!("• {}", s(x)))
                                .small()
                                .color(p.text_muted),
                        );
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
            "No design yet",
            "Open a project and design the area first.",
        );
        return;
    };
    let Some(conns) = d.tables.get("fiber_connection") else {
        empty(ui, st, "Fibres are not assigned yet", "Ask the Planner to run size_cables and assign_fibers; the splice schedule appears here.");
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
        ui.label(
            RichText::new(format!(
                "{} connections in {} splice points",
                conns.len(),
                points.len()
            ))
            .strong(),
        );
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.add(
                egui::TextEdit::singleline(&mut tabs.splice_filter)
                    .hint_text("filter by closure, cable or type")
                    .desired_width(260.0),
            );
        });
    });
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
    rows.sort_by(|a, b| (&a.closure, &a.kind, &a.from).cmp(&(&b.closure, &b.kind, &b.from)));
    let shown = rows.len().min(2000);
    if rows.len() > shown {
        ui.label(
            RichText::new(format!(
                "showing the first {shown} of {} rows; narrow the filter",
                rows.len()
            ))
            .small()
            .color(p.text_muted),
        );
    }
    TableBuilder::new(ui)
        .id_salt("splices")
        .striped(true)
        .column(Column::auto().at_least(120.0))
        .column(Column::auto().at_least(70.0))
        .column(Column::remainder().at_least(200.0))
        .column(Column::remainder().at_least(200.0))
        .column(Column::auto().at_least(60.0))
        .header(22.0, |mut h| {
            for t in ["Closure", "Type", "From", "To", "Loss dB"] {
                h.col(|ui| {
                    ui.label(RichText::new(t).small().strong().color(p.text_muted));
                });
            }
        })
        .body(|body| {
            body.rows(20.0, shown, |mut row| {
                let r = &rows[row.index()];
                row.col(|ui| {
                    ui.label(&r.closure);
                });
                row.col(|ui| {
                    let (fg, bg) = match r.kind.as_str() {
                        "splitter" => (p.kind("fdh"), p.surface_alt),
                        "patch" => (p.kind("pop"), p.surface_alt),
                        _ => (p.text_muted, p.surface_alt),
                    };
                    pill(ui, &r.kind, fg, bg);
                });
                row.col(|ui| {
                    ui.label(RichText::new(&r.from).monospace().small());
                });
                row.col(|ui| {
                    ui.label(RichText::new(&r.to).monospace().small());
                });
                row.col(|ui| {
                    ui.label(format!("{:.1}", r.loss));
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
            "No versions yet",
            "Every engine tool call becomes a version you can compare and approve.",
        );
        return;
    }
    ui.horizontal(|ui| {
        ui.label(
            RichText::new(format!(
                "{} versions · one per tool call",
                st.versions.len()
            ))
            .strong(),
        );
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if ghost_button(ui, &p, "Refresh").clicked() {
                st.refresh_versions();
            }
            if !st.changed.is_empty() && ghost_button(ui, &p, "Clear highlight").clicked() {
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
    let mut approve: Option<String> = None;
    let mut compare: Option<String> = None;
    egui::ScrollArea::vertical().id_salt("versions").auto_shrink([false, false]).show(ui, |ui| {
        for (i, v) in versions.iter().enumerate() {
            let ver = s(&v["version"]);
            let subject = s(&v["subject"]);
            let (stage, rest) = split_subject(&subject);
            card(ui, &p, |ui| {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(short_id(&ver)).monospace().color(p.text_muted));
                    if i == 0 {
                        pill(ui, "head", egui::Color32::WHITE, p.accent);
                    }
                    if approved.contains(&ver) {
                        pill(ui, "approved", egui::Color32::WHITE, p.success);
                    }
                    if !stage.is_empty() {
                        pill(ui, &stage, p.text_muted, p.surface_alt);
                    }
                    if let Some(t) = v["time"].as_i64() {
                        ui.label(RichText::new(relative_time(t * 1000)).small().color(p.text_muted));
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if !approved.contains(&ver) && ui.small_button("Approve").on_hover_text("Tag this version as approved by an engineer").clicked() {
                            approve = Some(ver.clone());
                        }
                        if i > 0 && ui.small_button("Changes since").on_hover_text("Highlight on the map what changed between this version and head").clicked() {
                            compare = Some(ver.clone());
                        }
                    });
                });
                ui.label(rest);
            });
            ui.add_space(4.0);
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
        ui.label(RichText::new("Every tool call, request and error, as it happens").strong());
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if ghost_button(ui, &p, "Reload").clicked() {
                st.refresh_backstage();
            }
            ui.add(
                egui::TextEdit::singleline(&mut tabs.backstage_filter)
                    .hint_text("filter")
                    .desired_width(200.0),
            );
        });
    });
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
                    "error" => p.danger,
                    "tool.started" => p.info,
                    "tool.done" => p.success,
                    _ => p.text_muted,
                };
                ui.horizontal_wrapped(|ui| {
                    ui.label(
                        RichText::new(crate::widgets::clock_time(e["at"].as_i64().unwrap_or(0)))
                            .monospace()
                            .small()
                            .color(p.text_muted),
                    );
                    crate::widgets::dot(ui, c, 3.0);
                    ui.label(
                        RichText::new(st.folk_name(e["folk"].as_str().unwrap_or("·")))
                            .small()
                            .strong(),
                    );
                    ui.label(RichText::new(&event).small().color(c));
                    let detail = e["detail"].to_string();
                    ui.label(
                        RichText::new(if detail.chars().count() > 160 {
                            format!("{}…", detail.chars().take(160).collect::<String>())
                        } else {
                            detail.clone()
                        })
                        .monospace()
                        .small()
                        .color(p.text_muted),
                    )
                    .on_hover_text(detail);
                });
            }
        });
}

fn empty(ui: &mut Ui, st: &AppState, title: &str, hint: &str) {
    let p = st.palette;
    ui.add_space(40.0);
    ui.vertical_centered(|ui| {
        ui.label(RichText::new(title).size(17.0).strong());
        ui.label(RichText::new(hint).color(p.text_muted));
    });
}

fn capitalize(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}
