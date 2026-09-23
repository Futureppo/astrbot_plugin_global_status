import copy
from dataclasses import replace

import pytest

from data.plugins.astrbot_plugin_global_status.monitor_state import (
    cleanup_resolved,
    delivery_fingerprint,
    mark_health_delivered,
    plan_health_notices,
    presentation_result,
    reconcile_source,
)
from data.plugins.astrbot_plugin_global_status.sources import (
    Issue,
    SourceResult,
    SourceSpec,
)

SPEC = SourceSpec("vendor", "Vendor", "statuspage", "https://status.test", "https://status.test/")
TARGETS = {"1|100": "1:GroupMessage:100", "1|200": "1:GroupMessage:200"}
BASELINE = "2026-09-20T09:00:00Z"
NOW = "2026-09-20T10:00:00Z"


def state():
    return {"version": 1, "sources": {}, "deliveries": {}, "initialized_sources": []}


def result(**kwargs):
    return SourceResult(SPEC, True, fetched_at=NOW, **kwargs)


def resolved_issue(ended="2026-09-20T09:55:00Z", key="incident_one"):
    return Issue("vendor", "Vendor", key, "warning", "Short outage", detail="Restored",
                 started_at="2026-09-20T09:54:00Z", resolved_at=ended, updated_at=ended)


def closed(issue):
    return result(resolved_issue_ids={issue.issue_id}, resolved_issues={issue.issue_id: issue})


def baseline(s):
    reconcile_source(s, replace(result(), fetched_at=BASELINE), TARGETS, 24)


def acknowledge(s, target, events):
    for stage, issue in events:
        s["deliveries"][target][issue.key] = delivery_fingerprint(stage, issue)


def test_upgrade_and_first_run_baseline_old_history_without_flooding():
    s = state()
    s["initialized_sources"] = ["vendor"]
    assert reconcile_source(s, closed(resolved_issue()), TARGETS, 24) == {}
    assert s["sources"]["vendor"]["history_baseline_at"] == NOW


def test_short_incident_catchup_retries_only_failed_target_across_restart():
    s = state()
    baseline(s)
    event = closed(resolved_issue())
    pending = reconcile_source(s, event, TARGETS, 24)
    assert {events[0][0] for events in pending.values()} == {"missed"}
    acknowledge(s, "1|100", pending["1|100"])
    cleanup_resolved(s, "vendor", set(TARGETS), True)
    s = copy.deepcopy(s)
    pending = reconcile_source(s, event, TARGETS, 24)
    assert set(pending) == {"1|200"}
    acknowledge(s, "1|200", pending["1|200"])
    cleanup_resolved(s, "vendor", set(TARGETS), True)
    assert not s["sources"]["vendor"]["catchups"]
    assert reconcile_source(s, event, TARGETS, 24) == {}


@pytest.mark.parametrize("ended", ["", "invalid", "2026-09-18T10:00:00Z", "2026-09-21T10:00:00Z", BASELINE])
def test_catchup_ignores_unknown_old_future_or_prebaseline_resolution(ended):
    s = state()
    baseline(s)
    assert reconcile_source(s, closed(resolved_issue(ended)), TARGETS, 24) == {}


def test_disabled_catchup_still_reconciles_known_incident_recovery():
    s = state()
    baseline(s)
    issue = replace(resolved_issue(), resolved_at="")
    pending = reconcile_source(s, result(issues={issue.issue_id: issue}), TARGETS, 0)
    for target, events in pending.items():
        acknowledge(s, target, events)
    pending = reconcile_source(s, closed(resolved_issue()), TARGETS, 0)
    assert pending["1|100"][0][0] == "recovered"


@pytest.mark.parametrize("kind", ["rss", "statuspage", "google", "flashduty", "aistudio"])
def test_disappearance_without_resolution_is_unconfirmed_not_recovery(kind):
    s = state()
    issue = replace(resolved_issue(), resolved_at="")
    r = replace(result(issues={issue.issue_id: issue}), spec=replace(SPEC, kind=kind))
    pending = reconcile_source(s, r, TARGETS, 24)
    for target, events in pending.items():
        acknowledge(s, target, events)
    for _ in range(3):
        missing = replace(result(), spec=r.spec)
        assert reconcile_source(s, missing, TARGETS, 24) == {}
        assert not missing.complete
        assert issue.issue_id in s["sources"]["vendor"]["issues"]
    recovered = replace(closed(resolved_issue()), spec=r.spec)
    assert reconcile_source(s, recovered, TARGETS, 24)["1|100"][0][0] == "recovered"


