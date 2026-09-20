"""Normalize Better Stack's public JSON:API status page without scraping HTML."""

from __future__ import annotations

from typing import Any

from .sources import SourceResult, SourceSpec, object_list, parse_statuspage, parse_timestamp

STATUS_MAP = {
    "operational": "operational",
    "resolved": "operational",
    "degraded": "degraded_performance",
    "downtime": "major_outage",
    "maintenance": "under_maintenance",
}


def _attributes(record: dict[str, Any]) -> dict[str, Any]:
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        raise ValueError("Better Stack attributes are missing")
    return attributes


def _linked(
    records: dict[tuple[str, str], dict[str, Any]], owner: dict[str, Any], field: str, kind: str,
) -> list[dict[str, Any]]:
    relationships = owner.get("relationships")
    if not isinstance(relationships, dict) or not isinstance(relationships.get(field), dict):
        raise ValueError(f"Better Stack relationship {field} is missing")
    references = object_list(relationships[field].get("data"), field)
    result = []
    for ref in references:
        record = records.get((kind, str(ref.get("id", ""))))
        if ref.get("type") != kind or record is None:
            raise ValueError(f"Better Stack included {field} record is missing")
        result.append(record)
    return result


def parse_betterstack(spec: SourceSpec, payload: Any, notify_maintenance: bool) -> SourceResult:
    """Validate linked resources and reports, preserving valid current data on history errors."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Better Stack status page data is missing")
    page = payload["data"]
    if page.get("type") != "status_page":
        raise ValueError("Unexpected Better Stack data type")
    attributes = _attributes(page)
    aggregate = attributes.get("aggregate_state")
    if aggregate not in STATUS_MAP:
        raise ValueError("Unknown Better Stack aggregate state")
    included = object_list(payload.get("included"), "Better Stack included")
    records = {(str(x.get("type")), str(x.get("id"))): x for x in included}
    resources = _linked(records, page, "resources", "status_page_resource")
    components = []
    names = {}
    errors = []
    for resource in resources:
        value = _attributes(resource)
        name = value.get("public_name")
        if not name:
            raise ValueError("Better Stack resource name is missing")
        names[str(resource["id"])] = str(name)
        status = value.get("status")
        if status not in STATUS_MAP:
            errors.append(f"Unconfirmed resource status: {name}")
            continue
        # Page refresh timestamps and uptime percentages must not produce alert updates.
        components.append({"id": str(resource["id"]), "name": name, "status": STATUS_MAP[status]})
    summary = {
        "components": components,
        "status": {"indicator": {"operational": "none", "resolved": "none", "degraded": "minor",
                                 "downtime": "major", "maintenance": "maintenance"}[aggregate],
                   "description": "Scheduled maintenance" if aggregate == "maintenance" else str(aggregate)},
    }
    incidents = []
    history_complete = True
    try:
        reports = _linked(records, page, "status_reports", "status_report")
        for report in reports:
            value = _attributes(report)
            if not value.get("title") or not value.get("aggregate_state"):
                raise ValueError("Better Stack report title and aggregate state are required")
            affected = object_list(value.get("affected_resources"), "affected_resources")
            updates = _linked(records, report, "status_updates", "status_update")
            normalized_updates = []
            states = [str(x.get("status")) for x in affected]
            resolved_at = ""
            for update in updates:
                content = _attributes(update)
                published = content.get("published_at")
                if not parse_timestamp(published):
                    raise ValueError("Better Stack update timestamp is invalid")
                update_affected = object_list(content.get("affected_resources"), "update.affected_resources")
                update_resolved = bool(update_affected) and all(x.get("status") == "resolved" for x in update_affected)
                if update_resolved and parse_timestamp(published) > parse_timestamp(resolved_at):
                    resolved_at = str(published)
                states.extend(str(x.get("status")) for x in update_affected)
                normalized_updates.append({"body": content.get("message", ""), "created_at": published,
                                           "status": "resolved" if update_resolved else "investigating"})
            maintenance = value.get("report_type") == "maintenance" or value["aggregate_state"] == "maintenance"
            resolved = value["aggregate_state"] == "resolved"
            severity_states = states if resolved else [str(x.get("status")) for x in affected]
            impact = "maintenance" if maintenance else "major" if "downtime" in severity_states else "minor"
            # ends_at can be a scheduled maintenance end, not an actual resolution.
            if resolved and not resolved_at:
                resolved_at = str(value.get("ends_at") or "")
            latest_at = max((str(x["created_at"]) for x in normalized_updates), key=parse_timestamp, default="")
            incidents.append({
                "id": str(report["id"]), "name": str(value["title"]),
                "status": "resolved" if resolved else "in_progress" if maintenance else "investigating",
                "impact": impact, "created_at": value.get("starts_at", ""),
                "updated_at": latest_at, "resolved_at": resolved_at if resolved else "",
                "components": [{"id": str(x.get("status_page_resource_id")),
                                "name": names.get(str(x.get("status_page_resource_id")), "Unknown service")}
                               for x in affected],
                "incident_updates": normalized_updates,
            })
    except ValueError as exc:
        errors.append(str(exc))
        history_complete = False
    result = parse_statuspage(spec, summary, {"incidents": incidents}, notify_maintenance)
    if errors:
        result.complete = False
        result.history_complete = history_complete
        result.error = "; ".join(errors)
    return result
