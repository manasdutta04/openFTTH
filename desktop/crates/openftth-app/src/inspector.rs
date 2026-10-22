//! Properties panel for the selected element: attributes, connections, recorded reasons.

use egui::{Frame, Margin, Ui};

use crate::icons::Icon;
use crate::state::{s, AppState};
use crate::theme::Weight;
use crate::widgets::{
    button, button_ex, hairline, icon_button, kv, panel_header, section, tag, text, Kind,
};

pub enum InspectorAction {
    Ask(String),
    ZoomTo(String),
    /// Pick a new position on the map for this site.
    Move(String),
    /// Take this building out of the design.
    Exclude(String),
}

/// What the engineer may move by hand: the POP, cabinets and outdoor FATs. An indoor FAT
/// sits in its building.
pub fn is_movable(e: &openftth_core::Element) -> bool {
    matches!(e.kind(), "pop" | "fdh" | "fat")
        && e.openftth.get("mdu").and_then(serde_json::Value::as_bool) != Some(true)
}

pub fn is_excluded(e: &openftth_core::Element) -> bool {
    e.openftth
        .get("excluded")
        .and_then(serde_json::Value::as_bool)
        == Some(true)
}

/// The trace as a list from the OLT port to the home: what each hop is, and its loss.
fn fibre_path(ui: &mut Ui, p: &crate::theme::Palette, t: &serde_json::Value) {
    let d = &t["data"];
    if d["reaches_olt"].as_bool() != Some(true) {
        ui.label(text(s(&t["summary"]), 12.0, Weight::Regular, p.red));
        return;
    }
    let steps = d["steps"].as_array().cloned().unwrap_or_default();
    for (i, st) in steps.iter().enumerate() {
        let name = s(&st["name"]);
        let detail = if st["side"].as_str() == Some("OLT") {
            format!("OLT port {}", s(&st["port"]))
        } else if let Some(side) = st["side"].as_str() {
            if side.ends_with("splitter in") {
                "splitter input".to_string()
            } else if side == "indoor" {
                format!("indoor port {}", s(&st["port"]))
            } else {
                format!("{side} port {}", s(&st["port"]))
            }
        } else {
            format!(
                "tube {} fibre {} · {:.0} m",
                s(&st["tube"]),
                s(&st["fiber"]),
                st["length_m"].as_f64().unwrap_or(0.0)
            )
        };
        if let Some(via) = st.get("via").filter(|v| v.is_object()) {
            let at = via["at"]
                .as_str()
                .map(|a| format!(" at {a}"))
                .unwrap_or_default();
            ui.label(text(
                format!(
                    "  ↓ {}{} · {:.1} dB",
                    s(&via["connection"]),
                    at,
                    via["loss_db"].as_f64().unwrap_or(0.0)
                ),
                11.0,
                Weight::Regular,
                p.text3,
            ));
        }
        ui.horizontal(|ui| {
            ui.label(text(
                format!("{:>2}", i + 1),
                11.0,
                Weight::Regular,
                p.text3,
            ));
            ui.label(text(name, 12.0, Weight::Semibold, p.text));
            ui.label(text(detail, 11.5, Weight::Regular, p.text2));
        });
    }
    ui.add_space(4.0);
    kv(
        ui,
        p,
        "fibre + joints",
        &format!(
            "{:.2} dB",
            d["fibre_and_joint_loss_db"].as_f64().unwrap_or(0.0)
        ),
    );
    if let Some(odn) = d["odn_loss_db"].as_f64() {
        kv(ui, p, "ODN loss (budget check)", &format!("{odn:.2} dB"));
    }
}

