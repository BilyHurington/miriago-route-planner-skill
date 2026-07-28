"""Structured journey-note validation and MiriaGo Markdown rendering."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

JOURNEY_NOTES_SCHEMA_VERSION = 1
MANAGED_SECTION_START = "<!-- plan-miriago-route:journey-notes:start -->"
MANAGED_SECTION_END = "<!-- plan-miriago-route:journey-notes:end -->"
MODES = {
    "walk": "步行",
    "rail": "铁路",
    "subway": "地铁",
    "tram": "有轨电车",
    "bus": "公交",
    "ferry": "轮渡",
    "taxi": "出租车",
    "bicycle": "自行车",
    "other": "其他",
}
CONFIDENCE_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
}


class JourneyNotesError(ValueError):
    """Raised when structured journey notes are invalid or cannot be merged."""


def load_journey_notes_spec(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JourneyNotesError(f"Cannot read journey notes {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise JourneyNotesError("Journey notes must be a JSON object.")
    validate_journey_notes_spec(value)
    return value


def validate_journey_notes_spec(spec: Mapping[str, Any]) -> None:
    errors: list[str] = []
    _unknown_fields(
        spec,
        {
            "schemaVersion",
            "title",
            "timezone",
            "checkedAt",
            "includeAreaWalking",
            "summaryNotes",
            "segments",
            "globalCautions",
            "actionItems",
        },
        "notes",
        errors,
    )
    if spec.get("schemaVersion") != JOURNEY_NOTES_SCHEMA_VERSION:
        errors.append(
            f"schemaVersion must be {JOURNEY_NOTES_SCHEMA_VERSION}."
        )
    _optional_text(spec, "title", "notes", errors)
    _required_text(spec, "timezone", "notes", errors)
    _required_text(spec, "checkedAt", "notes", errors)
    include_area_walking = spec.get("includeAreaWalking", True)
    if not isinstance(include_area_walking, bool):
        errors.append("notes.includeAreaWalking must be a boolean.")
    for field in ("summaryNotes", "globalCautions", "actionItems"):
        _text_list(spec.get(field, []), f"notes.{field}", errors)

    segments = spec.get("segments", [])
    if not isinstance(segments, list):
        errors.append("notes.segments must be a list.")
        segments = []
    for index, segment in enumerate(segments):
        _validate_segment(segment, index, errors)

    has_content = bool(
        include_area_walking
        or segments
        or spec.get("summaryNotes")
        or spec.get("globalCautions")
        or spec.get("actionItems")
    )
    if not has_content:
        errors.append("Journey notes contain no content to render.")
    if errors:
        raise JourneyNotesError("\n".join(errors))


def render_journey_notes(
    proposal: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> str:
    validate_journey_notes_spec(spec)
    title = str(spec.get("title") or "巡礼行程建议")
    lines = [
        MANAGED_SECTION_START,
        f"## {_markdown_text(title)}",
        "",
        f"> 数据更新时间：{_markdown_text(str(spec['checkedAt']))}  ",
        f"> 时区：{_markdown_text(str(spec['timezone']))}  ",
        "> 公共交通与步行时间均为计划参考，出发前请重新确认。",
    ]
    summary_notes = spec.get("summaryNotes", [])
    if summary_notes:
        lines.extend(["", "### 总览"])
        lines.extend(f"- {_markdown_text(item)}" for item in summary_notes)

    if spec.get("includeAreaWalking", True):
        lines.extend(["", "### 片区内步行"])
        groups = proposal.get("groups", [])
        if not groups:
            lines.append("- 当前没有可展示的片区。")
        for group in groups:
            if isinstance(group, Mapping):
                lines.extend(_render_group_walking(group))

    segments = spec.get("segments", [])
    if segments:
        lines.extend(["", "### 片区间行程"])
        for segment in segments:
            if isinstance(segment, Mapping):
                lines.extend(_render_segment(segment))

    global_cautions = spec.get("globalCautions", [])
    if global_cautions:
        lines.extend(["", "### 注意事项"])
        lines.extend(f"> {_markdown_text(item)}" for item in global_cautions)

    action_items = spec.get("actionItems", [])
    if action_items:
        lines.extend(["", "### 出发前确认"])
        lines.extend(f"- [ ] {_markdown_text(item)}" for item in action_items)
    lines.extend(["", MANAGED_SECTION_END])
    return "\n".join(lines)


def merge_managed_journey_notes(existing_memo: str, markdown: str) -> str:
    if MANAGED_SECTION_START not in markdown or MANAGED_SECTION_END not in markdown:
        raise JourneyNotesError("Rendered journey notes are missing managed markers.")
    start_count = existing_memo.count(MANAGED_SECTION_START)
    end_count = existing_memo.count(MANAGED_SECTION_END)
    if start_count != end_count or start_count > 1:
        raise JourneyNotesError(
            "Existing plan memo has malformed or duplicate journey-note markers."
        )
    if start_count == 0:
        prefix = existing_memo.rstrip()
        return f"{prefix}\n\n{markdown}" if prefix else markdown

    start = existing_memo.index(MANAGED_SECTION_START)
    end = existing_memo.index(MANAGED_SECTION_END, start) + len(
        MANAGED_SECTION_END
    )
    old_section = existing_memo[start:end]
    replacement = _preserve_checked_tasks(old_section, markdown)
    before = existing_memo[:start].rstrip()
    after = existing_memo[end:].lstrip()
    parts = [part for part in (before, replacement, after) if part]
    return "\n\n".join(parts)


def route_fingerprint(proposal: Mapping[str, Any]) -> str:
    groups: list[dict[str, Any]] = []
    for group in proposal.get("groups", []):
        if not isinstance(group, Mapping):
            continue
        metrics = group.get("metrics")
        groups.append(
            {
                "id": group.get("id"),
                "name": group.get("name"),
                "pointIds": group.get("pointIds"),
                "walkingPathMeters": metrics.get("walkingPathMeters")
                if isinstance(metrics, Mapping)
                else None,
                "walkingDurationSeconds": metrics.get("walkingDurationSeconds")
                if isinstance(metrics, Mapping)
                else None,
                "roughWalkingMeters": metrics.get("roughWalkingMeters")
                if isinstance(metrics, Mapping)
                else None,
                "routeEvaluation": group.get("routeEvaluation"),
            }
        )
    material = {
        "groups": groups,
        "excludedPoints": proposal.get("excludedPoints", []),
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_segment(value: Any, index: int, errors: list[str]) -> None:
    context = f"notes.segments[{index}]"
    if not isinstance(value, Mapping):
        errors.append(f"{context} must be an object.")
        return
    _unknown_fields(
        value,
        {
            "from",
            "to",
            "mode",
            "operator",
            "service",
            "direction",
            "departureWindow",
            "durationMinutes",
            "walkingMinutes",
            "transferMinutes",
            "bufferMinutes",
            "fare",
            "instructions",
            "cautions",
            "actionItems",
            "alternatives",
            "source",
            "confidence",
        },
        context,
        errors,
    )
    _required_text(value, "from", context, errors)
    _required_text(value, "to", context, errors)
    mode = value.get("mode")
    if mode not in MODES:
        errors.append(f"{context}.mode must be one of {sorted(MODES)}.")
    for field in ("operator", "service", "direction", "departureWindow"):
        _optional_text(value, field, context, errors)
    _duration(value.get("durationMinutes"), f"{context}.durationMinutes", errors)
    for field in ("walkingMinutes", "transferMinutes", "bufferMinutes"):
        _optional_non_negative_number(value, field, context, errors)
    for field in ("instructions", "cautions", "actionItems", "alternatives"):
        _text_list(value.get(field, []), f"{context}.{field}", errors)
    confidence = value.get("confidence")
    if confidence is not None and confidence not in CONFIDENCE_LABELS:
        errors.append(
            f"{context}.confidence must be high, medium, low, or null."
        )
    _validate_fare(value.get("fare"), context, errors)
    _validate_source(value.get("source"), context, errors)
    if mode != "walk" and value.get("source") is None:
        errors.append(
            f"{context}.source is required for non-walking travel information."
        )


def _validate_fare(value: Any, context: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        errors.append(f"{context}.fare must be an object or null.")
        return
    _unknown_fields(value, {"amount", "currency"}, f"{context}.fare", errors)
    amount = value.get("amount")
    if not _is_non_negative_number(amount):
        errors.append(f"{context}.fare.amount must be a non-negative number.")
    _required_text(value, "currency", f"{context}.fare", errors)


def _validate_source(value: Any, context: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        errors.append(f"{context}.source must be an object or null.")
        return
    _unknown_fields(
        value,
        {"provider", "url", "checkedAt"},
        f"{context}.source",
        errors,
    )
    _required_text(value, "provider", f"{context}.source", errors)
    _optional_text(value, "checkedAt", f"{context}.source", errors)
    url = value.get("url")
    if url is not None:
        if not isinstance(url, str) or urlparse(url).scheme not in {"http", "https"}:
            errors.append(f"{context}.source.url must be an HTTP(S) URL or null.")


def _render_group_walking(group: Mapping[str, Any]) -> list[str]:
    name = _markdown_text(str(group.get("name") or group.get("id") or "未命名片区"))
    point_ids = group.get("pointIds")
    point_count = len(point_ids) if isinstance(point_ids, list) else 0
    metrics = group.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    evaluation = group.get("routeEvaluation")
    evaluation = evaluation if isinstance(evaluation, Mapping) else {}
    lines = ["", f"#### {name}", "", f"- **点位**：{point_count} 个"]
    if evaluation.get("status") == "routed":
        lines.append(
            f"- **路网步行距离**：{_format_distance(metrics.get('walkingPathMeters'))}"
        )
        lines.append(
            f"- **预计步行时间**：{_format_seconds(metrics.get('walkingDurationSeconds'))}"
        )
        lines.append("- **评估方式**：步行路网")
    else:
        lines.append(
            f"- **粗略步行距离**：{_format_distance(metrics.get('roughWalkingMeters'))}"
        )
        status = evaluation.get("status")
        if status in {"skipped", "failed"}:
            reason = _markdown_text(str(evaluation.get("reason") or "未提供原因"))
            lines.append(f"- **路网评估**：{status}（{reason}）")
        else:
            lines.append("- **预计步行时间**：未取得路网用时")
    return lines


def _render_segment(segment: Mapping[str, Any]) -> list[str]:
    origin = _markdown_text(str(segment["from"]))
    destination = _markdown_text(str(segment["to"]))
    lines = ["", f"#### {origin} → {destination}", ""]
    lines.append(f"- **方式**：{MODES[str(segment['mode'])]}")
    for field, label in (
        ("operator", "运营方"),
        ("service", "线路/班次"),
        ("direction", "方向"),
        ("departureWindow", "建议出发时段"),
    ):
        value = segment.get(field)
        if value:
            lines.append(f"- **{label}**：{_markdown_text(str(value))}")
    duration = segment.get("durationMinutes")
    if duration is not None:
        lines.append(f"- **行程时间**：{_format_duration(duration)}")
    for field, label in (
        ("walkingMinutes", "接驳步行"),
        ("transferMinutes", "换乘"),
        ("bufferMinutes", "建议缓冲"),
    ):
        value = segment.get(field)
        if value is not None:
            lines.append(f"- **{label}**：{_format_number(value)} 分钟")
    fare = segment.get("fare")
    if isinstance(fare, Mapping):
        lines.append(
            f"- **预计费用**：{_format_number(fare.get('amount'))} "
            f"{_markdown_text(str(fare.get('currency')))}"
        )
    confidence = segment.get("confidence")
    if confidence in CONFIDENCE_LABELS:
        lines.append(f"- **信息可信度**：{CONFIDENCE_LABELS[str(confidence)]}")
    source = segment.get("source")
    if isinstance(source, Mapping):
        provider = _markdown_text(str(source.get("provider")))
        url = source.get("url")
        provider_text = f"[{provider}]({_markdown_url(str(url))})" if url else provider
        checked_at = source.get("checkedAt")
        suffix = f"，核对于 {_markdown_text(str(checked_at))}" if checked_at else ""
        lines.append(f"- **来源**：{provider_text}{suffix}")
    for instruction in segment.get("instructions", []):
        lines.append(f"- {_markdown_text(instruction)}")
    for alternative in segment.get("alternatives", []):
        lines.append(f"- **备选**：{_markdown_text(alternative)}")
    for caution in segment.get("cautions", []):
        lines.append(f"> 注意：{_markdown_text(caution)}")
    for action in segment.get("actionItems", []):
        lines.append(f"- [ ] {_markdown_text(action)}")
    return lines


def _duration(value: Any, context: str, errors: list[str]) -> None:
    if value is None:
        return
    if _is_non_negative_number(value):
        return
    if not isinstance(value, Mapping):
        errors.append(f"{context} must be a non-negative number, range, or null.")
        return
    _unknown_fields(value, {"min", "max"}, context, errors)
    minimum = value.get("min")
    maximum = value.get("max")
    if not _is_non_negative_number(minimum) or not _is_non_negative_number(maximum):
        errors.append(f"{context}.min and max must be non-negative numbers.")
    elif float(minimum) > float(maximum):
        errors.append(f"{context}.min must not exceed max.")


def _required_text(
    value: Mapping[str, Any], field: str, context: str, errors: list[str]
) -> None:
    text = value.get(field)
    if not isinstance(text, str) or not text.strip():
        errors.append(f"{context}.{field} must be a non-empty string.")


def _optional_text(
    value: Mapping[str, Any], field: str, context: str, errors: list[str]
) -> None:
    text = value.get(field)
    if text is not None and (not isinstance(text, str) or not text.strip()):
        errors.append(f"{context}.{field} must be a non-empty string or null.")


def _text_list(value: Any, context: str, errors: list[str]) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        errors.append(f"{context} must be a list of non-empty strings.")


def _optional_non_negative_number(
    value: Mapping[str, Any], field: str, context: str, errors: list[str]
) -> None:
    number = value.get(field)
    if number is not None and not _is_non_negative_number(number):
        errors.append(f"{context}.{field} must be a non-negative number or null.")


def _is_non_negative_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _format_duration(value: Any) -> str:
    if isinstance(value, Mapping):
        minimum = _format_number(value.get("min"))
        maximum = _format_number(value.get("max"))
        return f"约 {minimum}-{maximum} 分钟"
    return f"约 {_format_number(value)} 分钟"


def _format_distance(value: Any) -> str:
    if not _is_non_negative_number(value):
        return "未知"
    meters = float(value)
    if meters >= 1_000:
        return f"{meters / 1_000:.1f} 公里"
    return f"{round(meters)} 米"


def _format_seconds(value: Any) -> str:
    if not _is_non_negative_number(value):
        return "未知"
    minutes = max(0, round(float(value) / 60))
    if minutes >= 60:
        hours, remainder = divmod(minutes, 60)
        return f"{hours} 小时 {remainder} 分钟" if remainder else f"{hours} 小时"
    return f"{minutes} 分钟"


def _format_number(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "未知"
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def _markdown_text(value: str) -> str:
    compact = re.sub(r"\s+", " ", value.strip())
    return re.sub(r"([\\`*_[\]<>#])", r"\\\1", compact)


def _markdown_url(value: str) -> str:
    return value.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    context: str,
    errors: list[str],
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        errors.append(f"{context} contains unknown fields: {unknown}")


def _preserve_checked_tasks(old: str, new: str) -> str:
    task_pattern = re.compile(r"^- \[(?P<mark>[ xX])\] (?P<text>.+)$", re.MULTILINE)
    checked = {
        match.group("text")
        for match in task_pattern.finditer(old)
        if match.group("mark").lower() == "x"
    }
    if not checked:
        return new

    def replace(match: re.Match[str]) -> str:
        mark = "x" if match.group("text") in checked else match.group("mark")
        return f"- [{mark}] {match.group('text')}"

    return task_pattern.sub(replace, new)
