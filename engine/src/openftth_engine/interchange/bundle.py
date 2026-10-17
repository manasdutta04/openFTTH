"""The in-memory design and its canonical GeoJSON bundle on disk.

Canonical writing (features sorted by `element_uuid`, one feature per line, fixed precision,
sorted keys) makes git diffs between versions meaningful and lets `DesignDiff` work by
identity rather than by position.

Besides spatial features the bundle holds **side tables** (splice points, fibre connections,
…): `<name>.table.json`, one row per line, rows sorted by their `uuid`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import Feature, Metadata

METADATA_FILE = "_openftth_metadata.json"
TABLE_SUFFIX = ".table.json"


@dataclass
class DesignDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def summary(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "modified": len(self.modified),
        }


class Design:
    """Interchange features keyed by `element_uuid`, side tables keyed by name, plus metadata."""

    def __init__(self, metadata: Metadata | None = None) -> None:
        self.metadata = metadata or Metadata()
        self._features: dict[str, Feature] = {}
        self.tables: dict[str, list[dict[str, Any]]] = {}

    # ---- collection API ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._features)

    def __iter__(self) -> Iterator[Feature]:
        return iter(self._features.values())

    def __contains__(self, element_uuid: str) -> bool:
        return element_uuid in self._features

    def get(self, element_uuid: str) -> Feature:
        return self._features[element_uuid]

    def add(self, feature: Feature) -> Feature:
        self._features[feature.element_uuid] = feature
        return feature

    def extend(self, features: Iterable[Feature]) -> None:
        for f in features:
            self.add(f)

    def remove(self, element_uuid: str) -> None:
        self._features.pop(element_uuid, None)

    def remove_where(self, **openftth_match: Any) -> int:
        doomed = [
            f.element_uuid
            for f in self
            if all(f.openftth.get(k) == v for k, v in openftth_match.items())
        ]
        for u in doomed:
            self.remove(u)
        return len(doomed)

    def by_type(self, element_type: str) -> list[Feature]:
        return sorted(
            (f for f in self if f.element_type == element_type), key=lambda f: f.element_uuid
        )

    def by_kind(self, kind: str) -> list[Feature]:
        return sorted((f for f in self if f.kind == kind), key=lambda f: f.element_uuid)

    def set_table(self, name: str, rows: list[dict[str, Any]]) -> None:
        """Replace a side table. Rows need a `uuid`; they are stored sorted by it."""
        for r in rows:
            if "uuid" not in r:
                raise ValueError(f"table {name}: every row needs a uuid")
        self.tables[name] = sorted(rows, key=lambda r: str(r["uuid"]))

    def copy(self) -> Design:
        d = Design(self.metadata.model_copy())
        d._features = {k: v.model_copy(deep=True) for k, v in self._features.items()}
        d.tables = {k: [dict(r) for r in v] for k, v in self.tables.items()}
        return d

    # ---- diff ----------------------------------------------------------------------------

    def diff(self, other: Design) -> DesignDiff:
        """Changes from `self` (older) to `other` (newer), by identity. Table rows count too,
        keyed as `table:<name>:<uuid>`."""
        out = DesignDiff()
        mine, theirs = self._features, other._features
        for u in sorted(theirs.keys() - mine.keys()):
            out.added.append(u)
        for u in sorted(mine.keys() - theirs.keys()):
            out.removed.append(u)
        for u in sorted(mine.keys() & theirs.keys()):
            if mine[u].canonical_json() != theirs[u].canonical_json():
                out.modified.append(u)
        mine_rows = self._row_index()
        their_rows = other._row_index()
        for k in sorted(their_rows.keys() - mine_rows.keys()):
            out.added.append(k)
        for k in sorted(mine_rows.keys() - their_rows.keys()):
            out.removed.append(k)
        for k in sorted(mine_rows.keys() & their_rows.keys()):
            if mine_rows[k] != their_rows[k]:
                out.modified.append(k)
        return out

    def _row_index(self) -> dict[str, str]:
        return {
            f"table:{name}:{row['uuid']}": _canonical_row(row)
            for name, rows in self.tables.items()
            for row in rows
        }

    # ---- bundle I/O ----------------------------------------------------------------------

    def file_groups(self) -> dict[str, list[Feature]]:
        groups: dict[str, list[Feature]] = {}
        for f in self:
            groups.setdefault(f.file_stem(), []).append(f)
        for stem in groups:
            groups[stem].sort(key=lambda f: f.element_uuid)
        return groups

    def write(self, directory: str | Path) -> list[Path]:
        """Write the canonical bundle. Removes stale files for vanished groups and tables."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        groups = self.file_groups()
        written: list[Path] = []
        for stem, feats in sorted(groups.items()):
            p = d / f"{stem}.geojson"
            _write_collection(p, stem, feats)
            written.append(p)
        for name, rows in sorted(self.tables.items()):
            p = d / f"{name}{TABLE_SUFFIX}"
            _write_table(p, rows)
            written.append(p)
        keep = {p.name for p in written}
        for stale in list(d.glob("*.geojson")) + list(d.glob(f"*{TABLE_SUFFIX}")):
            if stale.name not in keep:
                stale.unlink()
        meta_path = d / METADATA_FILE
        meta_path.write_text(
            json.dumps(self.metadata.to_table(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(meta_path)
        return written

    @classmethod
    def read(cls, directory: str | Path) -> Design:
        d = Path(directory)
        meta_path = d / METADATA_FILE
        meta = Metadata()
        if meta_path.exists():
            table = json.loads(meta_path.read_text(encoding="utf-8"))
            if table.get("format") not in (None, "openftth-design"):
                raise ValueError(f"not an openFTTH design bundle: {table.get('format')!r}")
            major = str(table.get("format_version", "1.0")).split(".")[0]
            if major != "1":
                raise ValueError(f"unsupported bundle major version {major}")
            meta = Metadata.model_validate(
                {
                    k: (int(v) if k == "crs_epsg" else v)
                    for k, v in table.items()
                    if k in Metadata.model_fields
                }
            )
        design = cls(meta)
        for p in sorted(d.glob("*.geojson")):
            fc = json.loads(p.read_text(encoding="utf-8"))
            for obj in fc.get("features", []):
                design.add(Feature.from_geojson(obj))
        for p in sorted(d.glob(f"*{TABLE_SUFFIX}")):
            name = p.name[: -len(TABLE_SUFFIX)]
            design.tables[name] = json.loads(p.read_text(encoding="utf-8"))
        return design


def _canonical_row(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _write_collection(path: Path, name: str, feats: list[Feature]) -> None:
    lines = [f.canonical_json() for f in feats]
    body = ",\n".join(lines)
    text = (
        '{"type":"FeatureCollection","name":'
        + json.dumps(name)
        + ',"features":[\n'
        + body
        + "\n]}\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_table(path: Path, rows: list[dict[str, Any]]) -> None:
    body = ",\n".join(_canonical_row(r) for r in rows)
    path.write_text("[\n" + body + "\n]\n", encoding="utf-8", newline="\n")
