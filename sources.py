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

    @property
    def severity(self) -> str:
        """Return the worst active severity for overview rendering."""
        if not self.success:
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
        "xai",
        "xAI",
        "rss",
        "https://status.x.ai/feed.xml",
        "https://status.x.ai/",
    ),
    SourceSpec(
        "deepseek",
        "DeepSeek",
        "rss",
        "https://deepseek.statuspage.io/history.atom",
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
        "https://azure.status.microsoft/en-us/status/feed/",
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


def parse_statuspage(
    spec: SourceSpec,
    summary: dict[str, Any],
    incidents_payload: dict[str, Any] | None,
    notify_maintenance: bool,
) -> SourceResult:
    """Parse Statuspage summary and incident JSON into normalized issues.

    Args:
        spec: Source metadata.
        summary: Parsed `/api/v2/summary.json` object.
        incidents_payload: Parsed `/api/v2/incidents.json` object, if supported.
        notify_maintenance: Whether maintenance components should become issues.

    Returns:
        Successful source result containing active issues.
    """
    result = SourceResult(spec=spec, success=True)
    incident_component_ids: set[str] = set()
    incidents = (incidents_payload or {}).get("incidents", [])
    if isinstance(incidents, list):
        for incident in incidents:
            if not isinstance(incident, dict):
                continue
            status = str(incident.get("status", "")).lower()
            incident_id = str(incident.get("id", "")).strip()
            if not incident_id:
                continue
            if status in {"resolved", "postmortem", "completed"}:
                result.resolved_issue_ids.add(f"incident_{incident_id}")
                continue

            components = incident.get("components", [])
            affected: list[str] = []
            if isinstance(components, list):
                for component in components:
                    if not isinstance(component, dict):
                        continue
                    component_id = str(component.get("id", ""))
                    if component_id:
                        incident_component_ids.add(component_id)
                    name = str(component.get("name", "")).strip()
                    if name:
                        affected.append(name)

            update = _latest_incident_update(incident)
            incident_severity = _status_severity(str(incident.get("impact", "minor")))
            if incident_severity == "operational":
                incident_severity = "info"
            issue = Issue(
                source_id=spec.source_id,
                source_name=spec.name,
                issue_id=f"incident_{incident_id}",
                severity=incident_severity,
                title=str(incident.get("name", "Service incident")).strip(),
                affected_services=tuple(dict.fromkeys(affected)),
                detail=clean_text(update.get("body") or incident.get("body")),
                updated_at=str(
                    update.get("updated_at")
                    or update.get("created_at")
                    or incident.get("updated_at")
                    or ""
                ),
                status_url=str(incident.get("shortlink") or spec.status_url),
            )
            result.issues[issue.issue_id] = issue

    component_entries: list[tuple[str, str, str]] = []
    component_severity = "operational"
    components = summary.get("components", [])
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict) or component.get("group") is True:
                continue
            component_id = str(component.get("id", ""))
            if component_id and component_id in incident_component_ids:
                continue
            status = str(component.get("status", "operational")).lower()
            severity = _status_severity(status)
            if severity == "operational":
                continue
            if severity == "maintenance" and not notify_maintenance:
                continue
            name = str(component.get("name", "Unknown component")).strip()
            component_entries.append(
                (
                    name,
                    status.replace("_", " "),
                    str(component.get("updated_at", "")),
                )
            )
            if SEVERITY_RANK[severity] > SEVERITY_RANK[component_severity]:
                component_severity = severity

    if component_entries:
        issue = Issue(
            source_id=spec.source_id,
            source_name=spec.name,
            issue_id="components",
            severity=component_severity,
            title="Component status degradation",
            affected_services=tuple(name for name, _, _ in component_entries),
            detail="; ".join(
                f"{name}: {status}" for name, status, _ in component_entries
            ),
            updated_at=max(
                (updated_at for _, _, updated_at in component_entries),
                default="",
            ),
            status_url=spec.status_url,
        )
        result.issues[issue.issue_id] = issue

    overall = summary.get("status", {})
    indicator = (
        str(overall.get("indicator", "none")) if isinstance(overall, dict) else "none"
    )
    description = (
        str(overall.get("description", "")) if isinstance(overall, dict) else ""
    )
    overall_severity = _status_severity(indicator)
    maintenance_only = "maintenance" in description.lower()
    if (
        not result.issues
        and overall_severity != "operational"
        and (notify_maintenance or not maintenance_only)
    ):
        issue = Issue(
            source_id=spec.source_id,
            source_name=spec.name,
            issue_id="overall",
            severity=overall_severity,
            title=description or "Service status degradation",
            detail=description,
            updated_at=str(summary.get("page", {}).get("updated_at", "")),
            status_url=spec.status_url,
        )
        result.issues[issue.issue_id] = issue
    return result


