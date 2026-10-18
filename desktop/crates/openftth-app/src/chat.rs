//! The crew panel: presence, stage progress, approvals, the thread, and the composer.

use egui::{vec2, CursorIcon, Frame, Key, KeyboardShortcut, Margin, Modifiers, Sense, Stroke, Ui};

use crate::icons::{self, Icon};
use crate::markdown;
use crate::state::{stage_index, AppState, ChatMessage, STAGES};
use crate::theme::{Weight, R};
use crate::widgets::{
    self, button, button_ex, caps, clock_time, dot, hairline, icon_button_sized, mono_text,
    panel_header, pulse_dot, segments, tag, text, Kind,
};

pub const PROMPT_DESIGN: &str = "Design this area with the current rulebook: import the data, build the twin, place POP, FDHs and FATs, route everything, validate and give me the BOQ.";
pub const PROMPT_VALIDATE: &str = "@Checker validate the current design and list any violations.";
pub const PROMPT_SCENARIOS: &str = "Generate design scenarios and show me the cost table.";
pub const PROMPT_BOQ: &str = "@Drafter give me the bill of quantities and the splice schedule.";

#[derive(Default)]
pub struct Composer {
    pub text: String,
    pub want_focus: bool,
    pub show_history: bool,
}

/// Something the composer asks the shell to do.
pub enum ChatAction {
    ConfigureEngine(String),
}

pub fn chat_panel(ui: &mut Ui, st: &mut AppState, composer: &mut Composer) -> Option<ChatAction> {
    let mut action = None;
    let p = st.palette;
    Frame::new()
        .inner_margin(Margin::symmetric(10, 0))
        .show(ui, |ui| {
            panel_header(ui, &p, "Crew", |ui| {
                let n = st.pending_approvals().count();
                if n > 0 {
                    tag(
                        ui,
                        &format!("{n} waiting"),
                        p.amber,
                        p.amber.gamma_multiply(0.2),
                    );
                }
            });
            ui.add_space(6.0);
            presence(ui, st);
            ui.add_space(6.0);
            stage_row(ui, st);
            ui.add_space(4.0);
            hairline(ui, &p);
        });

    egui::Panel::bottom("composer")
        .resizable(false)
        .show_separator_line(false)
        .frame(
            Frame::new()
                .fill(p.bg1)
                .inner_margin(Margin::symmetric(10, 8)),
        )
        .show(ui, |ui| {
            action = composer_ui(ui, st, composer);
        });

    egui::ScrollArea::vertical().id_salt("thread").auto_shrink([false, false]).stick_to_bottom(true).show(ui, |ui| {
        Frame::new().inner_margin(Margin::symmetric(10, 6)).show(ui, |ui| {
            let pending: Vec<_> = st.pending_approvals().cloned().collect();
            let mut decision: Option<(String, bool)> = None;
            for a in &pending {
                approval_card(ui, st, a, &mut decision);
            }
            if let Some((id, allow)) = decision {
                st.decide(&id, allow);
            }
            if !st.chips.is_empty() {
                chips(ui, st);
            }
            if st.messages.is_empty() {
                ui.add_space(18.0);
                ui.vertical_centered(|ui| {
                    ui.label(text("No messages yet", 12.5, Weight::Medium, p.text2));
                    ui.label(text("Planner answers by default. Address a folk with @Surveyor, @Checker or @Drafter.", 11.5, Weight::Regular, p.text3));
                });
            }
            let messages = st.messages.clone();
            for m in &messages {
                message_row(ui, st, m);
            }
            if st.busy() && !st.messages.iter().any(|m| m.streaming()) {
                ui.horizontal(|ui| {
                    ui.add_space(18.0);
                    pulse_dot(ui, p.accent, 3.0);
                    let who = st.folk_states.iter().find(|(_, v)| *v != "idle").map(|(k, _)| st.folk_name(k)).unwrap_or_else(|| "Crew".into());
                    ui.label(text(format!("{who} is working"), 11.5, Weight::Regular, p.text3));
                });
            }
        });
    });
    action
}

