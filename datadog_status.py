"""Adapt the public Datadog Status Pages snapshot used by OpenRouter."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .sources import SourceResult, SourceSpec, object_list, parse_statuspage, parse_timestamp

COMPONENT_STATES = {"operational", "maintenance", "degraded", "partial_outage", "major_outage"}


def _components(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        raise ValueError("Datadog component nesting exceeds limit")
    result = []
    for component in object_list(value, "Datadog components"):
        if not component.get("id") or not component.get("name"):
            raise ValueError("Datadog component id and name are required")
        if component.get("type") == "ComponentGroup":
            result.extend(_components(component.get("components"), depth + 1))
        else:
            if component.get("status") not in COMPONENT_STATES:
                raise ValueError("Unknown Datadog component status")
            result.append(component)
    return result


def _incident(value: dict[str, Any], maintenance: bool) -> dict[str, Any]:
    if not value.get("id") or not value.get("title") or not value.get("currentStatus"):
        raise ValueError("Datadog incident id, title and currentStatus are required")
    status = value["currentStatus"]
    resolved = status in {"resolved", "completed", "canceled"}
    if not maintenance and isinstance(value.get("resolved"), bool) and value["resolved"] != resolved:
        raise ValueError("Contradictory Datadog resolution flags")
    affected = _components(value.get("componentsAffected", []))
    timeline = object_list(value.get("timeline", []), "Datadog timeline")
    updates = []
    resolution_dates = []
    historical_states = [x["status"] for x in affected]
    for update in timeline:
        # Imported incidents can have a recent createdAt but a much older effective startedAt.
        timestamp = update.get("startedAt") or update.get("createdAt")
        if not parse_timestamp(timestamp):
            raise ValueError("Datadog timeline timestamp is invalid")
        update_status = update.get("status", "")
        terminal = update_status in {"resolved", "completed", "canceled"}
        if terminal:
            resolution_dates.append(str(timestamp))
        historical_states.extend(x["status"] for x in _components(update.get("componentsAffected", [])))
        updates.append({"body": update.get("description", ""), "created_at": timestamp,
                        "status": "completed" if maintenance and terminal else "resolved" if terminal else update_status})
    states = historical_states if resolved else [x["status"] for x in affected]
    impact = "maintenance" if maintenance else "major" if "major_outage" in states else "minor"
    resolved_at = str(value.get("completedDate" if maintenance else "resolvedDate") or "")
    if resolved and not resolved_at:
        resolved_at = max(resolution_dates, key=parse_timestamp, default="")
    return {
        "id": str(value["id"]), "name": value["title"],
        "status": "completed" if maintenance and resolved else "resolved" if resolved else status,
        "impact": impact, "components": affected,
        "created_at": value.get("startDate" if maintenance else "publishedDate", ""),
        "updated_at": max((str(x["created_at"]) for x in updates), key=parse_timestamp, default=""),
        "resolved_at": resolved_at if resolved else "", "incident_updates": updates,
        "body": value.get("description") or value.get("scheduledDescription", ""),
    }


def parse_datadog(spec: SourceSpec, payload: Any, notify_maintenance: bool) -> SourceResult:
    """Normalize grouped components and incident timelines with independent validation."""
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ValueError("Datadog status snapshot is missing")
    if payload.get("customDomain") != urlparse(spec.status_url).hostname:
        raise ValueError("Unexpected Datadog status page identity")
    errors = []
    try:
        summary = {"components": _components(payload.get("components")),
                   "status": {"indicator": "none"}}
    except ValueError as exc:
        errors.append(str(exc))
        summary = None
    incidents = []
    history_complete = True
    try:
        for incident in object_list(payload.get("incidents"), "Datadog incidents"):
            incidents.append(_incident(incident, False))
        if notify_maintenance:
            for maintenance in object_list(payload.get("maintenances") or [], "Datadog maintenance"):
                incidents.append(_incident(maintenance, True))
    except ValueError as exc:
        errors.append(str(exc))
        history_complete = False
    if summary is None and not history_complete:
        raise ValueError("; ".join(errors))
    result = parse_statuspage(spec, summary, {"incidents": incidents}, notify_maintenance)
    if errors:
        result.complete = False
        result.history_complete = history_complete
        result.error = "; ".join(errors)
    return result
