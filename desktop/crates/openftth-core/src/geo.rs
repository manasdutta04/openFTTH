//! Web Mercator projection and bounds, enough for a 2D map view.

use std::f64::consts::PI;

/// Web Mercator in metres (EPSG:3857). Good for drawing; never for lengths (use the engine's).
pub struct Mercator;

impl Mercator {
    pub const R: f64 = 6_378_137.0;

    pub fn project(lon: f64, lat: f64) -> [f64; 2] {
        let x = Self::R * lon.to_radians();
        let lat = lat.clamp(-85.05, 85.05).to_radians();
        let y = Self::R * ((PI / 4.0 + lat / 2.0).tan()).ln();
        [x, y]
    }

    pub fn unproject(x: f64, y: f64) -> (f64, f64) {
        let lon = (x / Self::R).to_degrees();
        let lat = (2.0 * (y / Self::R).exp().atan() - PI / 2.0).to_degrees();
        (lon, lat)
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Bounds {
    pub min: [f64; 2],
    pub max: [f64; 2],
}

impl Bounds {
    pub fn empty() -> Self {
        Self {
            min: [f64::INFINITY, f64::INFINITY],
            max: [f64::NEG_INFINITY, f64::NEG_INFINITY],
        }
    }

    pub fn is_empty(&self) -> bool {
        self.min[0] > self.max[0]
    }

    pub fn include(&mut self, p: [f64; 2]) {
        self.min[0] = self.min[0].min(p[0]);
        self.min[1] = self.min[1].min(p[1]);
        self.max[0] = self.max[0].max(p[0]);
        self.max[1] = self.max[1].max(p[1]);
    }

    pub fn center(&self) -> [f64; 2] {
        [
            (self.min[0] + self.max[0]) / 2.0,
            (self.min[1] + self.max[1]) / 2.0,
        ]
    }

    pub fn size(&self) -> [f64; 2] {
        [self.max[0] - self.min[0], self.max[1] - self.min[1]]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mercator_round_trips() {
        let (lon, lat) = (3.68, 51.07);
        let [x, y] = Mercator::project(lon, lat);
        let (lon2, lat2) = Mercator::unproject(x, y);
        assert!((lon - lon2).abs() < 1e-9);
        assert!((lat - lat2).abs() < 1e-9);
        let [one_deg, zero] = Mercator::project(1.0, 0.0);
        assert!((one_deg - 111_319.49).abs() < 0.01, "1° = {one_deg} m");
        assert!(zero.abs() < 1e-6);
    }

    #[test]
    fn bounds_grow_and_center() {
        let mut b = Bounds::empty();
        assert!(b.is_empty());
        b.include([0.0, 0.0]);
        b.include([10.0, 4.0]);
        assert_eq!(b.center(), [5.0, 2.0]);
        assert_eq!(b.size(), [10.0, 4.0]);
    }
}
