"""Stable identifiers.

An `element_uuid` is at most 64 characters and is never regenerated. openFTTH derives it
deterministically from (project id, element type, local key) so that re-running the same
design on the same inputs yields the same ids, and diffs between versions are by identity.
"""

from __future__ import annotations

import uuid

NAMESPACE = uuid.UUID("6f3f1b0e-9f1c-5b4a-8f2a-0f9d8c7b6a51")


def stable_uuid(project_id: str, element_type: str, local_key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{project_id}|{element_type}|{local_key}"))