pub fn inspector(ui: &mut Ui, st: &mut AppState) -> Option<InspectorAction> {
    let p = st.palette;
    let uuid = st.selection.first().cloned()?;
    let Some(e) = st.design.as_ref().and_then(|d| d.get(&uuid)).cloned() else {
        ui.label(text(
            "Not in the current design",
            12.0,
            Weight::Regular,
            p.text3,
        ));
        return None;
    };
    let mut action: Option<InspectorAction> = None;
    let mut close = false;
    Frame::new()
        .inner_margin(Margin::symmetric(10, 0))
        .show(ui, |ui| {
            panel_header(ui, &p, "Properties", |ui| {
                if icon_button(ui, &p, Icon::Close, "Clear selection  (Esc)").clicked() {
                    close = true;
                }
            });
        });
    egui::ScrollArea::vertical()
        .auto_shrink([false, false])
        .show(ui, |ui| {
            Frame::new()
                .inner_margin(Margin::symmetric(10, 8))
                .show(ui, |ui| {
                    if st.selection.len() > 1 {
                        // Several selected (Shift-click or a Shift-drag box): what is in it, then
                        // the first one in detail.
                        let mut kinds: std::collections::BTreeMap<String, usize> =
                            Default::default();
                        if let Some(d) = st.design.as_ref() {
                            for id in &st.selection {
                                if let Some(x) = d.get(id) {
                                    *kinds.entry(x.kind().to_string()).or_default() += 1;
                                }
                            }
                        }
                        ui.label(text(
                            format!("{} selected", st.selection.len()),
                            14.0,
                            Weight::Semibold,
                            p.text,
                        ));
                        ui.label(text(
                            kinds
                                .iter()
                                .map(|(k, n)| format!("{n} {k}"))
                                .collect::<Vec<_>>()
                                .join(" · "),
                            12.0,
                            Weight::Regular,
                            p.text2,
                        ));
                        ui.label(text(
                            "Del deletes them as one edit · J joins two cables · first one below",
                            11.5,
                            Weight::Regular,
                            p.text3,
                        ));
                        ui.add_space(8.0);
                        crate::widgets::hairline(ui, &p);
                        ui.add_space(8.0);
                    }
                    ui.label(text(e.name(), 14.0, Weight::Semibold, p.text));
                    ui.horizontal(|ui| {
                        tag(
                            ui,
                            e.kind(),
                            egui::Color32::WHITE,
                            p.kind(e.kind()).gamma_multiply(0.85),
                        );
                        tag(ui, &e.placement, p.text2, p.bg3);
                        if st.changed.iter().any(|c| c == &uuid) {
                            tag(ui, "changed", p.red, p.red.gamma_multiply(0.2));
                        }
                        if e.openftth.get("placed_by").and_then(serde_json::Value::as_str)
                            == Some("engineer")
                        {
                            tag(ui, "placed by you", p.accent, p.accent.gamma_multiply(0.2));
                        }
                        if is_excluded(&e) {
                            tag(ui, "excluded", p.red, p.red.gamma_multiply(0.2));
                        }
                        if let Some(state) = e.openftth.get("lifecycle").and_then(|v| v.as_str()) {
                            tag(ui, &state.replace('_', " "), p.text2, p.bg3);
                        }
                        if let Some(src) = e
                            .openftth
                            .get("provenance")
                            .and_then(|v| v.get("source"))
                            .and_then(|v| v.as_str())
                        {
                            tag(ui, &format!("from {src}"), p.text3, p.bg2)
                                .on_hover_text("Where this object's data comes from");
                        }
                    });
                    if let Some(issues) = st.violations.get(&uuid) {
                        ui.add_space(6.0);
                        for m in issues {
                            ui.add(egui::Label::new(text(m, 12.0, Weight::Regular, p.red)).wrap());
                        }
                    }
                    ui.add_space(6.0);
                    let busy = st.busy();
                    ui.horizontal_wrapped(|ui| {
                        if button_ex(ui, &p, Some(Icon::Target), "Zoom to", Kind::Quiet, true)
                            .clicked()
                        {
                            action = Some(InspectorAction::ZoomTo(uuid.clone()));
                        }
                        if button(ui, &p, "Ask the crew why", Kind::Prominent).clicked() {
                            action = Some(InspectorAction::Ask(format!(
                                "Why is {} placed there? Quote the recorded decision.",
                                e.name()
                            )));
                        }
                        if is_movable(&e)
                            && button_ex(ui, &p, None, "Move…", Kind::Quiet, !busy)
                                .on_hover_text(if busy {
                                    "The crew is working; wait for the turn to finish"
                                } else {
                                    "Click the new position on the map. The engine snaps it to the street and plans the routes again."
                                })
                                .clicked()
                        {
                            action = Some(InspectorAction::Move(uuid.clone()));
                        }
                        if matches!(e.kind(), "home" | "drop")
                            && button_ex(ui, &p, None, "Trace fibre", Kind::Quiet, true)
                                .on_hover_text("Follow this home's fibre from the OLT port, hop by hop")
                                .clicked()
                        {
                            st.trace(&uuid);
                        }
                        if e.kind() == "building"
                            && !is_excluded(&e)
                            && button_ex(ui, &p, None, "Exclude…", Kind::Quiet, !busy)
                                .on_hover_text(if busy {
                                    "The crew is working; wait for the turn to finish"
                                } else {
                                    "Take this building out of the design: its homes and drops go, routes are planned again."
                                })
                                .clicked()
                        {
                            action = Some(InspectorAction::Exclude(uuid.clone()));
                        }
                    });

                    section(ui, &p, "Attributes");
                    let currency = st
                        .boq
                        .as_ref()
                        .and_then(|b| b["currency"].as_str())
                        .unwrap_or("EUR")
                        .to_string();
                    for (k, v) in e.props.iter().filter(|(k, _)| *k != "name") {
                        if let Some((label, value)) = pretty(k, v, &e.props, &currency) {
                            kv(ui, &p, &label, &value);
                        }
                    }
                    let skip = [
                        "kind", "edges", "surfaces", "node", "building", "fat_ids", "mdu",
                        "fat_id", "fdh_id", "home", "lifecycle", "provenance",
                    ];
                    let shown: Vec<String> = e.props.values().map(s).collect();
                    for (k, v) in e
                        .openftth
                        .iter()
                        .filter(|(k, v)| !skip.contains(&k.as_str()) && !shown.contains(&s(v)))
                    {
                        let value = match v {
                            serde_json::Value::String(id)
                                if id.len() == 36
                                    && st
                                        .design
                                        .as_ref()
                                        .map(|d| d.get(id).is_some())
                                        .unwrap_or(false) =>
                            {
                                st.element_name(id)
                            }
                            other => {
                                if let Some((label, value)) =
                                    pretty(k, other, &e.openftth, &currency)
                                {
                                    kv(ui, &p, &label, &value);
                                }
                                continue;
                            }
                        };
                        kv(ui, &p, &label_of(k), &value);
                    }

                    let links: Vec<(&str, String)> = ["fat_id", "fdh_id", "home"]
                        .iter()
                        .filter_map(|k| {
                            e.openftth
                                .get(*k)
                                .and_then(|v| v.as_str())
                                .map(|v| (*k, v.to_string()))
                        })
                        .filter(|(_, id)| {
                            st.design
                                .as_ref()
                                .map(|d| d.get(id).is_some())
                                .unwrap_or(false)
                        })
                        .collect();
                    if !links.is_empty() {
                        section(ui, &p, "Connected");
                        ui.horizontal_wrapped(|ui| {
                            for (_, id) in &links {
                                if button_ex(
                                    ui,
                                    &p,
                                    Some(Icon::ChevronRight),
                                    &st.element_name(id),
                                    Kind::Ghost,
                                    true,
                                )
                                .clicked()
                                {
                                    st.selection = vec![id.clone()];
                                }
                            }
                        });
                    }

                    let trace = st
                        .trace
                        .as_ref()
                        .filter(|(id, _)| {
                            *id == uuid
                                || e.openftth.get("home").and_then(|v| v.as_str()) == Some(id)
                        })
                        .map(|(_, v)| v.clone());
                    if let Some(t) = trace {
                        section(ui, &p, "Fibre path");
                        fibre_path(ui, &p, &t);
                    }

                    section(ui, &p, "Reasons");
                    match st.explanations.get(&uuid).cloned() {
                        Some(x) => {
                            let decisions = x["decisions"].as_array().cloned().unwrap_or_default();
                            if decisions.is_empty() {
                                ui.label(text(
                                    "No recorded decision mentions this element.",
                                    11.5,
                                    Weight::Regular,
                                    p.text3,
                                ));
                            }
                            for d in decisions.iter().rev().take(4) {
                                ui.horizontal(|ui| {
                                    tag(ui, d["tool"].as_str().unwrap_or("tool"), p.text2, p.bg3);
                                    tag(
                                        ui,
                                        d["source"].as_str().unwrap_or("solver"),
                                        p.text3,
                                        p.bg2,
                                    );
                                });
                                ui.add(
                                    egui::Label::new(text(
                                        s(&d["reason"]),
                                        12.0,
                                        Weight::Regular,
                                        p.text2,
                                    ))
                                    .wrap(),
                                );
                                ui.add_space(4.0);
                                hairline(ui, &p);
                            }
                        }
                        None => {
                            if button(ui, &p, "Load reasons", Kind::Quiet).clicked() {
                                st.explain(&uuid);
                            }
                        }
                    }
                });
        });
    if close {
        st.selection.clear();
    }
    action
}

