"""AstrBot plugin entry point for global vendor status monitoring."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain

from .monitor_state import (
    cleanup_resolved,
    delivery_fingerprint,
    mark_health_delivered,
    plan_health_notices,
    presentation_result,
    reconcile_source,
 )
from .renderer import build_alert_fallback, render_alert_card, render_overview
from .sources import (
    Issue,
    SourceResult,
    build_source_specs,
    fetch_all_sources,
)
from .translation import TranslationService, normalize_language

STATE_KEY = "monitor_state_v1"
STATE_VERSION = 1
TRANSLATION_CACHE_KEY = "translation_cache_v1"


class GlobalStatusMonitor(star.Star):
    """Monitor official vendor status sources and push image alerts."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._fetch_lock = asyncio.Lock()
        self._state = self._empty_state()
        self._last_results: list[SourceResult] = []
        self._platform_warning = ""
        self._translator = TranslationService(context)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "sources": {},
            "deliveries": {},
            "initialized_sources": [],
            "health": {},
        }

    async def initialize(self) -> None:
        """Load persistent state, create the HTTP client, and start polling."""
        stored = await self.get_kv_data(STATE_KEY, self._empty_state())
        if (
            isinstance(stored, dict)
            and stored.get("version") == STATE_VERSION
            and isinstance(stored.get("sources"), dict)
            and isinstance(stored.get("deliveries"), dict)
        ):
            self._state = stored
            initialized_sources = self._state.get("initialized_sources")
            if not isinstance(initialized_sources, list):
                # States created before this option already completed their baseline.
                self._state["initialized_sources"] = list(self._state["sources"])
        else:
            self._state = self._empty_state()
        translation_cache = await self.get_kv_data(TRANSLATION_CACHE_KEY, {})
        self._translator.load_cache(translation_cache)

        timeout = aiohttp.ClientTimeout(total=15)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            trust_env=True,
            headers={"User-Agent": "AstrBot-Global-Status-Monitor/1.0"},
        )
        self._maybe_start_monitor()

    def _maybe_start_monitor(self) -> None:
        """按当前状态决定是否立即启动轮询任务。

        冷启动时平台适配器尚未实例化（platform_manager.initialize 在本插件
        initialize 之后执行），此时推迟到 on_astrbot_loaded 钩子再启动；
        运行时热重载时平台已就绪，直接启动。
        """
        if not bool(self.config.get("enabled", True)):
            logger.info(
                "Global status monitor is disabled; query command remains available."
            )
            return
        if not self._platforms_ready():
            logger.info(
                "Global status monitor will start after AstrBot finishes loading."
            )
            return
        self._start_monitor()

    def _start_monitor(self) -> None:
        """启动轮询任务（幂等，重复调用不会创建多个任务）。"""
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._stop_event.clear()
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name="astrbot-global-status-monitor",
        )
        logger.info("Global status monitor started.")

    def _platforms_ready(self) -> bool:
        """平台适配器是否已实例化。

        冷启动期间 platform_manager 尚未填充实例，返回 False；AstrBot 完成
        启动后（含运行时热重载）至少存在 webchat 适配器，恒返回 True。
        """
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return False
        try:
            return len(manager.get_insts()) > 0
        except Exception:
            return False

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self) -> None:
        """AstrBot 启动完成、平台适配器就绪后再启动轮询。"""
        self._maybe_start_monitor()

    async def terminate(self) -> None:
        """Stop polling and close the shared HTTP client."""
        self._stop_event.set()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
        self._monitor_task = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("Global status monitor stopped.")

    def _source_specs(self):
        source_config = self.config.get("sources", {})
        custom_sources = self.config.get("custom_statuspage_sources", [])
        return build_source_specs(
            source_config if isinstance(source_config, dict) else {},
            custom_sources if isinstance(custom_sources, list) else [],
        )

    def _display_timezone_name(self) -> str:
        """Return the plugin timezone override or AstrBot's global timezone."""
        configured = str(self.config.get("timezone", "") or "").strip()
        if configured:
            return configured
        try:
            astrbot_config = self.context.get_config()
        except (AttributeError, TypeError):
            return ""
        return str(astrbot_config.get("timezone", "") or "").strip()

    def _display_datetime(self) -> datetime:
        """Return the current time in the configured display timezone."""
        timezone_name = self._display_timezone_name()
        if not timezone_name:
            return datetime.now().astimezone()
        try:
            return datetime.now(ZoneInfo(timezone_name))
        except (ValueError, ZoneInfoNotFoundError):
            logger.warning(
                "Invalid status image timezone %r; using the system timezone.",
                timezone_name,
            )
            return datetime.now().astimezone()

    def _bounded_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(self.config.get(key, default))))
        except (TypeError, ValueError):
            return default

    def _history_hours(self) -> int:
        return self._bounded_int("history_lookback_hours", 24, 0, 168)

    async def _fetch_sources(self) -> list[SourceResult]:
        """Fetch all enabled sources under a lock shared with the query command."""
        if self._session is None or self._session.closed:
            raise RuntimeError("Status monitor HTTP session is not available")
        async with self._fetch_lock:
            specs = self._source_specs()
            results = await fetch_all_sources(
                self._session,
                specs,
                bool(self.config.get("notify_maintenance", False)),
                self._history_hours(),
            )
            self._last_results = [presentation_result(result, self._state) for result in results]
            for result in results:
                if not result.success or not result.complete:
                    logger.warning(
                        "Status source %s incomplete: %s",
                        result.spec.name,
                        result.error,
                    )
            return results

    async def _monitor_loop(self) -> None:
        interval_value = self.config.get("poll_interval_seconds", 300)
        try:
            interval = max(60, int(interval_value))
        except (TypeError, ValueError):
            interval = 300
        while not self._stop_event.is_set():
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected global status monitor cycle failure.")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    def _configured_groups(self) -> list[str]:
        """Normalize group_whitelist entries: trim, deduplicate, and return.

        Entries may be pure digit group IDs, group_openid strings, or full
        UMO strings (platform_id:MessageType:session_id). Format-specific
        validation is deferred to _targets().
        """
        raw_groups = self.config.get("group_whitelist", [])
        if not isinstance(raw_groups, list):
            return []
        entries: list[str] = []
        for raw_group in raw_groups:
            entry = str(raw_group).strip()
            if not entry or entry in entries:
                continue
            entries.append(entry)
        return entries

    def _platform_type(self) -> str:
        """Return the configured platform adapter type name."""
        raw = str(self.config.get("platform_type", "aiocqhttp") or "").strip()
        if not raw:
            return "aiocqhttp"
        known = {"aiocqhttp", "qq_official"}
        if raw not in known:
            logger.warning(
                "Unknown platform_type %r in configuration; known types: %s. "
                "Falling back to 'aiocqhttp' for plain group ID resolution. "
                "Use full UMO in group_whitelist to target other platforms.",
                raw,
                ", ".join(sorted(known)),
            )
            return "aiocqhttp"
        return raw

    def _resolve_platform_id(self) -> str | None:
        configured_id = str(self.config.get("platform_id", "")).strip()
        adapter_type = self._platform_type()
        manager = getattr(self.context, "platform_manager", None)
        platforms = manager.get_insts() if manager is not None else []
        matched_platforms = []
        for platform in platforms:
            try:
                metadata = platform.meta()
            except Exception:
                continue
            if metadata.name == adapter_type:
                matched_platforms.append(metadata)

        resolved: str | None = None
        if configured_id:
            match = next(
                (item for item in matched_platforms if str(item.id) == configured_id),
                None,
            )
            if match is not None:
                resolved = str(match.id)
            else:
                warning = (
                    f"Configured platform_id {configured_id!r} is not an active "
                    f"{adapter_type} instance."
                )
                if warning != self._platform_warning:
                    logger.warning(warning)
                    self._platform_warning = warning
                return None
        elif len(matched_platforms) == 1:
            resolved = str(matched_platforms[0].id)
        else:
            warning = (
                f"Cannot auto-select {adapter_type} platform: expected exactly one "
                f"active instance, found {len(matched_platforms)}."
            )
            if warning != self._platform_warning:
                logger.warning(warning)
                self._platform_warning = warning
            return None
        self._platform_warning = ""
        return resolved

    def _targets(self, groups: list[str]) -> dict[str, str]:
        if not groups:
            return {}
        targets: dict[str, str] = {}
        plain_groups: list[str] = []
        active_ids: set[str] | None = None
        for entry in groups:
            if ":" in entry:
                # Full UMO — validate format and platform existence.
                try:
                    from astrbot.core.platform.message_session import MessageSesion

                    session = MessageSesion.from_str(entry)
                except Exception:
                    logger.warning(
                        "Invalid UMO format in group_whitelist: %r, skipping.", entry
                    )
                    continue
                if active_ids is None:
                    manager = getattr(self.context, "platform_manager", None)
                    platforms = manager.get_insts() if manager is not None else []
                    active_ids = set()
                    for p in platforms:
                        try:
                            active_ids.add(str(p.meta().id))
                        except Exception:
                            continue
                if str(session.platform_id) not in active_ids:
                    logger.warning(
                        "UMO %r references unknown platform_id %r, skipping.",
                        entry,
                        session.platform_id,
                    )
                    continue
                targets[entry] = entry
            else:
                plain_groups.append(entry)
        if plain_groups:
            platform_id = self._resolve_platform_id()
            if platform_id:
                seen_umos = set(targets.values())
                for group_id in plain_groups:
                    umo = f"{platform_id}:GroupMessage:{group_id}"
                    if umo in seen_umos:
                        continue
                    seen_umos.add(umo)
                    targets[f"{platform_id}|{group_id}"] = umo
        return targets

    def _prune_disabled_sources(self, enabled_source_ids: set[str]) -> None:
        health = self._state.setdefault("health", {})
        disabled = (set(self._state["sources"]) | set(health)) - enabled_source_ids
        for source_id in disabled:
            self._state["sources"].pop(source_id, None)
            health.pop(source_id, None)
        self._state["initialized_sources"] = [
            source_id for source_id in self._state.get("initialized_sources", [])
            if source_id in enabled_source_ids
        ]
        for delivered in self._state["deliveries"].values():
            for issue_key in list(delivered):
                if issue_key.split(":", 1)[0] in disabled:
                    delivered.pop(issue_key, None)

    def _prune_removed_groups(self, groups: list[str]) -> None:
        allowed = set(groups)
        ledgers = [self._state["deliveries"]]
        ledgers.extend(x.get("deliveries", {}) for x in self._state.get("health", {}).values())
        for deliveries in ledgers:
            for target_key in list(deliveries):
                if target_key.rsplit("|", 1)[-1] not in allowed:
                    deliveries.pop(target_key, None)

    def _reconcile_source(
        self, result: SourceResult, targets: dict[str, str],
    ) -> dict[str, list[tuple[str, Issue]]]:
        """Reconcile active incidents and bounded catch-up without guessing recoveries."""
        return reconcile_source(self._state, result, targets, self._history_hours())

    async def _send_events(
        self,
        unified_message_origin: str,
        source_name: str,
        events: list[tuple[str, Issue]],
        translations: dict[str, str] | None = None,
    ) -> bool:
        language = normalize_language(self.config.get("display_language", "bilingual"))
        if translations is None:
            translations = await self._translator.translate_issues(
                (issue for _, issue in events),
                bool(self.config.get("enable_ai_translation", True))
                and language != "en-US",
                str(self.config.get("translation_provider_id", "")).strip(),
            )
        try:
            generated_at = self._display_datetime()
            png = await asyncio.to_thread(
                render_alert_card,
                source_name,
                events,
                translations,
                language,
                generated_at.tzinfo,
                str(self.config.get("card_theme", "paper")),
                generated_at,
            )
            chain = MessageChain([Image.fromBytes(png)])
        except Exception:
            logger.exception("Failed to render status alert card; using text fallback.")
            chain = MessageChain(
                [
                    Plain(
                        build_alert_fallback(
                            source_name,
                            events,
                            translations,
                            language,
                        )
                    )
                ]
            )
        try:
            sent = await self.context.send_message(unified_message_origin, chain)
            if not sent:
                logger.warning(
                    "No platform matched status alert target %s.",
                    unified_message_origin,
                )
            return bool(sent)
        except Exception:
            logger.exception(
                "Failed to send status alert to %s.",
                unified_message_origin,
            )
            return False

    def _mark_delivered(
        self,
        target_key: str,
        events: list[tuple[str, Issue]],
    ) -> None:
        delivered = self._state["deliveries"].setdefault(target_key, {})
        for stage, issue in events:
            delivered[issue.key] = delivery_fingerprint(stage, issue)

    def _cleanup_recoveries(
        self,
        source_id: str,
        target_keys: set[str],
        has_configured_groups: bool,
    ) -> None:
        cleanup_resolved(self._state, source_id, target_keys, has_configured_groups)

    async def _run_cycle(self) -> None:
        results = await self._fetch_sources()
        successful_results = [result for result in results if result.success]
        self._prune_disabled_sources({result.spec.source_id for result in results})
        groups = self._configured_groups()
        self._prune_removed_groups(groups)
        targets = self._targets(groups)

        reconciled: list[tuple[SourceResult, dict[str, list[tuple[str, Issue]]]]] = []
        initialized_sources = set(self._state.get("initialized_sources", []))
        notify_existing = bool(
            self.config.get("notify_existing_on_first_startup", True)
        )
        for result in successful_results:
            pending = self._reconcile_source(result, targets)
            if not notify_existing and result.spec.source_id not in initialized_sources:
                for target_key, events in pending.items():
                    self._mark_delivered(target_key, events)
                pending = {}
                logger.info(
                    "Recorded initial status baseline for %s without notification.",
                    result.spec.name,
                )
            initialized_sources.add(result.spec.source_id)
            reconciled.append((result, pending))
        self._state["initialized_sources"] = sorted(initialized_sources)

        language = normalize_language(self.config.get("display_language", "bilingual"))
        changed_issues = [
            issue
            for _, pending in reconciled
            for events in pending.values()
            for _, issue in events
        ]
        translations = await self._translator.translate_issues(
            changed_issues,
            bool(self.config.get("enable_ai_translation", True))
            and language != "en-US",
            str(self.config.get("translation_provider_id", "")).strip(),
        )

        for result, pending in reconciled:
            for target_key, events in pending.items():
                if await self._send_events(
                    targets[target_key],
                    result.spec.name,
                    events,
                    translations,
                ):
                    self._mark_delivered(target_key, events)
            self._cleanup_recoveries(
                result.spec.source_id,
                set(targets),
                bool(groups),
            )
        health_pending: dict[str, list[tuple[str, Issue]]] = {}
        for result in results:
            notices = plan_health_notices(
                self._state, result, targets,
                bool(self.config.get("notify_source_failures", True)),
                self._bounded_int("source_failure_threshold", 3, 1, 100),
                self._bounded_int("source_failure_cooldown_seconds", 3600, 60, 86400),
            )
            for target_key, events in notices.items():
                health_pending.setdefault(target_key, []).extend(events)
        for target_key, events in health_pending.items():
            if await self._send_events(targets[target_key], "Status monitor", events, {}):
                mark_health_delivered(self._state, target_key, events)
        self._last_results = [presentation_result(result, self._state) for result in results]
        await self.put_kv_data(STATE_KEY, self._state)
        if self._translator.dirty:
            await self.put_kv_data(
                TRANSLATION_CACHE_KEY,
                self._translator.dump_cache(),
            )
            self._translator.dirty = False

    @filter.command("厂商状态", alias={"vendor_status"})
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL
    )
    async def vendor_status(self, event: AstrMessageEvent):
        """Query all enabled vendor sources and return a current status image."""
        try:
            results = [presentation_result(result, self._state)
                       for result in await self._fetch_sources()]
            issues = [
                issue
                for result in results
                if result.success
                for issue in result.issues.values()
            ]
            translations = await self._translator.translate_issues(
                issues,
                bool(self.config.get("enable_ai_translation", True))
                and normalize_language(self.config.get("display_language", "bilingual"))
                != "en-US",
                str(self.config.get("translation_provider_id", "")).strip(),
            )
            png = await asyncio.to_thread(
                render_overview,
                results,
                translations,
                normalize_language(self.config.get("display_language", "bilingual")),
                self._display_datetime(),
                str(self.config.get("card_theme", "paper")),
            )
            if self._translator.dirty:
                await self.put_kv_data(
                    TRANSLATION_CACHE_KEY,
                    self._translator.dump_cache(),
                )
                self._translator.dirty = False
            yield event.chain_result([Image.fromBytes(png)])
        except Exception:
            logger.exception("Failed to build on-demand vendor status overview.")
            yield event.plain_result(
                "厂商状态查询失败，请检查 AstrBot 日志和网络配置。"
            )

    def _normalize_group_whitelist(self) -> list[str]:
        """读取并清洗 group_whitelist；非列表或为空返回 []。"""
        return self._configured_groups()

    def _subscription_entries_for_umo(
        self, groups: list[str], umo: str
    ) -> list[str]:
        """Return whitelist entries that resolve to the current unified origin."""
        return [
            entry
            for entry in groups
            if entry == umo or umo in self._targets([entry]).values()
        ]

    async def _set_group_subscription(self, umo: str, action: str) -> str:
        """按确定动作改写当前群 UMO 的订阅状态，并持久化到配置文件。

        action 仅识别 "开" / "关" 两个确切取值；其它任何值一律返回用法，
        不做推测匹配、不修改配置。
        """
        token = action.strip()
        if token not in {"开", "关"}:
            return "用法：/厂商订阅 开｜关"

        groups = self._normalize_group_whitelist()
        matching_entries = self._subscription_entries_for_umo(groups, umo)

        if token == "开":
            if matching_entries:
                return "ℹ️ 当前群组已订阅厂商状态自动推送，无需重复开启。"
            groups.append(umo)
        elif not matching_entries:
            return "ℹ️ 当前群组未订阅厂商状态自动推送，无需关闭。"
        else:
            groups = [entry for entry in groups if entry not in matching_entries]

        # 一次调用完成「内存更新 + 落盘」，重启后保持。
        await self.config.save_config_async({"group_whitelist": groups})

        if token == "开":
            return (
                "✅ 已为当前群组开启厂商状态自动订阅，下一轮轮询后开始推送告警。\n"
                f"当前订阅群组数：{len(groups)}。"
            )
        return (
            "✅ 已为当前群组关闭厂商状态自动订阅，将不再收到主动告警。\n"
            f"剩余订阅群组数：{len(groups)}。"
        )

    @filter.command("厂商订阅", alias={"vendor_subscribe"})
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL
    )
    async def vendor_subscribe(self, event: AstrMessageEvent, action: str = ""):
        """开启/关闭当前群组的厂商状态自动订阅，并持久化到配置文件。

        用法：/厂商订阅 开｜关（参数非法时仅返回用法，不做猜测）。
        """
        if not event.is_admin():
            yield event.plain_result("⚠️ 仅 Bot 管理员可执行此指令。")
            return
        umo = event.unified_msg_origin
        if ":GroupMessage:" not in umo:
            yield event.plain_result("⚠️ 该指令需在群聊中使用。")
            return
        reply = await self._set_group_subscription(umo, action)
        yield event.plain_result(reply)