fn presence(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    ui.horizontal(|ui| {
        ui.spacing_mut().item_spacing.x = 12.0;
        for m in &st.crew {
            let state = st
                .folk_states
                .get(&m.key)
                .cloned()
                .unwrap_or_else(|| "idle".into());
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 5.0;
                let c = p.folk(&m.key);
                if state == "idle" {
                    dot(ui, c.gamma_multiply(0.7), 3.5);
                } else {
                    pulse_dot(ui, c, 3.5);
                }
                ui.label(text(
                    &m.name,
                    12.0,
                    Weight::Medium,
                    if state == "idle" { p.text2 } else { p.text },
                ));
                if state != "idle" {
                    ui.label(text(&state, 11.0, Weight::Regular, p.text3));
                }
            })
            .response
            .on_hover_text(&m.title);
        }
    });
}

fn stage_row(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    let idx = st.stage.as_deref().map(stage_index);
    ui.horizontal(|ui| {
        let label = match (idx, &st.stage) {
            (Some(i), Some(s)) => format!("{s}  {}/{}", i + 1, STAGES.len()),
            _ => "not started".into(),
        };
        let w = ui.available_width() - 96.0;
        segments(ui, &p, STAGES.len(), idx, w.max(80.0));
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.label(text(label, 11.0, Weight::Regular, p.text3));
        });
    });
}

fn chips(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    ui.horizontal_wrapped(|ui| {
        for c in st.chips.iter().rev().take(3) {
            let (fg, bg) = match c.state.as_str() {
                "started" => (p.accent, p.accent_soft),
                "failed" => (p.red, p.red.gamma_multiply(0.2)),
                _ => (p.text2, p.bg3),
            };
            let label = format!("{} · {}", st.folk_name(&c.folk), c.tool.replace('_', " "));
            let r = tag(ui, &label, fg, bg);
            if let Some(s) = &c.summary {
                r.on_hover_text(s);
            }
        }
    });
    ui.add_space(4.0);
}

fn approval_card(
    ui: &mut Ui,
    st: &mut AppState,
    a: &crate::state::ApprovalCard,
    decision: &mut Option<(String, bool)>,
) {
    let p = st.palette;
    Frame::new()
        .fill(p.bg2)
        .stroke(Stroke::new(1.0, p.amber.gamma_multiply(0.6)))
        .corner_radius(R as f32)
        .inner_margin(Margin::symmetric(10, 8))
        .show(ui, |ui| {
            ui.horizontal(|ui| {
                dot(ui, p.amber, 3.5);
                ui.label(text("Approval", 12.0, Weight::Semibold, p.text));
                ui.label(text(st.folk_name(&a.folk), 11.5, Weight::Regular, p.text3));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.label(text(
                        widgets::relative_time(a.created_at),
                        11.0,
                        Weight::Regular,
                        p.text3,
                    ));
                });
            });
            ui.add_space(2.0);
            ui.label(text(&a.summary, 13.0, Weight::Regular, p.text));
            ui.horizontal_wrapped(|ui| {
                ui.label(mono_text(&a.tool, 11.0, p.text3));
                if let Some(obj) = a.input.as_object() {
                    for (k, v) in obj.iter().filter(|(k, _)| *k != "approval_id").take(5) {
                        ui.label(mono_text(
                            format!("{k}={}", crate::state::s(v)),
                            11.0,
                            p.text3,
                        ));
                    }
                }
            });
            if let Some(card) = st.approvals.iter_mut().find(|x| x.id == a.id) {
                ui.add(
                    egui::TextEdit::singleline(&mut card.note)
                        .hint_text("Note for the audit log")
                        .desired_width(f32::INFINITY),
                );
            }
            ui.add_space(2.0);
            ui.horizontal(|ui| {
                if button(ui, &p, "Allow", Kind::Prominent).clicked() {
                    *decision = Some((a.id.clone(), true));
                }
                if button(ui, &p, "Deny", Kind::Quiet).clicked() {
                    *decision = Some((a.id.clone(), false));
                }
            });
        });
    ui.add_space(6.0);
}