def parse_google_cloud(spec: SourceSpec, payload: list[Any]) -> SourceResult:
    """Parse active Vertex AI and Gemini incidents from Google Cloud JSON."""
    result = SourceResult(spec=spec, success=True)
    for incident in payload:
        if not isinstance(incident, dict):
            continue
        products = incident.get("affected_products", [])
        product_names = [
            str(item.get("title", ""))
            for item in products
            if isinstance(item, dict) and item.get("title")
        ]
        searchable = " ".join(
            [
                str(incident.get("service_name", "")),
                str(incident.get("external_desc", "")),
                *product_names,
            ]
        ).lower()
        if not any(
            term in searchable for term in ("vertex ai", "vertex gemini", "gemini")
        ):
            continue

        incident_id = str(incident.get("id", "")).strip()
        if not incident_id:
            continue
        latest = incident.get("most_recent_update", {})
        if not isinstance(latest, dict):
            latest = {}
        if incident.get("end") or str(latest.get("status", "")).upper() == "AVAILABLE":
            result.resolved_issue_ids.add(f"incident_{incident_id}")
            continue

        impact = str(
            incident.get("status_impact")
            or latest.get("status")
            or incident.get("severity")
            or "medium"
        ).lower()
        severity = "warning"
        if "disruption" in impact or impact in {"high", "critical"}:
            severity = "critical"
        elif impact in {"low", "information", "service_information"}:
            severity = "info"

        issue = Issue(
            source_id=spec.source_id,
            source_name=spec.name,
            issue_id=f"incident_{incident_id}",
            severity=severity,
            title=str(
                incident.get("external_desc")
                or incident.get("service_name")
                or "Google Cloud incident"
            ).strip(),
            affected_services=tuple(dict.fromkeys(product_names)),
            detail=clean_text(latest.get("text")),
            updated_at=str(
                latest.get("modified")
                or latest.get("created")
                or incident.get("modified")
                or ""
            ),
            status_url=spec.status_url,
        )
        result.issues[issue.issue_id] = issue
    return result


def _xml_child_text(element: ElementTree.Element, name: str) -> str:
    for child in element:
        if child.tag.rsplit("}", 1)[-1].lower() == name.lower():
            return "".join(child.itertext()).strip()
    return ""


