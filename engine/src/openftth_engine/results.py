"""The contract every engine tool returns.

Agents see exactly this. Numbers live in `decisions[].values`; the LLM only narrates them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Decision(BaseModel):
    stage: str
    tool: str
    element: str | None = Field(None, description="element_uuid of the element concerned, if any")
    reason: str
    values: dict[str, Any] = Field(default_factory=dict)
    source: str = Field("solver", description="solver | rulebook | assumption | user")


class ToolResult(BaseModel):
    tool: str
    summary: str
    changes: dict[str, int] = Field(
        default_factory=lambda: {"added": 0, "removed": 0, "modified": 0}
    )
    decisions: list[Decision] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    version: str | None = Field(None, description="project commit id after this tool ran")
    data: dict[str, Any] = Field(default_factory=dict, description="tool-specific payload")
