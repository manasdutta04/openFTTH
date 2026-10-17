//! The design bundle a project keeps on disk: one GeoJSON file per element type (and
//! placement), each feature carrying `element_type`, `placement`, `element_uuid` and an
//! `extra_json` string whose `openftth` object holds our attributes.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use serde::Deserialize;
use serde_json::{Map, Value};

use crate::geo::{Bounds, Mercator};

#[derive(Debug, thiserror::Error)]
pub enum BundleError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("unsupported bundle format {0:?}")]
    Format(Option<String>),
    #[error("unsupported bundle major version {0}")]
    Version(String),
}

#[derive(Debug, Clone, PartialEq)]
pub enum Geometry {
    Point([f64; 2]),
    LineString(Vec<[f64; 2]>),
    Polygon(Vec<Vec<[f64; 2]>>),
    MultiPolygon(Vec<Vec<Vec<[f64; 2]>>>),
    Other,
}

impl Geometry {
    fn from_json(v: &Value) -> Geometry {
        let coords = v.get("coordinates");
        match (v.get("type").and_then(Value::as_str), coords) {
            (Some("Point"), Some(c)) => pt(c).map(Geometry::Point).unwrap_or(Geometry::Other),
            (Some("LineString"), Some(c)) => Geometry::LineString(line(c)),
            (Some("Polygon"), Some(c)) => Geometry::Polygon(rings(c)),
            (Some("MultiPolygon"), Some(c)) => Geometry::MultiPolygon(
                c.as_array()
                    .map(|ps| ps.iter().map(rings).collect())
                    .unwrap_or_default(),
            ),
            _ => Geometry::Other,
        }
    }

    /// Every coordinate, for bounds and hit tests.
    pub fn coords(&self) -> Vec<[f64; 2]> {
        match self {
            Geometry::Point(p) => vec![*p],
            Geometry::LineString(l) => l.clone(),
            Geometry::Polygon(r) => r.iter().flatten().copied().collect(),
            Geometry::MultiPolygon(m) => m.iter().flatten().flatten().copied().collect(),
            Geometry::Other => vec![],
        }
    }

    /// A representative point in lon/lat (first coordinate).
    pub fn anchor(&self) -> Option<[f64; 2]> {
        self.coords().first().copied()
    }
}

fn pt(c: &Value) -> Option<[f64; 2]> {
    let a = c.as_array()?;
    Some([a.first()?.as_f64()?, a.get(1)?.as_f64()?])
}
fn line(c: &Value) -> Vec<[f64; 2]> {
    c.as_array()
        .map(|a| a.iter().filter_map(pt).collect())
        .unwrap_or_default()
}
fn rings(c: &Value) -> Vec<Vec<[f64; 2]>> {
    c.as_array()
        .map(|a| a.iter().map(line).collect())
        .unwrap_or_default()
}

#[derive(Debug, Clone)]
pub struct Element {
    pub element_uuid: String,
    pub element_type: String,
    pub placement: String,
    pub geometry: Geometry,
    /// Generic element fields (name, cable_type, fiber_count, length_m, …).
    pub props: Map<String, Value>,
    /// Our namespace from `extra_json["openftth"]` (kind, units, fat_id, …).
    pub openftth: Map<String, Value>,
}

impl Element {
    pub fn kind(&self) -> &str {
        self.openftth
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or(&self.element_type)
    }

    pub fn name(&self) -> String {
        self.props
            .get("name")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| self.kind().to_string())
    }

    fn from_feature(f: &Value) -> Option<Element> {
        let props = f.get("properties")?.as_object()?.clone();
        let mut props: Map<String, Value> = props;
        let element_uuid = props.remove("element_uuid")?.as_str()?.to_string();
        let element_type = props.remove("element_type")?.as_str()?.to_string();
        let placement = props
            .remove("placement")
            .and_then(|v| v.as_str().map(str::to_string))
            .unwrap_or_else(|| "outdoor".into());
        let extra = props.remove("extra_json");
        let openftth = extra
            .and_then(|e| match e {
                Value::String(s) => serde_json::from_str::<Value>(&s).ok(),
                other => Some(other),
            })
            .and_then(|e| e.get("openftth").cloned())
            .and_then(|o| o.as_object().cloned())
            .unwrap_or_default();
        Some(Element {
            element_uuid,
            element_type,
            placement,
            geometry: Geometry::from_json(f.get("geometry")?),
            props,
            openftth,
        })
    }
}

#[derive(Debug, Default, Clone)]
pub struct Design {
    pub metadata: BTreeMap<String, String>,
    pub elements: Vec<Element>,
}

