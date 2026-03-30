from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ObservationBundle:
    incident_id: str
    incident_class_hint: str
    namespace_hint: str | None
    source: str
    alert_context: dict[str, Any]
    kubernetes: dict[str, Any]
    prometheus: dict[str, Any]
    collected_at: str
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.incident_id, str):
            raise TypeError("incident_id must be a str")
        if not self.incident_id:
            raise ValueError("incident_id must be non-empty")
        if not isinstance(self.incident_class_hint, str):
            raise TypeError("incident_class_hint must be a str")
        if not self.incident_class_hint:
            raise ValueError("incident_class_hint must be non-empty")
        if self.namespace_hint is not None:
            if not isinstance(self.namespace_hint, str):
                raise TypeError("namespace_hint must be a str or None")
            if not self.namespace_hint:
                raise ValueError("namespace_hint must be non-empty when provided")
        if not isinstance(self.source, str):
            raise TypeError("source must be a str")
        if not self.source:
            raise ValueError("source must be non-empty")
        if not isinstance(self.alert_context, dict):
            raise TypeError("alert_context must be a dict")
        if not isinstance(self.kubernetes, dict):
            raise TypeError("kubernetes must be a dict")
        if not isinstance(self.prometheus, dict):
            raise TypeError("prometheus must be a dict")
        if not isinstance(self.collected_at, str):
            raise TypeError("collected_at must be a str")
        if not self.collected_at:
            raise ValueError("collected_at must be non-empty")
        if not isinstance(self.errors, list):
            raise TypeError("errors must be a list[str]")
        for item in self.errors:
            if not isinstance(item, str):
                raise TypeError("errors must contain only strings")


def observation_bundle_from_dict(payload: dict[str, Any]) -> ObservationBundle:
    if not isinstance(payload, dict):
        raise TypeError("ObservationBundle payload must be a dict")

    namespace_hint = payload.get("namespace_hint")
    if namespace_hint is not None:
        namespace_hint = str(namespace_hint)

    return ObservationBundle(
        incident_id=str(payload["incident_id"]),
        incident_class_hint=str(payload["incident_class_hint"]),
        namespace_hint=namespace_hint,
        source=str(payload["source"]),
        alert_context=dict(payload["alert_context"]),
        kubernetes=dict(payload["kubernetes"]),
        prometheus=dict(payload["prometheus"]),
        collected_at=str(payload["collected_at"]),
        errors=[str(item) for item in list(payload.get("errors", []))],
    )
