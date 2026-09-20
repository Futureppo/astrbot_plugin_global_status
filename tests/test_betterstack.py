import json
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_global_status.betterstack import parse_betterstack
from data.plugins.astrbot_plugin_global_status.sources import (
    BUILTIN_SOURCES,
    build_source_specs,
    parse_statuspage,
)

FIXTURES = Path(__file__).parent / "fixtures"
SPEC = next(x for x in BUILTIN_SOURCES if x.source_id == "novita")


def data():
    return json.loads((FIXTURES / "novita.json").read_text())


def test_novita_real_json_api_uses_latest_recovery_update_not_null_ends_at():
    payload = data()
    payload["included"].reverse()
    result = parse_betterstack(SPEC, payload, False)
    assert result.complete and result.severity == "operational"
    issue = result.resolved_issues["incident_1034267"]
    assert issue.resolved_at == "2026-08-26T13:43:36.854Z"
    assert issue.affected_services == ("MiniMax M3",)
    assert issue.detail == "MiniMax M3 recovered."


def test_novita_components_without_report_notify_and_ignore_old_uptime_history():
    payload = data()
    resource = payload["included"][0]["attributes"]
    resource["status"] = "downtime"
    resource["status_history"] = [{"day": "2020-01-01", "status": "not_monitored"}]
    result = parse_betterstack(SPEC, payload, False)
    assert result.severity == "critical" and "components" in result.issues
    fingerprint = result.issues["components"].fingerprint
    payload["data"]["attributes"]["updated_at"] = "2030-01-01T00:00:00Z"
    assert parse_betterstack(SPEC, payload, False).issues["components"].fingerprint == fingerprint


def test_novita_active_report_does_not_duplicate_components():
    payload = data()
    payload["included"][0]["attributes"]["status"] = "downtime"
    report = payload["included"][-1]
    report["attributes"]["aggregate_state"] = "downtime"
    report["attributes"]["affected_resources"][0]["status"] = "downtime"
    result = parse_betterstack(SPEC, payload, False)
    assert set(result.issues) == {"incident_1034267"}
    assert result.issues["incident_1034267"].resolved_at == ""


def test_novita_unknown_current_state_is_not_healthy_or_a_vendor_outage():
    payload = data()
    payload["included"][0]["attributes"]["status"] = "not_monitored"
    result = parse_betterstack(SPEC, payload, False)
    assert result.success and not result.complete and result.severity == "unavailable"


def test_novita_missing_linked_update_retains_current_degradation():
    payload = data()
    payload["included"][0]["attributes"]["status"] = "downtime"
    payload["included"] = [x for x in payload["included"] if x["id"] != "5657979"]
    result = parse_betterstack(SPEC, payload, False)
    assert not result.complete and not result.history_complete
    assert result.issues["components"].severity == "critical"


@pytest.mark.parametrize("payload", [{}, {"data": {"type": "error"}}, {"data": {"type": "status_page", "attributes": {}}}])
def test_novita_rejects_unrecognized_json(payload):
    with pytest.raises(ValueError):
        parse_betterstack(SPEC, payload, False)


def test_fireworks_compatibility_payload_without_component_links():
    payload = json.loads((FIXTURES / "fireworks.json").read_text())
    spec = next(x for x in BUILTIN_SOURCES if x.source_id == "fireworks")
    result = parse_statuspage(spec, payload["summary"], payload["history"], False)
    assert result.complete and len(result.resolved_issues) == 1
    assert next(iter(result.resolved_issues.values())).resolved_at == "2026-09-19T17:04:04Z"


def test_added_sources_have_matching_config_and_can_be_disabled():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    for source_id in ("fireworks", "novita", "vercel", "gemini_developer"):
        assert schema["sources"]["items"][source_id]["default"] is True
        assert not any(x.source_id == source_id for x in build_source_specs({source_id: False}, []))
