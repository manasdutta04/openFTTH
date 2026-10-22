//! The attribute table: every element of one layer as a row, sortable by any column, filterable,
//! with the attributes the engineer owns editable in place (name, status, ports, fibres, cables,
//! slack, chamber type). An edit is one engine call with the reason from the edit bar.

use egui::{CursorIcon, Ui};
use egui_extras::{Column, TableBuilder};
use openftth_core::bundle::Element;
use serde_json::{json, Value};

use crate::state::{s, AppState};
use crate::theme::Weight;
use crate::widgets::{caps, fmt_int, fmt_metres, mono_text, text};

/// The layers the table offers, with the label of their chip.
const LAYERS: [(&str, &str); 9] = [
    ("fat", "FATs"),
    ("fdh", "Cabinets"),
    ("pop", "POPs"),
    ("chamber", "Chambers"),
    ("feeder", "Feeder"),
    ("distribution", "Distribution"),
    ("drop", "Drops"),
    ("home", "Homes"),
    ("crossing", "Crossings"),
];

/// A column: its title, where its value lives, and the attribute key when it is editable.
struct Col {
    title: &'static str,
    get: fn(&Element, &AppState) -> String,
    sort: fn(&Element) -> f64,
    edit: Option<&'static str>,
}

fn prop(e: &Element, k: &str) -> String {
    e.props.get(k).map(s).unwrap_or_default()
}
fn of(e: &Element, k: &str) -> String {
    e.openftth.get(k).map(s).unwrap_or_default()
}
fn num(e: &Element, k: &str) -> f64 {
    e.props
        .get(k)
        .or_else(|| e.openftth.get(k))
        .and_then(Value::as_f64)
        .unwrap_or(f64::NAN)
}
fn linked(e: &Element, st: &AppState, k: &str) -> String {
    e.openftth
        .get(k)
        .and_then(Value::as_str)
        .map(|id| st.element_name(id))
        .unwrap_or_default()
}

fn columns(kind: &str) -> Vec<Col> {
    let name = Col {
        title: "Name",
        get: |e, _| e.name(),
        sort: |_| f64::NAN,
        edit: Some("name"),
    };
    let status = Col {
        title: "Status",
        get: |e, _| prop(e, "status"),
        sort: |_| f64::NAN,
        edit: Some("status"),
    };
    let by = Col {
        title: "By",
        get: |e, _| {
            if e.openftth.get("placed_by").and_then(Value::as_str) == Some("engineer")
                || e.openftth.get("drawn_by").and_then(Value::as_str) == Some("engineer")
            {
                "engineer".into()
            } else {
                "engine".into()
            }
        },
        sort: |_| f64::NAN,
        edit: None,
    };
    let units = Col {
        title: "Units",
        get: |e, _| of(e, "units"),
        sort: |e| num(e, "units"),
        edit: None,
    };
    let ports = Col {
        title: "Ports",
        get: |e, _| prop(e, "port_capacity"),
        sort: |e| num(e, "port_capacity"),
        edit: Some("port_capacity"),
    };
    match kind {
        "fat" => vec![
            name,
            units,
            ports,
            Col {
                title: "PON",
                get: |e, _| of(e, "pon_id"),
                sort: |_| f64::NAN,
                edit: None,
            },
            Col {
                title: "Cabinet",
                get: |e, st| linked(e, st, "fdh_id"),
                sort: |_| f64::NAN,
                edit: None,
            },
            status,
            by,
        ],
        "fdh" => vec![
            name,
            units,
            ports,
            Col {
                title: "PONs",
                get: |e, _| of(e, "pons"),
                sort: |e| num(e, "pons"),
                edit: None,
            },
            Col {
                title: "POP",
                get: |e, st| linked(e, st, "pop_id"),
                sort: |_| f64::NAN,
                edit: None,
            },
            status,
            by,
        ],
        "pop" => vec![name, units, by],
        "chamber" => vec![
            name,
            Col {
                title: "Type",
                get: |e, _| prop(e, "manhole_type"),
                sort: |_| f64::NAN,
                edit: Some("manhole_type"),
            },
            status,
            by,
        ],
        "feeder" | "distribution" | "drop" => vec![
            name,
            Col {
                title: "Fibres",
                get: |e, _| prop(e, "fiber_count"),
                sort: |e| num(e, "fiber_count"),
                edit: Some("fiber_count"),
            },
            Col {
                title: "Cables",
                get: |e, _| prop(e, "cable_count"),
                sort: |e| num(e, "cable_count"),
                edit: Some("cable_count"),
            },
            Col {
                title: "Length",
                get: |e, _| {
                    e.props
                        .get("length_m")
                        .and_then(Value::as_f64)
                        .map(fmt_metres)
                        .unwrap_or_default()
                },
                sort: |e| num(e, "length_m"),
                edit: None,
            },
            Col {
                title: "Slack m",
                get: |e, _| prop(e, "slack_m"),
                sort: |e| num(e, "slack_m"),
                edit: Some("slack_m"),
            },
            status,
            by,
        ],
        "home" => vec![
            name,
            units,
            Col {
                title: "FAT",
                get: |e, st| linked(e, st, "fat_id"),
                sort: |_| f64::NAN,
                edit: None,
            },
            Col {
                title: "Path to POP",
                get: |e, _| {
                    e.openftth
                        .get("path_to_pop_m")
                        .and_then(Value::as_f64)
                        .map(fmt_metres)
                        .unwrap_or_default()
                },
                sort: |e| num(e, "path_to_pop_m"),
                edit: None,
            },
            Col {
                title: "ODN loss",
                get: |e, _| {
                    e.openftth
                        .get("odn_loss_db")
                        .and_then(Value::as_f64)
                        .map(|v| format!("{v:.2} dB"))
                        .unwrap_or_default()
                },
                sort: |e| num(e, "odn_loss_db"),
                edit: None,
            },
        ],
        _ => vec![
            name,
            Col {
                title: "Under",
                get: |e, _| of(e, "barrier"),
                sort: |_| f64::NAN,
                edit: None,
            },
            Col {
                title: "Length",
                get: |e, _| {
                    e.props
                        .get("length_m")
                        .and_then(Value::as_f64)
                        .map(fmt_metres)
                        .unwrap_or_default()
                },
                sort: |e| num(e, "length_m"),
                edit: None,
            },
            Col {
                title: "Cost",
                get: |e, _| {
                    e.openftth
                        .get("cost")
                        .and_then(Value::as_f64)
                        .map(|v| fmt_int(v as i64))
                        .unwrap_or_default()
                },
                sort: |e| num(e, "cost"),
                edit: None,
            },
        ],
    }
}