fn message_row(ui: &mut Ui, st: &AppState, m: &ChatMessage) {
    let p = st.palette;
    match m.kind.as_str() {
        "tool" => {
            ui.horizontal(|ui| {
                ui.add_space(18.0);
                let is_err = m.json["isError"].as_bool() == Some(true);
                let (r, _) = ui.allocate_exact_size(vec2(12.0, 12.0), Sense::hover());
                crate::icons::paint(
                    ui.painter(),
                    r,
                    Icon::Tool,
                    if is_err { p.red } else { p.text3 },
                );
                let tool = m.json["tool"].as_str().unwrap_or("tool");
                let detail =
                    if m.text == format!("{tool} done") || m.text == format!("{tool} failed") {
                        if is_err { "failed" } else { "done" }.to_string()
                    } else {
                        m.text.clone()
                    };
                ui.label(mono_text(tool, 11.0, p.text3));
                ui.add(
                    egui::Label::new(text(
                        &detail,
                        11.5,
                        Weight::Regular,
                        if is_err { p.red } else { p.text2 },
                    ))
                    .truncate(),
                );
            });
        }
        "handoff" => {
            ui.horizontal(|ui| {
                ui.add_space(18.0);
                let (r, _) = ui.allocate_exact_size(vec2(12.0, 12.0), Sense::hover());
                crate::icons::paint(ui.painter(), r, Icon::ChevronRight, p.text3);
                ui.label(text(&m.text, 11.5, Weight::Regular, p.text3));
            });
        }
        "error" => {
            ui.horizontal(|ui| {
                let (r, _) = ui.allocate_exact_size(vec2(14.0, 14.0), Sense::hover());
                crate::icons::paint(ui.painter(), r, Icon::Warning, p.red);
                ui.add(egui::Label::new(text(&m.text, 12.0, Weight::Regular, p.red)).wrap());
            });
        }
        _ => {
            let is_user = m.role == "user";
            let name = if is_user {
                "You".to_string()
            } else {
                m.folk
                    .as_deref()
                    .map(|k| st.folk_name(k))
                    .unwrap_or_else(|| "System".into())
            };
            let accent = if is_user {
                p.text2
            } else {
                m.folk.as_deref().map(|k| p.folk(k)).unwrap_or(p.text3)
            };
            ui.add_space(6.0);
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                ui.add_space(2.0);
                ui.vertical(|ui| {
                    ui.add_space(5.0);
                    dot(ui, accent, 3.5);
                });
                ui.vertical(|ui| {
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 6.0;
                        ui.label(text(
                            name,
                            12.0,
                            Weight::Semibold,
                            if is_user { p.text } else { accent },
                        ));
                        if m.at > 0 {
                            ui.label(text(clock_time(m.at), 11.0, Weight::Regular, p.text3));
                        }
                        if m.streaming() {
                            pulse_dot(ui, accent, 2.5);
                        }
                        if let Some(cost) = m.json["costUsd"].as_f64() {
                            ui.label(mono_text(format!("${cost:.3}"), 10.5, p.text3));
                        }
                    });
                    if m.text.is_empty() && m.streaming() {
                        ui.label(text("…", 13.0, Weight::Regular, p.text3));
                    } else if is_user {
                        ui.add(
                            egui::Label::new(text(&m.text, 13.0, Weight::Regular, p.text)).wrap(),
                        );
                    } else {
                        markdown::render(ui, &p, &m.text);
                    }
                });
            });
        }
    }
}

