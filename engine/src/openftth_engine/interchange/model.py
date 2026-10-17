"""openFTTH design interchange: element model (format openftth-design v1.0).

Every feature carries `element_type`, `placement`, `element_uuid` and `extra_json`. openFTTH keeps
its own attributes under `extra_json["openftth"]` so generic GeoJSON readers ignore them and
openFTTH readers find them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

ElementType = Literal[
    # lines
    "route",
    "cable.aerial",
    "cable.underground",
    "duct.pe",
    "duct.transition",
    # points
    "pole",
    "manhole",
    "closure.joint",
    "odf",
    "otb",
    "to",
    "tb",
    "patch_panel",
    "slack",
    "fiber_break",
    # polygons
    "service_area",
    "building",
]
PlacementKind = Literal["indoor", "outdoor", "pole", "closure"]

LINE_TYPES = {"route", "cable.aerial", "cable.underground", "duct.pe", "duct.transition"}
POLYGON_TYPES = {"service_area", "building"}
# Point equipment whose file name carries the placement (`otb.indoor.geojson`).
PLACED_POINT_TYPES = {
    "otb",
    "to",
    "tb",
    "odf",
    "closure.joint",
    "patch_panel",
    "slack",
    "fiber_break",
}

# How openFTTH maps its network hierarchy onto interchange element types.
OPENFTTH_KINDS = {
    "pop": "odf",  # the POP / central office ODF
    "fdh": "otb",  # fibre distribution hub (splitter cabinet)
    "fat": "tb",  # fibre access terminal
    "home": "to",  # terminal outlet at the home (ONT side)
    "chamber": "manhole",
    "feeder": "cable.underground",
    "distribution": "cable.underground",
    "drop": "cable.underground",
}

COORD_DECIMALS = 7


class Feature(BaseModel):
    """One interchange feature. `props` are the generic element fields; `openftth` is our namespace."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    element_uuid: str = Field(max_length=64)
    element_type: ElementType
    placement: PlacementKind = "outdoor"
    geometry: BaseGeometry
    props: dict[str, Any] = Field(default_factory=dict)
    openftth: dict[str, Any] = Field(default_factory=dict)

    # ---- (de)serialisation ------------------------------------------------------------

    def to_geojson(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.openftth:
            extra["openftth"] = self.openftth
        properties = dict(self.props)
        properties.update(
            {
                "element_type": self.element_type,
                "placement": self.placement,
                "element_uuid": self.element_uuid,
                "extra_json": json.dumps(extra, sort_keys=True, separators=(",", ":")),
            }
        )
        return {
            "type": "Feature",
            "geometry": _round_geometry(mapping(self.geometry)),
            "properties": properties,
        }

    @classmethod
    def from_geojson(cls, obj: dict[str, Any]) -> Feature:
        props = dict(obj.get("properties") or {})
        extra_raw = props.pop("extra_json", "{}") or "{}"
        extra = json.loads(extra_raw) if isinstance(extra_raw, str) else dict(extra_raw)
        return cls(
            element_uuid=props.pop("element_uuid"),
            element_type=props.pop("element_type"),
            placement=props.pop("placement", "outdoor"),
            geometry=shape(obj["geometry"]),
            props=props,
            openftth=extra.get("openftth", {}),
        )

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_geojson(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @property
    def kind(self) -> str | None:
        return self.openftth.get("kind")

    def file_stem(self) -> str:
        if self.element_type in PLACED_POINT_TYPES:
            return f"{self.element_type}.{self.placement}"
        return self.element_type


class Metadata(BaseModel):
    """`_openftth_metadata` keys. Writers merge these keys and never replace the table."""

    format: Literal["openftth-design"] = "openftth-design"
    format_version: str = "1.0"
    schema_version: str = "1.0"
    produced_by: str = "openFTTH engine 0.1.0"
    produced_at: str = Field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    crs_epsg: int = 4326
    color_standard: str = "TIA-598-C"
    profile: str | None = "geojson-lite"
    color_catalog_json: str | None = None

    def to_table(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in self.model_dump().items():
            if v is not None:
                out[k] = str(v)
        return out


def _round_geometry(geom: dict[str, Any]) -> dict[str, Any]:
    def rnd(coords: Any) -> Any:
        if isinstance(coords, (list, tuple)):
            if coords and isinstance(coords[0], (int, float)):
                return [round(float(c), COORD_DECIMALS) for c in coords]
            return [rnd(c) for c in coords]
        return coords

    if geom.get("type") == "GeometryCollection":
        return {
            "type": "GeometryCollection",
            "geometries": [_round_geometry(g) for g in geom["geometries"]],
        }
    return {"type": geom["type"], "coordinates": rnd(geom["coordinates"])}
