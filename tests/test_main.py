import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from astrbot.api.message_components import Image
from data.plugins.astrbot_plugin_global_status.main import GlobalStatusMonitor
from data.plugins.astrbot_plugin_global_status.sources import (
    Issue,
    SourceResult,
    SourceSpec,
)


class DummyPlatform:
    def __init__(self, platform_id="1", name="aiocqhttp"):
        self.metadata = SimpleNamespace(id=platform_id, name=name)

    def meta(self):
        return self.metadata


class DummyPlatformManager:
    def __init__(self, platforms=None):
        self.platforms = platforms or [DummyPlatform()]

    def get_insts(self):
        return self.platforms


class DummyContext:
    def __init__(self, platforms=None, provider=None, global_config=None):
        self.platform_manager = DummyPlatformManager(platforms)
        self.provider = provider
        self.global_config = global_config or {"timezone": "Asia/Shanghai"}
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True

    def get_using_provider(self):
        return self.provider

    def get_config(self):
        return self.global_config


class DummyConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = []

    async def save_config_async(self, replace_config):
        self.update(replace_config)
        self.saved.append(dict(replace_config))


def _plugin(config=None, platforms=None):
    instance = GlobalStatusMonitor(
        DummyContext(platforms),
        config
        or {
            "enabled": True,
            "group_whitelist": ["100", "200"],
            "platform_id": "",
        },
    )
    instance._state = instance._empty_state()
    return instance


def _spec(kind="statuspage"):
    return SourceSpec("vendor", "Vendor", kind, "endpoint", "https://status.test/")


def _issue(detail="v1"):
    return Issue(
        "vendor",
        "Vendor",
        "incident_1",
        "warning",
        "API degradation",
        detail=detail,
        status_url="https://status.test/",
    )


def test_platform_auto_selection_and_unified_message_origins():
    plugin = _plugin()

    assert plugin._resolve_platform_id() == "1"
    assert plugin._targets(["100"]) == {"1|100": "1:GroupMessage:100"}


def test_platform_auto_selection_rejects_multiple_instances():
    plugin = _plugin(platforms=[DummyPlatform("1"), DummyPlatform("2")])

    assert plugin._resolve_platform_id() is None
    assert plugin._targets(["100"]) == {}


def test_image_timezone_override_and_astrbot_default():
    plugin = GlobalStatusMonitor(
        DummyContext(global_config={"timezone": "Europe/London"}),
        {"timezone": ""},
    )

    assert plugin._display_timezone_name() == "Europe/London"
    assert getattr(plugin._display_datetime().tzinfo, "key", "") == "Europe/London"

    plugin.config["timezone"] = "America/New_York"
    assert plugin._display_timezone_name() == "America/New_York"
    assert getattr(plugin._display_datetime().tzinfo, "key", "") == ("America/New_York")


def test_reconcile_initial_update_dedup_and_recovery():
    plugin = _plugin()
    targets = plugin._targets(["100"])
    first_issue = _issue("v1")
    first = SourceResult(_spec(), True, {first_issue.issue_id: first_issue})

    pending = plugin._reconcile_source(first, targets)
    assert pending["1|100"][0][0] == "new"
    plugin._mark_delivered("1|100", pending["1|100"])

    assert plugin._reconcile_source(first, targets) == {}

    changed_issue = _issue("v2")
    changed = SourceResult(_spec(), True, {changed_issue.issue_id: changed_issue})
    pending = plugin._reconcile_source(changed, targets)
    assert pending["1|100"][0][0] == "update"
    plugin._mark_delivered("1|100", pending["1|100"])

    recovered = SourceResult(
        _spec(),
        True,
        {},
        resolved_issue_ids={changed_issue.issue_id},
    )
    pending = plugin._reconcile_source(recovered, targets)
    assert pending["1|100"][0][0] == "recovered"
    plugin._mark_delivered("1|100", pending["1|100"])
    plugin._cleanup_recoveries("vendor", {"1|100"}, True)
    assert plugin._state["sources"]["vendor"]["recoveries"] == {}


def test_rss_absence_never_implies_recovery():
    plugin = _plugin()
    targets = plugin._targets(["100"])
    issue = _issue()
    active = SourceResult(_spec("rss"), True, {issue.issue_id: issue})
    pending = plugin._reconcile_source(active, targets)
    plugin._mark_delivered("1|100", pending["1|100"])

    missing = SourceResult(_spec("rss"), True, {})
    assert plugin._reconcile_source(missing, targets) == {}
    assert issue.issue_id in plugin._state["sources"]["vendor"]["issues"]

    pending = plugin._reconcile_source(missing, targets)
    assert pending == {}
    assert issue.issue_id in plugin._state["sources"]["vendor"]["issues"]
    assert not missing.complete


