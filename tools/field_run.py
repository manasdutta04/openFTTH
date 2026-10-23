"""Run the design engine over every field-test area and collect what an operator would look at:
homes found, equipment placed, cable lengths, violations, cost per unit, wall time, and any
crash. Writes `runs/field/results.json` and prints a table. Run from the repo root:

    python tools/field_run.py [--only name] [--rulebook generic-gpon] [--time-limit 20]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELD = ROOT / "fixtures" / "field"
RUNS = ROOT / "runs" / "field"


def areas() -> list[tuple[str, Path, Path | None]]:
    """(name, area or profile, osm). An area designed from an import profile has no osm here:
    the profile names its own sources (an official address register, for example)."""
    out: list[tuple[str, Path, Path | None]] = []
    for geo in sorted(FIELD.glob("*.geojson")):
        osm = FIELD / "raw" / f"{geo.stem}.osm"
        if osm.exists():
            out.append((geo.stem, geo, osm))
    for profile in sorted(FIELD.glob("*.profile.yaml")):
        name = profile.name.removesuffix(".profile.yaml")
        if (FIELD / "raw" / f"{name.removesuffix('-register')}-addresses.geojson").exists():
            out.append((name, profile, None))
    out.append(("gent", ROOT / "fixtures" / "gent" / "area.geojson", ROOT / "fixtures" / "gent" / "gent.osm"))
    out.append(("gent-small", ROOT / "fixtures" / "gent" / "area-small.geojson", ROOT / "fixtures" / "gent" / "gent.osm"))
    return out


def run_one(name: str, geo: Path, osm: Path | None, rulebook: str, time_limit: float) -> dict:
    out = RUNS / name
    t0 = time.time()
    inputs = ["--profile", str(geo)] if osm is None else ["--area", str(geo), "--osm", str(osm)]
    proc = subprocess.run(
        [
            "uv", "run", "openftth-engine", "design", *inputs, "--rulebook", rulebook,
            "--out", str(out), "--time-limit", str(time_limit), "--fresh",
        ],
        cwd=ROOT / "engine",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = round(time.time() - t0, 1)
    rec: dict = {"name": name, "elapsed_s": elapsed, "exit": proc.returncode}
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-25:]
        rec["error"] = "\n".join(tail)
        return rec
    summary = json.loads((out / "deliverables" / "summary.json").read_text(encoding="utf-8"))
    validation = json.loads((out / "deliverables" / "validation.json").read_text(encoding="utf-8"))
    rec.update(
        {
            "homes": summary.get("homes"),
            "units": summary.get("units"),
            "fats": summary.get("fats"),
            "fdhs": summary.get("fdhs"),
            "pons": summary.get("pons"),
            "feeder_m": summary.get("feeder_m"),
            "distribution_m": summary.get("distribution_m"),
            "drop_m": summary.get("drop_m"),
            "boq_total": summary.get("boq_total"),
            "cost_per_unit": summary.get("cost_per_unit"),
            "violations": validation.get("summary"),
            "worst_db": (validation.get("power_budget") or {}).get("worst_db"),
            "warnings": summary.get("warnings", []),
        }
    )
    # The pipeline log line by line, for the notes.
    rec["log"] = [line for line in proc.stdout.splitlines() if "│" in line][:80]
    return rec


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # the engine prints box-drawing characters
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="one area name, or a comma-separated list")
    ap.add_argument("--rulebook", default="generic-gpon")
    ap.add_argument("--time-limit", type=float, default=5.0)
    args = ap.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    results_path = RUNS / "results.json"
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else []
    for name, geo, osm in areas():
        if args.only and name not in args.only.split(","):
            continue
        print(f"== {name}", flush=True)
        rec = run_one(name, geo, osm, args.rulebook, args.time_limit)
        results = [r for r in results if r["name"] != name] + [rec]
        results_path.write_text(json.dumps(results, indent=1), encoding="utf-8")
        if "error" in rec:
            print(f"   FAILED after {rec['elapsed_s']} s:\n   " + rec["error"].replace("\n", "\n   "), flush=True)
        else:
            v = rec["violations"] or {}
            print(
                f"   {rec['elapsed_s']} s · {rec['homes']} homes / {rec['units']} units · {rec['fats']} FATs · {rec['fdhs']} FDHs · "
                f"{rec['cost_per_unit']:,.0f}/unit · violations {sum(v.values()) if isinstance(v, dict) else v} · worst {rec['worst_db']} dB",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
