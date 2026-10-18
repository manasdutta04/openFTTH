//! The chat column: crew presence, stage stepper, live tool chips, approval cards, the
//! transcript as bubbles, quick actions and the composer.

use egui::{
    Color32, CornerRadius, Frame, Key, KeyboardShortcut, Margin, Modifiers, RichText, Stroke, Ui,
};

use crate::markdown;
use crate::state::{stage_index, AppState, STAGES};
use crate::widgets::{
    self, clock_time, dot, ghost_button, primary_button, pulse_dot, section_title, tinted_card,
};

#[derive(Default)]
pub struct Composer {
    pub text: String,
    pub want_focus: bool,
    pub show_approval_history: bool,
}

pub fn chat_panel(ui: &mut Ui, st: &mut AppState, composer: &mut Composer) {
    let p = st.palette;
    presence(ui, st);
    stepper(ui, st);
    if !st.chips.is_empty() {
        chips(ui, st);
    }
    ui.add_space(4.0);

    // Approvals first: they block the crew.
    let pending: Vec<_> = st.pending_approvals().cloned().collect();
    let mut decision: Option<(String, bool)> = None;
    for a in &pending {
        tinted_card(ui, p.approval_bg, p.approval_border, |ui| {
            ui.horizontal(|ui| {
                dot(ui, p.warning, 5.0);
                ui.label(RichText::new("Approval needed").strong());
                ui.label(RichText::new(format!("· {}", st.folk_name(&a.folk))).color(p.text_muted));
            });
            ui.label(RichText::new(&a.summary).size(15.0));
            ui.label(
                RichText::new(format!("tool {}", a.tool))
                    .small()
                    .monospace()
                    .color(p.text_muted),
            );
            if let Some(obj) = a.input.as_object() {
                let shown: Vec<String> = obj
                    .iter()
                    .filter(|(k, _)| *k != "approval_id")
                    .take(6)
                    .map(|(k, v)| format!("{k} = {}", crate::state::s(v)))
                    .collect();
                if !shown.is_empty() {
                    ui.label(RichText::new(shown.join("   ")).small().color(p.text_muted));
                }
            }
            if let Some(card) = st.approvals.iter_mut().find(|x| x.id == a.id) {
                ui.add(
                    egui::TextEdit::singleline(&mut card.note)
                        .hint_text("note for the audit log (optional)")
                        .desired_width(f32::INFINITY),
                );
            }
            ui.horizontal(|ui| {
                if primary_button(ui, &p, "Allow").clicked() {
                    decision = Some((a.id.clone(), true));
                }
                if ghost_button(ui, &p, "Deny").clicked() {
                    decision = Some((a.id.clone(), false));
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.label(
                        RichText::new(widgets::relative_time(a.created_at))
                            .small()
                            .color(p.text_muted),
                    );
                });
            });
        });
        ui.add_space(4.0);
    }
    if let Some((id, allow)) = decision {
        st.decide(&id, allow);
    }

    // Composer pinned to the bottom; the transcript takes what is left.
    egui::Panel::bottom("composer")
        .show_separator_line(false)
        .show(ui, |ui| {
            ui.add_space(4.0);
            composer_ui(ui, st, composer);
            ui.add_space(4.0);
        });
    egui::ScrollArea::vertical()
        .id_salt("transcript")
        .auto_shrink([false, false])
        .stick_to_bottom(true)
        .show(ui, |ui| {
            if st.messages.is_empty() {
                empty_state(ui, st);
            }
            let messages = st.messages.clone();
            for m in &messages {
                bubble(ui, st, m);
            }
            if st.busy() && !st.messages.iter().any(|m| m.streaming()) {
                ui.horizontal(|ui| {
                    pulse_dot(ui, p.accent, 4.0);
                    let who = st
                        .folk_states
                        .iter()
                        .find(|(_, v)| *v != "idle")
                        .map(|(k, _)| st.folk_name(k))
                        .unwrap_or_else(|| "The crew".into());
                    ui.label(
                        RichText::new(format!("{who} is working…"))
                            .small()
                            .color(p.text_muted),
                    );
                });
            }
        });
}

fn presence(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    ui.horizontal_wrapped(|ui| {
        for m in &st.crew {
            let state = st
                .folk_states
                .get(&m.key)
                .cloned()
                .unwrap_or_else(|| "idle".into());
            let c = match state.as_str() {
                "thinking" => p.info,
                "tooling" => p.warning,
                "waiting" => p.danger,
                _ => p.text_muted,
            };
            Frame::new()
                .fill(p.surface)
                .stroke(Stroke::new(1.0, p.border))
                .corner_radius(CornerRadius::same(12))
                .inner_margin(Margin::symmetric(8, 3))
                .show(ui, |ui| {
                    ui.horizontal(|ui| {
                        if state == "idle" {
                            dot(ui, p.folk(&m.key).gamma_multiply(0.55), 4.0);
                        } else {
                            pulse_dot(ui, c, 4.0);
                        }
                        ui.label(RichText::new(&m.name).small().color(p.text));
                        if state != "idle" {
                            ui.label(RichText::new(state).small().color(c));
                        }
                    });
                })
                .response
                .on_hover_text(&m.title);
        }
    });
}

