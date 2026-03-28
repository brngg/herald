from __future__ import annotations

from copy import deepcopy
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from schemas.incident import Incident
from services.alertmanager_client import incidents_from_alertmanager_payload
from services.incident_normalization import normalize_incident_class


InboxStatus = Literal[
    "pending_investigation",
    "ignored",
    "planning_started",
    "pending_execution_approval",
    "completed",
]

VALID_INBOX_STATUSES: tuple[InboxStatus, ...] = (
    "pending_investigation",
    "ignored",
    "planning_started",
    "pending_execution_approval",
    "completed",
)

_UNSET = object()


@dataclass(slots=True)
class InboxArtifactRecord:
    artifact_id: str
    artifact_dir: str
    incident_id: str
    incident_class: str
    source: str
    arrival_timestamp: str
    status: InboxStatus
    incident_metadata: dict[str, Any]
    raw_payload: dict[str, Any]
    gate0_decision: str | None = None
    first_pass_artifact: str | None = None
    final_result_artifact: str | None = None
    claimed_by: str | None = None
    claimed_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id must be non-empty")
        if self.status not in VALID_INBOX_STATUSES:
            raise ValueError(f"unsupported inbox status: {self.status}")
        if not isinstance(self.incident_metadata, dict):
            raise TypeError("incident_metadata must be a dict")
        if not isinstance(self.raw_payload, dict):
            raise TypeError("raw_payload must be a dict")


def default_inbox_root() -> Path:
    return Path(__file__).resolve().parents[1] / "artifacts" / "inbox"


def store_pending_alerts(
    payload: dict[str, Any],
    *,
    inbox_root: str | Path | None = None,
    arrival_timestamp: str | None = None,
) -> list[InboxArtifactRecord]:
    incidents = incidents_from_alertmanager_payload(payload)
    root = _resolve_inbox_root(inbox_root)
    root.mkdir(parents=True, exist_ok=True)
    arrived_at = arrival_timestamp or _utc_now()

    records: list[InboxArtifactRecord] = []
    for index, incident in enumerate(incidents, start=1):
        artifact_id = _build_artifact_id(
            incident=incident,
            arrival_timestamp=arrived_at,
            index=index,
        )
        artifact_dir = root / artifact_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        record = InboxArtifactRecord(
            artifact_id=artifact_id,
            artifact_dir=str(artifact_dir),
            incident_id=incident.incident_id,
            incident_class=normalize_incident_class(str(incident.incident_class)),
            source=str(incident.source),
            arrival_timestamp=arrived_at,
            status="pending_investigation",
            incident_metadata=_incident_metadata(incident),
            raw_payload=_payload_for_incident(payload, alert_index=index - 1),
            updated_at=arrived_at,
        )
        save_inbox_record(record)
        records.append(record)
    return records


def list_inbox_records(
    *,
    inbox_root: str | Path | None = None,
    status: InboxStatus | None = None,
) -> list[InboxArtifactRecord]:
    root = _resolve_inbox_root(inbox_root)
    if not root.exists():
        return []

    records: list[InboxArtifactRecord] = []
    for artifact_dir in sorted(root.iterdir()):
        if not artifact_dir.is_dir():
            continue
        alert_path = artifact_dir / "alert.json"
        if not alert_path.exists():
            continue
        record = load_inbox_record(artifact_dir)
        if status is None or record.status == status:
            records.append(record)
    records.sort(key=lambda item: item.arrival_timestamp)
    return records


def list_actionable_inbox_records(
    *,
    inbox_root: str | Path | None = None,
    claimer_id: str | None = None,
    reclaim_after_seconds: float = 300.0,
) -> list[InboxArtifactRecord]:
    records = list_inbox_records(inbox_root=inbox_root)
    actionable = [
        record
        for record in records
        if record.status in {"pending_execution_approval", "pending_investigation"}
        and _record_is_claimable(
            record,
            claimer_id=claimer_id,
            reclaim_after_seconds=reclaim_after_seconds,
        )
    ]
    actionable.sort(
        key=lambda record: (
            0 if record.status == "pending_execution_approval" else 1,
            record.arrival_timestamp,
        )
    )
    return actionable


def load_inbox_record(artifact_dir: str | Path) -> InboxArtifactRecord:
    path = _artifact_json_path(artifact_dir)
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError("alert.json must contain an object")
    return InboxArtifactRecord(
        artifact_id=str(payload["artifact_id"]),
        artifact_dir=str(payload["artifact_dir"]),
        incident_id=str(payload["incident_id"]),
        incident_class=str(payload["incident_class"]),
        source=str(payload["source"]),
        arrival_timestamp=str(payload["arrival_timestamp"]),
        status=payload["status"],
        incident_metadata=dict(payload["incident_metadata"]),
        raw_payload=dict(payload["raw_payload"]),
        gate0_decision=_optional_string(payload.get("gate0_decision")),
        first_pass_artifact=_optional_string(payload.get("first_pass_artifact")),
        final_result_artifact=_optional_string(payload.get("final_result_artifact")),
        claimed_by=_optional_string(payload.get("claimed_by")),
        claimed_at=_optional_string(payload.get("claimed_at")),
        updated_at=_optional_string(payload.get("updated_at")),
        completed_at=_optional_string(payload.get("completed_at")),
    )


