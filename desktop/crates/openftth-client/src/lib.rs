//! Client for the harness: JSON calls over HTTP and a background SSE subscription that
//! delivers frames on a channel. See `docs/PROTOCOL.md`.

pub mod sse;

use std::io::{BufRead, BufReader};
use std::sync::mpsc::Sender;
use std::thread::JoinHandle;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

pub use sse::{Frame, SseParser};

#[derive(Debug, thiserror::Error)]
pub enum ClientError {
    #[error("http: {0}")]
    Http(#[from] ureq::Error),
    /// The harness answered with an error status; the message is its own `error` field.
    #[error("{1}")]
    Api(u16, String),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Clone)]
pub struct Client {
    base: String,
    token: String,
    agent: ureq::Agent,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DesignContext {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub selection: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cursor: Option<[f64; 2]>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stage: Option<String>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        rename = "activeVersion"
    )]
    pub active_version: Option<String>,
}

/// Read a JSON body, turning an error status into `ClientError::Api` with the harness's words.
fn finish(mut res: ureq::http::Response<ureq::Body>) -> Result<Value, ClientError> {
    let status = res.status().as_u16();
    if status >= 400 {
        let text = res.body_mut().read_to_string().unwrap_or_default();
        let msg = serde_json::from_str::<Value>(&text)
            .ok()
            .and_then(|v| v["error"].as_str().map(str::to_string))
            .unwrap_or_else(|| {
                if text.is_empty() {
                    format!("HTTP {status}")
                } else {
                    text.clone()
                }
            });
        return Err(ClientError::Api(status, msg));
    }
    Ok(res.body_mut().read_json::<Value>()?)
}

