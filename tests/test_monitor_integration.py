from dataclasses import replace

import pytest

from data.plugins.astrbot_plugin_global_status.renderer import build_alert_fallback, render_alert_card
from data.plugins.astrbot_plugin_global_status.sources import SourceResult

from .test_main import _issue, _plugin, _spec


@pytest.mark.asyncio
async def test_monitor_health_batches_vendors_and_retries_failed_group(monkeypatch):
    plugin = _plugin({"group_whitelist": ["100", "200"], "source_failure_threshold": 1,
                      "enable_ai_translation": False})
    results = [SourceResult(_spec(), False, error="TimeoutError"),
               SourceResult(replace(_spec(), source_id="second", name="Second"), False, error="ValueError")]
    calls = []

    async def fetch():
        return results

    async def send(umo, source_name, events, translations=None):
        calls.append((umo, [stage for stage, _ in events]))
        return not umo.endswith(":200")

    async def save(*args):
        return None

    monkeypatch.setattr(plugin, "_fetch_sources", fetch)
    monkeypatch.setattr(plugin, "_send_events", send)
    monkeypatch.setattr(plugin, "put_kv_data", save)
    await plugin._run_cycle()
    assert len(calls) == 2 and len(calls[0][1]) == 2
    calls.clear()
    await plugin._run_cycle()
    assert calls == [("1:GroupMessage:200", ["source_unavailable", "source_unavailable"])]
    results[:] = [SourceResult(_spec(), True)]
    calls.clear()
    await plugin._run_cycle()
    assert calls == [("1:GroupMessage:100", ["source_recovered"])]
    assert "second" not in plugin._state["health"]


@pytest.mark.asyncio
async def test_incomplete_source_keeps_known_fault_and_reports_new_fault(monkeypatch):
    plugin = _plugin({"group_whitelist": ["100"], "source_failure_threshold": 3,
                      "enable_ai_translation": False})
    old = _issue()
    first = plugin._reconcile_source(SourceResult(_spec(), True, {old.issue_id: old}), plugin._targets(["100"]))
    plugin._mark_delivered("1|100", first["1|100"])
    new = replace(old, issue_id="incident_2", title="Another failure")
    partial = SourceResult(_spec(), True, {new.issue_id: new}, complete=False,
                           history_complete=False, error="incidents.json TimeoutError")
    calls = []

    async def fetch():
        return [partial]

    async def send(umo, name, events, translations=None):
        calls.extend(events)
        return True

    async def save(*args):
        return None

    monkeypatch.setattr(plugin, "_fetch_sources", fetch)
    monkeypatch.setattr(plugin, "_send_events", send)
    monkeypatch.setattr(plugin, "put_kv_data", save)
    await plugin._run_cycle()
    assert [(stage, issue.issue_id) for stage, issue in calls] == [("new", "incident_2")]
    assert set(plugin._state["sources"]["vendor"]["issues"]) == {old.issue_id, new.issue_id}
    assert not plugin._last_results[0].complete


def test_new_stages_render_images_and_unambiguous_fallback():
    issue = _issue()
    stages = ["missed", "source_unavailable", "source_recovered"]
    events = [(stage, issue) for stage in stages]
    text = build_alert_fallback("Vendor", events, {}, "zh-CN")
    assert "事件补报（已恢复）" in text and "采集异常" in text and "采集恢复" in text
    assert render_alert_card("Vendor", events, {}, "zh-CN").startswith(b"\x89PNG")