def save_inbox_record(record: InboxArtifactRecord) -> Path:
    artifact_dir = Path(record.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = _artifact_json_path(artifact_dir)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(asdict(record), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def update_inbox_record(
    artifact_dir: str | Path,
    *,
    status: InboxStatus,
    gate0_decision: str | None | object = _UNSET,
    first_pass_artifact: str | None | object = _UNSET,
    final_result_artifact: str | None | object = _UNSET,
    claimed_by: str | None | object = _UNSET,
    claimed_at: str | None | object = _UNSET,
    completed_at: str | None | object = _UNSET,
    expected_statuses: tuple[InboxStatus, ...] | None = None,
) -> InboxArtifactRecord:
    record = load_inbox_record(artifact_dir)
    if expected_statuses is not None and record.status not in expected_statuses:
        expected = ", ".join(expected_statuses)
        raise ValueError(
            f"Cannot update inbox artifact from status {record.status!r}; expected one of: {expected}."
        )
    updated = InboxArtifactRecord(
        artifact_id=record.artifact_id,
        artifact_dir=record.artifact_dir,
        incident_id=record.incident_id,
        incident_class=record.incident_class,
        source=record.source,
        arrival_timestamp=record.arrival_timestamp,
        status=status,
        incident_metadata=record.incident_metadata,
        raw_payload=record.raw_payload,
        gate0_decision=record.gate0_decision if gate0_decision is _UNSET else gate0_decision,
        first_pass_artifact=(
            record.first_pass_artifact if first_pass_artifact is _UNSET else first_pass_artifact
        ),
        final_result_artifact=(
            record.final_result_artifact
            if final_result_artifact is _UNSET
            else final_result_artifact
        ),
        claimed_by=record.claimed_by if claimed_by is _UNSET else claimed_by,
        claimed_at=record.claimed_at if claimed_at is _UNSET else claimed_at,
        updated_at=_utc_now(),
        completed_at=record.completed_at if completed_at is _UNSET else completed_at,
    )
    save_inbox_record(updated)
    return updated


def claim_inbox_record(
    artifact_dir: str | Path,
    *,
    claimer_id: str,
    allowed_statuses: tuple[InboxStatus, ...] = (
        "pending_investigation",
        "pending_execution_approval",
    ),
    reclaim_after_seconds: float = 300.0,
) -> InboxArtifactRecord | None:
    record = load_inbox_record(artifact_dir)
    if record.status not in allowed_statuses:
        return None
    if not _record_is_claimable(
        record,
        claimer_id=claimer_id,
        reclaim_after_seconds=reclaim_after_seconds,
    ):
        return None
    return update_inbox_record(
        artifact_dir,
        status=record.status,
        claimed_by=claimer_id,
        claimed_at=_utc_now(),
        expected_statuses=allowed_statuses,
    )


def clear_inbox_claim(
    artifact_dir: str | Path,
    *,
    expected_statuses: tuple[InboxStatus, ...] | None = None,
) -> InboxArtifactRecord:
    record = load_inbox_record(artifact_dir)
    return update_inbox_record(
        artifact_dir,
        status=record.status,
        claimed_by=None,
        claimed_at=None,
        expected_statuses=expected_statuses,
    )


def save_workflow_artifact(
    artifact_dir: str | Path,
    *,
    file_name: str,
    payload: dict[str, Any],
) -> Path:
    path = Path(artifact_dir) / file_name
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(_to_jsonable(payload), handle, default=str, indent=2)
        handle.write("\n")
    return path


def load_workflow_artifact(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError("workflow artifact JSON must contain an object")
    return payload


def _resolve_inbox_root(inbox_root: str | Path | None) -> Path:
    if inbox_root is None:
        return default_inbox_root()
    return Path(inbox_root)


def _artifact_json_path(artifact_dir: str | Path) -> Path:
    return Path(artifact_dir) / "alert.json"


def _incident_metadata(incident: Incident) -> dict[str, Any]:
    alert = incident.raw_context.get("alert")
    labels = alert.get("labels", {}) if isinstance(alert, dict) else {}
    annotations = alert.get("annotations", {}) if isinstance(alert, dict) else {}
    return {
        "incident_id": incident.incident_id,
        "incident_class": normalize_incident_class(str(incident.incident_class)),
        "source": incident.source,
        "detected_at": incident.detected_at.isoformat(),
        "alertname": labels.get("alertname"),
        "namespace": labels.get("namespace"),
        "severity": labels.get("severity"),
        "summary": annotations.get("summary"),
        "pod": labels.get("pod"),
        "container": labels.get("container"),
    }


def _payload_for_incident(payload: dict[str, Any], *, alert_index: int) -> dict[str, Any]:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise TypeError("Alertmanager payload must include an alerts list")
    single_payload = deepcopy(payload)
    single_payload["alerts"] = [deepcopy(alerts[alert_index])]
    single_payload["truncatedAlerts"] = 0
    return single_payload


def _build_artifact_id(
    *,
    incident: Incident,
    arrival_timestamp: str,
    index: int,
) -> str:
    ts = arrival_timestamp.replace(":", "").replace("-", "").replace("+00:00", "Z")
    slug = _slugify(incident.incident_id)
    unique = uuid4().hex[:8]
    return f"{ts}-{slug}-{index}-{unique}"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return slug or "incident"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected optional string value")
    return value


def _record_is_claimable(
    record: InboxArtifactRecord,
    *,
    claimer_id: str | None,
    reclaim_after_seconds: float,
) -> bool:
    del claimer_id
    return record.claimed_by is None or _claim_is_stale(record.claimed_at, reclaim_after_seconds)


def _claim_is_stale(claimed_at: str | None, reclaim_after_seconds: float) -> bool:
    if claimed_at is None:
        return False
    claimed_at_dt = _parse_iso_datetime(claimed_at)
    age_seconds = (datetime.now(UTC) - claimed_at_dt).total_seconds()
    return age_seconds >= reclaim_after_seconds


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
