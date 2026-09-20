import json
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_global_status.datadog_status import parse_datadog
from data.plugins.astrbot_plugin_global_status.sources import BUILTIN_SOURCES

SPEC = next(x for x in BUILTIN_SOURCES if x.source_id == "openrouter")
ROOT = Path(__file__).parents[1]


def data():
    return json.loads((ROOT / "tests/fixtures/openrouter.json").read_text())


def test_openrouter_real_snapshot_uses_effective_dates_not_import_dates():
    result = parse_datadog(SPEC, data(), False)
    assert result.complete and result.severity == "operational"
    issue = next(iter(result.resolved_issues.values()))
    assert issue.updated_at == "2026-09-04T21:12:00Z"
    assert issue.resolved_at == "2026-09-04T21:12:00Z"
    assert issue.detail == "This incident has been resolved."


def test_openrouter_grouped_component_fault_without_incident():
    payload = data()
    payload["components"][0]["components"][0]["status"] = "major_outage"
    result = parse_datadog(SPEC, payload, False)
    assert result.issues["components"].severity == "critical"
    assert result.issues["components"].affected_services == ("Video (/api/v1/videos)",)


def test_openrouter_active_incident_and_unordered_timeline():
    payload = data()
    incident = payload["incidents"][0]
    incident.update(currentStatus="investigating", resolved=False, resolvedDate=None)
    incident["timeline"] = [incident["timeline"][1]]
    result = parse_datadog(SPEC, payload, False)
    assert result.issues and not result.resolved_issue_ids
    assert next(iter(result.issues.values())).resolved_at == ""


def test_openrouter_maintenance_is_opt_in():
    payload = data()
    payload["maintenances"] = [{"id": "planned", "title": "Maintenance", "currentStatus": "scheduled",
                                "scheduledDescription": "Planned work", "startDate": "2026-10-01T00:00:00Z"}]
    assert not parse_datadog(SPEC, payload, False).issues
    result = parse_datadog(SPEC, payload, True)
    assert result.issues["incident_planned"].severity == "maintenance"


def test_openrouter_partial_history_failure_keeps_current_components():
    payload = data()
    payload["incidents"] = [{}]
    payload["components"][0]["components"][0]["status"] = "degraded"
    result = parse_datadog(SPEC, payload, False)
    assert result.issues and not result.complete and not result.history_complete


@pytest.mark.parametrize("payload", [{}, {"id": "one", "customDomain": "wrong.test"}])
def test_openrouter_rejects_unrecognized_snapshot(payload):
    with pytest.raises(ValueError):
        parse_datadog(SPEC, payload, False)


def test_builtin_readme_table_matches_configured_sources():
    readme = (ROOT / "README.md").read_text()
    section = readme.split("## 内置来源", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    assert rows[:2] == ["| 来源 | 官方状态页 |", "| --- | --- |"]
    assert len(rows) - 2 == len(BUILTIN_SOURCES)
    for spec in BUILTIN_SOURCES:
        assert f"]({spec.status_url})" in section
    assert "## 验证与升级" not in readme
