"""Loading rulebooks from YAML and locating the shipped presets."""

from __future__ import annotations

from pathlib import Path

import yaml

from .model import Rulebook


def _repo_rulebooks_dir() -> Path | None:
    """`rulebooks/` next to the engine when running from a checkout."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "rulebooks"
        if (candidate / "generic-gpon.yaml").exists():
            return candidate
    return None


def preset_path(name: str) -> Path:
    d = _repo_rulebooks_dir()
    if d is None:
        raise FileNotFoundError("rulebooks/ directory not found next to the engine")
    p = d / f"{name}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"no rulebook preset named {name!r} in {d}")
    return p


def list_presets() -> list[str]:
    d = _repo_rulebooks_dir()
    if d is None:
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def load_rulebook(path_or_name: str | Path) -> Rulebook:
    """Load a rulebook from a YAML path, or from a preset name such as `belgium`."""
    p = Path(path_or_name)
    if not p.exists():
        p = preset_path(str(path_or_name))
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Rulebook.model_validate(data)
