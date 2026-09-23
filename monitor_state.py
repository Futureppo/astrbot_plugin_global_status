"""Conservative reconciliation, bounded history and per-target source health notices."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .sources import Issue, SourceResult, parse_timestamp

MAX_HISTORY_RECORDS = 2000
MAX_CATCHUPS = 200


def _issues(raw: Any) -> dict[str, Issue]:
    if not isinstance(raw, dict):
        return {}
    return {key: Issue.from_dict(value) for key, value in raw.items() if isinstance(value, dict)}


def delivery_fingerprint(stage: str, issue: Issue) -> str:
    return f"recovered:{issue.fingerprint}" if stage in {"recovered", "missed"} else issue.fingerprint


def reconcile_source(
    state: dict[str, Any], result: SourceResult, targets: dict[str, str], lookback_hours: int,
) -> dict[str, list[tuple[str, Issue]]]:
    """Require explicit incident resolution and baseline history before catch-up."""
    source = state["sources"].setdefault(result.spec.source_id, {})
    previous = _issues(source.get("issues"))
    recoveries = _issues(source.get("recoveries"))
    catchups = _issues(source.get("catchups"))
    original_catchups = dict(catchups)
    current = dict(result.issues)
    now = parse_timestamp(result.fetched_at)
    cutoff = now - lookback_hours * 3600
    seen = source.get("history_seen", {})
    if not isinstance(seen, dict):
        seen = {}
    seen = {key: value for key, value in seen.items() if parse_timestamp(value) >= cutoff}
    baseline = parse_timestamp(source.get("history_baseline_at"))
    generation_changed = source.get("history_source") != result.spec.kind
    baseline_ready = bool(baseline) and not generation_changed
    floor = max(cutoff, baseline, parse_timestamp(source.get("history_floor_at")))

    def eligible(issue: Issue) -> bool:
        ended = parse_timestamp(issue.resolved_at)
        return lookback_hours > 0 and baseline_ready and floor < ended <= now

    for key in current:
        recoveries.pop(key, None)
        catchups.pop(key, None)
        seen.pop(key, None)
    unconfirmed = []
    for key, old in previous.items():
        if key in current:
            continue
        explicit = key in result.resolved_issue_ids
        aggregate_normal = key in {"components", "overall"} and result.complete
        if not explicit and not aggregate_normal:
            current[key] = old
            unconfirmed.append(key)
            continue
        recoveries[key] = result.resolved_issues.get(key, old)
        catchups.pop(key, None)
        resolved_at = recoveries[key].resolved_at
        seen[key] = resolved_at if parse_timestamp(resolved_at) else result.fetched_at

    if result.history_complete:
        for key, issue in result.resolved_issues.items():
            if key in current:
                continue
            if key not in seen and key not in previous and key not in recoveries and eligible(issue):
                catchups[key] = issue
            if cutoff <= parse_timestamp(issue.resolved_at) <= now:
                seen[key] = issue.resolved_at
        if not baseline_ready:
            source["history_baseline_at"] = result.fetched_at
            source["history_source"] = result.spec.kind
        source["history_cursor_at"] = result.fetched_at

    catchups = {key: issue for key, issue in catchups.items()
                if lookback_hours > 0 and parse_timestamp(issue.resolved_at) >= cutoff}
    catchups = dict(sorted(catchups.items(), key=lambda x: parse_timestamp(x[1].resolved_at))[-MAX_CATCHUPS:])
    ordered_seen = sorted(seen.items(), key=lambda x: parse_timestamp(x[1]))
    if len(ordered_seen) > MAX_HISTORY_RECORDS:
        # Evicted IDs remain outside the catch-up window instead of becoming new again.
        evicted_until = ordered_seen[-MAX_HISTORY_RECORDS - 1][1]
        if parse_timestamp(evicted_until) > parse_timestamp(source.get("history_floor_at")):
            source["history_floor_at"] = evicted_until
    source["history_seen"] = dict(ordered_seen[-MAX_HISTORY_RECORDS:])
    for key, issue in original_catchups.items():
        if key in catchups or key in current or key in recoveries:
            continue
        expected = delivery_fingerprint("missed", issue)
        for delivered in state["deliveries"].values():
            if delivered.get(issue.key) == expected:
                delivered.pop(issue.key, None)
    source["issues"] = {key: issue.to_dict() for key, issue in current.items()}
    source["recoveries"] = {key: issue.to_dict() for key, issue in recoveries.items()}
    source["catchups"] = {key: issue.to_dict() for key, issue in catchups.items()}
    source["missing_counts"] = {}
    source["unconfirmed"] = unconfirmed
    if unconfirmed:
        result.complete = False
        result.error = "; ".join(filter(None, [result.error,
            f"Resolution not confirmed for {len(unconfirmed)} retained event(s)"]))

    pending: dict[str, list[tuple[str, Issue]]] = {}
    for target in targets:
        delivered = state["deliveries"].setdefault(target, {})
        events = []
        for key, issue in current.items():
            # Retained data is not a new observation and must not manufacture an update.
            if key in unconfirmed:
                continue
            fingerprint = delivered.get(issue.key)
            if fingerprint != issue.fingerprint:
                stage = "new" if key not in previous else "current" if fingerprint is None else "update"
                events.append((stage, issue))
        for key, issue in recoveries.items():
            fingerprint = delivered.get(issue.key)
            recovery = delivery_fingerprint("recovered", issue)
            if fingerprint == recovery:
                continue
            if fingerprint is None:
                if eligible(issue):
                    events.append(("missed", issue))
                else:
                    delivered[issue.key] = recovery
            else:
                events.append(("recovered", issue))
        for issue in catchups.values():
            if delivered.get(issue.key) != delivery_fingerprint("missed", issue):
                events.append(("missed", issue))
        if events:
            pending[target] = events
    return pending


def cleanup_resolved(state: dict[str, Any], source_id: str, targets: set[str], configured: bool) -> None:
    """Keep failed-target deliveries pending; retain bounded seen IDs after cleanup."""
    if configured and not targets:
        return
    source = state["sources"].get(source_id, {})
    for field in ("recoveries", "catchups"):
        records = source.get(field, {})
        for key, issue in _issues(records).items():
            expected = delivery_fingerprint("recovered", issue)
            if all(state["deliveries"].get(target, {}).get(issue.key) == expected for target in targets):
                records.pop(key, None)
                for delivered in state["deliveries"].values():
                    delivered.pop(issue.key, None)


def plan_health_notices(
    state: dict[str, Any], result: SourceResult, targets: dict[str, str],
    enabled: bool, threshold: int, cooldown: int,
) -> dict[str, list[tuple[str, Issue]]]:
    """Only successful deliveries advance the per-target notice cooldown."""
    health = state.setdefault("health", {}).setdefault(result.spec.source_id, {})
    healthy = result.success and result.complete
    health["last_attempt_at"] = result.fetched_at
    if healthy:
        health["last_success_at"] = result.fetched_at
        health["consecutive_failures"] = 0
        health["last_error"] = ""
    else:
        health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
        health["last_error"] = result.error or "Incomplete source data"
    if not enabled:
        return {}
    now = parse_timestamp(result.fetched_at)
    deliveries = health.setdefault("deliveries", {})
    pending = {}
    for target in targets:
        delivery = deliveries.get(target, {})
        if healthy:
            if not delivery.get("active"):
                continue
            stage = "source_recovered"
            title = f"{result.spec.name}：监控数据已恢复 / Source data restored"
            detail = "采集已恢复；这不代表厂商事故已经恢复。 / Collection restored, not a vendor recovery."
        else:
            if health["consecutive_failures"] < threshold:
                continue
            if delivery.get("active") and now - parse_timestamp(delivery.get("sent_at")) < cooldown:
                continue
            stage = "source_unavailable"
            title = f"{result.spec.name}：监控数据不可用 / Source data unavailable"
            detail = (f"连续不完整采集 / Consecutive failures: {health['consecutive_failures']}\n"
                      f"最后成功 / Last success: {health.get('last_success_at') or 'never'}\n"
                      f"{health['last_error']}")
        issue = Issue(result.spec.source_id, result.spec.name, "__source_health__",
                      "info" if healthy else "unavailable", title, detail=detail,
                      updated_at=result.fetched_at, status_url=result.spec.status_url)
        pending[target] = [(stage, issue)]
    return pending


def mark_health_delivered(state: dict[str, Any], target: str, events: list[tuple[str, Issue]]) -> None:
    for stage, issue in events:
        state["health"][issue.source_id].setdefault("deliveries", {})[target] = {
            "active": stage == "source_unavailable", "sent_at": issue.updated_at,
        }


def presentation_result(result: SourceResult, state: dict[str, Any]) -> SourceResult:
    """Show retained issues as unconfirmed, without mutating notification state."""
    previous = _issues(state["sources"].get(result.spec.source_id, {}).get("issues"))
    retained = {key: issue for key, issue in previous.items()
                if key not in result.issues and key not in result.resolved_issue_ids
                and not (key in {"components", "overall"} and result.complete and result.success)}
    error = result.error
    if retained and not error:
        error = f"Resolution not confirmed for {len(retained)} retained event(s)"
    health = state.get("health", {}).get(result.spec.source_id, {})
    return replace(result, issues={**retained, **result.issues},
                   complete=result.complete and not retained, error=error,
                   last_success_at=health.get("last_success_at", ""))
