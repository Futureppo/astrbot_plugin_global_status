import asyncio
import copy
import json
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_global_status import sources
from data.plugins.astrbot_plugin_global_status.sources import (
    BUILTIN_SOURCES,
    SourceSpec,
    fetch_source,
    parse_google_cloud,
    parse_rss,
    parse_statuspage,
)

SPEC = SourceSpec("vendor", "Vendor", "statuspage", "https://status.test", "https://status.test/")
HEALTHY = {"components": [], "status": {"indicator": "none"}, "incidents": []}
ACTIVE = {"id": "one", "name": "API outage", "status": "investigating", "impact": "critical",
          "updated_at": "2026-09-20T10:00:00Z", "incident_updates": [], "components": []}


@pytest.mark.parametrize("summary,history", [({}, {}), ({"error": "denied"}, {"error": "denied"}),
                                             ({"components": []}, {"incidents": {}})])
def test_invalid_objects_do_not_mean_healthy(summary, history):
    with pytest.raises(ValueError):
        parse_statuspage(SPEC, summary, history, False)


def test_summary_only_active_incident_survives_history_failure():
    summary = {**HEALTHY, "incidents": [ACTIVE]}
    result = parse_statuspage(SPEC, summary, None, False)
    assert result.success and not result.complete and not result.history_complete
    assert result.severity == "critical" and "incident_one" in result.issues


def test_partial_empty_snapshot_is_unknown_not_healthy():
    result = parse_statuspage(SPEC, HEALTHY, None, False)
    assert result.success and result.severity == "unavailable"


def test_summary_and_history_merge_by_update_time_without_duplicate_recovery():
    resolved = {**ACTIVE, "status": "resolved", "updated_at": "2026-09-20T09:00:00Z"}
    result = parse_statuspage(SPEC, {**HEALTHY, "incidents": [ACTIVE]}, {"incidents": [resolved]}, False)
    assert len(result.issues) == 1 and not result.resolved_issue_ids
    resolved["updated_at"] = "2026-09-20T11:00:00Z"
    result = parse_statuspage(SPEC, {**HEALTHY, "incidents": [ACTIVE]}, {"incidents": [resolved]}, False)
    assert not result.issues and result.resolved_issue_ids == {"incident_one"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["summary", "incidents"])
async def test_fetch_preserves_successful_half_and_logs_exception_type(monkeypatch, failed):
    async def request(session, url):
        if url.endswith(f"/{failed}.json"):
            raise TimeoutError()
        return {**HEALTHY, "incidents": [ACTIVE]} if "summary" in url else {"incidents": [ACTIVE]}

    monkeypatch.setattr(sources, "_request_json", request)
    result = await fetch_source(None, SPEC, False)
    assert result.success and not result.complete and result.issues
    assert "TimeoutError" in result.error and f"/{failed}.json" in result.error


@pytest.mark.asyncio
async def test_fetch_cancels_all_sibling_requests(monkeypatch):
    ready = asyncio.Event()
    entered, finished = [], []

    async def request(session, url):
        entered.append(url)
        if len(entered) == 2:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(url)

    monkeypatch.setattr(sources, "_request_json", request)
    task = asyncio.create_task(fetch_source(None, SPEC, False))
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(entered) == sorted(finished)


@pytest.mark.parametrize("xml", ["<html><body>Unavailable</body></html>", "<rss/>", "<feed><entry/></feed>", "oops"])
def test_rss_rejects_error_pages_and_malformed_entries(xml):
    with pytest.raises(ValueError):
        parse_rss(SPEC, xml, False)


def test_xai_resolution_header_does_not_use_old_pubdate_for_catchup():
    xml = """<rss><channel><item><title>API outage</title><guid>one</guid>
      <pubDate>Mon, 14 Sep 2026 01:00:00 GMT</pubDate><category>resolved</category>
      <description>Resolved: Sun, 20 Sep 2026 10:00:00 GMT
      Service restored.</description></item></channel></rss>"""
    result = parse_rss(SPEC, xml, False)
    issue = next(iter(result.resolved_issues.values()))
    assert issue.resolved_at == "Sun, 20 Sep 2026 10:00:00 GMT"
    assert issue.started_at.startswith("Mon, 14")


def test_structured_active_rss_status_overrides_old_resolution_text():
    xml = """<rss><channel><item><title>API unavailable again</title><guid>one</guid>
      <category>investigating</category><description>Yesterday the issue has been resolved.</description>
      </item></channel></rss>"""
    assert parse_rss(SPEC, xml, False).issues


@pytest.mark.parametrize("payload", [[{}], {"error": "denied"}, [{"id": "one", "external_desc": "Vertex AI"}]])
def test_google_shape_errors_are_not_silently_skipped(payload):
    with pytest.raises(ValueError):
        parse_google_cloud(SPEC, payload)


def test_real_minimax_short_incident_preserves_resolved_content():
    payload = json.loads((Path(__file__).parent / "fixtures/minimax.json").read_text())
    result = parse_statuspage(SPEC, HEALTHY, payload, False)
    issue = result.resolved_issues["incident_mtkn3r6kwk1g"]
    assert issue.detail == "该问题已解决。"
    assert issue.resolved_at == "2026-09-14T09:06:45.217+08:00"
    assert issue.severity == "critical"


def test_builtin_source_inventory_keeps_windows_additions_and_official_links():
    by_id = {x.source_id: x for x in BUILTIN_SOURCES}
    assert {"cursor", "cerebras", "gemini_developer", "google_vertex_gemini"} <= set(by_id)
    assert "statuspage.io" not in by_id["deepseek"].endpoint
    assert by_id["azure"].endpoint.startswith("https://rssfeed.azure.status.microsoft/")
    assert by_id["vercel"].kind == "statuspage"
    assert by_id["vercel"].endpoint == "https://www.vercel-status.com"
    assert not any(x.source_id == "vercel" for x in sources.build_source_specs({"vercel": False}, []))
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    assert schema["sources"]["items"]["vercel"]["default"] is True


def test_malformed_history_entry_preserves_valid_summary_incident():
    summary = {**HEALTHY, "incidents": [ACTIVE]}
    result = parse_statuspage(SPEC, summary, {"incidents": [{}]}, False)
    assert result.success and not result.complete and not result.history_complete
    assert "incident_one" in result.issues


@pytest.mark.asyncio
async def test_both_request_errors_keep_their_exception_types(monkeypatch):
    async def request(session, url):
        raise TimeoutError()

    monkeypatch.setattr(sources, "_request_json", request)
    result = await fetch_source(None, SPEC, False)
    assert not result.success and "TimeoutError" in result.error
