"""Official status source adapters and normalized issue models."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import perf_counter
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import aiohttp

SEVERITY_RANK = {
    "operational": 0,
    "info": 1,
    "maintenance": 1,
    "warning": 2,
    "critical": 3,
}


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Configuration for one official status source."""

    source_id: str
    name: str
    kind: str
    endpoint: str
    status_url: str


@dataclass(frozen=True, slots=True)
class Issue:
    """Normalized active issue from any supported source."""

    source_id: str
    source_name: str
    issue_id: str
    severity: str
    title: str
    affected_services: tuple[str, ...] = ()
    detail: str = ""
    updated_at: str = ""
    status_url: str = ""
    started_at: str = ""
    resolved_at: str = ""

    @property
    def key(self) -> str:
        """Return a globally unique issue key."""
        return f"{self.source_id}:{self.issue_id}"

    @property
    def fingerprint(self) -> str:
        """Return a stable content fingerprint used for notification deduplication."""
        payload = {
            "severity": self.severity,
            "title": self.title,
            "affected_services": sorted(self.affected_services),
            "detail": self.detail,
            "updated_at": self.updated_at,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Serialize the issue for AstrBot's plugin KV store."""
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "issue_id": self.issue_id,
            "severity": self.severity,
            "title": self.title,
            "affected_services": list(self.affected_services),
            "detail": self.detail,
            "updated_at": self.updated_at,
            "status_url": self.status_url,
            "started_at": self.started_at,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Issue:
        """Restore an issue from persisted state.

        Args:
            value: Serialized issue dictionary.

        Returns:
            Restored normalized issue.
        """
        return cls(
            source_id=str(value.get("source_id", "")),
            source_name=str(value.get("source_name", "")),
            issue_id=str(value.get("issue_id", "")),
            severity=str(value.get("severity", "warning")),
            title=str(value.get("title", "Unknown issue")),
            affected_services=tuple(
                str(item) for item in value.get("affected_services", []) if item
            ),
            detail=str(value.get("detail", "")),
            updated_at=str(value.get("updated_at", "")),
            status_url=str(value.get("status_url", "")),
            started_at=str(value.get("started_at", "")),
            resolved_at=str(value.get("resolved_at", "")),
        )


@dataclass(slots=True)
class SourceResult:
    """Result of one source fetch and parse operation."""

    spec: SourceSpec
    success: bool
    issues: dict[str, Issue] = field(default_factory=dict)
    resolved_issue_ids: set[str] = field(default_factory=set)
    error: str = ""
    fetched_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    complete: bool = True
    history_complete: bool = True
    resolved_issues: dict[str, Issue] = field(default_factory=dict)
    last_success_at: str = ""

    @property
    def severity(self) -> str:
        """Return the worst active severity for overview rendering."""
        if not self.success or (not self.complete and not self.issues):
            return "unavailable"
        if not self.issues:
            return "operational"
        return max(
            (issue.severity for issue in self.issues.values()),
            key=lambda value: SEVERITY_RANK.get(value, 2),
        )


BUILTIN_SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "openai",
        "OpenAI",
        "statuspage",
        "https://status.openai.com",
        "https://status.openai.com/",
    ),
    SourceSpec(
        "openrouter",
        "OpenRouter",
        "datadog",
        "https://status.openrouter.ai/config.json",
        "https://status.openrouter.ai/",
    ),
    SourceSpec(
        "claude",
        "Claude / Anthropic",
        "statuspage",
        "https://status.claude.com",
        "https://status.claude.com/",
    ),
    SourceSpec(
        "google_vertex_gemini",
        "Google Vertex AI / Gemini",
        "google",
        "https://status.cloud.google.com/incidents.json",
        "https://status.cloud.google.com/",
    ),
    SourceSpec(
        "gemini_developer",
        "Gemini Developer API / AI Studio",
        "aistudio",
        "https://aistudio.google.com/status",
        "https://aistudio.google.com/status",
    ),
    SourceSpec(
        "groq",
        "Groq",
        "statuspage",
        "https://groqstatus.com",
        "https://groqstatus.com/",
    ),
    SourceSpec(
        "cohere",
        "Cohere",
        "statuspage",
        "https://status.cohere.com",
        "https://status.cohere.com/",
    ),
    SourceSpec(
        "moonshot",
        "Moonshot AI / Kimi",
        "statuspage",
        "https://status.moonshot.cn",
        "https://status.moonshot.cn/",
    ),
    SourceSpec(
        "minimax",
        "MiniMax",
        "statuspage",
        "https://status.minimaxi.com",
        "https://status.minimaxi.com/",
    ),
    SourceSpec(
        "fireworks",
        "Fireworks AI",
        "statuspage",
        "https://status.fireworks.ai",
        "https://status.fireworks.ai/",
    ),
    SourceSpec(
        "novita",
        "Novita AI",
        "betterstack",
        "https://status.novita.ai/index.json",
        "https://status.novita.ai/",
    ),
    SourceSpec(
        "xai",
        "xAI",
        "rss",
        "https://status.x.ai/feed.xml",
        "https://status.x.ai/",
    ),
    SourceSpec(
        "deepseek",
        "DeepSeek",
        "flashduty",
        "https://status.deepseek.com/api/status-page/6410630422455",
        "https://status.deepseek.com/",
    ),
    SourceSpec(
        "cursor",
        "Cursor",
        "statuspage",
        "https://status.cursor.com",
        "https://status.cursor.com/",
    ),
    SourceSpec(
        "cerebras",
        "Cerebras",
        "statuspage",
        "https://status.cerebras.ai",
        "https://status.cerebras.ai/",
    ),
    SourceSpec(
        "aws",
        "Amazon Web Services",
        "rss",
        "https://status.aws.amazon.com/rss/all.rss",
        "https://health.aws.amazon.com/health/status",
    ),
    SourceSpec(
        "azure",
        "Microsoft Azure",
        "rss",
        "https://rssfeed.azure.status.microsoft/en-us/status/feed/",
        "https://azure.status.microsoft/en-us/status",
    ),
    SourceSpec(
        "github",
        "GitHub",
        "statuspage",
        "https://www.githubstatus.com",
        "https://www.githubstatus.com/",
    ),
    SourceSpec(
        "vercel",
        "Vercel",
        "statuspage",
        "https://www.vercel-status.com",
        "https://www.vercel-status.com/",
    ),
    SourceSpec(
        "cloudflare",
        "Cloudflare",
        "statuspage",
        "https://www.cloudflarestatus.com",
        "https://www.cloudflarestatus.com/",
    ),
)