impl Design {
    /// Read every `*.geojson` in a bundle directory plus `_openftth_metadata.json`.
    pub fn read_dir(dir: &Path) -> Result<Design, BundleError> {
        let mut design = Design::default();
        let meta_path = dir.join("_openftth_metadata.json");
        if meta_path.exists() {
            let table: BTreeMap<String, String> =
                serde_json::from_str(&fs::read_to_string(&meta_path)?)?;
            match table.get("format").map(String::as_str) {
                None | Some("openftth-design") => {}
                other => return Err(BundleError::Format(other.map(str::to_string))),
            }
            if let Some(v) = table.get("format_version") {
                if v.split('.').next() != Some("1") {
                    return Err(BundleError::Version(v.clone()));
                }
            }
            design.metadata = table;
        }
        let mut names: Vec<_> = fs::read_dir(dir)?
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("geojson"))
            .collect();
        names.sort();
        for p in names {
            let fc: Value = serde_json::from_str(&fs::read_to_string(&p)?)?;
            if let Some(features) = fc.get("features").and_then(Value::as_array) {
                design
                    .elements
                    .extend(features.iter().filter_map(Element::from_feature));
            }
        }
        Ok(design)
    }

    pub fn by_kind<'a>(&'a self, kind: &'a str) -> impl Iterator<Item = &'a Element> + 'a {
        self.elements.iter().filter(move |e| e.kind() == kind)
    }

    pub fn get(&self, uuid: &str) -> Option<&Element> {
        self.elements.iter().find(|e| e.element_uuid == uuid)
    }

    /// Bounds in Web Mercator metres over every element except the service area polygon.
    pub fn bounds(&self) -> Bounds {
        let mut b = Bounds::empty();
        for e in self.elements.iter().filter(|e| e.kind() != "service_area") {
            for [lon, lat] in e.geometry.coords() {
                b.include(Mercator::project(lon, lat));
            }
        }
        b
    }

    pub fn counts(&self) -> BTreeMap<String, usize> {
        let mut out = BTreeMap::new();
        for e in &self.elements {
            *out.entry(e.kind().to_string()).or_insert(0) += 1;
        }
        out
    }
}

/// The street graph layer the engine writes for the renderer (`twin/streets.geojson`).
#[derive(Debug, Default, Clone)]
pub struct StreetLayer {
    pub segments: Vec<StreetSegment>,
}

#[derive(Debug, Clone)]
pub struct StreetSegment {
    pub highway: String,
    pub surface: String,
    pub line: Vec<[f64; 2]>,
}

#[derive(Deserialize)]
struct StreetProps {
    #[serde(default)]
    highway: String,
    #[serde(default)]
    surface: String,
}

impl StreetLayer {
    pub fn read(path: &Path) -> Result<StreetLayer, BundleError> {
        let fc: Value = serde_json::from_str(&fs::read_to_string(path)?)?;
        let mut layer = StreetLayer::default();
        for f in fc
            .get("features")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let props: StreetProps = f
                .get("properties")
                .cloned()
                .map(serde_json::from_value)
                .transpose()?
                .unwrap_or(StreetProps {
                    highway: String::new(),
                    surface: String::new(),
                });
            if let Some(Geometry::LineString(line)) = f.get("geometry").map(Geometry::from_json) {
                layer.segments.push(StreetSegment {
                    highway: props.highway,
                    surface: props.surface,
                    line,
                });
            }
        }
        Ok(layer)
    }

    pub fn bounds(&self) -> Bounds {
        let mut b = Bounds::empty();
        for s in &self.segments {
            for [lon, lat] in &s.line {
                b.include(Mercator::project(*lon, *lat));
            }
        }
        b
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(dir: &Path, name: &str, text: &str) {
        fs::write(dir.join(name), text).unwrap();
    }

    #[test]
    fn reads_a_bundle_and_our_namespace() {
        let dir = std::env::temp_dir().join(format!("openftth-core-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        write(
            &dir,
            "_openftth_metadata.json",
            r#"{"format":"openftth-design","format_version":"1.0","crs_epsg":"4326"}"#,
        );
        write(
            &dir,
            "tb.outdoor.geojson",
            r#"{"type":"FeatureCollection","name":"tb.outdoor","features":[
{"geometry":{"coordinates":[3.68,51.07],"type":"Point"},"properties":{"element_type":"tb","element_uuid":"u1","extra_json":"{\"openftth\":{\"kind\":\"fat\",\"units\":12}}","name":"FAT-001","placement":"outdoor"},"type":"Feature"}
]}"#,
        );
        write(
            &dir,
            "cable.underground.geojson",
            r#"{"type":"FeatureCollection","name":"cable.underground","features":[
{"geometry":{"coordinates":[[3.68,51.07],[3.681,51.071]],"type":"LineString"},"properties":{"element_type":"cable.underground","element_uuid":"u2","extra_json":"{\"openftth\":{\"kind\":\"distribution\"}}","fiber_count":24,"placement":"outdoor"},"type":"Feature"}
]}"#,
        );
        let d = Design::read_dir(&dir).unwrap();
        assert_eq!(d.elements.len(), 2);
        let fat = d.get("u1").unwrap();
        assert_eq!(fat.kind(), "fat");
        assert_eq!(fat.name(), "FAT-001");
        assert_eq!(fat.openftth["units"], 12);
        assert_eq!(d.by_kind("distribution").count(), 1);
        assert!(!d.bounds().is_empty());
        assert_eq!(d.counts()["fat"], 1);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn refuses_unknown_major_version() {
        let dir = std::env::temp_dir().join(format!("openftth-core-test-v-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        write(
            &dir,
            "_openftth_metadata.json",
            r#"{"format":"openftth-design","format_version":"2.0"}"#,
        );
        assert!(matches!(
            Design::read_dir(&dir),
            Err(BundleError::Version(_))
        ));
        fs::remove_dir_all(&dir).unwrap();
    }
}