fn stepper(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    let current = st.stage.as_deref().map(stage_index);
    let (rect, _) =
        ui.allocate_exact_size(egui::vec2(ui.available_width(), 34.0), egui::Sense::hover());
    let painter = ui.painter();
    let n = STAGES.len();
    let step = rect.width() / n as f32;
    let y = rect.top() + 10.0;
    for (i, name) in STAGES.iter().enumerate() {
        let x = rect.left() + step * (i as f32 + 0.5);
        let done = current.map(|c| i <= c).unwrap_or(false);
        let active = current == Some(i);
        if i + 1 < n {
            let next_done = current.map(|c| i < c).unwrap_or(false);
            painter.line_segment(
                [egui::pos2(x + 7.0, y), egui::pos2(x + step - 7.0, y)],
                Stroke::new(2.0, if next_done { p.accent } else { p.border }),
            );
        }
        let fill = if done { p.accent } else { p.surface };
        painter.circle(
            egui::pos2(x, y),
            if active { 7.0 } else { 5.5 },
            fill,
            Stroke::new(1.5, if done { p.accent } else { p.border }),
        );
        painter.text(
            egui::pos2(x, y + 15.0),
            egui::Align2::CENTER_CENTER,
            name,
            egui::FontId::proportional(10.5),
            if done { p.text } else { p.text_muted },
        );
    }
}

fn chips(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    ui.horizontal_wrapped(|ui| {
        for c in st.chips.iter().rev().take(4) {
            let (fg, bg) = match c.state.as_str() {
                "started" => (p.info, p.accent_soft),
                "failed" => (Color32::WHITE, p.danger),
                _ => (p.text_muted, p.surface_alt),
            };
            let text = match (&c.state[..], &c.summary) {
                ("started", _) => {
                    format!("{} · {}…", st.folk_name(&c.folk), c.tool.replace('_', " "))
                }
                (_, Some(sum)) => format!("{} · {}", c.tool.replace('_', " "), truncate(sum, 48)),
                _ => format!("{} · {}", c.tool.replace('_', " "), c.state),
            };
            let r = widgets::pill(ui, &text, fg, bg);
            if let Some(sum) = &c.summary {
                r.on_hover_text(sum);
            }
        }
    });
}

fn empty_state(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    ui.add_space(24.0);
    ui.vertical_centered(|ui| {
        ui.label(RichText::new("Tell the crew what to design").size(16.0).strong());
        ui.label(RichText::new("Planner answers by default. Mention @Surveyor, @Checker or @Drafter to address one folk.").small().color(p.text_muted));
    });
    ui.add_space(8.0);
}

fn bubble(ui: &mut Ui, st: &AppState, m: &crate::state::ChatMessage) {
    let p = st.palette;
    match m.kind.as_str() {
        "tool" => {
            ui.horizontal_wrapped(|ui| {
                ui.add_space(14.0);
                let is_err = m.json["isError"].as_bool() == Some(true);
                dot(ui, if is_err { p.danger } else { p.success }, 3.0);
                ui.label(
                    RichText::new(format!(
                        "{} · {}",
                        m.json["tool"].as_str().unwrap_or("tool").replace('_', " "),
                        m.text
                    ))
                    .small()
                    .color(p.text_muted),
                );
            });
        }
        "handoff" => {
            ui.vertical_centered(|ui| {
                ui.label(
                    RichText::new(format!("↳ {}", m.text))
                        .small()
                        .italics()
                        .color(p.text_muted),
                );
            });
        }
        "error" => {
            tinted_card(ui, p.danger.gamma_multiply(0.12), p.danger, |ui| {
                ui.label(RichText::new(&m.text).color(p.danger));
            });
        }
        _ => {
            let is_user = m.role == "user";
            let fill = if is_user {
                p.bubble_user
            } else {
                p.bubble_folk
            };
            let name = if is_user {
                "You".to_string()
            } else {
                m.folk
                    .as_deref()
                    .map(|k| st.folk_name(k))
                    .unwrap_or_else(|| "System".into())
            };
            let accent = if is_user {
                p.accent
            } else {
                m.folk.as_deref().map(|k| p.folk(k)).unwrap_or(p.text_muted)
            };
            let width = ui.available_width();
            ui.horizontal(|ui| {
                if is_user {
                    ui.add_space(width * 0.12);
                }
                Frame::new()
                    .fill(fill)
                    .corner_radius(CornerRadius::same(10))
                    .inner_margin(Margin::symmetric(12, 8))
                    .show(ui, |ui| {
                        ui.set_max_width(width * 0.88);
                        ui.horizontal(|ui| {
                            dot(ui, accent, 4.0);
                            ui.label(RichText::new(name).small().strong().color(accent));
                            if m.at > 0 {
                                ui.label(
                                    RichText::new(clock_time(m.at)).small().color(p.text_muted),
                                );
                            }
                            if m.streaming() {
                                pulse_dot(ui, accent, 3.0);
                            }
                            if let Some(cost) = m.json["costUsd"].as_f64() {
                                ui.with_layout(
                                    egui::Layout::right_to_left(egui::Align::Center),
                                    |ui| {
                                        ui.label(
                                            RichText::new(format!("${cost:.3}"))
                                                .small()
                                                .color(p.text_muted),
                                        );
                                    },
                                );
                            }
                        });
                        if m.text.is_empty() && m.streaming() {
                            ui.label(RichText::new("…").color(p.text_muted));
                        } else if is_user {
                            ui.label(&m.text);
                        } else {
                            markdown::render(ui, &p, &m.text);
                        }
                    });
            });
            ui.add_space(6.0);
        }
    }
}

