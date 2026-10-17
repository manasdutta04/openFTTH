"""The in-memory design and its canonical GeoJSON-lite bundle on disk.

Canonical writing (features sorted by `element_uuid`, one feature per line, fixed precision,
sorted keys) makes git diffs between versions meaningful and lets `DesignDiff` work by
identity rather than by position.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import Feature, Metadata

METADATA_FILE = "_openftth_metadata.json"


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
    """A set of interchange features keyed by `element_uuid`, plus metadata."""

    def __init__(self, metadata: Metadata | None = None) -> None:
        self.metadata = metadata or Metadata()
        self._features: dict[str, Feature] = {}

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

    def copy(self) -> Design:
        d = Design(self.metadata.model_copy())
        d._features = {k: v.model_copy(deep=True) for k, v in self._features.items()}
        return d

    # ---- diff ----------------------------------------------------------------------------

    def diff(self, other: Design) -> DesignDiff:
        """Changes from `self` (older) to `other` (newer), by identity."""
        out = DesignDiff()
        mine, theirs = self._features, other._features
        for u in sorted(theirs.keys() - mine.keys()):
            out.added.append(u)
        for u in sorted(mine.keys() - theirs.keys()):
            out.removed.append(u)
        for u in sorted(mine.keys() & theirs.keys()):
            if mine[u].canonical_json() != theirs[u].canonical_json():
                out.modified.append(u)
        return out

    # ---- bundle I/O ----------------------------------------------------------------------

    def file_groups(self) -> dict[str, list[Feature]]:
        groups: dict[str, list[Feature]] = {}
        for f in self:
            groups.setdefault(f.file_stem(), []).append(f)
        for stem in groups:
            groups[stem].sort(key=lambda f: f.element_uuid)
        return groups

    def write(self, directory: str | Path) -> list[Path]:
        """Write the canonical bundle. Removes stale `*.geojson` files for vanished groups."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        groups = self.file_groups()
        written: list[Path] = []
        for stem, feats in sorted(groups.items()):
            p = d / f"{stem}.geojson"
            _write_collection(p, stem, feats)
            written.append(p)
        keep = {p.name for p in written}
        for stale in d.glob("*.geojson"):
            if stale.name not in keep:
                stale.unlink()
        meta_path = d / METADATA_FILE
        meta_path.write_text(
            json.dumps(self.metadata.to_table(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
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
                raise ValueError(f"unsupported interchange major version {major}")
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
        return design


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
