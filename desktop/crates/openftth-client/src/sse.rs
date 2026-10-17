//! Incremental Server-Sent Events parser.

use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub id: String,
    pub event: String,
    pub data: Value,
}

impl Frame {
    /// Synthetic frame the subscription emits when the connection drops.
    pub fn disconnected() -> Frame {
        Frame {
            id: String::new(),
            event: "disconnected".into(),
            data: Value::Null,
        }
    }
}

#[derive(Default)]
pub struct SseParser {
    id: Option<String>,
    event: Option<String>,
    data: Vec<String>,
}

impl SseParser {
    /// Feed one line (without its newline). Returns a frame when a blank line closes one.
    pub fn push_line(&mut self, line: &str) -> Option<Frame> {
        if line.is_empty() {
            return self.flush();
        }
        if let Some(v) = line.strip_prefix("id: ") {
            self.id = Some(v.to_string());
        } else if let Some(v) = line.strip_prefix("event: ") {
            self.event = Some(v.to_string());
        } else if let Some(v) = line.strip_prefix("data: ") {
            self.data.push(v.to_string());
        } else if line.starts_with(':') {
            // comment
        }
        None
    }

    /// Feed arbitrary text (may hold several frames or a partial one).
    pub fn push(&mut self, text: &str) -> Vec<Frame> {
        // The piece after a final newline is not a line yet; a blank line shows up as "" between
        // two newlines, so drop only the trailing remainder when the text ends with a newline.
        let mut pieces: Vec<&str> = text.split('\n').collect();
        if text.ends_with('\n') {
            pieces.pop();
        }
        pieces
            .into_iter()
            .filter_map(|l| self.push_line(l.trim_end_matches('\r')))
            .collect()
    }

    fn flush(&mut self) -> Option<Frame> {
        if self.data.is_empty() && self.event.is_none() {
            self.id = None;
            return None;
        }
        let joined = self.data.join("\n");
        let data = serde_json::from_str(&joined).unwrap_or(Value::String(joined));
        let frame = Frame {
            id: self.id.take().unwrap_or_default(),
            event: self.event.take().unwrap_or_else(|| "message".into()),
            data,
        };
        self.data.clear();
        Some(frame)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_frames_and_keeps_partial_state() {
        let mut p = SseParser::default();
        let frames = p.push("id: s:1\nevent: hello\ndata: {\"cursor\":\"s:1\",\"resumed\":false}\n\nid: s:2\nevent: message\ndata: {\"text\":\"hi\"}\n\nid: s:3\nevent: ping\n");
        assert_eq!(frames.len(), 2);
        assert_eq!(frames[0].event, "hello");
        assert_eq!(frames[0].data["resumed"], false);
        assert_eq!(frames[1].id, "s:2");
        // the third frame is still open until a blank line arrives
        let rest = p.push("data: {}\n\n");
        assert_eq!(rest.len(), 1);
        assert_eq!(rest[0].event, "ping");
        assert_eq!(rest[0].id, "s:3");
    }

    #[test]
    fn non_json_data_is_kept_as_a_string() {
        let mut p = SseParser::default();
        let frames = p.push("event: note\ndata: plain text\n\n");
        assert_eq!(frames[0].data, Value::String("plain text".into()));
    }
}