fn composer_ui(ui: &mut Ui, st: &mut AppState, composer: &mut Composer) {
    let p = st.palette;
    // quick actions
    let mut quick: Option<String> = None;
    ui.horizontal_wrapped(|ui| {
        if st.project.is_some() {
            if st.design.as_ref().map(|d| d.by_kind("fat").next().is_none()).unwrap_or(true) {
                if ghost_button(ui, &p, "Design this area").clicked() {
                    quick = Some("Design this area with the current rulebook: import the data, build the twin, place POP, FDHs and FATs, route everything, validate and give me the BOQ.".into());
                }
            } else {
                if ghost_button(ui, &p, "Validate").clicked() {
                    quick = Some("@Checker please validate the current design and list any violations.".into());
                }
                if ghost_button(ui, &p, "Scenarios").clicked() {
                    quick = Some("Generate design scenarios and show me the cost table.".into());
                }
                if ghost_button(ui, &p, "BOQ").clicked() {
                    quick = Some("@Drafter give me the bill of quantities and the splice schedule.".into());
                }
            }
            if !st.selection.is_empty() && ghost_button(ui, &p, "Why here?").clicked() {
                let names: Vec<String> = st.selection.iter().map(|u| st.element_name(u)).collect();
                quick = Some(format!("Why is {} placed there?", names.join(", ")));
            }
        }
    });
    if let Some(q) = quick {
        st.send(&q);
    }

    section_title(
        ui,
        &p,
        if st.selection.is_empty() {
            "Message"
        } else {
            "Message · with selection"
        },
    );
    let shortcut = KeyboardShortcut::new(Modifiers::SHIFT, Key::Enter);
    let response = ui.add(
        egui::TextEdit::multiline(&mut composer.text)
            .desired_rows(2)
            .desired_width(f32::INFINITY)
            .hint_text(if st.project.is_some() {
                "Tell the crew what to do… (Enter to send, Shift+Enter for a new line)"
            } else {
                "Open a project to start"
            })
            .return_key(Some(shortcut))
            .interactive(st.project.is_some()),
    );
    if composer.want_focus {
        response.request_focus();
        composer.want_focus = false;
    }
    let enter =
        response.lost_focus() && ui.input(|i| i.key_pressed(Key::Enter) && !i.modifiers.shift);
    ui.horizontal(|ui| {
        let can_send = st.project.is_some() && !composer.text.trim().is_empty();
        let send_clicked = ui
            .add_enabled_ui(can_send, |ui| primary_button(ui, &p, "Send"))
            .inner
            .clicked();
        if (send_clicked || (enter && can_send)) && can_send {
            let text = composer.text.clone();
            st.send(&text);
            composer.text.clear();
            composer.want_focus = true;
        } else if enter {
            composer.want_focus = true;
        }
        if st.busy() && ghost_button(ui, &p, "Stop").clicked() {
            st.interrupt();
        }
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if !st.selection.is_empty() {
                let names: Vec<String> = st.selection.iter().map(|u| st.element_name(u)).collect();
                widgets::pill(
                    ui,
                    &format!("selection: {}", truncate(&names.join(", "), 40)),
                    p.accent,
                    p.accent_soft,
                )
                .on_hover_text("Travels with your next message as design context. Esc clears it.");
            }
            let n_hist = st.approvals.iter().filter(|a| a.state != "pending").count();
            if n_hist > 0
                && ui
                    .small_button(format!("{n_hist} past approvals"))
                    .clicked()
            {
                composer.show_approval_history = !composer.show_approval_history;
            }
        });
    });
    if composer.show_approval_history {
        egui::ScrollArea::vertical()
            .id_salt("approval-history")
            .max_height(120.0)
            .show(ui, |ui| {
                for a in st.approvals.iter().filter(|a| a.state != "pending").rev() {
                    ui.horizontal(|ui| {
                        let c = match a.state.as_str() {
                            "allowed" => p.success,
                            "denied" => p.danger,
                            _ => p.text_muted,
                        };
                        dot(ui, c, 3.0);
                        ui.label(
                            RichText::new(format!("{} · {} · {}", a.state, a.tool, a.summary))
                                .small(),
                        );
                    });
                }
            });
    }
}

fn truncate(text: &str, n: usize) -> String {
    if text.chars().count() <= n {
        text.to_string()
    } else {
        format!("{}…", text.chars().take(n).collect::<String>())
    }
}
