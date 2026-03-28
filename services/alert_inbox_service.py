from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from services.alert_inbox import store_pending_alerts
from services.alertmanager_client import AlertmanagerParseError


def create_app(*, inbox_root: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="HERALD Alert Inbox Service")

    @app.post("/alerts", status_code=202)
    async def alerts(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:  # pragma: no cover - framework-level parsing guard
            raise HTTPException(status_code=400, detail="Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Alertmanager payload must be a JSON object.")
        try:
            records = store_pending_alerts(payload, inbox_root=inbox_root)
        except AlertmanagerParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "status": "accepted",
            "stored": len(records),
            "artifacts": [
                {
                    "artifact_id": record.artifact_id,
                    "incident_id": record.incident_id,
                    "incident_class": record.incident_class,
                    "status": record.status,
                    "artifact_dir": record.artifact_dir,
                }
                for record in records
            ],
        }

    return app


app = create_app()
