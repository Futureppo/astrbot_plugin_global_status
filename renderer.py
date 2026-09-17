"""SVG-backed Pillow status alert and overview card renderer."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from email.utils import parsedate_to_datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from PIL import (
    Image,
    ImageChops,
    ImageColor,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
)

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .sources import Issue, SourceResult
from .translation import normalize_language

WIDTH = 1200
ICON_DIR = Path(__file__).parent / "assets" / "icons"
PROJECT_SIGNATURE = "github.com/Futureppo/astrbot_plugin_global_status"


@dataclass(frozen=True, slots=True)
class CardTheme:
    """Color and geometry tokens for one status card theme."""

    background_top: str
    background_bottom: str
    surface: str
    surface_alt: str
    icon_surface: str
    text: str
    text_soft: str
    muted: str
    border: str
    panel_highlight: str
    chrome: str
    divider: str
    link: str
    footer: str
    critical_surface: str
    warning_surface: str
    unavailable_surface: str
    severity_colors: dict[str, str]
    panel_radius: int
    badge_radius: int
    table_radius: int
    liquid_glass: bool = False


CARD_THEMES: dict[str, CardTheme] = {
    "paper": CardTheme(
        background_top="#ECEAE5",
        background_bottom="#ECEAE5",
        surface="#FAF9F6",
        surface_alt="#F5F3EE",
        icon_surface="#FAF9F6",
        text="#19222C",
        text_soft="#36414C",
        muted="#727B84",
        border="#D4D1CA",
        panel_highlight="#FFFFFF",
        chrome="#222B34",
        divider="#E6E3DC",
        link="#315F7C",
        footer="#858B91",
        critical_surface="#FBF0F1",
        warning_surface="#FCF6E9",
        unavailable_surface="#F1F2F2",
        severity_colors={
            "critical": "#BD3342",
            "warning": "#A86B0A",
            "maintenance": "#31699D",
            "info": "#31699D",
            "operational": "#287256",
            "unavailable": "#707982",
        },
        panel_radius=16,
        badge_radius=16,
        table_radius=14,
    ),
    "midnight": CardTheme(
        background_top="#0B1020",
        background_bottom="#11182C",
        surface="#151D31",
        surface_alt="#11192B",
        icon_surface="#F8FAFC",
        text="#F3F6FC",
        text_soft="#D7E1F1",
        muted="#91A0B8",
        border="#2C3955",
        panel_highlight="#263450",
        chrome="#8B72FF",
        divider="#25334D",
        link="#73C7FF",
        footer="#72829C",
        critical_surface="#2A1722",
        warning_surface="#2B2418",
        unavailable_surface="#1A2230",
        severity_colors={
            "critical": "#FF6B7A",
            "warning": "#F5B942",
            "maintenance": "#75A7FF",
            "info": "#75A7FF",
            "operational": "#5FD0A5",
            "unavailable": "#94A3B8",
        },
        panel_radius=12,
        badge_radius=12,
        table_radius=12,
    ),
    "porcelain": CardTheme(
        background_top="#E8F1EC",
        background_bottom="#F5F0E7",
        surface="#FBFCF8",
        surface_alt="#F0F6F1",
        icon_surface="#FBFCF8",
        text="#17322E",
        text_soft="#385B54",
        muted="#6D817C",
        border="#C9D9D2",
        panel_highlight="#FFFFFF",
        chrome="#2F7468",
        divider="#D7E3DE",
        link="#236E75",
        footer="#72847F",
        critical_surface="#FAEDEF",
        warning_surface="#F9F2DF",
        unavailable_surface="#EEF2F0",
        severity_colors={
            "critical": "#B83A4B",
            "warning": "#A56713",
            "maintenance": "#327B7A",
            "info": "#327B7A",
            "operational": "#26715D",
            "unavailable": "#708078",
        },
        panel_radius=24,
        badge_radius=24,
        table_radius=22,
    ),
    "terminal": CardTheme(
        background_top="#07110C",
        background_bottom="#020805",
        surface="#0A1710",
        surface_alt="#0D1D14",
        icon_surface="#F6FFF8",
        text="#D9FFE5",
        text_soft="#A8DDB7",
        muted="#6D9B79",
        border="#1D4A2C",
        panel_highlight="#123520",
        chrome="#39FF88",
        divider="#163C24",
        link="#58E6FF",
        footer="#4D8A5E",
        critical_surface="#251017",
        warning_surface="#241F0B",
        unavailable_surface="#101A15",
        severity_colors={
            "critical": "#FF4D6D",
            "warning": "#FFD166",
            "maintenance": "#00E5FF",
            "info": "#00E5FF",
            "operational": "#39FF88",
            "unavailable": "#91A39A",
        },
        panel_radius=2,
        badge_radius=2,
        table_radius=2,
    ),
    "liquid_glass": CardTheme(
        background_top="#E9E9E7",
        background_bottom="#DCDDDC",
        surface="#FFFFFF64",
        surface_alt="#F4F4F250",
        icon_surface="#FAFAF8",
        text="#18191B",
        text_soft="#2E3135",
        muted="#565A60",
        border="#C8CBCD",
        panel_highlight="#FFFFFF",
        chrome="#242629",
        divider="#C9CCCE",
        link="#0068D9",
        footer="#777B80",
        critical_surface="#FFE8EB82",
        warning_surface="#FFF3DD82",
        unavailable_surface="#E9EBEC5A",
        severity_colors={
            "critical": "#C82035",
            "warning": "#9B6100",
            "maintenance": "#0068D9",
            "info": "#0068D9",
            "operational": "#18794E",
            "unavailable": "#6F7479",
        },
        panel_radius=28,
        badge_radius=22,
        table_radius=26,
        liquid_glass=True,
    ),
}

DEFAULT_CARD_THEME = "paper"


def normalize_card_theme(value: object) -> str:
    """Return a supported card theme identifier."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in CARD_THEMES else DEFAULT_CARD_THEME


VENDOR_COLORS = {
    "openai": "#10A37F",
    "claude": "#D97757",
    "google_vertex_gemini": "#7C8CF8",
    "groq": "#F55036",
    "cohere": "#2D8C78",
    "moonshot": "#111111",
    "minimax": "#E2167E",
    "xai": "#111111",
    "deepseek": "#4D6BFE",
    "cursor": "#111111",
    "cerebras": "#F15A29",
    "aws": "#E99024",
    "azure": "#1689D4",
    "github": "#64748B",
    "cloudflare": "#F48120",
}

STAGE_LABELS = {
    "zh-CN": {
        "new": "新异常",
        "current": "当前异常",
        "update": "状态更新",
        "recovered": "已恢复",
    },
    "en-US": {
        "new": "New incident",
        "current": "Active incident",
        "update": "Status update",
        "recovered": "Recovered",
    },
    "bilingual": {
        "new": "新异常  /  New incident",
        "current": "当前异常  /  Active incident",
        "update": "状态更新  /  Status update",
        "recovered": "已恢复  /  Recovered",
    },
}