def build_source_specs(
    source_config: dict[str, Any] | None,
    custom_sources: list[dict[str, Any]] | None,
) -> list[SourceSpec]:
    """Build enabled built-in and custom source specifications.

    Args:
        source_config: Mapping of built-in source IDs to enabled flags.
        custom_sources: Statuspage source entries from plugin configuration.

    Returns:
        Valid, de-duplicated source specifications.
    """
    enabled = source_config if isinstance(source_config, dict) else {}
    specs = [spec for spec in BUILTIN_SOURCES if enabled.get(spec.source_id, True)]
    seen_endpoints = {spec.endpoint.lower().rstrip("/") for spec in specs}

    for item in custom_sources or []:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        name = str(item.get("name", "")).strip()
        base_url = str(item.get("base_url", "")).strip().rstrip("/")
        parsed = urlparse(base_url)
        if not name or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if base_url.lower() in seen_endpoints:
            continue
        source_hash = hashlib.sha256(base_url.lower().encode("utf-8")).hexdigest()[:12]
        specs.append(
            SourceSpec(
                source_id=f"custom_{source_hash}",
                name=name,
                kind="statuspage",
                endpoint=base_url,
                status_url=f"{base_url}/",
            )
        )
        seen_endpoints.add(base_url.lower())
    return specs


