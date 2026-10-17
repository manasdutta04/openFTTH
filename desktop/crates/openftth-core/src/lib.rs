//! openFTTH core types shared by the apps: the design bundle on disk, the rulebook, and the
//! map geometry helpers the renderer needs. No UI and no network here.

pub mod bundle;
pub mod geo;
pub mod rulebook;

pub use bundle::{Design, Element, Geometry, StreetLayer, StreetSegment};
pub use geo::{Bounds, Mercator};
pub use rulebook::Rulebook;