fn composer_ui(ui: &mut Ui, st: &mut AppState, composer: &mut Composer) -> Option<ChatAction> {
    let p = st.palette;
    let open = st.project.is_some();
    let mut action: Option<ChatAction> = None;
    let mut quick: Option<String> = None;
    ui.horizontal_wrapped(|ui| {
        if open {
            let designed = st
                .design
                .as_ref()
                .map(|d| d.by_kind("fat").next().is_some())
                .unwrap_or(false);
            if !designed {
                if button(ui, &p, "Design this area", Kind::Quiet).clicked() {
                    quick = Some(PROMPT_DESIGN.into());
                }
            } else {
                if button(ui, &p, "Validate", Kind::Quiet).clicked() {
                    quick = Some(PROMPT_VALIDATE.into());
                }
                if button(ui, &p, "Scenarios", Kind::Quiet).clicked() {
                    quick = Some(PROMPT_SCENARIOS.into());
                }
                if button(ui, &p, "BOQ", Kind::Quiet).clicked() {
                    quick = Some(PROMPT_BOQ.into());
                }
            }
            if !st.selection.is_empty() && button(ui, &p, "Why here?", Kind::Quiet).clicked() {
                let names: Vec<String> = st.selection.iter().map(|u| st.element_name(u)).collect();
                quick = Some(format!("Why is {} placed there?", names.join(", ")));
            }
        }
    });
    if let Some(q) = quick {
        st.send(&q);
    }
    ui.add_space(4.0);
    let shortcut = KeyboardShortcut::new(Modifiers::SHIFT, Key::Enter);
    let mut send_now = false;
    Frame::new()
        .fill(p.bg2)
        .stroke(Stroke::new(1.0, p.line))
        .corner_radius(R as f32)
        .inner_margin(Margin {
            left: 10,
            right: 8,
            top: 8,
            bottom: 6,
        })
        .show(ui, |ui| {
            let response = ui.add_sized(
                [ui.available_width(), 0.0],
                egui::TextEdit::multiline(&mut composer.text)
                    .frame(Frame::NONE)
                    .desired_rows(2)
                    .hint_text(if open {
                        "Message the crew"
                    } else {
                        "Open a project first"
                    })
                    .return_key(Some(shortcut))
                    .interactive(open)
                    .font(crate::theme::font(13.0, Weight::Regular)),
            );
            if composer.want_focus {
                response.request_focus();
                composer.want_focus = false;
            }
            let enter = response.lost_focus()
                && ui.input(|i| i.key_pressed(Key::Enter) && !i.modifiers.shift);
            let can_send = open && !composer.text.trim().is_empty();
            if enter {
                composer.want_focus = true;
                send_now = can_send;
            }
            ui.add_space(4.0);
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 6.0;
                action = engine_chip(ui, st);
                if !st.selection.is_empty() {
                    let names: Vec<String> =
                        st.selection.iter().map(|u| st.element_name(u)).collect();
                    tag(
                        ui,
                        &format!("with {}", widgets::truncate(&names.join(", "), 30)),
                        p.accent,
                        p.accent_soft,
                    )
                    .on_hover_text("The selection travels with your next message. Esc clears it.");
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if st.busy() {
                        if icon_button_sized(
                            ui,
                            &p,
                            Icon::Stop,
                            "Stop the current turn",
                            26.0,
                            true,
                        )
                        .clicked()
                        {
                            st.interrupt();
                        }
                    } else {
                        let (r, resp) = ui.allocate_exact_size(
                            vec2(26.0, 26.0),
                            if can_send {
                                Sense::click()
                            } else {
                                Sense::hover()
                            },
                        );
                        let fill = if can_send {
                            if resp.hovered() {
                                p.accent.gamma_multiply(1.15)
                            } else {
                                p.accent
                            }
                        } else {
                            p.bg3
                        };
                        ui.painter().rect_filled(r, R as f32, fill);
                        icons::paint(
                            ui.painter(),
                            icons::square(r.center(), 14.0),
                            Icon::Send,
                            if can_send {
                                egui::Color32::WHITE
                            } else {
                                p.text3
                            },
                        );
                        if can_send && resp.on_hover_cursor(CursorIcon::PointingHand).clicked() {
                            send_now = true;
                        }
                    }
                    ui.label(text(
                        if can_send { "Enter to send" } else { "" },
                        10.5,
                        Weight::Regular,
                        p.text3,
                    ));
                });
            });
        });
    if send_now {
        let t = composer.text.clone();
        st.send(&t);
        composer.text.clear();
        composer.want_focus = true;
    }
    let past = st.approvals.iter().filter(|a| a.state != "pending").count();
    if past > 0 {
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if button_ex(
                ui,
                &p,
                None,
                &format!("{past} past approval{}", if past == 1 { "" } else { "s" }),
                Kind::Ghost,
                true,
            )
            .clicked()
            {
                composer.show_history = !composer.show_history;
            }
        });
    }
    if composer.show_history && past > 0 {
        egui::ScrollArea::vertical()
            .id_salt("approval-history")
            .max_height(110.0)
            .show(ui, |ui| {
                for a in st.approvals.iter().filter(|a| a.state != "pending").rev() {
                    ui.horizontal(|ui| {
                        let (icon, c) = match a.state.as_str() {
                            "allowed" => (Icon::Check, p.green),
                            "denied" => (Icon::Close, p.red),
                            _ => (Icon::Stop, p.text3),
                        };
                        let (r, _) = ui.allocate_exact_size(vec2(12.0, 12.0), Sense::hover());
                        crate::icons::paint(ui.painter(), r, icon, c);
                        ui.label(text(
                            format!("{} · {} · {}", a.state, a.tool, a.summary),
                            11.5,
                            Weight::Regular,
                            p.text2,
                        ));
                    });
                }
            });
    }
    let _ = caps;
    action
}

