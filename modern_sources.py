"""Public FlashDuty and AI Studio protocols with browser-compatible HTTP."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .sources import (
    MAX_RESPONSE_BYTES,
    SEVERITY_RANK,
    Issue,
    SourceResult,
    SourceSpec,
    clean_text,
    object_list,
    record_issue,
    source_error,
)

AISTUDIO_RPC = (
    "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
    "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListIncidentsHistory"
)


def _unix_iso(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("Invalid incident timestamp")
    try:
        seconds = float(value)
        if not 1_000_000_000 <= seconds <= 4_000_000_000:
            raise ValueError
        return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid incident timestamp") from exc


def _flash_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("FlashDuty data object is missing")
    return payload["data"]


def _flash_changes(value: Any) -> list[dict[str, Any]]:
    changes = object_list(value, "FlashDuty changes")
    for change in changes:
        if any(not change.get(key) for key in ("change_id", "title", "status")):
            raise ValueError("FlashDuty change identity, title and status are required")
        _unix_iso(change.get("start_at_seconds"))
        if change.get("close_at_seconds"):
            _unix_iso(change["close_at_seconds"])
        object_list(change.get("affected_components", []), "affected_components")
        for update in object_list(change.get("updates", []), "FlashDuty updates"):
            _unix_iso(update.get("at_seconds"))
            object_list(update.get("component_changes", []), "component_changes")
    return changes


def parse_flashduty(
    spec: SourceSpec, active: Any, history: Any, notify_maintenance: bool,
) -> SourceResult:
    """Merge the live component snapshot with explicitly resolved historical events."""
    errors: list[str] = []
    try:
        current = _flash_data(active)
        page = current.get("page")
        if not isinstance(page, dict) or page.get("custom_domain") != "status.deepseek.com":
            raise ValueError("Unexpected FlashDuty page identity")
        object_list(page.get("components"), "FlashDuty components")
        active_items = _flash_changes(current.get("active_changes"))
    except ValueError as exc:
        errors.append(str(exc))
        active_items = []
        active = None
    try:
        history_items = _flash_changes(_flash_data(history).get("items"))
    except ValueError as exc:
        errors.append(str(exc))
        history_items = []
        history = None
    if active is None and history is None:
        raise ValueError("; ".join(errors))
    result = SourceResult(
        spec, True, complete=not errors, history_complete=history is not None,
        error="; ".join(errors),
    )
    # The live endpoint wins ties and re-opened incidents over historical snapshots.
    merged: dict[str, dict[str, Any]] = {}
    for change in [*history_items, *active_items]:
        merged[str(change["change_id"])] = change
    for change_id, change in merged.items():
        maintenance = change.get("type") == "maintenance"
        if maintenance and not notify_maintenance:
            continue
        components = object_list(change.get("affected_components", []), "affected_components")
        updates = object_list(change.get("updates", []), "FlashDuty updates")
        latest = max(updates, key=lambda x: float(x["at_seconds"]), default={})
        resolved = change["status"] in {"resolved", "completed", "cancelled"}
        states = [str(x.get("status", "")) for x in components]
        if resolved:
            states.extend(str(c.get("status", "")) for u in updates
                          for c in u.get("component_changes", []))
        severity = "critical" if any(x in {"partial_outage", "full_outage", "major_outage"}
                                     for x in states) else "warning"
        if maintenance:
            severity = "maintenance"
        started_at = _unix_iso(change.get("start_at_seconds"))
        updated_at = _unix_iso(latest["at_seconds"]) if latest else started_at
        closed = change.get("close_at_seconds")
        issue = Issue(
            spec.source_id, spec.name, f"incident_{change_id}", severity,
            str(change["title"]),
            affected_services=tuple(dict.fromkeys(str(x["name"]) for x in components if x.get("name"))),
            detail=clean_text(latest.get("description") or change.get("description")),
            updated_at=updated_at, status_url=spec.status_url, started_at=started_at,
            resolved_at=(_unix_iso(closed) if closed else updated_at) if resolved else "",
        )
        record_issue(result, issue, resolved)
    return result


def parse_aistudio(spec: SourceSpec, payload: Any) -> SourceResult:
    """Parse the public protobuf-array feed; unknown stages never imply recovery."""
    entries = payload
    for _ in range(4):
        if not isinstance(entries, list):
            raise ValueError("AI Studio response must be a protobuf array")
        if not entries:
            break
        if all(isinstance(x, list) and x and isinstance(x[0], str) for x in entries):
            break
        if len(entries) != 1:
            raise ValueError("Unrecognized AI Studio response wrapper")
        entries = entries[0]
    else:
        raise ValueError("Unrecognized AI Studio response depth")
    result = SourceResult(spec, True)
    component_names = {1: "Gemini API", 2: "Multimodal Live API", 3: "Google AI Studio"}
    for entry in entries:
        if len(entry) < 6 or not entry[0] or not isinstance(entry[1], str):
            raise ValueError("Malformed AI Studio incident")
        updates, components = entry[3], entry[5]
        if not isinstance(updates, list) or not updates or not isinstance(components, list):
            raise ValueError("AI Studio updates and components are required")
        for update in updates:
            if (not isinstance(update, list) or len(update) < 4
                    or not isinstance(update[2], list) or not update[2]):
                raise ValueError("Malformed AI Studio update")
            _unix_iso(update[2][0])
        ordered = sorted(updates, key=lambda x: float(x[2][0]))
        latest = ordered[-1]
        resolved = latest[0] == 4
        severity = {0: "info", 1: "warning", 2: "critical"}.get(entry[2], "warning")
        issue = Issue(
            spec.source_id, spec.name, f"incident_{entry[0]}", severity, entry[1],
            affected_services=tuple(dict.fromkeys(component_names.get(x, f"Component {x}")
                                                  for x in components)),
            detail=clean_text(latest[3]), updated_at=_unix_iso(latest[2][0]),
            status_url=spec.status_url, started_at=_unix_iso(ordered[0][2][0]),
            resolved_at=_unix_iso(latest[2][0]) if resolved else "",
        )
        record_issue(result, issue, resolved)
    return result


class SourceHTTPError(RuntimeError):
    def __init__(self, status: int, path: str):
        self.status = status
        super().__init__(f"HTTP {status} at {path}")


async def _request(client: Any, method: str, url: str, **kwargs: Any) -> str:
    """Do not expose request headers, frontend keys or proxy credentials in errors."""
    path = urlparse(url).path
    try:
        response = await client.request(method, url, allow_redirects=False,
                                        discard_cookies=True, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise RuntimeError(f"{type(exc).__name__} at {path}") from exc
    if response.status_code != 200:
        raise SourceHTTPError(response.status_code, path)
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("Response exceeds 4 MiB")
    return response.text


async def _json(client: Any, url: str) -> Any:
    return json.loads(await _request(client, "GET", url))


def public_frontend_keys(html: str) -> list[str]:
    """Discover only keys shipped by the anonymous status page, not user credentials."""
    primary = re.findall(r'"WIu0Nc"\s*:\s*"(AIza[0-9A-Za-z_-]{35})"', html)
    candidates = re.findall(r"AIza[0-9A-Za-z_-]{35}", html)
    keys = list(dict.fromkeys([*primary, *candidates]))[:4]
    if not keys:
        raise ValueError("AI Studio public frontend configuration is unavailable")
    return keys


async def fetch_modern_source(
    spec: SourceSpec, notify_maintenance: bool, history_hours: int,
) -> SourceResult:
    """Use isolated, short-lived browser TLS sessions without a browser or login state."""
    from curl_cffi.requests import AsyncSession

    async with AsyncSession(impersonate="chrome", timeout=15, max_clients=3) as client:
        if spec.kind == "flashduty":
            now = int(datetime.now(UTC).timestamp())
            # Notification lookback is separate from retrieval; keep long incidents visible.
            days = max(90, min(168, history_hours) // 24)
            urls = [f"{spec.endpoint}/summary/active",
                    f"{spec.endpoint}/change/list?start_at_seconds={now - days * 86400}&end_at_seconds={now}"]
            payloads = await asyncio.gather(*(_json(client, u) for u in urls), return_exceptions=True)
            for value in payloads:
                if isinstance(value, asyncio.CancelledError):
                    raise value
            errors = [source_error(x) for x in payloads if isinstance(x, BaseException)]
            try:
                result = parse_flashduty(
                    spec, *(None if isinstance(x, BaseException) else x for x in payloads),
                    notify_maintenance,
                )
            except ValueError as exc:
                raise ValueError("; ".join([str(exc), *errors])) from exc
            if errors:
                result.error = "; ".join(filter(None, [result.error, *errors]))
            return result
        html = await _request(client, "GET", spec.endpoint)
        for key in public_frontend_keys(html):
            try:
                text = await _request(client, "POST", AISTUDIO_RPC, data="[]", headers={
                    "Content-Type": "application/json+protobuf",
                    "X-Goog-Api-Key": key,
                    "Referer": "https://aistudio.google.com/",
                })
                return parse_aistudio(spec, json.loads(text))
            except SourceHTTPError as exc:
                if exc.status not in {401, 403}:
                    raise
        raise ValueError("AI Studio public frontend configuration was rejected")
