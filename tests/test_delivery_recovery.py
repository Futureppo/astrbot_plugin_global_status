"""Delivery interruption checks using isolated adapters and persisted snapshots."""

import asyncio
import copy

import pytest

from data.plugins.astrbot_plugin_global_status import main
from data.plugins.astrbot_plugin_global_status.sources import SourceResult

from .test_main import _issue, _plugin, _spec


@pytest.mark.asyncio
async def test_stalled_adapter_times_out_and_other_target_can_send(monkeypatch):
    plugin = _plugin({"enable_ai_translation": False})
    monkeypatch.setattr(main, "SEND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(main, "render_alert_card", lambda *args: b"image")
    cancelled = asyncio.Event()

    async def send(umo, chain):
        if umo.endswith(":100"):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return True

    monkeypatch.setattr(plugin.context, "send_message", send)
    events = [("new", _issue())]
    assert not await asyncio.wait_for(
        plugin._send_events("1:GroupMessage:100", "Vendor", events, {}), 0.5
    )
    assert cancelled.is_set()
    assert await plugin._send_events("1:GroupMessage:200", "Vendor", events, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("health_notice", [False, True])
async def test_restart_after_partial_delivery_does_not_replay_successful_target(
    monkeypatch, health_notice
):
    plugin = _plugin(
        {
            "group_whitelist": ["100", "200"],
            "enable_ai_translation": False,
            "source_failure_threshold": 1,
        }
    )
    issue = _issue()
    result = (
        SourceResult(_spec(), False, error="TimeoutError")
        if health_notice
        else SourceResult(_spec(), True, {issue.issue_id: issue})
    )
    saved = copy.deepcopy(plugin._state)
    second_started = asyncio.Event()

    async def fetch():
        return [result]

    async def save(key, value):
        nonlocal saved
        if key == main.STATE_KEY:
            saved = copy.deepcopy(value)

    async def send(umo, name, events, translations=None):
        if umo.endswith(":200"):
            second_started.set()
            await asyncio.Event().wait()
        return True

    monkeypatch.setattr(plugin, "_fetch_sources", fetch)
    monkeypatch.setattr(plugin, "put_kv_data", save)
    monkeypatch.setattr(plugin, "_send_events", send)
    task = asyncio.create_task(plugin._run_cycle())
    try:
        await asyncio.wait_for(second_started.wait(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    reloaded = _plugin(plugin.config)
    reloaded._state = saved
    sent = []

    async def recovered_send(umo, name, events, translations=None):
        sent.append(umo)
        return True

    monkeypatch.setattr(reloaded, "_fetch_sources", fetch)
    monkeypatch.setattr(reloaded, "put_kv_data", save)
    monkeypatch.setattr(reloaded, "_send_events", recovered_send)
    await reloaded._run_cycle()
    assert sent == ["1:GroupMessage:200"]


@pytest.mark.asyncio
async def test_storage_failure_prevents_external_delivery(monkeypatch):
    plugin = _plugin({"group_whitelist": ["100"], "enable_ai_translation": False})
    issue = _issue()

    async def fetch():
        return [SourceResult(_spec(), True, {issue.issue_id: issue})]

    async def save(*args):
        raise OSError("Disk unavailable")

    async def send(*args):
        pytest.fail("External delivery preceded durable state")

    monkeypatch.setattr(plugin, "_fetch_sources", fetch)
    monkeypatch.setattr(plugin, "put_kv_data", save)
    monkeypatch.setattr(plugin, "_send_events", send)
    with pytest.raises(OSError, match="Disk unavailable"):
        await plugin._run_cycle()