@pytest.mark.asyncio
async def test_run_cycle_retries_only_failed_group(monkeypatch):
    plugin = _plugin()
    issue = _issue()
    result = SourceResult(_spec(), True, {issue.issue_id: issue})

    async def fetch_sources():
        return [result]

    calls = []
    fail_second = True

    async def send_events(umo, source_name, events, translations=None):
        nonlocal fail_second
        calls.append(umo)
        if umo.endswith(":200") and fail_second:
            fail_second = False
            return False
        return True

    async def save_state(key, value):
        return None

    monkeypatch.setattr(plugin, "_fetch_sources", fetch_sources)
    monkeypatch.setattr(plugin, "_send_events", send_events)
    monkeypatch.setattr(plugin, "put_kv_data", save_state)

    await plugin._run_cycle()
    assert calls == ["1:GroupMessage:100", "1:GroupMessage:200"]

    calls.clear()
    await plugin._run_cycle()
    assert calls == ["1:GroupMessage:200"]


@pytest.mark.asyncio
async def test_run_cycle_batches_translation_through_default_provider(monkeypatch):
    class Provider:
        calls = 0

        async def text_chat(self, prompt, system_prompt):
            self.calls += 1
            payload = json.loads(prompt)
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "translations": [
                            {"id": item["id"], "zh_cn": "状态事件中文译文"}
                            for item in payload["items"]
                        ]
                    },
                    ensure_ascii=False,
                )
            )

    provider = Provider()
    plugin = GlobalStatusMonitor(
        DummyContext(provider=provider),
        {
            "enabled": True,
            "group_whitelist": ["100", "200"],
            "platform_id": "",
            "display_language": "bilingual",
            "enable_ai_translation": True,
        },
    )
    plugin._state = plugin._empty_state()
    first_issue = _issue("First official update")
    second_issue = Issue(
        "vendor_two",
        "Vendor Two",
        "incident_2",
        "critical",
        "Second service incident",
        detail="Second official update",
    )
    results = [
        SourceResult(_spec(), True, {first_issue.issue_id: first_issue}),
        SourceResult(
            SourceSpec(
                "vendor_two",
                "Vendor Two",
                "statuspage",
                "endpoint",
                "https://status-two.test/",
            ),
            True,
            {second_issue.issue_id: second_issue},
        ),
    ]

    async def fetch_sources():
        return results

    delivered_translations = []

    async def send_events(umo, source_name, events, translations=None):
        delivered_translations.append(translations)
        return True

    async def save_state(key, value):
        return None

    monkeypatch.setattr(plugin, "_fetch_sources", fetch_sources)
    monkeypatch.setattr(plugin, "_send_events", send_events)
    monkeypatch.setattr(plugin, "put_kv_data", save_state)

    await plugin._run_cycle()

    assert provider.calls == 1
    assert len(delivered_translations) == 4
    assert all("API degradation" in item for item in delivered_translations)
    assert all("Second service incident" in item for item in delivered_translations)


@pytest.mark.asyncio
async def test_first_startup_can_baseline_existing_incidents_without_sending(
    monkeypatch,
):
    plugin = _plugin(
        {
            "enabled": True,
            "group_whitelist": ["100"],
            "platform_id": "",
            "notify_existing_on_first_startup": False,
            "enable_ai_translation": False,
        }
    )
    current_issue = _issue("Existing incident")
    current_result = SourceResult(
        _spec(), True, {current_issue.issue_id: current_issue}
    )

    async def fetch_sources():
        return [current_result]

    calls = []

    async def send_events(umo, source_name, events, translations=None):
        calls.append((umo, events))
        return True

    async def save_state(key, value):
        return None

    monkeypatch.setattr(plugin, "_fetch_sources", fetch_sources)
    monkeypatch.setattr(plugin, "_send_events", send_events)
    monkeypatch.setattr(plugin, "put_kv_data", save_state)

    await plugin._run_cycle()
    await plugin._run_cycle()

    assert calls == []
    assert plugin._state["initialized_sources"] == ["vendor"]
    assert (
        plugin._state["deliveries"]["1|100"][current_issue.key]
        == current_issue.fingerprint
    )

    updated_issue = _issue("New official update")
    current_result.issues = {updated_issue.issue_id: updated_issue}
    await plugin._run_cycle()

    assert len(calls) == 1
    assert calls[0][1][0][0] == "update"


