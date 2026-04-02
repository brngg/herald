from __future__ import annotations


def normalize_incident_class(value: str) -> str:
    """Normalize incident_class values into HERALD's canonical labels."""

    normalized = value.strip().lower()
    if normalized in {"crashloop", "cpu_saturation", "bad_config", "network_partition"}:
        return normalized
    if "crashloop" in normalized:
        return "crashloop"
    if "cpu" in normalized:
        return "cpu_saturation"
    if "config" in normalized:
        return "bad_config"
    if "partition" in normalized or "dependency" in normalized:
        return "network_partition"
    return normalized
