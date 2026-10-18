//! The inspector: what is selected on the map, its attributes, and why the engine put it there.

use egui::{RichText, Ui};

use crate::state::{s, AppState};
use crate::widgets::{card, ghost_button, pill, primary_button, section_title};

pub enum InspectorAction {
    Ask(String),
    ZoomTo(String),
}

pub fn inspector(ui: &mut Ui, st: &mut AppState) -> Option<InspectorAction> {
    let p = st.palette;
    let uuid = st.selection.first().cloned()?;
    let Some(e) = st.design.as_ref().and_then(|d| d.get(&uuid)).cloned() else {
        ui.label(
            RichText::new("Selected element is not in the current design.").color(p.text_muted),
        );
        return None;
    };
    let mut action: Option<InspectorAction> = None;
    ui.horizontal(|ui| {
        ui.heading(e.name());
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if ui
                .small_button("×")
                .on_hover_text("Clear selection (Esc)")
                .clicked()
            {
                st.selection.clear();
            }
            if ui.small_button("Zoom to").clicked() {
                action = Some(InspectorAction::ZoomTo(uuid.clone()));
            }
        });
    });
    ui.horizontal(|ui| {
        pill(ui, e.kind(), egui::Color32::WHITE, p.kind(e.kind()));
        pill(ui, &e.placement, p.text_muted, p.surface_alt);
        if st.changed.iter().any(|c| c == &uuid) {
            pill(ui, "changed", egui::Color32::WHITE, p.danger);
        }
    });
    ui.add_space(6.0);

    card(ui, &p, |ui| {
        section_title(ui, &p, "Attributes");
        egui::Grid::new("attrs")
            .num_columns(2)
            .spacing([12.0, 4.0])
            .striped(true)
            .show(ui, |ui| {
                for (k, v) in e.props.iter().filter(|(k, _)| *k != "name") {
                    ui.label(
                        RichText::new(k.replace('_', " "))
                            .small()
                            .color(p.text_muted),
                    );
                    ui.label(RichText::new(short(&s(v))).small());
                    ui.end_row();
                }
                for (k, v) in e.openftth.iter().filter(|(k, _)| {
                    !["kind", "edges", "surfaces", "node", "building", "fat_ids"]
                        .contains(&k.as_str())
                }) {
                    ui.label(
                        RichText::new(k.replace('_', " "))
                            .small()
                            .color(p.text_muted),
                    );
                    let text = match v {
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
                        other => short(&s(other)),
                    };
                    ui.label(RichText::new(text).small());
                    ui.end_row();
                }
            });
    });
    ui.add_space(6.0);

    // Related elements (navigate)
    let links: Vec<(&str, String)> = ["fat_id", "fdh_id", "home", "pon_id"]
        .iter()
        .filter_map(|k| {
            e.openftth
                .get(*k)
                .and_then(|v| v.as_str())
                .map(|v| (*k, v.to_string()))
        })
        .collect();
    if !links.is_empty() {
        card(ui, &p, |ui| {
            section_title(ui, &p, "Connected");
            ui.horizontal_wrapped(|ui| {
                for (k, id) in &links {
                    if *k == "pon_id" {
                        pill(ui, id, p.text_muted, p.surface_alt);
                    } else if st
                        .design
                        .as_ref()
                        .map(|d| d.get(id).is_some())
                        .unwrap_or(false)
                        && ui.link(st.element_name(id)).clicked()
                    {
                        st.selection = vec![id.clone()];
                    }
                }
            });
        });
        ui.add_space(6.0);
    }

    card(ui, &p, |ui| {
        section_title(ui, &p, "Why here");
        match st.explanations.get(&uuid).cloned() {
            Some(x) => {
                let decisions = x["decisions"].as_array().cloned().unwrap_or_default();
                if decisions.is_empty() {
                    ui.label(
                        RichText::new("No recorded decision mentions this element yet.")
                            .small()
                            .color(p.text_muted),
                    );
                } else {
                    for d in decisions.iter().rev().take(4) {
                        ui.horizontal_wrapped(|ui| {
                            pill(
                                ui,
                                d["tool"].as_str().unwrap_or("tool"),
                                p.accent,
                                p.accent_soft,
                            );
                            pill(
                                ui,
                                d["source"].as_str().unwrap_or("solver"),
                                p.text_muted,
                                p.surface_alt,
                            );
                        });
                        ui.label(RichText::new(s(&d["reason"])).small());
                        ui.add_space(4.0);
                    }
                }
            }
            None => {
                if ghost_button(ui, &p, "Load the engine's reasons").clicked() {
                    st.explain(&uuid);
                }
            }
        }
        ui.add_space(4.0);
        if primary_button(ui, &p, "Ask the crew why").clicked() {
            action = Some(InspectorAction::Ask(format!(
                "Why is {} placed there? Quote the recorded decision.",
                e.name()
            )));
        }
    });
    action
}

fn short(text: &str) -> String {
    if text.chars().count() > 60 {
        format!("{}…", text.chars().take(60).collect::<String>())
    } else {
        text.to_string()
    }
}
