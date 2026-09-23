from datetime import datetime, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image

from data.plugins.astrbot_plugin_global_status.renderer import (
    CARD_THEMES,
    ICON_DIR,
    PROJECT_SIGNATURE,
    _event_layout,
    _font,
    _format_event_time,
    _format_overview_date,
    _svg_icon,
    _text_width,
    _wrap_text,
    build_alert_fallback,
    normalize_card_theme,
    render_alert_card,
    render_overview,
)
from data.plugins.astrbot_plugin_global_status.sources import (
    Issue,
    SourceResult,
    SourceSpec,
)


def test_render_alert_card_returns_valid_dynamic_png():
    issue = Issue(
        source_id="openai",
        source_name="OpenAI",
        issue_id="incident_1",
        severity="critical",
        title="Elevated errors affecting API requests",
        affected_services=("API", "ChatGPT", "Responses"),
        detail="We are investigating elevated error rates affecting a subset of requests. "
        * 8,
        updated_at="2026-07-20T01:00:00Z",
        status_url="https://status.openai.com/",
    )

    data = render_alert_card("OpenAI", [("new", issue), ("recovered", issue)])
    image = Image.open(BytesIO(data))

    assert image.format == "PNG"
    assert image.width == 1200
    assert image.height > 500
    assert PROJECT_SIGNATURE == "github.com/Futureppo/astrbot_plugin_global_status"


def test_svg_icon_assets_rasterize_without_native_dependencies():
    expected = {
        "openai",
        "claude",
        "google_vertex_gemini",
        "groq",
        "cohere",
        "moonshot",
        "minimax",
        "xai",
        "deepseek",
        "cursor",
        "cerebras",
        "aws",
        "azure",
        "github",
        "cloudflare",
        "vendor",
        "alert",
        "update",
        "check",
        "service",
        "clock",
        "link",
        "unavailable",
    }

    assert expected <= {path.stem for path in ICON_DIR.glob("*.svg")}
    for name in expected:
        icon = _svg_icon(name, 48, "#34D399")
        assert icon.mode == "RGBA"
        assert icon.size == (48, 48)
        assert icon.getbbox() is not None


def test_event_times_use_the_configured_image_timezone():
    shanghai = ZoneInfo("Asia/Shanghai")
    new_york = ZoneInfo("America/New_York")

    assert _format_event_time("2026-07-20T08:31:10.729Z", shanghai) == (
        "2026-07-20 16:31:10 UTC+08:00"
    )
    assert _format_event_time("Mon, 20 Jul 2026 01:00:00 GMT", shanghai) == (
        "2026-07-20 09:00:00 UTC+08:00"
    )
    assert _format_event_time("2026-07-20T08:31:10+00:00", new_york) == (
        "2026-07-20 04:31:10 UTC-04:00"
    )
    assert _format_event_time("unknown time", shanghai) == "unknown time"


def test_all_card_themes_render_alerts_and_overviews():
    issue = Issue(
        source_id="openai",
        source_name="OpenAI",
        issue_id="theme-preview",
        severity="critical",
        title="Elevated API errors",
        detail="We are investigating elevated errors.",
        updated_at="2026-07-20T08:31:10Z",
        status_url="https://status.openai.com/",
    )
    result = SourceResult(
        SourceSpec(
            "openai",
            "OpenAI",
            "statuspage",
            "https://status.openai.com",
            "https://status.openai.com/",
        ),
        True,
        {issue.issue_id: issue},
    )
    top_bar_colors = set()

    for theme_name in CARD_THEMES:
        alert = Image.open(
            BytesIO(
                render_alert_card(
                    "OpenAI",
                    [("new", issue)],
                    card_theme=theme_name,
                )
            )
        )
        overview = Image.open(BytesIO(render_overview([result], card_theme=theme_name)))

        assert alert.format == "PNG"
        assert overview.format == "PNG"
        top_bar_colors.add(alert.convert("RGB").getpixel((10, 3)))

    assert len(top_bar_colors) == len(CARD_THEMES)
    assert normalize_card_theme("MIDNIGHT") == "midnight"
    assert normalize_card_theme("unknown") == "paper"


def test_bilingual_alert_preserves_english_and_localizes_fallback():
    issue = Issue(
        source_id="openai",
        source_name="OpenAI",
        issue_id="incident_2",
        severity="warning",
        title="Elevated API errors",
        detail="We are investigating.",
        status_url="https://status.openai.com/",
    )
    translations = {
        issue.title: "API 错误率升高",
        issue.detail: "我们正在调查。",
    }

    for language in ("zh-CN", "en-US", "bilingual"):
        data = render_alert_card("OpenAI", [("update", issue)], translations, language)
        image = Image.open(BytesIO(data))
        assert image.format == "PNG"
        assert image.width == 1200

    fallback = build_alert_fallback(
        "OpenAI", [("update", issue)], translations, "bilingual"
    )
    assert "API 错误率升高" in fallback
    assert "Elevated API errors" in fallback


def test_long_custom_vendor_name_is_bounded_in_alert_header():
    source_name = (
        "A Very Long Custom Statuspage Vendor Name / 超长自定义厂商名称用于边界压力测试"
    )
    source_name_lines = _wrap_text(source_name, _font(40, True), 610, 1)
    issue = Issue(
        source_id="custom_extreme_vendor",
        source_name=source_name,
        issue_id="custom-1",
        severity="critical",
        title="Major service disruption",
        status_url="https://status.example.com/",
    )

    data = render_alert_card(source_name, [("new", issue)])
    image = Image.open(BytesIO(data))

    assert image.format == "PNG"
    assert len(source_name_lines) == 1
    assert source_name_lines[0].endswith("…")
    assert _text_width(source_name_lines[0], _font(40, True)) <= 610


def test_official_note_is_never_truncated():
    detail = "Official status update with diagnostic details. " * 80 + "TAIL_MARKER"
    translated = "包含诊断细节的官方状态更新。" * 80 + "末尾标记"
    issue = Issue(
        source_id="cloudflare",
        source_name="Cloudflare",
        issue_id="long-note",
        severity="warning",
        title="Component status degradation",
        detail=detail,
        status_url="https://www.cloudflarestatus.com/",
    )

    layout = _event_layout(
        "new",
        issue,
        {detail: translated},
        "bilingual",
    )
    data = render_alert_card(
        "Cloudflare",
        [("new", issue)],
        {detail: translated},
        "bilingual",
    )
    image = Image.open(BytesIO(data))

    assert "末尾标记" in "".join(layout["detail_lines"])
    assert "TAIL_MARKER" in "".join(layout["detail_original_lines"])
    assert image.height > 1500


def test_render_overview_includes_operational_and_unavailable_rows():
    ok_spec = SourceSpec("ok", "Operational", "statuspage", "url", "url")
    bad_spec = SourceSpec("bad", "Unavailable", "rss", "url", "url")
    results = [
        SourceResult(spec=ok_spec, success=True),
        SourceResult(spec=bad_spec, success=False, error="timeout"),
    ]

    generated_at = datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc)
    data = render_overview(results, generated_at=generated_at)
    image = Image.open(BytesIO(data))

    assert image.format == "PNG"
    assert image.size[0] == 1200
    assert image.size[1] >= 400
    assert (
        _format_overview_date(generated_at, "bilingual") == "2026年07月20日  13:00:00"
    )
    assert _format_overview_date(generated_at, "zh-CN") == "2026年07月20日  13:00:00"
    assert _format_overview_date(generated_at, "en-US") == "JULY 20, 2026  13:00:00"
