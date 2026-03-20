from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


IncidentSource = Literal["prometheus", "k8s"]


@dataclass(slots=True)
class Incident:
    incident_id: str
    incident_class: str
    detected_at: datetime
    source: IncidentSource
    raw_context: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.incident_id:
            raise ValueError("incident_id must be non-empty")
        if not self.incident_class:
            raise ValueError("incident_class must be non-empty")
        if self.source not in ("prometheus", "k8s"):
            raise ValueError(f"unsupported source: {self.source}")
        if self.detected_at.tzinfo is None:
            raise ValueError("detected_at must be timezone-aware")
        if not isinstance(self.raw_context, dict):
            raise TypeError("raw_context must be a dict")
