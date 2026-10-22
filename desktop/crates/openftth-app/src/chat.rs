//! The crew panel: presence, stage progress, approvals, the thread, and the composer.

use egui::{vec2, CursorIcon, Frame, Key, KeyboardShortcut, Margin, Modifiers, Sense, Stroke, Ui};

use crate::icons::{self, Icon};
use crate::markdown;
use crate::state::{stage_index, AppState, ChatMessage, STAGES};
use crate::theme::{Weight, R};
use crate::widgets::{
    self, button, button_ex, caps, clock_time, dot, hairline, mono_text, panel_header, pulse_dot,
    tag, text, Kind,
};

pub const PROMPT_DESIGN: &str = "Design this area with the current rulebook: import the data, build the twin, place POP, FDHs and FATs, route everything, validate and give me the BOQ.";
pub const PROMPT_VALIDATE: &str = "@Checker validate the current design and list any violations.";
pub const PROMPT_SCENARIOS: &str = "Generate design scenarios and show me the cost table.";
pub const PROMPT_BOQ: &str = "@Drafter give me the bill of quantities and the splice schedule.";
pub const PROMPT_DELIVER: &str = "@Drafter produce every deliverable for the current design: bill of quantities, splice schedule, GeoPackage and the map.";

#[derive(Default)]
pub struct Composer {
    pub text: String,
    pub want_focus: bool,
    pub show_history: bool,
    /// An "@name" being typed at the end of the text: (start byte, partial), from last frame.
    pub mention: Option<(usize, String)>,
    pub mention_pick: usize,
    pub caret_to_end: bool,
    /// Measured height of each conversation row (by row key), so rows far from the visible
    /// part are skipped and only their space is reserved.
    pub row_heights: std::collections::HashMap<String, f32>,
    /// Folded groups of tool steps the user opened (by the group's first message id).
    pub open_groups: std::collections::HashSet<String>,
    /// Windows voice typing was opened from the mic button and not closed since.
    pub dictating: bool,
    /// Open voice typing once the field has focus (it types into the focused field).
    pub dictate_pending: bool,
}

/// Tool calls that are the crew engine's own plumbing, not steps worth showing.
const HIDDEN_TOOLS: [&str; 6] = [
    "ToolSearch",
    "Read",
    "Glob",
    "Grep",
    "approve",
    "ListMcpResourcesTool",
];

/// One row of the conversation: a message, or a run of consecutive tool steps.
enum Row {
    Msg(usize),
    Tools(Vec<usize>),
}