def _rss_timestamp(value: str) -> float:
    """Parse an RFC 2822 or ISO 8601 feed timestamp.

    Args:
        value: Timestamp text from an RSS or Atom element.

    Returns:
        Unix timestamp, or zero when the value cannot be parsed.
    """
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
    spec: SourceSpec,
    xml_text: str,
    notify_maintenance: bool,
) -> SourceResult:
    """Parse and merge current official RSS or Atom status entries.

    Args:
        spec: Source metadata.
        xml_text: Raw RSS or Atom XML document.
        notify_maintenance: Whether planned maintenance entries should be included.

    Returns:
        Successful result with only the newest entry for each stable incident.

    Raises:
        ValueError: If the response is not valid feed XML.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Invalid feed XML: {exc}") from exc

    newest: dict[str, tuple[float, str, str, str, str, tuple[str, ...]]] = {}
    for element in root.iter():
        element_name = element.tag.rsplit("}", 1)[-1].lower()
        if element_name not in {"item", "entry"}:
            continue
        title = _xml_child_text(element, "title")
        description = clean_text(
            _xml_child_text(element, "description")
            or _xml_child_text(element, "content")
            or _xml_child_text(element, "summary")
        )
        guid = _xml_child_text(element, "guid") or _xml_child_text(element, "id")
        link = _xml_child_text(element, "link")
        if not link:
            for child in element:
                if child.tag.rsplit("}", 1)[-1].lower() != "link":
                    continue
                if child.attrib.get("rel", "alternate").lower() != "alternate":
                    continue
                link = child.attrib.get("href", "").strip()
                if link:
                    break
        published = (
            _xml_child_text(element, "pubDate")
            or _xml_child_text(element, "updated")
            or _xml_child_text(element, "published")
        )
        categories = tuple(
            (
                "".join(child.itertext()).strip()
                or child.attrib.get("term", "").strip()
            ).lower()
            for child in element
            if child.tag.rsplit("}", 1)[-1].lower() == "category"
        )
        incident_match = (
            re.search(r"/incidents/([^/?#]+)", link, flags=re.IGNORECASE)
            if element_name == "entry" and spec.source_id == "deepseek"
            else None
        )
        issue_id = (
            f"incident_{incident_match.group(1)}"
            if incident_match
            else _rss_issue_id(guid, link, title)
        )
        candidate = (
            _rss_timestamp(published),
            title,
            description,
            published,
            link,
            categories,
        )
        if issue_id not in newest or candidate[0] >= newest[issue_id][0]:
            newest[issue_id] = candidate

    result = SourceResult(spec=spec, success=True)
    resolved_phrases = (
        "issue has been resolved",
        "incident has been resolved",
        "service has returned to normal",
        "services have returned to normal",
        "resolved:",
        "[resolved]",
        "已恢复",
        "已解决",
    )
    maintenance_phrases = (
        "scheduled maintenance",
        "planned maintenance",
        "计划维护",
        "预定维护",
    )
    for issue_id, (
        _,
        title,
        description,
        published,
        link,
        categories,
    ) in newest.items():
        combined = f"{title} {description}".lower()
        if "resolved" in categories or any(
            phrase in combined for phrase in resolved_phrases
        ):
            result.resolved_issue_ids.add(issue_id)
            continue
        if not notify_maintenance and any(
            phrase in combined for phrase in maintenance_phrases
        ):
            continue

        title_lower = title.lower()
        severity = "warning"
        if (
            {"unavailable", "outage", "critical"} & set(categories)
            or "disruption" in title_lower
            or "outage" in title_lower
            or "not available" in title_lower
            or "unavailable" in title_lower
            or "不可用" in title
        ):
            severity = "critical"
        elif "maintenance" in title_lower:
            severity = "maintenance"

        issue = Issue(
            source_id=spec.source_id,
            source_name=spec.name,
            issue_id=issue_id,
            severity=severity,
            title=title or "Service status event",
            detail=description,
            updated_at=published,
            status_url=(
                spec.status_url
                if spec.source_id == "deepseek"
                else link or spec.status_url
            ),
        )
        result.issues[issue.issue_id] = issue
    return result


async def _request_json(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def _request_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.text()


async def fetch_source(
    session: aiohttp.ClientSession,
    spec: SourceSpec,
    notify_maintenance: bool,
) -> SourceResult:
    """Fetch and parse one source without leaking request failures.

    Args:
        session: Shared HTTP client session.
        spec: Source to query.
        notify_maintenance: Whether planned maintenance should become issues.

    Returns:
        Source result. Failures are represented with `success=False`.
    """
    try:
        if spec.kind == "statuspage":
            summary_url = f"{spec.endpoint.rstrip('/')}/api/v2/summary.json"
            incidents_url = f"{spec.endpoint.rstrip('/')}/api/v2/incidents.json"
            summary_task = asyncio.create_task(_request_json(session, summary_url))
            incidents_task = asyncio.create_task(_request_json(session, incidents_url))
            summary, incidents = await asyncio.gather(summary_task, incidents_task)
            if not isinstance(summary, dict) or not isinstance(incidents, dict):
                raise ValueError("Statuspage response must be a JSON object")
            return parse_statuspage(spec, summary, incidents, notify_maintenance)

        if spec.kind == "google":
            payload = await _request_json(session, spec.endpoint)
            if not isinstance(payload, list):
                raise ValueError("Google status response must be a JSON array")
            return parse_google_cloud(spec, payload)

        if spec.kind == "rss":
            return parse_rss(
                spec,
                await _request_text(session, spec.endpoint),
                notify_maintenance,
            )
        raise ValueError(f"Unsupported source kind: {spec.kind}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return SourceResult(spec=spec, success=False, error=str(exc))


async def fetch_all_sources(
    session: aiohttp.ClientSession,
    specs: list[SourceSpec],
    notify_maintenance: bool,
) -> list[SourceResult]:
    """Fetch all enabled sources concurrently while preserving source order."""
    return await asyncio.gather(
        *(fetch_source(session, spec, notify_maintenance) for spec in specs)
    )