impl Client {
    pub fn new(base: impl Into<String>, token: impl Into<String>) -> Client {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(120)))
            .http_status_as_error(false)
            .build()
            .into();
        Client {
            base: base.into().trim_end_matches('/').to_string(),
            token: token.into(),
            agent,
        }
    }

    pub fn base(&self) -> &str {
        &self.base
    }

    fn get(&self, path: &str) -> Result<Value, ClientError> {
        let res = self
            .agent
            .get(format!("{}{}", self.base, path))
            .header("authorization", &format!("Bearer {}", self.token))
            .call()?;
        finish(res)
    }

    fn send(&self, method: &str, path: &str, body: &Value) -> Result<Value, ClientError> {
        let url = format!("{}{}", self.base, path);
        let auth = format!("Bearer {}", self.token);
        let res = match method {
            "DELETE" => self
                .agent
                .delete(&url)
                .header("authorization", &auth)
                .call()?,
            _ => self
                .agent
                .post(&url)
                .header("authorization", &auth)
                .send_json(body)?,
        };
        finish(res)
    }

    pub fn health(&self) -> Result<Value, ClientError> {
        let mut res = self.agent.get(format!("{}/api/health", self.base)).call()?;
        Ok(res.body_mut().read_json::<Value>()?)
    }

    pub fn crew(&self) -> Result<Value, ClientError> {
        self.get("/api/crew")
    }

    pub fn project(&self) -> Result<Value, ClientError> {
        self.get("/api/project")
    }

    pub fn open_project(
        &self,
        area_path: &str,
        osm_path: Option<&str>,
        rulebook: &str,
    ) -> Result<Value, ClientError> {
        let mut body = json!({ "area_path": area_path, "rulebook": rulebook });
        if let Some(osm) = osm_path {
            body["osm_path"] = json!(osm);
        }
        self.send("POST", "/api/project", &body)
    }

    /// The layers in data files and the import profile the engine proposes for them.
    pub fn import_inspect(&self, paths: &[String]) -> Result<Value, ClientError> {
        self.send("POST", "/api/import/inspect", &json!({ "paths": paths }))
    }

    /// Save an import profile and read its data: returns `{path, report}` (the quality report).
    pub fn import_profile(
        &self,
        name: &str,
        profile: &Value,
        rulebook: &str,
    ) -> Result<Value, ClientError> {
        self.send(
            "POST",
            "/api/import/profile",
            &json!({ "name": name, "profile": profile, "rulebook": rulebook }),
        )
    }

    pub fn close_project(&self) -> Result<Value, ClientError> {
        self.send("DELETE", "/api/project", &Value::Null)
    }

    /// Close the project and delete everything it produced.
    /// Design the open project end to end with the engine alone (no AI, no cost).
    pub fn autodesign(&self) -> Result<Value, ClientError> {
        self.send("POST", "/api/project/autodesign", &json!({}))
    }

    pub fn reset_project(&self) -> Result<Value, ClientError> {
        self.send("DELETE", "/api/project?purge=1", &Value::Null)
    }

    pub fn versions(&self, limit: usize) -> Result<Value, ClientError> {
        self.get(&format!("/api/project/versions?limit={limit}"))
    }

    pub fn diff(&self, from: &str, to: &str) -> Result<Value, ClientError> {
        self.get(&format!("/api/project/diff?from={from}&to={to}"))
    }

    /// The engineer's own change (`move_site`, `exclude_building`, `forbid_street`): the
    /// harness runs the engine tool directly and, unless told not to, asks the Checker to
    /// validate the design again. A rejection comes back as `ClientError::Api(422, words)`.
    pub fn steer(&self, tool: &str, args: &Value, revalidate: bool) -> Result<Value, ClientError> {
        self.send(
            "POST",
            "/api/project/steer",
            &json!({ "tool": tool, "args": args, "revalidate": revalidate }),
        )
    }

    pub fn approve_version(&self, version: &str) -> Result<Value, ClientError> {
        self.send(
            "POST",
            "/api/project/approve",
            &json!({ "version": version }),
        )
    }

    pub fn messages(&self, since: i64) -> Result<Value, ClientError> {
        self.get(&format!("/api/room/messages?since={since}"))
    }

    pub fn send_message(
        &self,
        text: &str,
        context: Option<&DesignContext>,
    ) -> Result<Value, ClientError> {
        let mut body = json!({ "text": text });
        if let Some(c) = context {
            body["designContext"] = serde_json::to_value(c)?;
        }
        self.send("POST", "/api/room/messages", &body)
    }

    pub fn interrupt(&self) -> Result<Value, ClientError> {
        self.send("POST", "/api/room/interrupt", &Value::Null)
    }

    pub fn approvals(&self) -> Result<Value, ClientError> {
        self.get("/api/approvals")
    }

    pub fn decide(&self, id: &str, allow: bool, note: Option<&str>) -> Result<Value, ClientError> {
        let mut body = json!({ "decision": if allow { "allow" } else { "deny" } });
        if let Some(n) = note {
            body["note"] = json!(n);
        }
        self.send("POST", &format!("/api/approvals/{id}"), &body)
    }

    pub fn backstage(&self, limit: usize) -> Result<Value, ClientError> {
        self.get(&format!("/api/backstage?limit={limit}"))
    }

    pub fn rulebooks(&self) -> Result<Value, ClientError> {
        self.get("/api/rulebooks")
    }

    pub fn engines(&self) -> Result<Value, ClientError> {
        self.get("/api/engines")
    }

    pub fn set_engine(&self, body: &Value) -> Result<Value, ClientError> {
        self.send("POST", "/api/engine", body)
    }

    pub fn projects(&self) -> Result<Value, ClientError> {
        self.get("/api/projects")
    }

    pub fn explain_element(&self, uuid: &str) -> Result<Value, ClientError> {
        self.get(&format!("/api/project/element/{uuid}"))
    }

    /// A home's fibre path from OLT port to drop (the engine's `trace_fibre` result).
    pub fn trace(&self, uuid: &str) -> Result<Value, ClientError> {
        self.get(&format!("/api/project/trace/{uuid}"))
    }

    pub fn assumptions(&self) -> Result<Value, ClientError> {
        self.get("/api/project/assumptions")
    }

    /// Subscribe to `/api/events` on a background thread. Reconnects with the last id until
    /// the receiver is dropped.
    pub fn subscribe(&self, tx: Sender<Frame>) -> JoinHandle<()> {
        let client = self.clone();
        std::thread::spawn(move || {
            let mut last_id: Option<String> = None;
            loop {
                let agent: ureq::Agent = ureq::Agent::config_builder()
                    .timeout_global(None)
                    .build()
                    .into();
                let mut req = agent
                    .get(format!("{}/api/events", client.base))
                    .header("authorization", &format!("Bearer {}", client.token));
                if let Some(id) = &last_id {
                    req = req.header("last-event-id", id);
                }
                match req.call() {
                    Ok(res) => {
                        let reader = BufReader::new(res.into_body().into_reader());
                        let mut parser = SseParser::default();
                        for line in reader.lines() {
                            let Ok(line) = line else { break };
                            if let Some(frame) = parser.push_line(&line) {
                                last_id = Some(frame.id.clone());
                                if tx.send(frame).is_err() {
                                    return;
                                }
                            }
                        }
                    }
                    Err(_) => {
                        if tx.send(Frame::disconnected()).is_err() {
                            return;
                        }
                    }
                }
                std::thread::sleep(Duration::from_millis(800));
            }
        })
    }
}