@pytest.mark.asyncio
async def test_empty_whitelist_updates_state_without_sending(monkeypatch):
    plugin = _plugin({"enabled": True, "group_whitelist": [], "platform_id": ""})
    issue = _issue()

    async def fetch_sources():
        return [SourceResult(_spec(), True, {issue.issue_id: issue})]

    async def save_state(key, value):
        return None

    monkeypatch.setattr(plugin, "_fetch_sources", fetch_sources)
    monkeypatch.setattr(plugin, "put_kv_data", save_state)

    await plugin._run_cycle()

    assert issue.issue_id in plugin._state["sources"]["vendor"]["issues"]
    assert plugin.context.sent == []


@pytest.mark.asyncio
async def test_failed_source_does_not_clear_previous_issue(monkeypatch):
    plugin = _plugin()
    issue = _issue()
    plugin._reconcile_source(
        SourceResult(_spec(), True, {issue.issue_id: issue}),
        {},
    )
    before = copy.deepcopy(plugin._state)

    async def fetch_sources():
        return [SourceResult(_spec(), False, error="timeout")]

    async def save_state(key, value):
        return None

    monkeypatch.setattr(plugin, "_fetch_sources", fetch_sources)
    monkeypatch.setattr(plugin, "put_kv_data", save_state)

    await plugin._run_cycle()

    assert plugin._state["sources"] == before["sources"]
    assert plugin._state["deliveries"] == before["deliveries"]
    assert plugin._state["health"]["vendor"]["consecutive_failures"] == 1
    assert not plugin.context.sent


@pytest.mark.asyncio
async def test_vendor_status_command_returns_image_without_mutating_state(monkeypatch):
    plugin = _plugin()
    result = SourceResult(_spec(), True)
    before = copy.deepcopy(plugin._state)
    fetch_count = 0

    async def fetch_sources():
        nonlocal fetch_count
        fetch_count += 1
        return [result]

    class DummyEvent:
        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return text

    monkeypatch.setattr(plugin, "_fetch_sources", fetch_sources)

    responses = [item async for item in plugin.vendor_status(DummyEvent())]

    assert len(responses) == 1
    assert isinstance(responses[0][0], Image)
    assert fetch_count == 1
    assert plugin._state == before


@pytest.mark.asyncio
async def test_group_subscription_closes_plain_group_id(monkeypatch):
    config = DummyConfig(
        {"enabled": True, "group_whitelist": ["100"], "platform_id": ""}
    )
    plugin = _plugin(config)

    reply = await plugin._set_group_subscription("1:GroupMessage:100", "关")

    assert config["group_whitelist"] == []
    assert config.saved == [{"group_whitelist": []}]
    assert "已为当前群组关闭" in reply


@pytest.mark.asyncio
async def test_group_subscription_closes_all_matching_whitelist_formats():
    umo = "1:GroupMessage:100"
    config = DummyConfig(
        {
            "enabled": True,
            "group_whitelist": ["100", umo, "200", "100"],
            "platform_id": "",
        }
    )
    plugin = _plugin(config)

    await plugin._set_group_subscription(umo, "关")

    assert config["group_whitelist"] == ["200"]
    assert config.saved == [{"group_whitelist": ["200"]}]


@pytest.mark.asyncio
async def test_group_subscription_handles_qq_official_group_openid():
    umo = "qq-instance:GroupMessage:group-openid"
    config = DummyConfig(
        {
            "enabled": True,
            "platform_type": "qq_official",
            "group_whitelist": ["group-openid"],
            "platform_id": "",
        }
    )
    plugin = _plugin(config, platforms=[DummyPlatform("qq-instance", "qq_official")])

    await plugin._set_group_subscription(umo, "关")

    assert config["group_whitelist"] == []


@pytest.mark.asyncio
async def test_group_subscription_invalid_action_does_not_persist():
    config = DummyConfig(
        {"enabled": True, "group_whitelist": ["100"], "platform_id": ""}
    )
    plugin = _plugin(config)

    reply = await plugin._set_group_subscription("1:GroupMessage:100", "开启")

    assert reply == "用法：/厂商订阅 开｜关"
    assert config["group_whitelist"] == ["100"]
    assert config.saved == []


@pytest.mark.asyncio
async def test_terminate_cancels_task_and_closes_session():
    plugin = _plugin()

    class FakeSession:
        closed = False

        async def close(self):
            self.closed = True

    session = FakeSession()
    task = asyncio.create_task(asyncio.sleep(60))
    plugin._session = session
    plugin._monitor_task = task

    await plugin.terminate()

    assert task.cancelled()
    assert session.closed
    assert plugin._session is None
