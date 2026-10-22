"""Import profiles: which source layer plays which part in a project, and which of its columns
carry what. A profile is proposed from the files (geometry, layer and column names), confirmed
or corrected by the engineer, saved as YAML, and reused for every later import of that kind of
data. Nothing about a country, operator or dataset is hard-coded here: the synonyms only seed
the proposal.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .sources import LayerInfo, list_layers

Role = Literal[
    "area",
    "osm",
    "streets",
    "buildings",
    "addresses",
    "existing_ducts",
    "poles",
    "manholes",
    "no_dig_zones",
]

ROLE_LABEL: dict[str, str] = {
    "area": "Design area",
    "osm": "OpenStreetMap extract (streets and buildings)",
    "streets": "Streets",
    "buildings": "Buildings",
    "addresses": "Address register (homes and units)",
    "existing_ducts": "Existing ducts",
    "poles": "Existing poles",
    "manholes": "Existing manholes",
    "no_dig_zones": "No-dig zones",
}

# The attributes each role understands, with name fragments that usually carry them.
ROLE_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "area": {"name": ("name", "naam", "nom")},
    "osm": {},
    "streets": {
        "id": ("osm_id", "id", "fid", "objectid", "wegid", "road_id"),
        "class": ("highway", "class", "type", "categ", "fclass", "roadtype", "wegtype"),
        "surface": ("surface", "verharding", "revetement", "paving"),
        "name": ("name", "straat", "street", "rue", "naam", "nom"),
    },
    "buildings": {
        "id": ("osm_id", "id", "fid", "objectid", "uprn", "gebouw", "bat_id"),
        "use": ("building", "use", "type", "functie", "usage", "bestemming"),
        "levels": ("levels", "storeys", "floors", "verdiep", "etages"),
        "units": ("units", "flats", "dwellings", "woningen", "logements", "wooneenheden"),
    },
    "addresses": {
        "id": ("id", "address_id", "adres_id", "uprn", "fid", "objectid", "gid"),
        "units": (
            "units",
            "unit_count",
            "dwellings",
            "woningen",
            "logements",
            "wooneenheden",
            "flats",
            "huishoudens",
        ),
        "kind": ("kind", "type", "usage", "functie", "category"),
        "street": ("street", "straat", "rue", "straatnaam", "addr:street"),
        "housenumber": ("housenumber", "huisnummer", "number", "numero", "nr", "addr:housenumber"),
        "postcode": ("postcode", "postal", "zip", "code_postal"),
        "building_id": ("building_id", "gebouw_id", "bat_id", "building"),
    },
    "existing_ducts": {
        "id": ("id", "duct_id", "fid", "objectid"),
        "owner": ("owner", "eigenaar", "proprietaire", "operator"),
        "free": ("free", "spare", "available", "vrij", "libre"),
    },
    "poles": {
        "id": ("id", "pole_id", "fid", "objectid"),
        "owner": ("owner", "eigenaar", "proprietaire", "operator"),
    },
    "manholes": {
        "id": ("id", "manhole_id", "fid", "objectid"),
        "type": ("type", "size", "kind"),
    },
    "no_dig_zones": {"reason": ("reason", "reden", "raison", "note", "comment", "until")},
}

ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "area": ("area", "boundary", "scope", "perimeter", "gebied", "zone_projet", "polygon"),
    "no_dig_zones": ("nodig", "no_dig", "no-dig", "forbid", "exclusion", "verboden", "interdit"),
    "existing_ducts": ("duct", "conduit", "buis", "fourreau", "koker", "trace", "civil"),
    "poles": ("pole", "mast", "paal", "palen", "poteau", "support", "pylon"),
    "manholes": ("manhole", "chamber", "handhole", "chambre", "putten", "kamer", "schacht"),
    "addresses": ("address", "adres", "adresse", "premise", "uprn", "dwelling", "woning", "home"),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Binding(_Strict):
    path: str = Field(description="Source file")
    layer: str | None = Field(None, description="Layer inside the file (GeoPackage, DXF, …)")
    role: Role
    crs: str | None = Field(None, description="CRS when the file has none, e.g. EPSG:31370")
    x: str | None = Field(None, description="Table x / longitude column")
    y: str | None = Field(None, description="Table y / latitude column")
    columns: dict[str, str] = Field(
        default_factory=dict, description="Role attribute → source column"
    )
    values: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Role attribute → {source value: openFTTH value}, e.g. road classes",
    )


class ImportProfile(_Strict):
    format: str = Field("openftth.import", pattern=r"^openftth\.import$")
    version: int = 1
    name: str = "import"
    description: str = ""
    bindings: list[Binding]

    def by_role(self, role: str) -> list[Binding]:
        return [b for b in self.bindings if b.role == role]

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(exclude_defaults=False)
        p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> ImportProfile:
        with Path(path).open(encoding="utf-8") as fh:
            return cls.model_validate(yaml.safe_load(fh) or {})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def guess_columns(role: str, columns: list[str]) -> dict[str, str]:
    """Source columns for a role's attributes, matched on known name fragments."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for attr, names in ROLE_FIELDS.get(role, {}).items():
        best = None
        for n in names:
            for c in columns:
                if c in used:
                    continue
                nc = _norm(c)
                if nc == _norm(n):
                    best = c
                    break
            if best:
                break
        if best is None:
            for n in names:
                hit = next(
                    (c for c in columns if c not in used and _norm(n) and _norm(n) in _norm(c)),
                    None,
                )
                if hit:
                    best = hit
                    break
        if best is not None:
            out[attr] = best
            used.add(best)
    return out


def guess_role(info: LayerInfo) -> Role | None:
    """What a layer most probably is, from its geometry, names and columns."""
    name = _norm(f"{Path(info.path).stem} {info.layer}")
    if Path(info.path).suffix.lower() in (".osm", ".pbf"):
        return "osm"
    geom = info.geometry.lower()

    def hinted(role: str) -> bool:
        return any(_norm(h) in name for h in ROLE_HINTS[role])

    cols = {_norm(c) for c in info.columns}
    if "polygon" in geom:
        if hinted("no_dig_zones"):
            return "no_dig_zones"
        if hinted("area") or (0 < info.count <= 3):
            return "area"
        return "buildings"
    if "line" in geom:
        return "existing_ducts" if hinted("existing_ducts") else "streets"
    if "point" in geom or geom == "wkt":
        for role in ("poles", "manholes", "addresses"):
            if hinted(role):
                return role  # type: ignore[return-value]
        if {"housenumber", "huisnummer", "addrhousenumber", "uprn"} & cols:
            return "addresses"
        return "addresses"
    return None


def propose_profile(
    paths: list[str | Path], name: str = "import"
) -> tuple[ImportProfile, list[LayerInfo]]:
    """A first profile for a set of files: every layer whose role can be guessed is bound,
    with its columns matched. The engineer confirms or corrects it."""
    infos: list[LayerInfo] = []
    bindings: list[Binding] = []
    for path in paths:
        for info in list_layers(path):
            infos.append(info)
            role = guess_role(info)
            if role is None:
                continue
            b = Binding(path=str(Path(path).resolve()), layer=info.layer, role=role)
            if Path(path).suffix.lower() in (".csv", ".txt"):
                from .sources import csv_coordinates

                x, y, _ = csv_coordinates(info.columns)
                b.x, b.y = x, y
                b.layer = None
            if role == "osm":
                b.layer = None
            b.columns = guess_columns(role, info.columns)
            bindings.append(b)
    return ImportProfile(name=name, bindings=bindings), infos