/// The engine chip in the composer: the runtime the crew runs on, and a menu to switch it.
fn engine_chip(ui: &mut Ui, st: &mut AppState) -> Option<ChatAction> {
    let p = st.palette;
    let mut action = None;
    let current = st.engine.clone();
    let info = current
        .as_ref()
        .and_then(|e| st.engines.iter().find(|x| x.kind == e.kind).cloned());
    let ok = info.as_ref().map(|x| x.available).unwrap_or(false);
    let label = match &current {
        Some(e) => match &e.model {
            Some(m) if !m.is_empty() => format!("{}  ·  {}", e.label, widgets::truncate(m, 22)),
            _ => e.label.clone(),
        },
        None => "Choose an engine".to_string(),
    };
    let kind = current.as_ref().map(|e| e.kind.clone()).unwrap_or_default();
    let galley = ui.painter().layout_no_wrap(
        label.clone(),
        crate::theme::font(12.0, Weight::Medium),
        p.text,
    );
    let w = 8.0 + 16.0 + 6.0 + galley.size().x + 4.0 + 12.0 + 8.0;
    let (rect, resp) = ui.allocate_exact_size(vec2(w, 24.0), Sense::click());
    let hovered = resp.hovered();
    ui.painter().rect(
        rect,
        R as f32,
        if hovered { p.bg3 } else { p.bg2 },
        Stroke::new(1.0, if hovered { p.line_strong } else { p.line }),
        egui::StrokeKind::Inside,
    );
    icons::paint_engine_mark(
        ui.painter(),
        egui::Rect::from_center_size(
            egui::pos2(rect.left() + 16.0, rect.center().y),
            vec2(16.0, 16.0),
        ),
        &kind,
        &p,
    );
    ui.painter().galley(
        egui::pos2(rect.left() + 30.0, rect.center().y - galley.size().y / 2.0),
        galley,
        if ok { p.text } else { p.amber },
    );
    icons::paint(
        ui.painter(),
        icons::square(egui::pos2(rect.right() - 14.0, rect.center().y), 12.0),
        Icon::ChevronDown,
        p.text3,
    );
    let resp = resp
        .on_hover_cursor(CursorIcon::PointingHand)
        .on_hover_text(match &info {
            Some(i) => format!("{}\n{}", i.description, i.detail),
            None => "The AI runtime the crew runs on.".to_string(),
        });
    egui::Popup::menu(&resp)
        .id(ui.make_persistent_id("engine-chip-menu"))
        .layout(egui::Layout::top_down(egui::Align::Min))
        .show(|ui| {
            widgets::menu_begin(ui, 300.0);
            ui.label(caps("Crew engine", p.text3));
            ui.add_space(2.0);
            let engines = st.engines.clone();
            let mut pick: Option<(String, Option<String>)> = None;
            let mut configure: Option<String> = None;
            for e in &engines {
                let detail = if e.available {
                    match &e.model {
                        Some(m) if !m.is_empty() => m.clone(),
                        _ => "ready".to_string(),
                    }
                } else {
                    e.detail.clone()
                };
                let needs_form = e.needs.iter().any(|n| n == "apiKey" || n == "baseUrl");
                let row = widgets::menu_row_mark(ui, &p, &e.kind, &e.label, &detail, e.current);
                if row {
                    if needs_form || !e.available || (e.kind == "ollama" && e.models.len() > 1) {
                        configure = Some(e.kind.clone());
                    } else {
                        pick = Some((e.kind.clone(), None));
                    }
                }
            }
            widgets::menu_separator(ui, &p);
            if widgets::menu_item(ui, &p, "Engine settings…", None, true) {
                configure = Some(
                    current
                        .as_ref()
                        .map(|e| e.kind.clone())
                        .unwrap_or_else(|| "claude".into()),
                );
            }
            if let Some((kind, model)) = pick {
                st.set_engine(&kind, model.as_deref(), None, None);
            }
            if let Some(kind) = configure {
                action = Some(ChatAction::ConfigureEngine(kind));
            }
        });
    action
}