/// A field name as the engineer reads it: "distribution_path_m" → "Distribution path".
fn label_of(key: &str) -> String {
    let known = match key {
        "pon_id" => Some("PON"),
        "pop_id" => Some("POP"),
        "port_capacity" => Some("Ports"),
        "reserve_capacity" => Some("Spare ports"),
        "total_connections" => Some("Connected"),
        "distribution_est_m" => Some("Planned length"),
        "distribution_path_m" => Some("Distribution path"),
        "feeder_path_m" => Some("Feeder path"),
        "path_to_pop_m" => Some("Path to the POP"),
        "odn_loss_db" => Some("ODN loss"),
        "fiber_count" => Some("Fibres per cable"),
        "cable_count" => Some("Cables"),
        "fibers_used" => Some("Fibres used"),
        "downstream_units" => Some("Units downstream"),
        "offset_m" => Some("Home to street"),
        "splices_on_path" => Some("Splices on the path"),
        "manhole_type" => Some("Chamber type"),
        "dimensions_cm" => Some("Size (cm)"),
        "splitter_port" => Some("Splitter port"),
        "fat_splitter" => Some("FAT splitter"),
        "splitters" => Some("FAT splitters"),
        "pons" => Some("PONs"),
        "detour_m" => Some("Going round"),
        "detour_cost" => Some("Going round costs"),
        "barrier" => Some("Under"),
        _ => None,
    };
    if let Some(k) = known {
        return k.to_string();
    }
    let base = key
        .trim_end_matches("_m")
        .trim_end_matches("_db")
        .trim_end_matches("_pct")
        .replace('_', " ")
        .replace("fiber", "fibre");
    let mut chars = base.chars();
    match chars.next() {
        Some(c) => c.to_uppercase().collect::<String>() + chars.as_str(),
        None => base,
    }
}