def clean_text(value: Any, limit: int = 4000) -> str:
    """Normalize HTML or Markdown-like status text into compact plain text."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"[*_`~]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _status_severity(status: str) -> str:
    normalized = status.lower().strip()
    if normalized in {"major_outage", "critical", "major"}:
        return "critical"
    if normalized in {
        "partial_outage",
        "degraded_performance",
        "minor",
        "warning",
    }:
        return "warning"
    if normalized in {"under_maintenance", "maintenance"}:
        return "maintenance"
    if normalized in {"none", "operational", "available", "ok"}:
        return "operational"
    return "warning"


def _latest_incident_update(incident: dict[str, Any]) -> dict[str, Any]:
    updates = incident.get("incident_updates", [])
    if not isinstance(updates, list) or not updates:
        return {}
    return max(
        (update for update in updates if isinstance(update, dict)),
        key=lambda update: str(
            update.get("updated_at") or update.get("created_at") or ""
        ),
        default={},
    )


def object_list(value: Any, label: str) -> list[dict[str, Any]]:
    """Reject malformed collections instead of turning them into healthy results."""
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ValueError(f"{label} must be an array of objects")
    return value


def record_issue(result: SourceResult, issue: Issue, resolved: bool) -> None:
    """Keep resolved event content for bounded catch-up notifications."""
    if resolved:
        result.resolved_issue_ids.add(issue.issue_id)
        result.resolved_issues[issue.issue_id] = issue
    else:
        result.issues[issue.issue_id] = issue


def _statuspage_incidents(value: Any) -> list[dict[str, Any]]:
    incidents = object_list(value, "Statuspage incidents")
    for incident in incidents:
        if any(not incident.get(key) for key in ("id", "name", "status")):
            raise ValueError("Incident id, name and status are required")
        object_list(incident.get("incident_updates", []), "incident_updates")
        object_list(incident.get("components", []), "incident.components")
    return incidents


def parse_statuspage(
    spec: SourceSpec,
    summary: dict[str, Any] | None,
    incidents_payload: dict[str, Any] | None,
    notify_maintenance: bool,
) -> SourceResult:
    """Merge validated current and historical data, retaining usable partial results."""
    errors: list[str] = []
    try:
        if not isinstance(summary, dict):
            raise ValueError("summary.json unavailable")
        components = object_list(summary.get("components"), "summary.components")
        for component in components:
            if not component.get("id") or not component.get("status"):
                raise ValueError("Component id and status are required")
        status = summary.get("status")
        if (not isinstance(status, dict) or not isinstance(status.get("indicator"), str)
                or not status["indicator"].strip()):
            raise ValueError("summary.status.indicator is missing")
        _statuspage_incidents(summary.get("incidents", []))
        _statuspage_incidents(summary.get("scheduled_maintenances", []))
    except ValueError as exc:
        errors.append(str(exc))
        summary = None
    try:
        if not isinstance(incidents_payload, dict):
            raise ValueError("incidents.json unavailable")
        _statuspage_incidents(incidents_payload.get("incidents"))
    except ValueError as exc:
        errors.append(str(exc))
        incidents_payload = None
    if summary is None and incidents_payload is None:
        raise ValueError("; ".join(errors))

    result = SourceResult(
        spec, True, complete=not errors,
        history_complete=incidents_payload is not None, error="; ".join(errors),
    )
    candidates = list((incidents_payload or {}).get("incidents", []))
    candidates.extend((summary or {}).get("incidents", []))
    if notify_maintenance:
        candidates.extend((summary or {}).get("scheduled_maintenances", []))
    merged: dict[str, dict[str, Any]] = {}
    for incident in candidates:
        incident_id = str(incident.get("id", "")).strip()
        previous = merged.get(incident_id)
        if previous is None or parse_timestamp(incident.get("updated_at", "")) >= (
            parse_timestamp(previous.get("updated_at", ""))
        ):
            merged[incident_id] = incident

    incident_component_ids: set[str] = set()
    for incident_id, incident in merged.items():
        status = str(incident["status"]).lower()
        maintenance = incident.get("impact") == "maintenance" or status in {
            "scheduled", "in_progress", "verifying", "completed",
        }
        if maintenance and not notify_maintenance:
            continue
        resolved = status in {"resolved", "postmortem", "completed"}
        components = incident.get("components", [])
        if not resolved:
            incident_component_ids.update(str(x.get("id", "")) for x in components)
        update = _latest_incident_update(incident)
        severity = _status_severity(str(incident.get("impact", "minor")))
        if severity == "operational":
            severity = "info"
        if maintenance:
            severity = "maintenance"
        resolved_at = str(incident.get("resolved_at") or "")
        if resolved and not resolved_at:
            resolution_updates = [
                x for x in incident.get("incident_updates", [])
                if x.get("status") in {"resolved", "completed"}
            ]
            resolved_at = max(
                (str(x.get("display_at") or x.get("created_at") or "")
                 for x in resolution_updates), default="",
            )
        issue = Issue(
            source_id=spec.source_id, source_name=spec.name,
            issue_id=f"incident_{incident_id}", severity=severity,
            title=str(incident["name"]).strip(),
            affected_services=tuple(dict.fromkeys(
                str(x["name"]) for x in components if x.get("name")
            )),
            detail=clean_text(update.get("body") or incident.get("body")),
            updated_at=str(update.get("updated_at") or update.get("created_at")
                           or incident.get("updated_at") or ""),
            status_url=str(incident.get("shortlink") or spec.status_url),
            started_at=str(incident.get("started_at") or incident.get("created_at") or ""),
            resolved_at=resolved_at,
        )
        record_issue(result, issue, resolved)

    entries: list[tuple[str, str, str]] = []
    worst = "operational"
    for component in (summary or {}).get("components", []):
        if component.get("group") is True:
            continue
        if str(component["id"]) in incident_component_ids:
            continue
        status = str(component["status"]).lower()
        severity = _status_severity(status)
        if severity == "operational" or (severity == "maintenance" and not notify_maintenance):
            continue
        entries.append((str(component.get("name") or component["id"]),
                        status.replace("_", " "), str(component.get("updated_at", ""))))
        if SEVERITY_RANK[severity] > SEVERITY_RANK[worst]:
            worst = severity
    if entries:
        issue = Issue(
            spec.source_id, spec.name, "components", worst,
            "Component status degradation",
            affected_services=tuple(name for name, _, _ in entries),
            detail="; ".join(f"{name}: {status}" for name, status, _ in entries),
            updated_at=max(updated for _, _, updated in entries),
            status_url=spec.status_url,
        )
        result.issues[issue.issue_id] = issue
    overall = (summary or {}).get("status", {})
    description = str(overall.get("description", ""))
    severity = _status_severity(str(overall.get("indicator", "none")))
    if not result.issues and severity != "operational" and (
        notify_maintenance or "maintenance" not in description.lower()
    ):
        issue = Issue(
            spec.source_id, spec.name, "overall", severity,
            description or "Service status degradation", detail=description,
            updated_at=str((summary or {}).get("page", {}).get("updated_at", "")),
            status_url=spec.status_url,
        )
        result.issues[issue.issue_id] = issue
    return result


def parse_google_cloud(spec: SourceSpec, payload: list[Any]) -> SourceResult:
    """Read Vertex incidents without conflating the separate Developer API feed."""
    result = SourceResult(spec=spec, success=True)
    for incident in object_list(payload, "Google incidents"):
        if not incident.get("id") or not incident.get("external_desc"):
            raise ValueError("Google incident id and external_desc are required")
        products = object_list(incident.get("affected_products", []), "affected_products")
        names = [str(x["title"]) for x in products if x.get("title")]
        searchable = " ".join([str(incident.get("service_name", "")),
                               str(incident["external_desc"]), *names]).lower()
        if not any(term in searchable for term in ("vertex ai", "vertex gemini", "gemini")):
            continue
        latest = incident.get("most_recent_update")
        if not isinstance(latest, dict) or not latest.get("status"):
            raise ValueError("Google most_recent_update.status is required")
        resolved = bool(incident.get("end")) or latest["status"] == "AVAILABLE"
        impact = str(incident.get("status_impact") or latest["status"]).lower()
        severe = impact in {"high", "critical"} or any(x in impact for x in ("outage", "disruption"))
        severity = "critical" if severe else "warning"
        if impact in {"low", "information", "service_information"}:
            severity = "info"
        updated_at = str(latest.get("modified") or latest.get("created")
                         or incident.get("modified") or "")
        issue = Issue(
            spec.source_id, spec.name, f"incident_{incident['id']}", severity,
            str(incident["external_desc"]), affected_services=tuple(dict.fromkeys(names)),
            detail=clean_text(latest.get("text")), updated_at=updated_at,
            status_url=spec.status_url,
            started_at=str(incident.get("begin") or incident.get("created") or ""),
            resolved_at=str(incident.get("end") or (updated_at if resolved else "")),
        )
        record_issue(result, issue, resolved)
    return result


def _xml_child_text(element: ElementTree.Element, name: str) -> str:
    for child in element:
        if child.tag.rsplit("}", 1)[-1].lower() == name.lower():
            return "".join(child.itertext()).strip()
    return ""


def parse_timestamp(value: Any) -> float:
    """Parse feed or ISO timestamps, returning zero for unknown values."""
    value = str(value or "")
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _rss_issue_id(guid: str, link: str, title: str) -> str:
    identity = guid or link
    if identity:
        identity = re.sub(r"_\d{9,}$", "", identity.strip())
    else:
        identity = title.lower().strip()
    return "rss_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def parse_rss(
    spec: SourceSpec, xml_text: str, notify_maintenance: bool,
) -> SourceResult:
    """Parse valid RSS/Atom and preserve explicit resolutions, never infer them from absence."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError("Invalid feed XML") from exc
    root_name = root.tag.rsplit("}", 1)[-1].lower()
    if root_name not in {"rss", "feed"}:
        raise ValueError("Response root is not RSS or Atom")
    if root_name == "rss" and not any(
        x.tag.rsplit("}", 1)[-1].lower() == "channel" for x in root
    ):
        raise ValueError("RSS channel is missing")

    newest: dict[str, tuple[float, str, str, str, str, tuple[str, ...], str]] = {}
    for element in root.iter():
        element_name = element.tag.rsplit("}", 1)[-1].lower()
        if element_name not in {"item", "entry"}:
            continue
        title = _xml_child_text(element, "title")
        if not title:
            raise ValueError("Feed entry title is missing")
        raw = (_xml_child_text(element, "description") or
               _xml_child_text(element, "content") or _xml_child_text(element, "summary"))
        description = clean_text(raw)
        guid = _xml_child_text(element, "guid") or _xml_child_text(element, "id")
        link = _xml_child_text(element, "link")
        if not link:
            link = next((
                x.attrib.get("href", "").strip() for x in element
                if x.tag.rsplit("}", 1)[-1].lower() == "link"
                and x.attrib.get("rel", "alternate").lower() == "alternate"
            ), "")
        published = (_xml_child_text(element, "updated") or
                     _xml_child_text(element, "pubDate") or _xml_child_text(element, "published"))
        categories = tuple((
            "".join(x.itertext()).strip() or x.attrib.get("term", "").strip()
        ).lower() for x in element if x.tag.rsplit("}", 1)[-1].lower() == "category")
        incident_match = re.search(r"/incidents/([^/?#]+)", link, re.IGNORECASE)
        issue_id = (f"incident_{incident_match.group(1)}"
                    if element_name == "entry" and incident_match else _rss_issue_id(guid, link, title))
        candidate = (parse_timestamp(published), title, description, published, link, categories, raw)
        if issue_id not in newest or candidate[0] >= newest[issue_id][0]:
            newest[issue_id] = candidate

    result = SourceResult(spec, True)
    for issue_id, (_, title, description, published, link, categories, raw) in newest.items():
        combined = f"{title} {description}".lower()
        maintenance = any(x in combined for x in (
            "scheduled maintenance", "planned maintenance", "计划维护", "预定维护",
        ))
        if maintenance and not notify_maintenance:
            continue
        structured_status = set(categories) & {"investigating", "identified", "monitoring", "resolved"}
        resolved = "resolved" in structured_status
        if not structured_status:
            resolved = any(x in combined for x in (
                "issue has been resolved", "incident has been resolved",
                "service has returned to normal", "services have returned to normal",
                "[resolved]", "已恢复", "已解决",
            )) or bool(re.search(r"\bresolved:", combined))
        title_lower = title.lower()
        severity = "warning"
        if (set(categories) & {"unavailable", "outage", "critical"} or any(
            x in title_lower for x in ("disruption", "outage", "not available", "unavailable", "不可用")
        )):
            severity = "critical"
        if maintenance:
            severity = "maintenance"
        resolved_at = published if resolved else ""
        # xAI pubDate is the incident start; its explicit Resolved header is authoritative.
        match = re.search(r"\bResolved:\s*([^<\n]+)", raw, re.IGNORECASE)
        if resolved and match and parse_timestamp(match.group(1).strip()):
            resolved_at = match.group(1).strip()
        issue = Issue(
            spec.source_id, spec.name, issue_id, severity, title,
            detail=description, updated_at=published,
            status_url=spec.status_url if spec.source_id == "deepseek" else link or spec.status_url,
            started_at=published, resolved_at=resolved_at,
        )
        record_issue(result, issue, resolved)
    return result


MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def source_error(exc: BaseException) -> str:
    """Keep exception types while excluding response bodies and credentials."""
    if isinstance(exc, (ValueError, RuntimeError)):
        detail = re.sub(r"AIza[0-9A-Za-z_-]{35}", "[redacted]", str(exc))
        return f"{type(exc).__name__}: {detail[:240]}"
    status = getattr(exc, "status", None) or getattr(exc, "code", None)
    return f"{type(exc).__name__}" + (f" ({status})" if status else "")


async def _request_text(session: aiohttp.ClientSession, url: str) -> str:
    for attempt in range(2):
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError("Response exceeds 4 MiB")
                return body.decode(response.charset or "utf-8-sig")
        except (aiohttp.ClientConnectionError, TimeoutError):
            if attempt:
                raise
            await asyncio.sleep(0.25)
    raise RuntimeError("Unreachable request state")


async def _request_json(session: aiohttp.ClientSession, url: str) -> Any:
    return json.loads(await _request_text(session, url))


async def fetch_source(
    session: aiohttp.ClientSession, spec: SourceSpec, notify_maintenance: bool,
    history_hours: int = 24,
) -> SourceResult:
    """Fetch one source with a deadline and independent current/history failure handling."""
    started = perf_counter()
    try:
        async with asyncio.timeout(50):
            if spec.kind == "statuspage":
                urls = [f"{spec.endpoint.rstrip('/')}/api/v2/{name}.json"
                        for name in ("summary", "incidents")]
                payloads = await asyncio.gather(
                    *(_request_json(session, url) for url in urls), return_exceptions=True,
                )
                for value in payloads:
                    if isinstance(value, asyncio.CancelledError):
                        raise value
                errors = [f"{urlparse(url).path}: {source_error(value)}"
                          for url, value in zip(urls, payloads) if isinstance(value, BaseException)]
                try:
                    result = parse_statuspage(
                        spec, *(None if isinstance(x, BaseException) else x for x in payloads),
                        notify_maintenance,
                    )
                except ValueError as exc:
                    raise ValueError("; ".join([str(exc), *errors])) from exc
                if errors:
                    result.error = "; ".join(filter(None, [result.error, *errors]))
            elif spec.kind == "google":
                result = parse_google_cloud(spec, await _request_json(session, spec.endpoint))
            elif spec.kind == "rss":
                result = parse_rss(spec, await _request_text(session, spec.endpoint), notify_maintenance)
            elif spec.kind == "datadog":
                from .datadog_status import parse_datadog

                result = parse_datadog(spec, await _request_json(session, spec.endpoint), notify_maintenance)
            elif spec.kind == "betterstack":
                from .betterstack import parse_betterstack

                result = parse_betterstack(spec, await _request_json(session, spec.endpoint), notify_maintenance)
            elif spec.kind in {"flashduty", "aistudio"}:
                from .modern_sources import fetch_modern_source

                result = await fetch_modern_source(spec, notify_maintenance, history_hours)
            else:
                raise ValueError(f"Unsupported source kind: {spec.kind}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result = SourceResult(
            spec, False, error=source_error(exc), complete=False, history_complete=False,
        )
    result.fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    if result.error:
        result.error += f" ({perf_counter() - started:.1f}s)"
    return result


async def fetch_all_sources(
    session: aiohttp.ClientSession, specs: list[SourceSpec], notify_maintenance: bool,
    history_hours: int = 24,
) -> list[SourceResult]:
    """Fetch enabled sources concurrently; cancellation remains owned by the caller."""
    return await asyncio.gather(
        *(fetch_source(session, spec, notify_maintenance, history_hours) for spec in specs)
    )
