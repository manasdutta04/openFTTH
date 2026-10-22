//! What the engineer chose for the window, kept between launches: the theme and the view
//! toggles. A small JSON file in the openFTTH data folder; a missing or unreadable file means
//! the defaults.

use std::path::PathBuf;

use serde_json::{json, Value};

#[derive(Clone, Copy, PartialEq)]
pub struct Prefs {
    pub dark: bool,
    pub show_crew: bool,
    pub show_cursors: bool,
    pub follow_crew: bool,
}

impl Default for Prefs {
    fn default() -> Self {
        Self {
            dark: true,
            show_crew: true,
            show_cursors: true,
            follow_crew: false,
        }
    }
}

fn path() -> Option<PathBuf> {
    let dir = std::env::var_os("OPENFTTH_DATA_DIR")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("USERPROFILE")
                .or_else(|| std::env::var_os("HOME"))
                .map(|h| PathBuf::from(h).join(".openftth"))
        })?;
    Some(dir.join("app.json"))
}

impl Prefs {
    pub fn load() -> Self {
        let d = Self::default();
        let Some(v) = path()
            .and_then(|p| std::fs::read_to_string(p).ok())
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        else {
            return d;
        };
        let flag = |k: &str, or: bool| v[k].as_bool().unwrap_or(or);
        Self {
            dark: v["theme"].as_str().map(|t| t != "light").unwrap_or(d.dark),
            show_crew: flag("show_crew", d.show_crew),
            show_cursors: flag("show_cursors", d.show_cursors),
            follow_crew: flag("follow_crew", d.follow_crew),
        }
    }

    pub fn save(&self) {
        let Some(p) = path() else {
            return;
        };
        let v = json!({
            "theme": if self.dark { "dark" } else { "light" },
            "show_crew": self.show_crew,
            "show_cursors": self.show_cursors,
            "follow_crew": self.follow_crew,
        });
        if let Some(dir) = p.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let _ = std::fs::write(p, serde_json::to_string_pretty(&v).unwrap_or_default());
    }
}