/// A field and its value with the unit its name implies; `None` for the half of a pair shown
/// with its other half (fibre start and end read as one range).
fn pretty(
    key: &str,
    v: &serde_json::Value,
    all: &serde_json::Map<String, serde_json::Value>,
    currency: &str,
) -> Option<(String, String)> {
    if key == "fiber_end" && all.contains_key("fiber_start") {
        return None;
    }
    if key == "fiber_start" {
        let lo = v.as_i64()?;
        return Some(match all.get("fiber_end").and_then(|x| x.as_i64()) {
            Some(hi) if hi > lo => ("Fibres".into(), format!("{lo}–{hi}")),
            _ => ("Fibre".into(), lo.to_string()),
        });
    }
    let label = label_of(key);
    let value = match v {
        serde_json::Value::Bool(b) => (if *b { "yes" } else { "no" }).to_string(),
        serde_json::Value::Number(n) => {
            let x = n.as_f64().unwrap_or_default();
            if key.ends_with("_m") {
                crate::widgets::fmt_metres(x)
            } else if key.ends_with("_db") {
                format!("{x:.2} dB")
            } else if key.ends_with("_pct") {
                format!("{x:.0}%")
            } else if key.contains("cost") {
                crate::widgets::fmt_money(x, currency)
            } else if n.is_i64() || n.is_u64() || x.fract() == 0.0 {
                crate::widgets::fmt_int(x as i64)
            } else {
                format!("{x:.2}")
            }
        }
        serde_json::Value::String(text) if key == "barrier" => match text.as_str() {
            "water" => "a river or canal".to_string(),
            "unmapped" => "an obstacle the map does not show".to_string(),
            other => format!("the {other}"),
        },
        other => s(other),
    };
    Some((label, value))
}