#[derive(Default)]
pub struct TableState {
    kind: String,
    filter: String,
    sort: Option<(usize, bool)>,
    editing: Option<(String, &'static str, String, bool)>,
}

/// What the table asks the app to do.
pub enum TableAction {
    Select(String),
    Zoom(String),
}

pub fn table_tab(
    ui: &mut Ui,
    st: &mut AppState,
    t: &mut TableState,
    reason: &str,
) -> Option<TableAction> {
    let p = st.palette;
    if t.kind.is_empty() {
        t.kind = "fat".into();
    }
    let Some(design) = st.design.clone() else {
        ui.label(text(
            "Open a project to see its elements.",
            12.5,
            Weight::Regular,
            p.text3,
        ));
        return None;
    };
    let mut action = None;
    ui.horizontal_wrapped(|ui| {
        for (kind, label) in LAYERS {
            let n = design.by_kind(kind).count();
            if n == 0 {
                continue;
            }
            let on = t.kind == kind;
            if ui
                .selectable_label(
                    on,
                    text(
                        format!("{label} {n}"),
                        12.0,
                        Weight::Medium,
                        if on { p.accent } else { p.text2 },
                    ),
                )
                .clicked()
            {
                t.kind = kind.into();
                t.sort = None;
                t.editing = None;
            }
        }
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.add(
                egui::TextEdit::singleline(&mut t.filter)
                    .hint_text("Filter")
                    .desired_width(200.0),
            );
        });
    });
    ui.add_space(6.0);
    let cols = columns(&t.kind);
    let q = t.filter.trim().to_lowercase();
    let mut rows: Vec<&Element> = design
        .by_kind(&t.kind)
        .filter(|e| {
            q.is_empty()
                || cols
                    .iter()
                    .any(|c| (c.get)(e, st).to_lowercase().contains(&q))
        })
        .collect();
    match t.sort {
        Some((i, asc)) => {
            let c = &cols[i];
            rows.sort_by(|a, b| {
                let (x, y) = ((c.sort)(a), (c.sort)(b));
                let o = if x.is_nan() || y.is_nan() {
                    crate::tabs::natural_cmp(&(c.get)(a, st), &(c.get)(b, st))
                } else {
                    x.partial_cmp(&y).unwrap_or(std::cmp::Ordering::Equal)
                };
                if asc {
                    o
                } else {
                    o.reverse()
                }
            });
        }
        None => rows.sort_by(|a, b| crate::tabs::natural_cmp(&a.name(), &b.name())),
    }
    ui.label(text(
        format!(
            "{} {} · editable columns: click a value, Enter saves with the reason \"{reason}\"",
            rows.len(),
            t.kind.replace('_', " ")
        ),
        11.5,
        Weight::Regular,
        p.text3,
    ));
    ui.add_space(4.0);
    let mut commit: Option<(String, &'static str, String)> = None;
    let selected: Vec<String> = st.selection.clone();
    let mut table = TableBuilder::new(ui)
        .id_salt(("attributes", t.kind.clone()))
        .vscroll(true)
        .auto_shrink([false, false])
        .striped(true)
        .sense(egui::Sense::click());
    for (i, _) in cols.iter().enumerate() {
        table = table.column(
            if i == 0 {
                Column::initial(200.0).at_least(120.0)
            } else {
                Column::initial(120.0).at_least(60.0)
            }
            .resizable(true),
        );
    }
    table
        .header(22.0, |mut h| {
            for (i, c) in cols.iter().enumerate() {
                h.col(|ui| {
                    let arrow = match t.sort {
                        Some((j, true)) if j == i => " ↑",
                        Some((j, false)) if j == i => " ↓",
                        _ => "",
                    };
                    let resp = ui
                        .add(
                            egui::Label::new(caps(
                                &format!("{}{arrow}", c.title),
                                if c.edit.is_some() { p.text2 } else { p.text3 },
                            ))
                            .sense(egui::Sense::click()),
                        )
                        .on_hover_cursor(CursorIcon::PointingHand);
                    if resp.clicked() {
                        t.sort = match t.sort {
                            Some((j, asc)) if j == i => Some((i, !asc)),
                            _ => Some((i, true)),
                        };
                    }
                });
            }
        })
        .body(|body| {
            body.rows(24.0, rows.len(), |mut row| {
                let e = rows[row.index()];
                let id = e.element_uuid.clone();
                row.set_selected(selected.contains(&id));
                for c in &cols {
                    row.col(|ui| {
                        let value = (c.get)(e, st);
                        let editing_here = t
                            .editing
                            .as_ref()
                            .is_some_and(|(eid, k, _, _)| *eid == id && Some(*k) == c.edit);
                        if editing_here {
                            let (_, key, buf, focus) = t.editing.as_mut().expect("editing");
                            let resp = ui
                                .add(egui::TextEdit::singleline(buf).desired_width(f32::INFINITY));
                            if *focus {
                                resp.request_focus();
                                *focus = false;
                            }
                            if resp.lost_focus() {
                                if ui.input(|i| i.key_pressed(egui::Key::Enter))
                                    && buf.trim() != value
                                {
                                    commit = Some((id.clone(), *key, buf.trim().to_string()));
                                }
                                t.editing = None;
                            }
                        } else {
                            let label = if c.edit.is_some() {
                                text(&value, 12.0, Weight::Regular, p.text)
                            } else {
                                mono_text(&value, 11.5, p.text2)
                            };
                            let resp = ui.add(
                                egui::Label::new(label)
                                    .truncate()
                                    .sense(egui::Sense::click()),
                            );
                            if let Some(key) = c.edit {
                                let resp = resp
                                    .on_hover_cursor(CursorIcon::Text)
                                    .on_hover_text("Click to edit");
                                if resp.clicked() {
                                    t.editing = Some((id.clone(), key, value.clone(), true));
                                }
                            } else if resp.double_clicked() {
                                action = Some(TableAction::Zoom(id.clone()));
                            } else if resp.clicked() {
                                action = Some(TableAction::Select(id.clone()));
                            }
                        }
                    });
                }
                let resp = row.response();
                if resp.double_clicked() {
                    action = Some(TableAction::Zoom(id.clone()));
                } else if resp.clicked() {
                    action = Some(TableAction::Select(id.clone()));
                }
            });
        });
    if let Some((id, key, value)) = commit {
        let v: Value = value
            .parse::<f64>()
            .ok()
            .filter(|_| key != "name" && key != "status" && key != "manhole_type")
            .map(|n| {
                if n.fract() == 0.0 {
                    json!(n as i64)
                } else {
                    json!(n)
                }
            })
            .unwrap_or_else(|| json!(value));
        st.edit(
            "set_attribute",
            json!({ "element_uuid": id, "key": key, "value": v, "reason": reason }),
        );
    }
    action
}
