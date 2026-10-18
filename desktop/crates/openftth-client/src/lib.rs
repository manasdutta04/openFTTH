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

impl Client {
    pub fn new(base: impl Into<String>, token: impl Into<String>) -> Client {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(120)))
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
        let mut res = self
            .agent
            .get(format!("{}{}", self.base, path))
            .header("authorization", &format!("Bearer {}", self.token))
            .call()?;
        Ok(res.body_mut().read_json::<Value>()?)
    }

    fn send(&self, method: &str, path: &str, body: &Value) -> Result<Value, ClientError> {
        let url = format!("{}{}", self.base, path);
        let auth = format!("Bearer {}", self.token);
        let mut res = match method {
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
        Ok(res.body_mut().read_json::<Value>()?)
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

    pub fn close_project(&self) -> Result<Value, ClientError> {
        self.send("DELETE", "/api/project", &Value::Null)
    }

    pub fn versions(&self, limit: usize) -> Result<Value, ClientError> {
        self.get(&format!("/api/project/versions?limit={limit}"))
    }

    pub fn diff(&self, from: &str, to: &str) -> Result<Value, ClientError> {
        self.get(&format!("/api/project/diff?from={from}&to={to}"))
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

    pub fn projects(&self) -> Result<Value, ClientError> {
        self.get("/api/projects")
    }

    pub fn explain_element(&self, uuid: &str) -> Result<Value, ClientError> {
        self.get(&format!("/api/project/element/{uuid}"))
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