STATUS_LABELS = {
    "zh-CN": {
        "critical": "严重异常",
        "warning": "服务降级",
        "maintenance": "计划维护",
        "info": "状态提醒",
        "operational": "运行正常",
        "unavailable": "数据不可用",
    },
    "en-US": {
        "critical": "Critical",
        "warning": "Degraded",
        "maintenance": "Maintenance",
        "info": "Notice",
        "operational": "Operational",
        "unavailable": "Unavailable",
    },
    "bilingual": {
        "critical": "严重异常  /  Critical",
        "warning": "服务降级  /  Degraded",
        "maintenance": "计划维护  /  Maintenance",
        "info": "状态提醒  /  Notice",
        "operational": "运行正常  /  Operational",
        "unavailable": "数据不可用  /  Unavailable",
    },
}


def _font_candidates(bold: bool) -> list[str]:
    data_font = Path(get_astrbot_data_path()) / "font.ttf"
    if bold:
        return [
            str(data_font),
            r"C:\Windows\Fonts\msyhbd.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "msyhbd.ttc",
            "NotoSansCJK-Bold.ttc",
            "DejaVuSans-Bold.ttf",
        ]
    return [
        str(data_font),
        r"C:\Windows\Fonts\msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "msyh.ttc",
        "NotoSansCJK-Regular.ttc",
        "DejaVuSans.ttf",
    ]