def test_partial_response_cannot_clear_aggregate_but_complete_snapshot_can():
    s = state()
    issue = replace(resolved_issue(), issue_id="components", resolved_at="")
    pending = reconcile_source(s, result(issues={issue.issue_id: issue}), TARGETS, 24)
    acknowledge(s, "1|100", pending["1|100"])
    assert reconcile_source(s, result(complete=False), TARGETS, 24) == {}
    assert reconcile_source(s, result(), TARGETS, 24)["1|100"][0][0] == "recovered"


def test_summary_resolution_then_history_catchup_does_not_duplicate():
    s = state()
    baseline(s)
    issue = replace(resolved_issue(), resolved_at="")
    pending = reconcile_source(s, result(issues={issue.issue_id: issue}), TARGETS, 24)
    for target, events in pending.items():
        acknowledge(s, target, events)
    resolved = replace(closed(resolved_issue()), complete=False, history_complete=False)
    pending = reconcile_source(s, resolved, TARGETS, 24)
    for target, events in pending.items():
        acknowledge(s, target, events)
    cleanup_resolved(s, "vendor", set(TARGETS), True)
    assert reconcile_source(s, closed(resolved_issue()), TARGETS, 24) == {}


def test_history_failure_does_not_establish_baseline_or_discard_pending_recovery():
    s = state()
    reconcile_source(s, result(complete=False, history_complete=False), TARGETS, 24)
    assert "history_baseline_at" not in s["sources"]["vendor"]
    assert reconcile_source(s, closed(resolved_issue()), TARGETS, 24) == {}


def test_disabled_source_generation_rebases_history():
    s = state()
    baseline(s)
    r = replace(closed(resolved_issue()), spec=replace(SPEC, kind="flashduty"))
    assert reconcile_source(s, r, TARGETS, 24) == {}
    assert s["sources"]["vendor"]["history_source"] == "flashduty"


def test_presentation_is_read_only_and_retains_unconfirmed_issues():
    s = state()
    issue = replace(resolved_issue(), resolved_at="")
    reconcile_source(s, result(issues={issue.issue_id: issue}), TARGETS, 24)
    before = copy.deepcopy(s)
    r = presentation_result(result(), s)
    assert not r.complete and r.issues and r.error
    assert s == before


def test_health_threshold_cooldown_failed_target_and_recovery_retry():
    s = state()
    failed = replace(result(), success=False, complete=False, error="TimeoutError")
    assert plan_health_notices(s, failed, TARGETS, True, 3, 3600) == {}
    assert plan_health_notices(s, failed, TARGETS, True, 3, 3600) == {}
    pending = plan_health_notices(s, failed, TARGETS, True, 3, 3600)
    assert set(pending) == set(TARGETS)
    mark_health_delivered(s, "1|100", pending["1|100"])
    pending = plan_health_notices(s, failed, TARGETS, True, 3, 3600)
    assert set(pending) == {"1|200"}
    pending = plan_health_notices(s, result(), TARGETS, True, 3, 3600)
    assert set(pending) == {"1|100"}
    assert pending["1|100"][0][0] == "source_recovered"
    assert plan_health_notices(s, result(), TARGETS, True, 3, 3600)
    mark_health_delivered(s, "1|100", pending["1|100"])
    assert plan_health_notices(s, result(), TARGETS, True, 3, 3600) == {}
    assert s["health"]["vendor"]["last_success_at"] == NOW


def test_health_disabled_keeps_diagnostics_without_notifications():
    s = state()
    plan_health_notices(s, result(), TARGETS, False, 1, 60)
    r = result(complete=False, error="Incomplete history")
    assert plan_health_notices(s, r, TARGETS, False, 1, 60) == {}
    assert s["health"]["vendor"]["last_success_at"] == NOW
    assert s["health"]["vendor"]["consecutive_failures"] == 1


def test_catchup_without_targets_does_not_broadcast_old_history_to_new_groups():
    s = state()
    baseline(s)
    event = closed(resolved_issue())
    assert reconcile_source(s, event, {}, 24) == {}
    cleanup_resolved(s, "vendor", set(), False)
    assert reconcile_source(s, event, TARGETS, 24) == {}