fn conversation_rows(st: &AppState) -> Vec<Row> {
    let mut rows = Vec::new();
    let mut run: Vec<usize> = Vec::new();
    for (i, m) in st.messages.iter().enumerate() {
        if m.kind == "tool" {
            let tool = m.json["tool"].as_str().unwrap_or("");
            if !HIDDEN_TOOLS.contains(&tool) {
                run.push(i);
            }
            continue;
        }
        if !run.is_empty() {
            rows.push(Row::Tools(std::mem::take(&mut run)));
        }
        rows.push(Row::Msg(i));
    }
    if !run.is_empty() {
        rows.push(Row::Tools(run));
    }
    rows
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
            let mut mention: Option<String> = None;
            panel_header(ui, &p, "Crew", |ui| {
                let members: Vec<widgets::Member> = st
                    .crew
                    .iter()
                    .map(|m| {
                        let state = st
                            .folk_states
                            .get(&m.key)
                            .cloned()
                            .unwrap_or_else(|| "idle".into());
                        widgets::Member {
                            key: m.key.clone(),
                            name: m.name.clone(),
                            title: m.title.clone(),
                            state,
                        }
                    })
                    .collect();
                // Right to left: the stack sits at the edge, the waiting tag before it.
                if let Some(k) = widgets::avatar_stack(ui, &p, &members) {
                    mention = Some(k);
                }
                let n = st.pending_approvals().count();
                if n > 0 {
                    ui.add_space(6.0);
                    tag(
                        ui,
                        &format!("{n} waiting"),
                        p.amber,
                        p.amber.gamma_multiply(0.2),
                    );
                }
            });
            if let Some(name) = mention {
                if !composer.text.is_empty() && !composer.text.ends_with(' ') {
                    composer.text.push(' ');
                }
                composer.text.push('@');
                composer.text.push_str(&name);
                composer.text.push(' ');
                composer.caret_to_end = true;
            }
            ui.add_space(2.0);
            status_line(ui, st);
            ui.add_space(8.0);
            let idx = st.stage.as_deref().map(stage_index);
            let working = st.busy() || st.chips.iter().any(|c| c.state == "started");
            widgets::stage_bar(ui, &p, &STAGES, idx, working);
            ui.add_space(10.0);
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

    egui::ScrollArea::vertical().id_salt("thread").auto_shrink([false, false]).stick_to_bottom(true).show_viewport(ui, |ui, viewport| {
        Frame::new().inner_margin(Margin::symmetric(10, 6)).show(ui, |ui| {
            if st.messages.is_empty() {
                ui.add_space(18.0);
                ui.vertical_centered(|ui| {
                    ui.label(text("No messages yet", 12.5, Weight::Medium, p.text2));
                    ui.label(text("Press Design automatically for a free design by the engine alone, or ask the crew. Planner answers by default; address others with @Surveyor, @Checker or @Drafter.", 11.5, Weight::Regular, p.text3));
                });
            }
            // Only rows near the visible part are drawn; the rest keep their measured height.
            let origin = ui.min_rect().top();
            let width = ui.available_width().round() as i32;
            let visible = viewport.expand2(vec2(0.0, 600.0));
            for row in conversation_rows(st) {
                let key = match &row {
                    Row::Msg(i) => {
                        let m = &st.messages[*i];
                        format!("{}:{}:{}:{width}", m.id, m.text.len(), m.streaming())
                    }
                    Row::Tools(ix) => {
                        let first = &st.messages[ix[0]].id;
                        let open = composer.open_groups.contains(first);
                        format!("tools:{first}:{}:{open}:{width}", ix.len())
                    }
                };
                let y = ui.cursor().top() - origin;
                if let Some(h) = composer.row_heights.get(&key).copied() {
                    if y + h < visible.min.y || y > visible.max.y {
                        ui.add_space(h + ui.spacing().item_spacing.y);
                        continue;
                    }
                }
                let top = ui.cursor().top();
                match row {
                    Row::Msg(i) => message_row(ui, st, &st.messages[i]),
                    Row::Tools(ix) => tool_group(ui, st, composer, &ix),
                }
                let h = ui.cursor().top() - top - ui.spacing().item_spacing.y;
                composer.row_heights.insert(key, h.max(0.0));
            }
            if composer.row_heights.len() > 4000 {
                composer.row_heights.clear();
            }
            // Approvals and live tool chips sit at the end, where the conversation is read.
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

/// Consecutive tool steps: one line with the latest step, opening to the full list.
fn tool_group(ui: &mut Ui, st: &AppState, composer: &mut Composer, ix: &[usize]) {
    let p = st.palette;
    if ix.len() < 3 {
        for &i in ix {
            message_row(ui, st, &st.messages[i]);
        }
        return;
    }
    let first = st.messages[ix[0]].id.clone();
    let open = composer.open_groups.contains(&first);
    let failed = ix
        .iter()
        .filter(|&&i| st.messages[i].json["isError"].as_bool() == Some(true))
        .count();
    let last = &st.messages[*ix.last().unwrap_or(&ix[0])];
    let resp = ui
        .horizontal(|ui| {
            ui.add_space(14.0);
            let (r, _) = ui.allocate_exact_size(vec2(12.0, 12.0), Sense::hover());
            icons::paint(
                ui.painter(),
                r,
                if open {
                    Icon::ChevronDown
                } else {
                    Icon::ChevronRight
                },
                p.text3,
            );
            ui.label(text(
                format!(
                    "{} engine steps{}",
                    ix.len(),
                    if failed > 0 {
                        format!(", {failed} failed")
                    } else {
                        String::new()
                    }
                ),
                11.5,
                Weight::Medium,
                if failed > 0 { p.red } else { p.text2 },
            ));
            if !open {
                ui.add(
                    egui::Label::new(text(
                        format!("· {}", last.text),
                        11.5,
                        Weight::Regular,
                        p.text3,
                    ))
                    .truncate(),
                );
            }
        })
        .response
        .interact(Sense::click())
        .on_hover_cursor(CursorIcon::PointingHand);
    if resp.clicked() {
        if open {
            composer.open_groups.remove(&first);
        } else {
            composer.open_groups.insert(first);
        }
    }
    if open {
        for &i in ix {
            message_row(ui, st, &st.messages[i]);
        }
    }
}

/// What a tool does, as a phrase after "is": "placing cabinets".
pub fn tool_phrase(tool: &str) -> String {
    let known = match tool {
        "import_osm" => "importing the area and map",
        "build_twin" => "building the street twin",
        "set_pop" => "placing the POPs",
        "cluster_homes" => "placing FATs",
        "place_fdh" => "placing cabinets",
        "assign_pon" => "grouping homes into PONs",
        "route_feeder" => "routing feeder cables",
        "route_distribution" => "routing distribution cables",
        "route_drops" => "routing drops",
        "place_chambers" => "placing chambers",
        "size_cables" => "sizing cables",
        "assign_fibers" => "assigning fibres",
        "check_rules" => "checking the rules",
        "check_operator" => "checking the operator rules",
        "boq" => "pricing the bill of quantities",
        "splice_schedule" => "writing the splice schedule",
        "export_gpkg" => "exporting the GeoPackage",
        "trace_fibre" => "tracing a fibre",
        "homes_behind" => "finding the homes behind",
        "move_site" => "moving a site",
        "exclude_building" => "excluding a building",
        "forbid_street" => "closing a street to digging",
        "explain_element" => "reading the reasons",
        "design_summary" | "versions" | "diff_versions" => "reviewing the design",
        "get_rulebook" | "explain_rule" => "reading the rulebook",
        "set_rule" => "changing a rule",
        "generate_scenarios" => "comparing scenarios",
        "open_work_order" => "opening a work order",
        "post_work_order" => "posting the work order",
        _ => "",
    };
    if known.is_empty() {
        format!("running {}", tool.replace('_', " "))
    } else {
        known.to_string()
    }
}

/// One line under the title: who is doing what right now, or "Ready".
fn status_line(ui: &mut Ui, st: &AppState) {
    let p = st.palette;
    let started = st.chips.iter().rev().find(|c| c.state == "started");
    let busy_member = st
        .folk_states
        .iter()
        .find(|(_, v)| *v != "idle")
        .map(|(k, v)| (k.clone(), v.clone()));
    let (who, colour, doing) = match (started, busy_member) {
        (Some(c), _) => {
            let who = if c.folk.is_empty() {
                "openFTTH engine".to_string()
            } else {
                st.folk_name(&c.folk)
            };
            let colour = if c.folk.is_empty() {
                p.accent
            } else {
                p.folk(&c.folk)
            };
            (Some(who), colour, format!("is {}…", tool_phrase(&c.tool)))
        }
        (None, Some((k, state))) => {
            let doing = match state.as_str() {
                "waiting" => "is waiting for your approval".to_string(),
                "tooling" => "is using the engine…".to_string(),
                _ => "is thinking…".to_string(),
            };
            (Some(st.folk_name(&k)), p.folk(&k), doing)
        }
        _ => (None, p.text3, String::new()),
    };
    ui.horizontal(|ui| {
        ui.set_height(18.0);
        ui.spacing_mut().item_spacing.x = 6.0;
        match who {
            Some(who) => {
                pulse_dot(ui, colour, 3.0);
                ui.label(text(who, 12.0, Weight::Semibold, p.text));
                ui.add(egui::Label::new(text(doing, 12.0, Weight::Regular, p.text2)).truncate());
            }
            None => {
                dot(ui, p.green.gamma_multiply(0.8), 3.0);
                ui.label(text("Ready", 12.0, Weight::Medium, p.text2));
                ui.label(text(
                    "· ask the crew, or design automatically",
                    11.5,
                    Weight::Regular,
                    p.text3,
                ));
            }
        }
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
        // The engineer's own change, made through the app rather than a crew member.
        "steer" => {
            ui.add_space(6.0);
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                ui.add_space(2.0);
                ui.vertical(|ui| {
                    ui.add_space(2.0);
                    let (r, _) = ui.allocate_exact_size(vec2(13.0, 13.0), Sense::hover());
                    crate::icons::paint(ui.painter(), r, Icon::Tool, p.accent);
                });
                ui.vertical(|ui| {
                    let head = match m.json["tool"].as_str().unwrap_or_default() {
                        "check_rules" | "check_operator" => "You checked the design",
                        "open_work_order" => "You opened a work order",
                        "post_work_order" => "You posted the work order",
                        _ => "You changed the design",
                    };
                    ui.label(text(head, 12.0, Weight::Semibold, p.text));
                    ui.add(egui::Label::new(text(&m.text, 12.0, Weight::Regular, p.text2)).wrap());
                    for w in m.json["warnings"].as_array().into_iter().flatten() {
                        ui.add(
                            egui::Label::new(text(
                                w.as_str().unwrap_or_default(),
                                11.5,
                                Weight::Regular,
                                p.amber,
                            ))
                            .wrap(),
                        );
                    }
                });
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
                    .unwrap_or_else(|| "openFTTH engine".into())
            };
            let accent = if is_user {
                p.text2
            } else {
                m.folk.as_deref().map(|k| p.folk(k)).unwrap_or(p.text3)
            };
            ui.add_space(6.0);
            let fill = if is_user { p.accent_soft } else { p.bg2 };
            Frame::new()
                .fill(fill)
                .corner_radius(8.0)
                .inner_margin(Margin::symmetric(10, 8))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal_top(|ui| {
                        ui.spacing_mut().item_spacing.x = 8.0;
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
                                    ui.label(text(
                                        clock_time(m.at),
                                        11.0,
                                        Weight::Regular,
                                        p.text3,
                                    ));
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
                                    egui::Label::new(text(&m.text, 13.0, Weight::Regular, p.text))
                                        .wrap(),
                                );
                            } else {
                                markdown::render(ui, &p, &m.text);
                            }
                        });
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
                if button(ui, &p, "Design automatically", Kind::Prominent)
                    .on_hover_text("The engine designs the whole area by itself: free, no AI, about a minute or two")
                    .clicked()
                {
                    st.autodesign();
                }
                if button(ui, &p, "Ask the crew", Kind::Quiet)
                    .on_hover_text("The crew runs the same steps and explains each one (uses the crew engine)")
                    .clicked()
                {
                    quick = Some(PROMPT_DESIGN.into());
                }
                if button(ui, &p, "Lay out by hand", Kind::Quiet)
                    .on_hover_text("Prepare the area (free) and place every site and cable yourself; the engine routes, sizes and numbers what you lay out")
                    .clicked()
                {
                    st.lay_out_by_hand();
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
    // The @ picker takes the arrow and accept keys before the text field sees them.
    let mut accept_mention: Option<String> = None;
    if let Some((_, partial)) = composer.mention.clone() {
        let matches = mention_matches(st, &partial);
        if matches.is_empty() {
            composer.mention = None;
        } else {
            let (up, down, enter, tab, esc) = ui.input_mut(|i| {
                (
                    i.consume_key(Modifiers::NONE, Key::ArrowUp),
                    i.consume_key(Modifiers::NONE, Key::ArrowDown),
                    i.consume_key(Modifiers::NONE, Key::Enter),
                    i.consume_key(Modifiers::NONE, Key::Tab),
                    i.consume_key(Modifiers::NONE, Key::Escape),
                )
            });
            if up {
                composer.mention_pick = composer.mention_pick.saturating_sub(1);
            }
            if down {
                composer.mention_pick = (composer.mention_pick + 1).min(matches.len() - 1);
            }
            if esc {
                composer.mention = None;
            }
            if enter || tab {
                accept_mention = matches
                    .get(composer.mention_pick.min(matches.len() - 1))
                    .cloned();
            }
        }
    }
    if let Some(name) = accept_mention.take() {
        apply_mention(composer, &name);
    }
    // Enter sends, Shift+Enter is a new line. The field never loses focus on Enter (Shift+Enter
    // is its newline key), so the key is taken here while the field has focus.
    let field_id = egui::Id::unique("composer-field");
    let field_focused = ui.memory(|m| m.has_focus(field_id));
    if field_focused
        && composer.mention.is_none()
        && ui.input_mut(|i| i.consume_key(Modifiers::NONE, Key::Enter))
        && open
        && !composer.text.trim().is_empty()
    {
        send_now = true;
    }
    let border = if field_focused {
        p.accent.gamma_multiply(0.7)
    } else {
        p.line
    };
    Frame::new()
        .fill(p.bg2)
        .stroke(Stroke::new(1.0, border))
        .corner_radius(12.0)
        .inner_margin(Margin {
            left: 12,
            right: 8,
            top: 10,
            bottom: 8,
        })
        .show(ui, |ui| {
            let response = ui.add_sized(
                [ui.available_width(), 0.0],
                egui::TextEdit::multiline(&mut composer.text)
                    .id(field_id)
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
            if composer.caret_to_end {
                composer.caret_to_end = false;
                if let Some(mut state) = egui::TextEdit::load_state(ui.ctx(), response.id) {
                    let end = egui::text::CCursor::new(composer.text.chars().count());
                    state
                        .cursor
                        .set_char_range(Some(egui::text::CCursorRange::one(end)));
                    state.store(ui.ctx(), response.id);
                }
                response.request_focus();
            }
            composer_mention_state(composer, &response);
            if let Some(name) = mention_picker(ui, st, composer, response.rect) {
                apply_mention(composer, &name);
                composer.want_focus = true;
            }
            let can_send = open && !composer.text.trim().is_empty();
            if send_now {
                composer.want_focus = true;
            }
            if composer.dictate_pending && response.has_focus() {
                composer.dictate_pending = false;
                composer.dictating = crate::dictate::toggle();
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
                    ui.spacing_mut().item_spacing.x = 6.0;
                    // Send, or stop while the crew works: a round button with a drawn arrow.
                    let busy = st.busy();
                    let live = busy || can_send;
                    let (r, resp) = ui.allocate_exact_size(
                        vec2(30.0, 30.0),
                        if live { Sense::click() } else { Sense::hover() },
                    );
                    let fill = match (busy, can_send, resp.hovered()) {
                        (true, _, h) => {
                            if h {
                                p.red.gamma_multiply(0.9)
                            } else {
                                p.bg3
                            }
                        }
                        (false, true, true) => p.accent.gamma_multiply(1.12),
                        (false, true, false) => p.accent,
                        _ => p.bg3,
                    };
                    ui.painter().circle_filled(r.center(), 15.0, fill);
                    let glyph = if live { egui::Color32::WHITE } else { p.text3 };
                    icons::paint(
                        ui.painter(),
                        icons::square(r.center(), if busy { 13.0 } else { 15.0 }),
                        if busy { Icon::Stop } else { Icon::ArrowUp },
                        glyph,
                    );
                    let resp = resp.on_hover_text(if busy {
                        "Stop the current turn"
                    } else if can_send {
                        "Send  (Enter) · Shift+Enter for a new line"
                    } else {
                        "Type a message, or press the mic to speak"
                    });
                    if live && resp.on_hover_cursor(CursorIcon::PointingHand).clicked() {
                        if busy {
                            st.interrupt();
                        } else {
                            send_now = true;
                        }
                    }
                    // Dictation through Windows voice typing.
                    let (m, mresp) = ui.allocate_exact_size(
                        vec2(30.0, 30.0),
                        if open { Sense::click() } else { Sense::hover() },
                    );
                    let on = composer.dictating;
                    if on {
                        let t = ui.input(|i| i.time) as f32;
                        let pulse = 0.5 + 0.5 * (t * 4.0).sin();
                        ui.painter().circle_filled(
                            m.center(),
                            15.0,
                            p.accent.gamma_multiply(0.25 + 0.2 * pulse),
                        );
                    } else if mresp.hovered() && open {
                        ui.painter().circle_filled(m.center(), 15.0, p.bg3);
                    }
                    icons::paint(
                        ui.painter(),
                        icons::square(m.center(), 15.0),
                        Icon::Mic,
                        if on {
                            p.accent
                        } else if open {
                            p.text2
                        } else {
                            p.text3
                        },
                    );
                    let mresp = mresp.on_hover_text(if on {
                        "Stop dictating"
                    } else {
                        "Dictate: speak and the words appear here (Windows voice typing, free)"
                    });
                    if open && mresp.on_hover_cursor(CursorIcon::PointingHand).clicked() {
                        if on {
                            crate::dictate::toggle();
                            composer.dictating = false;
                        } else {
                            composer.want_focus = true;
                            composer.dictate_pending = true;
                        }
                    }
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
        // A fixed-height row: a right-to-left layout in this vertical panel would otherwise
        // claim all the height left in the sidebar and push the conversation out of view.
        let row = vec2(ui.available_width(), 24.0);
        ui.allocate_ui_with_layout(
            row,
            egui::Layout::right_to_left(egui::Align::Center),
            |ui| {
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
            },
        );
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
            Some(i) => {
                let cost = match i.cost.as_str() {
                    "free" => "Free: nothing to pay",
                    "free-tier" => "Free models available",
                    "subscription" => "Needs a paid plan",
                    _ => "May cost per use",
                };
                format!("{}\n{cost} ({})\n{}", i.description, i.cost_note, i.detail)
            }
            None => "The AI runtime the crew runs on.".to_string(),
        });
    egui::Popup::menu(&resp)
        .id(ui.make_persistent_id("engine-chip-menu"))
        .layout(egui::Layout::top_down(egui::Align::Min))
        .show(|ui| {
            widgets::menu_begin(ui, 380.0);
            ui.label(caps("Crew engine", p.text3));
            ui.add_space(2.0);
            let engines = st.engines.clone();
            let mut pick: Option<(String, Option<String>)> = None;
            let mut configure: Option<String> = None;
            for e in &engines {
                let state = if e.available {
                    match &e.model {
                        Some(m) if !m.is_empty() => m.clone(),
                        _ => "ready".to_string(),
                    }
                } else {
                    "not set up".to_string()
                };
                let detail = if e.cost_note.is_empty() {
                    state
                } else {
                    format!("{} · {state}", e.cost_note)
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

// ---- @ picker -----------------------------------------------------------------------------

/// Crew member names that start with what was typed after the "@".
fn mention_matches(st: &AppState, partial: &str) -> Vec<String> {
    let needle = partial.to_lowercase();
    st.crew
        .iter()
        .filter(|m| m.name.to_lowercase().starts_with(&needle))
        .map(|m| m.name.clone())
        .collect()
}

/// Is the user typing "@name" at the end of the text? Remember it for next frame's keys.
fn composer_mention_state(composer: &mut Composer, response: &egui::Response) {
    if !response.has_focus() {
        composer.mention = None;
        return;
    }
    let text = composer.text.as_str();
    let found = text.rfind('@').and_then(|at| {
        let before_ok = at == 0 || text[..at].ends_with(char::is_whitespace);
        let partial = &text[at + 1..];
        let partial_ok = partial.chars().all(|c| c.is_alphanumeric());
        (before_ok && partial_ok).then(|| (at, partial.to_string()))
    });
    if found.as_ref().map(|(at, _)| *at) != composer.mention.as_ref().map(|(at, _)| *at) {
        composer.mention_pick = 0;
    }
    composer.mention = found;
}

/// Replace the "@partial" being typed with "@Name " and put the caret at the end.
fn apply_mention(composer: &mut Composer, name: &str) {
    if let Some((at, _)) = composer.mention.take() {
        composer.text.truncate(at);
        composer.text.push('@');
        composer.text.push_str(name);
        composer.text.push(' ');
        composer.caret_to_end = true;
    }
}

/// The popup above the text field listing the crew members that match. Returns a chosen name.
fn mention_picker(
    ui: &mut Ui,
    st: &AppState,
    composer: &Composer,
    anchor: egui::Rect,
) -> Option<String> {
    let (_, partial) = composer.mention.as_ref()?;
    let matches = mention_matches(st, partial);
    if matches.is_empty() {
        return None;
    }
    let p = st.palette;
    let mut chosen = None;
    egui::Area::new(egui::Id::unique("mention-picker"))
        .order(egui::Order::Foreground)
        .fixed_pos(anchor.left_top() - vec2(0.0, 8.0))
        .pivot(egui::Align2::LEFT_BOTTOM)
        .show(ui.ctx(), |ui| {
            widgets::float_frame(&p).show(ui, |ui| {
                ui.set_width(260.0);
                ui.spacing_mut().item_spacing.y = 0.0;
                ui.label(widgets::caps("Address a crew member", p.text3));
                ui.add_space(4.0);
                for (i, name) in matches.iter().enumerate() {
                    let member = st.crew.iter().find(|m| &m.name == name);
                    let key = member.map(|m| m.key.as_str()).unwrap_or("");
                    let title = member.map(|m| m.title.as_str()).unwrap_or("");
                    let (rect, resp) =
                        ui.allocate_exact_size(vec2(ui.available_width(), 30.0), Sense::click());
                    let picked = i == composer.mention_pick.min(matches.len() - 1);
                    if picked || resp.hovered() {
                        ui.painter().rect_filled(rect, R as f32, p.accent_soft);
                    }
                    let badge = egui::Rect::from_center_size(
                        egui::pos2(rect.left() + 16.0, rect.center().y),
                        vec2(18.0, 18.0),
                    );
                    ui.painter().circle_filled(badge.center(), 9.0, p.folk(key));
                    ui.painter().text(
                        badge.center() + vec2(0.0, 0.5),
                        egui::Align2::CENTER_CENTER,
                        name.chars()
                            .next()
                            .unwrap_or('?')
                            .to_uppercase()
                            .to_string(),
                        crate::theme::font(9.0, Weight::Semibold),
                        egui::Color32::WHITE,
                    );
                    ui.painter().text(
                        egui::pos2(rect.left() + 32.0, rect.center().y),
                        egui::Align2::LEFT_CENTER,
                        format!("@{name}"),
                        crate::theme::font(12.5, Weight::Medium),
                        p.text,
                    );
                    ui.painter().text(
                        egui::pos2(rect.right() - 8.0, rect.center().y),
                        egui::Align2::RIGHT_CENTER,
                        widgets::truncate(title, 24),
                        crate::theme::font(11.0, Weight::Regular),
                        p.text3,
                    );
                    if resp.on_hover_cursor(CursorIcon::PointingHand).clicked() {
                        chosen = Some(name.clone());
                    }
                }
                ui.add_space(2.0);
                ui.label(text(
                    "↑↓ choose · Enter or Tab to insert",
                    10.0,
                    Weight::Regular,
                    p.text3,
                ));
            });
        });
    chosen
}