@lru_cache(maxsize=48)
def _font(
    size: int, bold: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _font_candidates(bold):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_width(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> float:
    try:
        return font.getlength(text)
    except AttributeError:
        left, _, right, _ = font.getbbox(text)
        return float(right - left)


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Wrap mixed Chinese and English text to a bounded number of lines.

    Args:
        text: Source text.
        font: Pillow font used for width measurement.
        max_width: Maximum rendered line width.
        max_lines: Maximum returned line count.

    Returns:
        Wrapped lines with an ellipsis when content is truncated.
    """
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    lines: list[str] = []
    current = ""
    cursor = 0
    while cursor < len(normalized) and len(lines) < max_lines:
        char = normalized[cursor]
        candidate = current + char
        if not current or _text_width(candidate, font) <= max_width:
            current = candidate
            cursor += 1
            continue
        break_at = current.rfind(" ")
        if break_at > 0:
            lines.append(current[:break_at].rstrip())
            current = current[break_at + 1 :]
        else:
            lines.append(current.rstrip())
            current = ""
    if current and len(lines) < max_lines:
        lines.append(current.rstrip())
    if cursor < len(normalized) and lines:
        suffix = "…"
        last = lines[-1]
        while last and _text_width(last + suffix, font) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + suffix
    return lines


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    position: tuple[int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    line_height: int,
) -> int:
    x, y = position
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height
    return y


def _parse_color(value: str | None, current_color: str) -> str | None:
    if not value or value == "none":
        return None
    return current_color if value == "currentColor" else value


_PATH_TOKEN_PATTERN = re.compile(
    r"[AaCcHhLlMmSsVvZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?"
)


def _arc_points(
    start: tuple[float, float],
    radius_x: float,
    radius_y: float,
    rotation: float,
    large_arc: bool,
    sweep: bool,
    end: tuple[float, float],
) -> list[tuple[float, float]]:
    """Approximate one SVG elliptical arc with short line segments.

    Args:
        start: Arc start point.
        radius_x: Horizontal ellipse radius.
        radius_y: Vertical ellipse radius.
        rotation: Ellipse-axis rotation in degrees.
        large_arc: Whether to select the larger arc.
        sweep: Whether to use the positive-angle direction.
        end: Arc end point.

    Returns:
        Sampled points excluding ``start`` and including ``end``.
    """
    x1, y1 = start
    x2, y2 = end
    radius_x = abs(radius_x)
    radius_y = abs(radius_y)
    if radius_x == 0 or radius_y == 0 or start == end:
        return [end]

    angle = math.radians(rotation % 360)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    half_x = (x1 - x2) / 2
    half_y = (y1 - y2) / 2
    transformed_x = cosine * half_x + sine * half_y
    transformed_y = -sine * half_x + cosine * half_y

    scale = transformed_x**2 / radius_x**2 + transformed_y**2 / radius_y**2
    if scale > 1:
        multiplier = math.sqrt(scale)
        radius_x *= multiplier
        radius_y *= multiplier

    numerator = max(
        0.0,
        radius_x**2 * radius_y**2
        - radius_x**2 * transformed_y**2
        - radius_y**2 * transformed_x**2,
    )
    denominator = radius_x**2 * transformed_y**2 + radius_y**2 * transformed_x**2
    coefficient = 0.0
    if denominator:
        coefficient = math.sqrt(numerator / denominator)
        if large_arc == sweep:
            coefficient = -coefficient
    center_x_transformed = coefficient * radius_x * transformed_y / radius_y
    center_y_transformed = -coefficient * radius_y * transformed_x / radius_x
    center_x = (
        cosine * center_x_transformed - sine * center_y_transformed + (x1 + x2) / 2
    )
    center_y = (
        sine * center_x_transformed + cosine * center_y_transformed + (y1 + y2) / 2
    )

    def vector_angle(first: tuple[float, float], second: tuple[float, float]) -> float:
        dot = first[0] * second[0] + first[1] * second[1]
        length = math.hypot(*first) * math.hypot(*second)
        value = max(-1.0, min(1.0, dot / length)) if length else 1.0
        result = math.acos(value)
        if first[0] * second[1] - first[1] * second[0] < 0:
            result = -result
        return result

    start_vector = (
        (transformed_x - center_x_transformed) / radius_x,
        (transformed_y - center_y_transformed) / radius_y,
    )
    end_vector = (
        (-transformed_x - center_x_transformed) / radius_x,
        (-transformed_y - center_y_transformed) / radius_y,
    )
    start_angle = vector_angle((1, 0), start_vector)
    angle_delta = vector_angle(start_vector, end_vector)
    if not sweep and angle_delta > 0:
        angle_delta -= math.tau
    elif sweep and angle_delta < 0:
        angle_delta += math.tau

    steps = max(4, math.ceil(abs(angle_delta) / (math.pi / 18)))
    points: list[tuple[float, float]] = []
    for step in range(1, steps + 1):
        position = start_angle + angle_delta * step / steps
        ellipse_x = radius_x * math.cos(position)
        ellipse_y = radius_y * math.sin(position)
        points.append(
            (
                center_x + cosine * ellipse_x - sine * ellipse_y,
                center_y + sine * ellipse_x + cosine * ellipse_y,
            )
        )
    points[-1] = end
    return points


def _svg_path_subpaths(data: str) -> list[list[tuple[float, float]]]:
    """Convert supported SVG path commands into sampled polygon subpaths.

    Args:
        data: SVG path ``d`` attribute.

    Returns:
        Source-coordinate point lists for each subpath.

    Raises:
        ValueError: If malformed or unsupported path data is encountered.
    """
    tokens = _PATH_TOKEN_PATTERN.findall(data.replace(",", " "))
    index = 0
    command = ""
    current = (0.0, 0.0)
    start = (0.0, 0.0)
    last_cubic_control: tuple[float, float] | None = None
    subpath: list[tuple[float, float]] = []
    subpaths: list[list[tuple[float, float]]] = []

    def number() -> float:
        nonlocal index
        if index >= len(tokens) or tokens[index].isalpha():
            raise ValueError("Malformed SVG path data")
        value = float(tokens[index])
        index += 1
        return value

    def arc_flag() -> bool:
        nonlocal index
        if index >= len(tokens) or tokens[index].isalpha():
            raise ValueError("Malformed SVG arc flag")
        token = tokens[index]
        if not token or token[0] not in {"0", "1"}:
            raise ValueError("SVG arc flags must be zero or one")
        value = token[0] == "1"
        if len(token) == 1:
            index += 1
        else:
            # Minified SVG permits adjacent flags and coordinates, such as
            # ``00-.856``. Keep the remainder available to the next reader.
            tokens[index] = token[1:]
        return value

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command in {"Z", "z"}:
                if subpath and subpath[-1] != start:
                    subpath.append(start)
                current = start
                last_cubic_control = None
                command = ""
                continue
        if not command:
            if index < len(tokens) and tokens[index].isalpha():
                continue
            raise ValueError("SVG path data is missing a command")

        relative = command.islower()
        operation = command.upper()
        if operation == "M":
            x, y = number(), number()
            if relative:
                x += current[0]
                y += current[1]
            if subpath:
                subpaths.append(subpath)
            current = (x, y)
            start = current
            subpath = [current]
            last_cubic_control = None
            command = "l" if relative else "L"
        elif operation == "L":
            x, y = number(), number()
            if relative:
                x += current[0]
                y += current[1]
            current = (x, y)
            subpath.append(current)
            last_cubic_control = None
        elif operation == "H":
            x = number() + (current[0] if relative else 0)
            current = (x, current[1])
            subpath.append(current)
            last_cubic_control = None
        elif operation == "V":
            y = number() + (current[1] if relative else 0)
            current = (current[0], y)
            subpath.append(current)
            last_cubic_control = None
        elif operation == "C":
            control_one = (number(), number())
            control_two = (number(), number())
            end = (number(), number())
            if relative:
                control_one = (
                    control_one[0] + current[0],
                    control_one[1] + current[1],
                )
                control_two = (
                    control_two[0] + current[0],
                    control_two[1] + current[1],
                )
                end = (end[0] + current[0], end[1] + current[1])
            origin = current
            for step in range(1, 13):
                position = step / 12
                inverse = 1 - position
                subpath.append(
                    (
                        inverse**3 * origin[0]
                        + 3 * inverse**2 * position * control_one[0]
                        + 3 * inverse * position**2 * control_two[0]
                        + position**3 * end[0],
                        inverse**3 * origin[1]
                        + 3 * inverse**2 * position * control_one[1]
                        + 3 * inverse * position**2 * control_two[1]
                        + position**3 * end[1],
                    )
                )
            current = end
            last_cubic_control = control_two
        elif operation == "S":
            control_two = (number(), number())
            end = (number(), number())
            if relative:
                control_two = (
                    control_two[0] + current[0],
                    control_two[1] + current[1],
                )
                end = (end[0] + current[0], end[1] + current[1])
            control_one = (
                current
                if last_cubic_control is None
                else (
                    2 * current[0] - last_cubic_control[0],
                    2 * current[1] - last_cubic_control[1],
                )
            )
            origin = current
            for step in range(1, 13):
                position = step / 12
                inverse = 1 - position
                subpath.append(
                    (
                        inverse**3 * origin[0]
                        + 3 * inverse**2 * position * control_one[0]
                        + 3 * inverse * position**2 * control_two[0]
                        + position**3 * end[0],
                        inverse**3 * origin[1]
                        + 3 * inverse**2 * position * control_one[1]
                        + 3 * inverse * position**2 * control_two[1]
                        + position**3 * end[1],
                    )
                )
            current = end
            last_cubic_control = control_two
        elif operation == "A":
            radius_x, radius_y, rotation = number(), number(), number()
            large_arc, sweep = arc_flag(), arc_flag()
            end = (number(), number())
            if relative:
                end = (end[0] + current[0], end[1] + current[1])
            subpath.extend(
                _arc_points(
                    current,
                    radius_x,
                    radius_y,
                    rotation,
                    large_arc,
                    sweep,
                    end,
                )
            )
            current = end
            last_cubic_control = None
        else:
            raise ValueError(f"Unsupported SVG path command: {command}")
    if subpath:
        subpaths.append(subpath)
    return subpaths


@lru_cache(maxsize=160)
def _svg_icon(name: str, size: int, current_color: str = "#FFFFFF") -> Image.Image:
    """Rasterize a controlled local SVG asset without native dependencies.

    The local icon set uses geometric primitives and sampled SVG paths. This keeps
    plugin installation portable while retaining SVG as the source of truth.

    Args:
        name: SVG asset stem.
        size: Output width and height in pixels.
        current_color: Replacement for SVG's ``currentColor`` value.

    Returns:
        Transparent RGBA icon image.

    Raises:
        ValueError: If the SVG viewBox or primitive data is invalid.
        OSError: If the asset cannot be read.
    """
    path = ICON_DIR / f"{name}.svg"
    root = ElementTree.fromstring(path.read_text(encoding="utf-8"))
    view_box = [
        float(value) for value in root.attrib.get("viewBox", "0 0 64 64").split()
    ]
    if len(view_box) != 4 or view_box[2] <= 0 or view_box[3] <= 0:
        raise ValueError(f"Invalid SVG viewBox in {path.name}")
    left, top, width, height = view_box
    scale = min(size / width, size / height)
    offset_x = (size - width * scale) / 2
    offset_y = (size - height * scale) / 2
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    gradients: dict[str, str] = {}
    for candidate in root.iter():
        if candidate.tag.rsplit("}", 1)[-1] != "linearGradient":
            continue
        gradient_id = candidate.attrib.get("id", "")
        for stop in candidate:
            if stop.tag.rsplit("}", 1)[-1] != "stop":
                continue
            color = stop.attrib.get("stop-color", "#000000")
            opacity = float(stop.attrib.get("stop-opacity", "1"))
            if color.startswith("#") and opacity < 1:
                raw = color.removeprefix("#")
                if len(raw) == 3:
                    raw = "".join(character * 2 for character in raw)
                color = f"#{raw[:6]}{round(opacity * 255):02X}"
            gradients[gradient_id] = color
            break

    def point(x: str | float, y: str | float) -> tuple[int, int]:
        return (
            round((float(x) - left) * scale + offset_x),
            round((float(y) - top) * scale + offset_y),
        )

    def render(element: ElementTree.Element, inherited: dict[str, str]) -> None:
        attrs = dict(inherited)
        attrs.update(element.attrib)
        tag = element.tag.rsplit("}", 1)[-1]
        fill_value = attrs.get("fill", "#000000")
        gradient_match = re.fullmatch(r"url\(#(.+)\)", fill_value)
        fill = (
            gradients.get(gradient_match.group(1), current_color)
            if gradient_match
            else _parse_color(fill_value, current_color)
        )
        stroke = _parse_color(attrs.get("stroke"), current_color)
        stroke_width = max(
            1,
            round(float(attrs.get("stroke-width", "1")) * scale),
        )
        if tag == "rect":
            x1, y1 = point(attrs.get("x", 0), attrs.get("y", 0))
            x2, y2 = point(
                float(attrs.get("x", 0)) + float(attrs.get("width", 0)),
                float(attrs.get("y", 0)) + float(attrs.get("height", 0)),
            )
            radius = round(float(attrs.get("rx", 0)) * scale)
            draw.rounded_rectangle(
                (x1, y1, x2, y2),
                radius=radius,
                fill=fill,
                outline=stroke,
                width=stroke_width,
            )
        elif tag in {"circle", "ellipse"}:
            cx = float(attrs.get("cx", 0))
            cy = float(attrs.get("cy", 0))
            rx = float(attrs.get("r", attrs.get("rx", 0)))
            ry = float(attrs.get("r", attrs.get("ry", 0)))
            x1, y1 = point(cx - rx, cy - ry)
            x2, y2 = point(cx + rx, cy + ry)
            draw.ellipse(
                (x1, y1, x2, y2),
                fill=fill,
                outline=stroke,
                width=stroke_width,
            )
        elif tag == "line":
            draw.line(
                (
                    *point(attrs.get("x1", 0), attrs.get("y1", 0)),
                    *point(attrs.get("x2", 0), attrs.get("y2", 0)),
                ),
                fill=stroke or fill,
                width=stroke_width,
            )
        elif tag in {"polyline", "polygon"}:
            values = [
                float(value)
                for value in attrs.get("points", "").replace(",", " ").split()
            ]
            points = [
                point(values[index], values[index + 1])
                for index in range(0, len(values), 2)
            ]
            if tag == "polygon" and fill:
                draw.polygon(points, fill=fill)
            if stroke and len(points) > 1:
                line_points = points + ([points[0]] if tag == "polygon" else [])
                draw.line(line_points, fill=stroke, width=stroke_width, joint="curve")
        elif tag == "path":
            subpaths = _svg_path_subpaths(attrs.get("d", ""))
            sampled = [
                [point(source_x, source_y) for source_x, source_y in subpath]
                for subpath in subpaths
                if len(subpath) >= 2
            ]
            if fill and sampled:
                path_mask = Image.new("1", (size, size), 0)
                even_odd = attrs.get("fill-rule", "").lower() == "evenodd"
                for polygon in sampled:
                    if len(polygon) < 3:
                        continue
                    if even_odd:
                        subpath_mask = Image.new("1", (size, size), 0)
                        ImageDraw.Draw(subpath_mask).polygon(polygon, fill=1)
                        path_mask = ImageChops.logical_xor(path_mask, subpath_mask)
                    else:
                        ImageDraw.Draw(path_mask).polygon(polygon, fill=1)
                overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                ImageDraw.Draw(overlay).bitmap((0, 0), path_mask, fill=fill)
                image.alpha_composite(overlay)
            if stroke:
                for sampled_path in sampled:
                    draw.line(
                        sampled_path,
                        fill=stroke,
                        width=stroke_width,
                        joint="curve",
                    )
        for child in element:
            render(child, attrs)

    render(root, {})
    return image


def _paste_icon(
    image: Image.Image,
    name: str,
    position: tuple[int, int],
    size: int,
    color: str = "#FFFFFF",
) -> None:
    icon = _svg_icon(name, size, color)
    image.alpha_composite(icon, position)


def _new_canvas(height: int, theme: CardTheme) -> Image.Image:
    image = Image.new("RGBA", (WIDTH, height), theme.background_top)
    draw = ImageDraw.Draw(image)
    if theme.background_top != theme.background_bottom:
        top = ImageColor.getrgb(theme.background_top)
        bottom = ImageColor.getrgb(theme.background_bottom)
        denominator = max(1, height - 1)
        for y in range(height):
            position = y / denominator
            color = tuple(
                round(start + (end - start) * position)
                for start, end in zip(top, bottom, strict=True)
            )
            draw.line((0, y, WIDTH, y), fill=color)
    if theme.liquid_glass:
        _paint_glass_backdrop(image, height)
    draw.rectangle((0, 0, WIDTH, 8), fill=theme.chrome)
    draw.line((52, 24, WIDTH - 52, 24), fill=theme.border, width=1)
    return image


def _paint_glass_backdrop(image: Image.Image, height: int) -> None:
    """Paint soft ambient light pools so the frosted glass has tone to refract.

    Kept low-saturation and gentle: the goal is a calm tonal gradient the glass
    blur can reveal as a distinct material, not colorful decoration.
    """
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    # 柔和彩色光场：中等饱和的粉彩色，重模糊后成平滑渐变，供玻璃折射
    blobs = [
        (120, int(height * 0.08), 360, (140, 180, 235, 120)),  # 淡蓝（左上）
        (1080, int(height * 0.15), 340, (200, 165, 225, 110)), # 淡紫（右上）
        (600, int(height * 0.48), 400, (165, 205, 200, 95)),   # 淡薄荷（中）
        (180, int(height * 0.88), 360, (235, 180, 175, 110)),  # 淡桃（左下）
        (1020, int(height * 0.82), 340, (180, 200, 235, 100)), # 淡蓝紫（右下）
        (620, int(height * 0.92), 300, (225, 200, 165, 90)),   # 淡金（底中）
    ]
    for cx, cy, radius, color in blobs:
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius), fill=color
        )
    layer = layer.filter(ImageFilter.GaussianBlur(65))
    image.alpha_composite(layer)


def _fill_rectangle(
    image: Image.Image,
    bounds: tuple[int, int, int, int],
    fill: str,
) -> None:
    """Draw an opaque or alpha-composited rectangle onto an RGBA image.

    Args:
        image: Target RGBA image.
        bounds: Rectangle coordinates in ``(left, top, right, bottom)`` order.
        fill: Pillow-compatible color string, optionally including alpha.
    """
    color = ImageColor.getcolor(fill, "RGBA")
    if color[3] == 255:
        ImageDraw.Draw(image).rectangle(bounds, fill=color)
        return
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle(bounds, fill=color)
    image.alpha_composite(overlay)


def _draw_panel(
    image: Image.Image,
    bounds: tuple[int, int, int, int],
    theme: CardTheme,
    fill: str | None = None,
    radius: int | None = None,
) -> None:
    x1, y1, x2, y2 = bounds
    panel_radius = theme.panel_radius if radius is None else radius
    panel_fill = fill or theme.surface
    fill_color = ImageColor.getcolor(panel_fill, "RGBA")
    if theme.liquid_glass:
        _draw_glass_panel(image, bounds, panel_radius, fill_color, theme)
    elif fill_color[3] < 255:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(
            bounds,
            radius=panel_radius,
            fill=fill_color,
            outline=ImageColor.getcolor(theme.border, "RGBA"),
            width=1,
        )
        image.alpha_composite(overlay)
    else:
        ImageDraw.Draw(image).rounded_rectangle(
            bounds,
            radius=panel_radius,
            fill=fill_color,
            outline=theme.border,
            width=1,
        )
    draw = ImageDraw.Draw(image)
    draw.line(
        (x1 + panel_radius, y1 + 1, x2 - panel_radius, y1 + 1),
        fill=theme.panel_highlight,
        width=1,
    )


def _draw_glass_panel(
    image: Image.Image,
    bounds: tuple[int, int, int, int],
    panel_radius: int,
    fill_color: tuple[int, int, int, int],
    theme: CardTheme,
) -> None:
    """Render a frosted-glass panel that reads as a physical sheet of glass.

    The glass effect comes from four material cues, not decoration:
    a softly blurred + saturated backdrop seen through a translucent tint,
    a bright specular highlight where overhead light reflects off the surface,
    rim lighting on the glass edge, and a grounding shadow.
    """
    x1, y1, x2, y2 = bounds
    pw, ph = x2 - x1, y2 - y1
    if pw <= 0 or ph <= 0:
        return

    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, pw - 1, ph - 1), radius=panel_radius, fill=255
    )

    # 1. 投影：玻璃浮在背景之上，下方柔和阴影落地
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (x1 + 2, y1 + 10, x2 + 2, y2 + 10),
        radius=panel_radius,
        fill=(30, 34, 40, 60),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    image.alpha_composite(shadow)

    # 2. 透过玻璃看到的背景：裁剪 → 模糊 → 轻微提饱和（磨砂折射）
    bg_blurred = image.crop(bounds).copy().filter(ImageFilter.GaussianBlur(16))
    bg_blurred = ImageEnhance.Color(bg_blurred).enhance(1.15)
    # 3. 半透明玻璃本色：压低白色占比，让柔化的背景色透出（玻璃感关键）
    tint_alpha = min(fill_color[3], 112)
    tint = Image.new("RGBA", (pw, ph), (*fill_color[:3], tint_alpha))
    glass = Image.alpha_composite(bg_blurred, tint)
    image.paste(glass, (x1, y1), mask)

    # 4. 镜面高光：顶部一条明亮的反射光带，玻璃质感的核心
    spec = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    spec_draw = ImageDraw.Draw(spec)
    highlight_h = min(max(ph // 3, 40), 110)
    for row in range(highlight_h):
        t = row / highlight_h
        alpha = int(85 * (1.0 - t) ** 1.8)
        spec_draw.line((0, row, pw, row), fill=(255, 255, 255, alpha))
    spec.putalpha(ImageChops.darker(spec.getchannel("A"), mask))
    image.alpha_composite(spec, (x1, y1))

    # 5. 边缘光：上/左亮边迎光，下/右暗边背光，勾勒玻璃厚度
    edge = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    edge_draw = ImageDraw.Draw(edge)
    edge_draw.rounded_rectangle(
        (1, 1, pw - 2, ph - 2),
        radius=max(panel_radius - 1, 1),
        outline=(255, 255, 255, 150),
        width=1,
    )
    edge_draw.rounded_rectangle(
        (2, 2, pw - 3, ph - 3),
        radius=max(panel_radius - 2, 1),
        outline=(40, 44, 50, 30),
        width=1,
    )
    edge.putalpha(ImageChops.darker(edge.getchannel("A"), mask))
    image.alpha_composite(edge, (x1, y1))

    # 6. 外描边
    border_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(border_overlay).rounded_rectangle(
        bounds,
        radius=panel_radius,
        outline=ImageColor.getcolor(theme.border, "RGBA"),
        width=1,
    )
    image.alpha_composite(border_overlay)


def _draw_project_footer(image: Image.Image, y: int, theme: CardTheme) -> None:
    """Draw the centered GitHub repository link in the image footer."""
    draw = ImageDraw.Draw(image)
    font = _font(17)
    icon_size = 18
    gap = 10
    link_width = _text_width(PROJECT_SIGNATURE, font)
    footer_width = icon_size + gap + link_width
    footer_x = (WIDTH - footer_width) / 2
    _paste_icon(
        image,
        "github",
        (round(footer_x), y + 1),
        icon_size,
        theme.footer,
    )
    draw.text(
        (footer_x + icon_size + gap, y - 1),
        PROJECT_SIGNATURE,
        font=font,
        fill=theme.footer,
    )


def _format_overview_date(value: datetime, language: str) -> str:
    """Format the overview heading timestamp without using system locales."""
    if language == "en-US":
        months = (
            "JANUARY",
            "FEBRUARY",
            "MARCH",
            "APRIL",
            "MAY",
            "JUNE",
            "JULY",
            "AUGUST",
            "SEPTEMBER",
            "OCTOBER",
            "NOVEMBER",
            "DECEMBER",
        )
        return (
            f"{months[value.month - 1]} {value.day:02d}, {value.year}  {value:%H:%M:%S}"
        )
    return f"{value.year}年{value.month:02d}月{value.day:02d}日  {value:%H:%M:%S}"


def _format_event_time(value: str, display_timezone: tzinfo | None) -> str:
    """Convert an official event timestamp to the image display timezone."""
    original = " ".join(str(value or "").split())
    if not original:
        return ""
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(
            f"{original[:-1]}+00:00" if original.endswith(("Z", "z")) else original
        )
    except ValueError:
        try:
            parsed = parsedate_to_datetime(original)
        except (TypeError, ValueError, OverflowError):
            return original
    if parsed.tzinfo is None:
        return original
    target_timezone = display_timezone or datetime.now().astimezone().tzinfo
    localized = parsed.astimezone(target_timezone)
    return _format_local_datetime(localized)


def _format_local_datetime(value: datetime) -> str:
    """Format an aware local datetime with a readable UTC offset."""
    offset = value.strftime("%z")
    offset_label = f"UTC{offset[:3]}:{offset[3:]}" if offset else ""
    return f"{value:%Y-%m-%d %H:%M:%S} {offset_label}".rstrip()


def _localized_pair(
    text: str,
    translations: dict[str, str],
    language: str,
) -> tuple[str, str]:
    original = " ".join(str(text or "").split())
    translated = translations.get(original, "")
    if language == "en-US":
        return original, ""
    if language == "zh-CN":
        return translated or original, ""
    if translated and translated != original:
        return translated, original
    return original, ""


def _stage_icon(stage: str) -> str:
    if stage == "recovered":
        return "check"
    if stage == "update":
        return "update"
    return "alert"


def _status_icon(severity: str) -> str:
    if severity == "operational":
        return "check"
    if severity == "unavailable":
        return "unavailable"
    if severity in {"info", "maintenance"}:
        return "update"
    return "alert"


def _vendor_icon(source_id: str) -> str:
    return source_id if (ICON_DIR / f"{source_id}.svg").is_file() else "vendor"


def _event_layout(
    stage: str,
    issue: Issue,
    translations: dict[str, str],
    language: str,
) -> dict[str, object]:
    title, title_original = _localized_pair(issue.title, translations, language)
    title_lines = _wrap_text(title, _font(36, True), 888, 3)
    title_original_lines = _wrap_text(title_original, _font(20), 888, 2)

    services = list(issue.affected_services[:12])
    localized_services = [
        _localized_pair(service, translations, language)[0] for service in services
    ]
    service_text = "、".join(localized_services)
    service_original = " · ".join(services) if language == "bilingual" else ""
    if len(issue.affected_services) > 12:
        service_text += f" 等 {len(issue.affected_services)} 项"
        service_original += f" · {len(issue.affected_services)} services total"
    if service_original == service_text:
        service_original = ""
    service_lines = _wrap_text(service_text, _font(23), 780, 3)
    service_original_lines = _wrap_text(service_original, _font(18), 780, 2)

    detail, detail_original = _localized_pair(issue.detail, translations, language)
    detail_lines = _wrap_text(detail, _font(23), 780, max(1, len(detail)))
    detail_original_lines = _wrap_text(
        detail_original,
        _font(18),
        780,
        max(1, len(detail_original)),
    )

    height = 82 + len(title_lines) * 47 + len(title_original_lines) * 27
    if service_lines:
        service_height = max(
            43 if language == "bilingual" else 28,
            len(service_lines) * 32 + len(service_original_lines) * 25,
        )
        height += 40 + service_height
    if detail_lines:
        detail_height = max(
            43 if language == "bilingual" else 28,
            len(detail_lines) * 32 + len(detail_original_lines) * 25,
        )
        height += 40 + detail_height
    height += 68
    return {
        "stage": stage,
        "issue": issue,
        "title_lines": title_lines,
        "title_original_lines": title_original_lines,
        "service_lines": service_lines,
        "service_original_lines": service_original_lines,
        "detail_lines": detail_lines,
        "detail_original_lines": detail_original_lines,
        "height": max(height, 228),
    }


def render_alert_card(
    source_name: str,
    events: list[tuple[str, Issue]],
    translations: dict[str, str] | None = None,
    language: str = "bilingual",
    display_timezone: tzinfo | None = None,
    card_theme: str = DEFAULT_CARD_THEME,
    generated_at: datetime | None = None,
) -> bytes:
    """Render one vendor's changed issues into a polished bilingual PNG card.

    Args:
        source_name: Human-readable vendor name.
        events: Stage and issue pairs for this notification.
        translations: Original official strings mapped to Chinese translations.
        language: ``zh-CN``, ``en-US``, or ``bilingual``.
        display_timezone: Timezone used for official event timestamps.
        card_theme: Configured visual theme identifier.
        generated_at: Timezone-aware card generation time.

    Returns:
        Encoded PNG bytes.

    Raises:
        ValueError: If no events were provided.
    """
    if not events:
        raise ValueError("At least one alert event is required")
    language = normalize_language(language)
    translations = translations or {}
    theme = CARD_THEMES[normalize_card_theme(card_theme)]
    if generated_at is None:
        generated_at = (
            datetime.now(display_timezone)
            if display_timezone is not None
            else datetime.now().astimezone()
        )
    elif generated_at.tzinfo is None:
        generated_at = generated_at.replace(
            tzinfo=display_timezone or datetime.now().astimezone().tzinfo
        )
    elif display_timezone is not None:
        generated_at = generated_at.astimezone(display_timezone)
    layouts = [
        _event_layout(stage, issue, translations, language) for stage, issue in events
    ]
    height = 164 + sum(int(layout["height"]) + 20 for layout in layouts) + 68
    image = _new_canvas(height, theme)
    draw = ImageDraw.Draw(image)

    source_id = events[0][1].source_id
    vendor_color = VENDOR_COLORS.get(source_id, "#386A8A")
    draw.rounded_rectangle(
        (54, 44, 126, 116),
        radius=theme.badge_radius,
        fill=theme.icon_surface,
        outline=theme.border,
        width=1,
    )
    _paste_icon(image, _vendor_icon(source_id), (70, 60), 40, vendor_color)
    source_name_lines = _wrap_text(source_name, _font(40, True), 610, 1)
    _draw_lines(
        draw,
        source_name_lines,
        (150, 42),
        _font(40, True),
        theme.text,
        48,
    )
    _paste_icon(image, "clock", (152, 95), 17, theme.muted)
    draw.text(
        (179, 91),
        _format_local_datetime(generated_at),
        font=_font(20),
        fill=theme.muted,
    )

    source_url = events[0][1].status_url
    source_host = urlparse(source_url).netloc or "official status page"
    source_label = {
        "zh-CN": "官方来源",
        "en-US": "Official source",
        "bilingual": "官方来源  /  Official source",
    }[language]
    draw.text((812, 46), source_label, font=_font(15, True), fill=theme.muted)
    draw.text((812, 72), source_host, font=_font(21, True), fill=theme.text_soft)
    count_label = (
        f"本次 {len(events)} 项变更"
        if language == "zh-CN"
        else f"{len(events)} change(s) in this bulletin"
    )
    if language == "bilingual":
        count_label = f"本次 {len(events)} 项变更  /  {len(events)} change(s)"
    draw.text((812, 100), count_label, font=_font(16), fill=theme.muted)
    _fill_rectangle(image, (54, 136, 272, 140), vendor_color)
    _fill_rectangle(image, (272, 137, WIDTH - 54, 139), theme.border)
    draw = ImageDraw.Draw(image)

    y = 164
    for event_index, layout in enumerate(layouts, start=1):
        stage = str(layout["stage"])
        issue = layout["issue"]
        assert isinstance(issue, Issue)
        block_height = int(layout["height"])
        severity = "operational" if stage == "recovered" else issue.severity
        accent = theme.severity_colors.get(severity, theme.severity_colors["warning"])
        _draw_panel(image, (54, y, WIDTH - 54, y + block_height), theme)
        draw = ImageDraw.Draw(image)
        _fill_rectangle(image, (54, y + 16, 59, y + block_height - 16), accent)
        draw = ImageDraw.Draw(image)
        draw.text(
            (82, y + 25),
            f"{event_index:02d}",
            font=_font(17, True),
            fill=theme.muted,
        )
        draw.line((116, y + 24, 116, y + 50), fill=theme.border, width=1)
        _paste_icon(image, _stage_icon(stage), (136, y + 27), 18, accent)
        draw.text(
            (166, y + 23),
            STAGE_LABELS[language].get(stage, stage),
            font=_font(19, True),
            fill=accent,
        )
        severity_label = STATUS_LABELS[language].get(severity, severity)
        severity_font = _font(18, True)
        severity_width = int(_text_width(severity_label, severity_font))
        severity_x = WIDTH - 82 - severity_width
        draw.ellipse((severity_x - 24, y + 31, severity_x - 12, y + 43), fill=accent)
        draw.text(
            (severity_x, y + 24),
            severity_label,
            font=severity_font,
            fill=accent,
        )

        cursor = y + 82
        event_vendor_color = VENDOR_COLORS.get(issue.source_id, "#386A8A")
        draw.rounded_rectangle(
            (82, cursor + 1, 126, cursor + 45),
            radius=min(theme.badge_radius, 11),
            fill=theme.icon_surface,
            outline=theme.border,
            width=1,
        )
        _paste_icon(
            image,
            _vendor_icon(issue.source_id),
            (90, cursor + 9),
            28,
            event_vendor_color,
        )
        draw = ImageDraw.Draw(image)
        cursor = _draw_lines(
            draw,
            layout["title_lines"],
            (144, cursor),
            _font(36, True),
            theme.text,
            47,
        )
        original_lines = layout["title_original_lines"]
        if original_lines:
            cursor = _draw_lines(
                draw, original_lines, (144, cursor), _font(20), theme.muted, 27
            )

        service_lines = layout["service_lines"]
        if service_lines:
            cursor += 20
            draw.line((82, cursor, WIDTH - 82, cursor), fill=theme.border, width=1)
            cursor += 20
            _paste_icon(image, "service", (82, cursor + 1), 20, accent)
            service_heading = (
                "Affected services" if language == "en-US" else "受影响服务"
            )
            draw.text(
                (112, cursor - 2),
                service_heading,
                font=_font(17, True),
                fill=theme.text_soft,
            )
            if language == "bilingual":
                draw.text(
                    (112, cursor + 20),
                    "Affected services",
                    font=_font(14),
                    fill=theme.muted,
                )
            content_cursor = _draw_lines(
                draw,
                service_lines,
                (280, cursor - 3),
                _font(23),
                theme.text_soft,
                32,
            )
            service_original_lines = layout["service_original_lines"]
            if service_original_lines:
                content_cursor = _draw_lines(
                    draw,
                    service_original_lines,
                    (280, content_cursor),
                    _font(18),
                    theme.muted,
                    25,
                )
            label_height = 43 if language == "bilingual" else 28
            cursor = max(cursor + label_height, content_cursor)

        detail_lines = layout["detail_lines"]
        if detail_lines:
            cursor += 20
            draw.line((82, cursor, WIDTH - 82, cursor), fill=theme.border, width=1)
            cursor += 20
            detail_heading = "Official note" if language == "en-US" else "官方说明"
            draw.text(
                (82, cursor - 2),
                detail_heading,
                font=_font(17, True),
                fill=theme.text_soft,
            )
            if language == "bilingual":
                draw.text(
                    (82, cursor + 20),
                    "Official note",
                    font=_font(14),
                    fill=theme.muted,
                )
            content_cursor = _draw_lines(
                draw,
                detail_lines,
                (280, cursor - 3),
                _font(23),
                theme.text_soft,
                32,
            )
            detail_original_lines = layout["detail_original_lines"]
            if detail_original_lines:
                content_cursor = _draw_lines(
                    draw,
                    detail_original_lines,
                    (280, content_cursor),
                    _font(18),
                    theme.muted,
                    25,
                )
            label_height = 43 if language == "bilingual" else 28
            cursor = max(cursor + label_height, content_cursor)

        meta_y = y + block_height - 45
        draw.line(
            (82, meta_y - 15, WIDTH - 82, meta_y - 15),
            fill=theme.divider,
            width=1,
        )
        if issue.updated_at:
            _paste_icon(image, "clock", (82, meta_y), 18, theme.muted)
            draw.text(
                (110, meta_y - 2),
                _format_event_time(issue.updated_at, display_timezone),
                font=_font(17),
                fill=theme.muted,
            )
        if issue.status_url:
            parsed = urlparse(issue.status_url)
            url_text = issue.status_url
            if len(url_text) > 58:
                url_text = f"{parsed.scheme}://{parsed.netloc}/…"
            url_width = _text_width(url_text, _font(17))
            link_x = WIDTH - 82 - int(url_width)
            _paste_icon(image, "link", (link_x - 27, meta_y), 18, theme.link)
            draw.text(
                (link_x, meta_y - 2),
                url_text,
                font=_font(17),
                fill=theme.link,
            )
        y += block_height + 20

    _draw_project_footer(image, height - 40, theme)
    output = BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_overview(
    results: list[SourceResult],
    translations: dict[str, str] | None = None,
    language: str = "bilingual",
    generated_at: datetime | None = None,
    card_theme: str = DEFAULT_CARD_THEME,
) -> bytes:
    """Render current status of all enabled sources into one bilingual PNG image.

    Args:
        results: Latest source query results.
        translations: Original official strings mapped to Chinese translations.
        language: ``zh-CN``, ``en-US``, or ``bilingual``.
        generated_at: Timezone-aware time used for the timestamp heading.
        card_theme: Configured visual theme identifier.

    Returns:
        Encoded PNG bytes.
    """
    language = normalize_language(language)
    translations = translations or {}
    generated_at = generated_at or datetime.now().astimezone()
    theme = CARD_THEMES[normalize_card_theme(card_theme)]
    rows: list[dict[str, object]] = []
    for result in results:
        primary_lines: list[str] = []
        original_lines: list[str] = []
        if result.success and result.issues:
            first_issue = max(
                result.issues.values(),
                key=lambda issue: (
                    issue.severity == "critical",
                    issue.updated_at,
                ),
            )
            primary, original = _localized_pair(
                first_issue.title, translations, language
            )
            primary_lines = _wrap_text(primary, _font(21, True), 370, 2)
            original_lines = _wrap_text(original, _font(16), 370, 2)
        row_height = max(
            96,
            68 + len(primary_lines) * 28 + len(original_lines) * 22,
        )
        rows.append(
            {
                "result": result,
                "primary_lines": primary_lines,
                "original_lines": original_lines,
                "height": max(112, row_height),
            }
        )
    if not rows:
        rows = [
            {"result": None, "primary_lines": [], "original_lines": [], "height": 96}
        ]
    table_header_height = 50
    table_height = table_header_height + sum(int(row["height"]) for row in rows)
    height = 144 + table_height + 68
    image = _new_canvas(height, theme)
    draw = ImageDraw.Draw(image)
    draw.text(
        (52, 48),
        _format_overview_date(generated_at, language),
        font=_font(43, True),
        fill=theme.text,
    )
    count_text = (
        f"监控来源  {len(results):02d}"
        if language == "zh-CN"
        else f"Sources monitored  {len(results):02d}"
    )
    if language == "bilingual":
        count_text = f"监控来源 / Sources  {len(results):02d}"
    draw.text((878, 52), count_text, font=_font(18, True), fill=theme.text_soft)
    live_text = {
        "zh-CN": "实时查询 · 官方状态接口",
        "en-US": "Live query · Official status APIs",
        "bilingual": "实时查询 / Live query · Official APIs",
    }[language]
    draw.text((878, 86), live_text, font=_font(16), fill=theme.muted)
    _fill_rectangle(image, (52, 118, 300, 122), theme.chrome)
    _fill_rectangle(image, (300, 119, WIDTH - 52, 121), theme.border)
    draw = ImageDraw.Draw(image)

    table_y = 144
    _draw_panel(
        image,
        (52, table_y, WIDTH - 52, table_y + table_height),
        theme,
        radius=theme.table_radius,
    )
    draw = ImageDraw.Draw(image)
    column_labels = {
        "zh-CN": ("厂商", "事件摘要", "当前状态"),
        "en-US": ("Vendor", "Incident summary", "Current status"),
        "bilingual": (
            "厂商 / Vendor",
            "事件摘要 / Incident summary",
            "当前状态 / Status",
        ),
    }[language]
    draw.text(
        (80, table_y + 15),
        column_labels[0],
        font=_font(15, True),
        fill=theme.muted,
    )
    draw.text(
        (488, table_y + 15),
        column_labels[1],
        font=_font(15, True),
        fill=theme.muted,
    )
    draw.text(
        (916, table_y + 15),
        column_labels[2],
        font=_font(15, True),
        fill=theme.muted,
    )
    draw.line(
        (52, table_y + table_header_height, WIDTH - 52, table_y + table_header_height),
        fill=theme.border,
        width=1,
    )
    draw.line(
        (460, table_y, 460, table_y + table_height),
        fill=theme.divider,
        width=1,
    )
    draw.line(
        (888, table_y, 888, table_y + table_height),
        fill=theme.divider,
        width=1,
    )

    y = table_y + table_header_height
    for index, row in enumerate(rows):
        result = row["result"]
        row_height = int(row["height"])
        draw = ImageDraw.Draw(image)
        if result is None:
            _fill_rectangle(
                image,
                (53, y, WIDTH - 53, y + row_height),
                theme.surface_alt,
            )
            draw = ImageDraw.Draw(image)
            _paste_icon(image, "unavailable", (82, y + 29), 38, theme.muted)
            empty_text = (
                "没有启用任何状态来源"
                if language != "en-US"
                else "No status sources enabled"
            )
            draw.text((144, y + 31), empty_text, font=_font(24), fill=theme.muted)
            y += row_height
            continue
        assert isinstance(result, SourceResult)
        severity = result.severity
        accent = theme.severity_colors.get(
            severity, theme.severity_colors["unavailable"]
        )
        row_fill = theme.surface if index % 2 == 0 else theme.surface_alt
        if severity == "critical":
            row_fill = theme.critical_surface
        elif severity == "warning":
            row_fill = theme.warning_surface
        elif not result.success:
            row_fill = theme.unavailable_surface
        _fill_rectangle(image, (53, y, WIDTH - 53, y + row_height), row_fill)
        draw = ImageDraw.Draw(image)
        row_center = y + row_height // 2
        vendor_color = VENDOR_COLORS.get(result.spec.source_id, "#386A8A")
        draw.rounded_rectangle(
            (80, row_center - 28, 136, row_center + 28),
            radius=theme.badge_radius,
            fill=theme.icon_surface,
            outline=theme.border,
            width=1,
        )
        _paste_icon(
            image,
            _vendor_icon(result.spec.source_id),
            (93, row_center - 15),
            30,
            vendor_color,
        )
        vendor_name_lines = _wrap_text(
            result.spec.name,
            _font(22, True),
            275,
            2,
        )
        vendor_block_height = len(vendor_name_lines) * 27 + 21
        vendor_cursor = row_center - vendor_block_height // 2
        vendor_cursor = _draw_lines(
            draw,
            vendor_name_lines,
            (156, vendor_cursor),
            _font(22, True),
            theme.text,
            27,
        )
        source_kind = {
            "statuspage": "Statuspage JSON",
            "google": "Google incidents",
            "rss": "RSS feed",
        }.get(result.spec.kind, result.spec.kind)
        draw.text(
            (156, vendor_cursor + 2),
            source_kind,
            font=_font(15),
            fill=theme.muted,
        )

        if result.success:
            if result.issues:
                subtitle_text = (
                    f"{len(result.issues)} 个活动事件"
                    if language != "en-US"
                    else f"{len(result.issues)} active incident(s)"
                )
                if language == "bilingual":
                    subtitle_text += f"  /  {len(result.issues)} active"
            else:
                subtitle_text = (
                    "未发现活动异常  /  No active incidents"
                    if language == "bilingual"
                    else (
                        "未发现活动异常"
                        if language == "zh-CN"
                        else "No active incidents"
                    )
                )
        else:
            subtitle_text = (
                "本次查询失败，不视为服务故障"
                if language == "zh-CN"
                else "Query failed; not treated as an outage"
            )
            if language == "bilingual":
                subtitle_text = "查询失败  /  Query failed; not treated as an outage"

        primary_lines = row["primary_lines"]
        original_lines = row["original_lines"]
        if primary_lines:
            draw.text((488, y + 17), subtitle_text, font=_font(15, True), fill=accent)
            cursor = _draw_lines(
                draw,
                primary_lines,
                (488, y + 43),
                _font(21, True),
                theme.text_soft,
                28,
            )
            if original_lines:
                _draw_lines(
                    draw,
                    original_lines,
                    (488, cursor),
                    _font(16),
                    theme.muted,
                    22,
                )
        else:
            summary_lines = _wrap_text(
                subtitle_text,
                _font(17),
                370,
                2,
            )
            _draw_lines(
                draw,
                summary_lines,
                (488, row_center - len(summary_lines) * 12),
                _font(17),
                theme.muted,
                24,
            )

        label = STATUS_LABELS[language].get(severity, severity)
        status_lines = _wrap_text(label, _font(16, True), 190, 2)
        status_y = row_center - len(status_lines) * 11
        _paste_icon(
            image,
            _status_icon(severity),
            (916, status_y + 1),
            22,
            accent,
        )
        _draw_lines(
            draw,
            status_lines,
            (948, status_y),
            _font(16, True),
            accent,
            22,
        )
        y += row_height
        if index < len(rows) - 1:
            draw.line((52, y, WIDTH - 52, y), fill=theme.border, width=1)

    draw.line(
        (460, table_y, 460, table_y + table_height),
        fill=theme.divider,
        width=1,
    )
    draw.line(
        (888, table_y, 888, table_y + table_height),
        fill=theme.divider,
        width=1,
    )

    _draw_project_footer(image, height - 40, theme)
    output = BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def build_alert_fallback(
    source_name: str,
    events: list[tuple[str, Issue]],
    translations: dict[str, str] | None = None,
    language: str = "bilingual",
) -> str:
    """Build localized text fallback for exceptional image rendering failures.

    Args:
        source_name: Human-readable vendor name.
        events: Stage and issue pairs.
        translations: Original official strings mapped to Chinese translations.
        language: ``zh-CN``, ``en-US``, or ``bilingual``.

    Returns:
        Multiline status alert.
    """
    language = normalize_language(language)
    translations = translations or {}
    lines = [f"【{source_name} 服务状态 / SERVICE STATUS】"]
    for stage, issue in events:
        title, original = _localized_pair(issue.title, translations, language)
        lines.append(f"{STAGE_LABELS[language].get(stage, stage)} · {title}")
        if original:
            lines.append(original)
        if issue.affected_services:
            services = [
                _localized_pair(item, translations, language)[0]
                for item in issue.affected_services[:12]
            ]
            prefix = "受影响服务：" if language != "en-US" else "Affected services: "
            lines.append(prefix + "、".join(services))
        if issue.detail:
            detail, original_detail = _localized_pair(
                issue.detail, translations, language
            )
            lines.append(detail[:500])
            if original_detail:
                lines.append(original_detail[:500])
        if issue.status_url:
            lines.append(issue.status_url)
    return "\n".join(lines)
