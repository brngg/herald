from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from schemas.incident import Incident


@dataclass(slots=True)
class AlertmanagerParseError(ValueError):
    message: str

    def __str__(self) -> str:
        return self.message


def incidents_from_alertmanager_payload(payload: dict[str, Any]) -> list[Incident]:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        raise AlertmanagerParseError("Alertmanager payload must include a non-empty alerts list")

    incidents: list[Incident] = []
    for alert in alerts:
        incidents.append(_incident_from_alert(payload, alert))
    return incidents


def _incident_from_alert(payload: dict[str, Any], alert: dict[str, Any]) -> Incident:
    labels = _require_dict(alert, "labels")
    annotations = _optional_dict(alert.get("annotations"))

    incident_id = _first_non_empty(
        alert.get("fingerprint"),
        payload.get("groupKey"),
    )
    if incident_id is None:
        raise AlertmanagerParseError("Alertmanager payload missing fingerprint and groupKey")

    incident_class = _first_non_empty(
        labels.get("incident_class"),
        labels.get("alertname"),
    )
    if incident_class is None:
        raise AlertmanagerParseError("Alertmanager alert missing incident_class and alertname")

    starts_at = alert.get("startsAt")
    if not isinstance(starts_at, str):
        raise AlertmanagerParseError("Alertmanager alert missing startsAt")

    detected_at = _parse_rfc3339(starts_at)
    raw_context = {
        "receiver": payload.get("receiver"),
        "status": payload.get("status"),
        "group_labels": _optional_dict(payload.get("groupLabels")),
        "common_labels": _optional_dict(payload.get("commonLabels")),
        "common_annotations": _optional_dict(payload.get("commonAnnotations")),
        "external_url": payload.get("externalURL"),
        "version": payload.get("version"),
        "group_key": payload.get("groupKey"),
        "truncated_alerts": payload.get("truncatedAlerts"),
        "alert": {
            "status": alert.get("status"),
            "labels": labels,
            "annotations": annotations,
            "starts_at": starts_at,
            "ends_at": alert.get("endsAt"),
            "generator_url": alert.get("generatorURL"),
            "fingerprint": alert.get("fingerprint"),
        },
    }

    return Incident(
        incident_id=incident_id,
        incident_class=incident_class,
        detected_at=detected_at,
        source="prometheus",
        raw_context=raw_context,
    )


def _parse_rfc3339(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AlertmanagerParseError(f"invalid startsAt timestamp: {value}") from exc


def _require_dict(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise AlertmanagerParseError(f"Alertmanager alert missing {key}")
    return value


def _optional_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AlertmanagerParseError("expected dict value in Alertmanager payload")
    return value


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None
