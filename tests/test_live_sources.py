"""Opt-in, read-only checks against the newly integrated official sources."""

import os

import aiohttp
import pytest

from data.plugins.astrbot_plugin_global_status.sources import BUILTIN_SOURCES, fetch_source

pytestmark = pytest.mark.skipif(
    os.environ.get("GLOBAL_STATUS_LIVE") != "1",
    reason="Set GLOBAL_STATUS_LIVE=1 to read public vendor status endpoints",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["deepseek", "gemini_developer", "vercel", "fireworks", "novita", "openrouter"])
async def test_official_source_live(source_id):
    spec = next(x for x in BUILTIN_SOURCES if x.source_id == source_id)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=True) as session:
        result = await fetch_source(session, spec, False)
    print({"source": source_id, "success": result.success, "complete": result.complete,
           "active": len(result.issues), "resolved": len(result.resolved_issues),
           "fetched_at": result.fetched_at, "error": result.error})
    assert result.success and result.complete, result.error


@pytest.mark.asyncio
async def test_existing_sources_live():
    from data.plugins.astrbot_plugin_global_status.sources import fetch_all_sources

    added = {"deepseek", "gemini_developer", "vercel", "fireworks", "novita", "openrouter"}
    specs = [spec for spec in BUILTIN_SOURCES if spec.source_id not in added]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=True) as session:
        results = await fetch_all_sources(session, specs, False)
    errors = []
    for result in results:
        print({"source": result.spec.source_id, "success": result.success, "complete": result.complete,
               "active": len(result.issues), "resolved": len(result.resolved_issues), "error": result.error})
        if not result.success or not result.complete:
            errors.append(f"{result.spec.source_id}: {result.error}")
    assert not errors, errors
