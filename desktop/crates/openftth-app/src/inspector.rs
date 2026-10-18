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
                    });
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
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
                    });

                    section(ui, &p, "Attributes");
                    for (k, v) in e.props.iter().filter(|(k, _)| *k != "name") {
                        kv(ui, &p, &k.replace('_', " "), &s(v));
                    }
                    let skip = [
                        "kind", "edges", "surfaces", "node", "building", "fat_ids", "mdu",
                        "fat_id", "fdh_id", "home",
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
                            other => s(other),
                        };
                        kv(ui, &p, &k.replace('_', " "), &value);
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
