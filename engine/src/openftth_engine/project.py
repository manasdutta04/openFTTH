"""A project on disk: the interchange bundle, the rulebook, the twin, and a git history.

Versions are commits made with dulwich (pure Python, no git.exe). Every tool call that changes
the design commits once through this class, which is the single writer.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from dulwich import porcelain
from dulwich.repo import Repo

from .interchange import Design
from .results import Decision, ToolResult

AUTHOR = b"openFTTH engine <engine@openftth.local>"
BUNDLE_DIR = "design"
TWIN_DIR = "twin"
DELIVER_DIR = "deliverables"
DECISIONS_FILE = "decisions.ndjson"
META_FILE = "project.json"


class Project:
    def __init__(
        self, directory: str | Path, project_id: str | None = None, create: bool = True
    ) -> None:
        self.dir = Path(directory).resolve()
        self._lock = threading.Lock()
        if (self.dir / ".git").exists():
            self.repo = Repo(str(self.dir))
        elif create:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.repo = porcelain.init(str(self.dir))
            (self.dir / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        else:
            raise FileNotFoundError(f"no project at {self.dir}")
        meta_path = self.dir / META_FILE
        if meta_path.exists():
            self.meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
        else:
            self.meta = {
                "project_id": project_id or self.dir.name,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "stage": "ingest",
            }
            self._write_meta()
        for sub in (BUNDLE_DIR, TWIN_DIR, DELIVER_DIR):
            (self.dir / sub).mkdir(exist_ok=True)

    # ---- paths -----------------------------------------------------------------------------

    @property
    def project_id(self) -> str:
        return str(self.meta["project_id"])

    @property
    def bundle_dir(self) -> Path:
        return self.dir / BUNDLE_DIR

    @property
    def twin_dir(self) -> Path:
        return self.dir / TWIN_DIR

    @property
    def deliver_dir(self) -> Path:
        return self.dir / DELIVER_DIR

    def _write_meta(self) -> None:
        (self.dir / META_FILE).write_text(
            json.dumps(self.meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    # ---- design I/O -----------------------------------------------------------------------

    def load_design(self) -> Design:
        return Design.read(self.bundle_dir)

    def save_design(self, design: Design) -> None:
        design.write(self.bundle_dir)

    def set_stage(self, stage: str) -> None:
        self.meta["stage"] = stage
        self._write_meta()

    # ---- versions -------------------------------------------------------------------------

    def commit(
        self,
        result: ToolResult,
        design: Design | None = None,
        extra_paths: list[Path] | None = None,
    ) -> str | None:
        """Write the design, append decisions, and commit everything in the project dir."""
        with self._lock:
            if design is not None:
                self.save_design(design)
            if result.decisions:
                with (self.dir / DECISIONS_FILE).open("a", encoding="utf-8") as fh:
                    for d in result.decisions:
                        fh.write(
                            json.dumps({"tool": result.tool, **d.model_dump()}, sort_keys=True)
                            + "\n"
                        )
            self._write_meta()
            paths = [
                str(p.relative_to(self.dir)).replace("\\", "/")
                for p in self.dir.rglob("*")
                if p.is_file() and ".git" not in p.parts
            ]
            porcelain.add(self.repo, paths=[str(self.dir / p) for p in paths])
            # Stage deletions of files that vanished (e.g. a bundle layer that emptied).
            index = self.repo.open_index()
            missing = [
                p.decode("utf-8") for p in index if not (self.dir / p.decode("utf-8")).exists()
            ]
            if missing:
                porcelain.remove(self.repo, paths=[str(self.dir / m) for m in missing], cached=True)
            status = porcelain.status(self.repo)
            staged = status.staged
            if not (staged["add"] or staged["modify"] or staged["delete"]):
                return self.head()
            message = (
                f"[{self.meta.get('stage', '?')}] {result.tool}: {result.summary}\n\n"
                + json.dumps(
                    {
                        "changes": result.changes,
                        "decisions": len(result.decisions),
                        "warnings": result.warnings,
                    },
                    sort_keys=True,
                )
            )
            sha = porcelain.commit(
                self.repo, message=message.encode("utf-8"), author=AUTHOR, committer=AUTHOR
            )
            version = sha.decode("ascii") if isinstance(sha, bytes) else str(sha)
            result.version = version
            return version

    def head(self) -> str | None:
        try:
            return self.repo.head().decode("ascii")
        except KeyError:
            return None

    RECORD_REF = b"refs/network/record"

    def record(self) -> str | None:
        """The version posted as the network of record, if any work order was posted."""
        try:
            return self.repo.refs[self.RECORD_REF].decode("ascii")
        except KeyError:
            return None

    def set_record(self, version: str) -> None:
        self.repo.refs[self.RECORD_REF] = version.encode("ascii")

    def log(self, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            walker = self.repo.get_walker(max_entries=limit)
        except KeyError:
            return out
        for entry in walker:
            c = entry.commit
            msg = c.message.decode("utf-8", "replace")
            out.append(
                {
                    "version": c.id.decode("ascii"),
                    "subject": msg.split("\n", 1)[0],
                    "time": c.commit_time,
                }
            )
        return out

    def design_at(self, version: str) -> Design:
        """Read the bundle as it was at `version` into a temporary directory."""
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="openftth-ver-"))
        try:
            tree = self.repo[version.encode("ascii")].tree
            bundle_tree = self.repo[tree]
            mode, sha = bundle_tree.lookup_path(self.repo.__getitem__, BUNDLE_DIR.encode())
            sub = self.repo[sha]
            for name, _mode, blob_sha in sub.iteritems():
                (tmp / name.decode()).write_bytes(self.repo[blob_sha].data)
            return Design.read(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- decisions --------------------------------------------------------------------------

    def decisions(self) -> list[Decision]:
        p = self.dir / DECISIONS_FILE
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Decision.model_validate(json.loads(line)))
        return out
