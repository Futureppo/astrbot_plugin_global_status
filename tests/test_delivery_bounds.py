from dataclasses import replace

import pytest

from data.plugins.astrbot_plugin_global_status import monitor_state
from data.plugins.astrbot_plugin_global_status.sources import SourceResult

from .test_main import _issue, _plugin, _spec
from .test_monitor_state import (
    TARGETS,
    acknowledge,
    baseline,
    closed,
    resolved_issue,
    result,
    state,
)


def test_seen_capacity_eviction_does_not_replay_delivered_history(monkeypatch):
    monkeypatch.setattr(monitor_state, "MAX_HISTORY_RECORDS", 2)
    stored = state()
    baseline(stored)
    issues = [resolved_issue(f"2026-09-20T09:55:0{i}Z", f"incident_{i}") for i in range(3)]
    snapshot = result(resolved_issues={x.issue_id: x for x in issues},
                      resolved_issue_ids={x.issue_id for x in issues})
    pending = monitor_state.reconcile_source(stored, snapshot, TARGETS, 24)
    for target, events in pending.items():
        acknowledge(stored, target, events)
    monitor_state.cleanup_resolved(stored, "vendor", set(TARGETS), True)
    assert monitor_state.reconcile_source(stored, snapshot, TARGETS, 24) == {}


def test_expired_pending_catchup_removes_orphaned_delivery_fingerprints():
    stored = state()
    baseline(stored)
    snapshot = closed(resolved_issue())
    pending = monitor_state.reconcile_source(stored, snapshot, TARGETS, 24)
    acknowledge(stored, "1|100", pending["1|100"])
    later = replace(snapshot, fetched_at="2026-09-22T10:00:00Z")
    monitor_state.reconcile_source(stored, later, TARGETS, 24)
    assert stored["deliveries"]["1|100"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_second", [False, True])
async def test_large_alert_batches_are_bounded_and_retry_only_unacknowledged(monkeypatch, fail_second):
    plugin = _plugin({"group_whitelist": ["100"], "enable_ai_translation": False})
    issues = [replace(_issue(), issue_id=f"incident_{i}") for i in range(25)]
    snapshot = SourceResult(_spec(), True, {x.issue_id: x for x in issues})
    calls = []

    async def fetch():
        return [snapshot]

    async def send(umo, name, events, translations=None):
        calls.append(len(events))
        return not (fail_second and len(calls) == 2)

    async def save(*args):
        return None

    monkeypatch.setattr(plugin, "_fetch_sources", fetch)
    monkeypatch.setattr(plugin, "_send_events", send)
    monkeypatch.setattr(plugin, "put_kv_data", save)
    await plugin._run_cycle()
    assert calls == ([5, 5] if fail_second else [5, 5, 5, 5])
    assert len(plugin._state["deliveries"]["1|100"]) == (5 if fail_second else 20)
    calls.clear()
    prior_failure = fail_second
    fail_second = False
    await plugin._run_cycle()
    assert calls == ([5, 5, 5, 5] if prior_failure else [5])
    assert len(plugin._state["deliveries"]["1|100"]) == 25
