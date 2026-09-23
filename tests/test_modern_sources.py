import asyncio
import copy
import json
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_global_status import modern_sources
from data.plugins.astrbot_plugin_global_status.modern_sources import (
    SourceHTTPError,
    parse_aistudio,
    parse_flashduty,
    public_frontend_keys,
)
from data.plugins.astrbot_plugin_global_status.sources import BUILTIN_SOURCES

FIXTURES = Path(__file__).parent / "fixtures"


def spec(source_id):
    return next(x for x in BUILTIN_SOURCES if x.source_id == source_id)


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_deepseek_live_fixture_and_active_variant():
    data = fixture("deepseek")
    result = parse_flashduty(spec("deepseek"), data["active"], data["history"], False)
    assert result.success and result.complete and not result.issues
    issue = result.resolved_issues["incident_7007335047287"]
    assert issue.started_at == "2026-09-20T08:45:32+00:00"
    assert issue.resolved_at == "2026-09-20T08:58:46+00:00"
    assert issue.affected_services[0].startswith("DeepSeek V4.1")
    change = copy.deepcopy(data["history"]["data"]["items"][0])
    change.update(status="monitoring", close_at_seconds=0, updates=change["updates"][:1])
    data["active"]["data"]["active_changes"] = [change]
    result = parse_flashduty(spec("deepseek"), data["active"], data["history"], False)
    assert set(result.issues) == {issue.issue_id}
    assert not result.resolved_issue_ids
    assert result.issues[issue.issue_id].resolved_at == ""


@pytest.mark.parametrize("active,history", [(None, None), ({}, {}), ({"data": {}}, {"data": {}})])
def test_deepseek_rejects_schema_drift(active, history):
    with pytest.raises(ValueError):
        parse_flashduty(spec("deepseek"), active, history, False)


def test_deepseek_partial_and_maintenance():
    data = fixture("deepseek")
    change = copy.deepcopy(data["history"]["data"]["items"][0])
    change.update(status="investigating", type="maintenance")
    data["active"]["data"]["active_changes"] = [change]
    hidden = parse_flashduty(spec("deepseek"), data["active"], None, False)
    assert hidden.success and not hidden.complete and not hidden.history_complete
    assert hidden.severity == "unavailable"
    shown = parse_flashduty(spec("deepseek"), data["active"], None, True)
    assert next(iter(shown.issues.values())).severity == "maintenance"


def test_aistudio_real_fixture_distinguishes_components_and_unix_time():
    result = parse_aistudio(spec("gemini_developer"), fixture("aistudio"))
    assert result.complete and not result.issues and len(result.resolved_issues) == 2
    batch = result.resolved_issues["incident_GeminiAPI-batch-delays-20260914"]
    assert batch.affected_services == ("Gemini API",)
    assert batch.resolved_at == "2026-09-16T04:11:00+00:00"
    billing = result.resolved_issues["incident_AIStudio-billing-issues-20260916"]
    assert billing.affected_services == ("Google AI Studio",)


def test_aistudio_unknown_stage_and_unsorted_updates_never_resolve():
    data = fixture("aistudio")
    row = data[0][0][0]
    row[3].append([99, "ignored display time", ["1789581800"], "New stage"])
    row[3].reverse()
    result = parse_aistudio(spec("gemini_developer"), data)
    assert "incident_" + row[0] in result.issues


@pytest.mark.parametrize("payload", [{"error": "denied"}, [None], [[{}, {}]], [[["bad"]]]])
def test_aistudio_rejects_malformed_nonempty_response(payload):
    with pytest.raises(ValueError):
        parse_aistudio(spec("gemini_developer"), payload)


@pytest.mark.parametrize("payload", [[], [[]], [[[]]]])
def test_aistudio_accepts_valid_empty_proto(payload):
    assert not parse_aistudio(spec("gemini_developer"), payload).issues


def test_public_frontend_configuration_prefers_verified_field_and_is_bounded():
    other, primary = "AIza" + "a" * 35, "AIza" + "b" * 35
    html = json.dumps({"PeqOqb": other, "WIu0Nc": primary})
    assert public_frontend_keys(html) == [primary, other]
    with pytest.raises(ValueError):
        public_frontend_keys("<html>Sign in</html>")


@pytest.mark.asyncio
async def test_modern_transport_closes_client_and_rotates_public_configuration(monkeypatch):
    import curl_cffi.requests

    clients, calls = [], []
    keys = ["AIza" + "a" * 35, "AIza" + "b" * 35]

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["impersonate"] == "chrome"
            self.closed = False
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    async def request(client, method, url, **kwargs):
        calls.append((method, url))
        if method == "GET":
            return json.dumps(keys)
        assert kwargs["headers"]["Referer"] == "https://aistudio.google.com/"
        if kwargs["headers"]["X-Goog-Api-Key"] == keys[0]:
            raise SourceHTTPError(403, "/rpc")
        return json.dumps(fixture("aistudio"))

    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", Client)
    monkeypatch.setattr(modern_sources, "_request", request)
    result = await modern_sources.fetch_modern_source(spec("gemini_developer"), False, 24)
    assert result.complete and len(calls) == 3 and clients[0].closed


@pytest.mark.asyncio
async def test_modern_request_does_not_expose_headers_or_proxy_secrets():
    class Client:
        async def request(self, *args, **kwargs):
            raise OSError("proxy://user:private-password@host")

    with pytest.raises(RuntimeError) as exc:
        await modern_sources._request(Client(), "GET", "https://status.test/public")
    assert "private-password" not in str(exc.value)
    assert str(exc.value) == "OSError at /public"


@pytest.mark.asyncio
async def test_modern_request_cancellation_propagates():
    class Client:
        async def request(self, *args, **kwargs):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await modern_sources._request(Client(), "GET", "https://status.test/public")


def test_malformed_deepseek_history_preserves_valid_current_data():
    data = fixture("deepseek")
    change = data["history"]["data"]["items"][0]
    change["status"] = "monitoring"
    data["active"]["data"]["active_changes"] = [change]
    result = parse_flashduty(spec("deepseek"), data["active"], {"data": {"items": [{}]}}, False)
    assert result.success and result.issues and not result.history_complete
