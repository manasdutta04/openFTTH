//! The rulebook as the app sees it: the few sections it displays typed, the rest kept verbatim.
//! The engine's pydantic model is the source of truth; `rulebooks/schema.json` validates files.

use std::collections::BTreeMap;
use std::path::Path;

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct Rulebook {
    pub version: u32,
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub country: Option<String>,
    #[serde(default = "default_currency")]
    pub currency: String,
    #[serde(default)]
    pub pon: Pon,
    #[serde(default)]
    pub fat: Fat,
    #[serde(default)]
    pub distances: Distances,
    /// Every other section, verbatim.
    #[serde(flatten)]
    pub rest: BTreeMap<String, serde_yaml::Value>,
}

fn default_currency() -> String {
    "EUR".into()
}

#[derive(Debug, Clone, Deserialize)]
pub struct Pon {
    #[serde(rename = "class", default = "default_class")]
    pub class: String,
    #[serde(default = "default_budget")]
    pub budget_db: f64,
    #[serde(default = "default_split")]
    pub split_ratio: u32,
    #[serde(default)]
    pub cascade: Option<Vec<u32>>,
}

fn default_class() -> String {
    "B+".into()
}
fn default_budget() -> f64 {
    28.0
}
fn default_split() -> u32 {
    32
}

impl Default for Pon {
    fn default() -> Self {
        Self {
            class: default_class(),
            budget_db: default_budget(),
            split_ratio: default_split(),
            cascade: None,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Fat {
    #[serde(default = "default_fat_max")]
    pub max_homes: u32,
    #[serde(default)]
    pub min_homes: u32,
}

fn default_fat_max() -> u32 {
    32
}

impl Default for Fat {
    fn default() -> Self {
        Self {
            max_homes: default_fat_max(),
            min_homes: 4,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Distances {
    #[serde(default = "default_drop")]
    pub max_drop_m: f64,
    #[serde(default = "default_dist")]
    pub max_dist_m: f64,
}

fn default_drop() -> f64 {
    150.0
}
fn default_dist() -> f64 {
    2000.0
}

impl Default for Distances {
    fn default() -> Self {
        Self {
            max_drop_m: default_drop(),
            max_dist_m: default_dist(),
        }
    }
}

impl Rulebook {
    pub fn from_yaml(text: &str) -> Result<Rulebook, serde_yaml::Error> {
        serde_yaml::from_str(text)
    }

    pub fn read(path: &Path) -> Result<Rulebook, Box<dyn std::error::Error>> {
        Ok(Self::from_yaml(&std::fs::read_to_string(path)?)?)
    }

    pub fn summary(&self) -> String {
        let split = match &self.pon.cascade {
            Some(c) => c
                .iter()
                .map(|s| format!("1:{s}"))
                .collect::<Vec<_>>()
                .join(" × "),
            None => format!("1:{}", self.pon.split_ratio),
        };
        format!(
            "{} · class {} ({} dB) · split {} · FAT ≤ {} · drop ≤ {} m",
            self.name,
            self.pon.class,
            self.pon.budget_db,
            split,
            self.fat.max_homes,
            self.distances.max_drop_m
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_shipped_presets() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../rulebooks");
        for name in ["generic-gpon", "belgium", "norway"] {
            let rb = Rulebook::read(&root.join(format!("{name}.yaml")))
                .unwrap_or_else(|e| panic!("{name}: {e}"));
            assert_eq!(rb.name, name);
            assert_eq!(rb.version, 1);
            assert!(rb.rest.contains_key("surfaces"));
        }
        let no = Rulebook::read(&root.join("norway.yaml")).unwrap();
        assert_eq!(no.pon.cascade, Some(vec![4, 8]));
        assert!(no.summary().contains("1:4 × 1:8"));
    }
}
