"""Station-assisted route planning from OpenStreetMap geography only.

Stations, entrances, and rail-line membership come from public Overpass data.
Nothing here produces timetables, fares, or ride durations: transitions are
labelled candidates for the user or an official source to complete.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from urllib.request import urlopen

from .clustering import haversine_meters
from .proposal import empty_station_assist
from .public_services import (
    DEFAULT_OVERPASS_ENDPOINT,
    JsonCache,
    PublicServiceError,
    _cache_key,
    _post_form_json,
    fetch_stations,
)

DEFAULT_WALK_THRESHOLD_METERS = 1_200
DEFAULT_MAX_ACCESS_METERS = 1_500
STOP_MATCH_METERS = 250
LINE_CACHE_DAYS = 30
ROUTE_TYPES = "train|subway|light_rail|tram|monorail"


def station_candidates(
    locations: Sequence[tuple[float, float]],
    *,
    search_meters: float = DEFAULT_MAX_ACCESS_METERS,
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    cache: JsonCache | None = None,
    requester: Callable[..., Any] = urlopen,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Fetch rail stations around locations and give each a unique label."""
    if not locations:
        return [], {"provider": "openstreetmap_overpass", "status": "empty_scope"}, []
    boxes = []
    for latitude, longitude in locations:
        dlat = search_meters / 111_320
        dlon = search_meters / (111_320 * max(0.2, math.cos(math.radians(latitude))))
        boxes.append((latitude - dlat, longitude - dlon, latitude + dlat, longitude + dlon))
    merged = _merge_boxes(boxes)
    result = fetch_stations(merged, endpoint=endpoint, cache=cache, requester=requester)
    stations = [
        dict(item) for item in result.stations if item.get("kind") != "subway_entrance"
    ]
    return label_stations(stations), dict(result.source), list(result.warnings)


def stations_from_files(
    files: Sequence[str],
    locations: Sequence[tuple[float, float]],
    *,
    search_meters: float = DEFAULT_MAX_ACCESS_METERS,
) -> list[dict[str, Any]]:
    """Read stations from local Overpass JSON and keep those near locations."""
    import json
    from pathlib import Path

    from .public_services import parse_overpass_stations

    elements: list[Any] = []
    for path in files:
        try:
            payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicServiceError(f"Cannot read station file {path}: {exc}") from exc
        elements.extend(payload.get("elements", []) if isinstance(payload, Mapping) else [])
    stations = [
        dict(item)
        for item in parse_overpass_stations({"elements": elements})
        if item.get("kind") != "subway_entrance"
        and any(
            haversine_meters(lat, lon, float(item["latitude"]), float(item["longitude"]))
            <= search_meters
            for lat, lon in locations
        )
    ]
    return label_stations(stations)


def label_stations(stations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``label``: the name, plus the operator when two stations share it."""
    by_name: dict[str, int] = {}
    for station in stations:
        key = _normalize(str(station.get("name") or ""))
        by_name[key] = by_name.get(key, 0) + 1
    labelled = []
    used: set[str] = set()
    for station in sorted(stations, key=lambda item: (str(item.get("name")), str(item.get("id")))):
        name = str(station.get("name") or "未命名站")
        label = name
        if by_name.get(_normalize(name), 0) > 1:
            owner = station.get("operator") or station.get("network")
            if owner:
                label = f"{name}（{owner}）"
        base, suffix = label, 2
        while label in used:
            label = f"{base}#{suffix}"
            suffix += 1
        used.add(label)
        labelled.append({**station, "label": label})
    return labelled


def fetch_station_lines(
    stations: Sequence[Mapping[str, Any]],
    *,
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    cache: JsonCache | None = None,
    requester: Callable[..., Any] = urlopen,
) -> tuple[dict[str, list[str]], list[str]]:
    """Return ``{station id: [line names]}`` from OSM route relations."""
    elements = {"node": [], "way": [], "relation": []}
    for station in stations:
        osm_type = station.get("osmType")
        osm_id = station.get("osmId")
        if osm_type in elements and isinstance(osm_id, int):
            elements[osm_type].append(str(osm_id))
    if not any(elements.values()):
        return {}, []
    selectors = "".join(
        f"{kind}(id:{','.join(ids)});" for kind, ids in elements.items() if ids
    )
    query = (
        "[out:json][timeout:90];"
        f"({selectors})->.st;"
        f'(node(around.st:{STOP_MATCH_METERS})["public_transport"="stop_position"];'
        f'node(around.st:{STOP_MATCH_METERS})["railway"~"^(stop|halt|station|tram_stop)$"];)->.stops;'
        f'rel(bn.stops)["route"~"^({ROUTE_TYPES})$"]->.routes;'
        ".stops out skel qt;.routes out body qt;"
    )
    cache_value = cache or JsonCache()
    key = _cache_key(endpoint, query)
    response = cache_value.get("overpass-lines", key, LINE_CACHE_DAYS)
    warnings: list[str] = []
    if response is None:
        try:
            response = _post_form_json(
                endpoint, {"data": query}, timeout_seconds=120, requester=requester
            )
            cache_value.put("overpass-lines", key, response)
        except PublicServiceError as exc:
            return {}, [f"Overpass line lookup failed: {exc}"]
    return parse_station_lines(response, stations), warnings


def parse_station_lines(
    response: Mapping[str, Any],
    stations: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    stops: dict[int, tuple[float, float]] = {}
    relations: list[Mapping[str, Any]] = []
    for element in response.get("elements", []) if isinstance(response, Mapping) else []:
        if not isinstance(element, Mapping):
            continue
        if element.get("type") == "node" and "lat" in element and "lon" in element:
            stops[int(element["id"])] = (float(element["lat"]), float(element["lon"]))
        elif element.get("type") == "relation":
            relations.append(element)
    lines: dict[str, set[str]] = {}
    for relation in relations:
        tags = relation.get("tags") if isinstance(relation.get("tags"), Mapping) else {}
        label = line_label(tags)
        if not label:
            continue
        for member in relation.get("members", []) or []:
            if not isinstance(member, Mapping) or member.get("type") != "node":
                continue
            coordinate = stops.get(int(member.get("ref", -1)))
            if coordinate is None:
                continue
            for station in stations:
                if (
                    haversine_meters(
                        coordinate[0],
                        coordinate[1],
                        float(station["latitude"]),
                        float(station["longitude"]),
                    )
                    <= STOP_MATCH_METERS
                ):
                    lines.setdefault(str(station["id"]), set()).add(label)
    return {key: sorted(value) for key, value in lines.items()}


def line_label(tags: Mapping[str, Any]) -> str | None:
    name = tags.get("name")
    if isinstance(name, str) and name.strip():
        base = re.split(r"\s*[(（:：]|\s+[-–→]\s+|\s*：", name.strip())[0].strip()
        if base:
            return base
    ref = tags.get("ref")
    operator = tags.get("operator")
    if isinstance(ref, str) and ref.strip():
        return f"{operator} {ref}".strip() if isinstance(operator, str) else ref.strip()
    return None


def assist_stations(
    package: Any,
    proposal: dict[str, Any],
    *,
    stations: Sequence[Mapping[str, Any]],
    lines: Mapping[str, Sequence[str]],
    walk_threshold_meters: float = DEFAULT_WALK_THRESHOLD_METERS,
    max_access_meters: float = DEFAULT_MAX_ACCESS_METERS,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill missing route ends with stations and classify area transitions.

    Existing ``routeEnds`` (for example a fixed summit finish from day-route
    planning) are kept. Groups whose ends change lose their route evaluation
    and must be refined again.
    """
    point_by_id = {point["id"]: point for point in package.plan["points"]}
    groups = [
        group
        for group in proposal.get("groups", [])
        if isinstance(group, dict) and group.get("kind", "route") == "route"
    ]
    station_list = [dict(item) for item in stations]
    for station in station_list:
        station["lines"] = list(lines.get(str(station.get("id")), []))
    before = {group["id"]: deepcopy(group.get("routeEnds")) for group in groups}
    transitions: list[dict[str, Any]] = []
    warnings: list[str] = []
    used: dict[str, dict[str, Any]] = {}

    def coordinates(group: Mapping[str, Any]) -> list[tuple[float, float]]:
        return [
            (float(point_by_id[point_id]["latitude"]), float(point_by_id[point_id]["longitude"]))
            for point_id in group.get("pointIds", [])
        ]

    def near(group: Mapping[str, Any], which: str = "any") -> list[tuple[float, dict[str, Any]]]:
        points = coordinates(group)
        if which == "first":
            points = points[:1]
        elif which == "last":
            points = points[-1:]
        ranked = []
        for station in station_list:
            distance = min(
                haversine_meters(lat, lon, float(station["latitude"]), float(station["longitude"]))
                for lat, lon in points
            )
            if distance <= max_access_meters:
                ranked.append((distance, station))
        return sorted(ranked, key=lambda item: (item[0], item[1]["label"]))

    def station_end(station: Mapping[str, Any]) -> dict[str, Any]:
        used[station["label"]] = station
        return {
            "kind": "station",
            "name": station["label"],
            "latitude": float(station["latitude"]),
            "longitude": float(station["longitude"]),
            "stationId": station.get("id"),
        }

    def ends_of(group: dict[str, Any]) -> dict[str, Any]:
        ends = group.get("routeEnds")
        if not isinstance(ends, dict):
            ends = {"start": None, "end": None}
            group["routeEnds"] = ends
        ends.setdefault("start", None)
        ends.setdefault("end", None)
        return ends

    def day_of(group: Mapping[str, Any]) -> Any:
        day = group.get("day")
        return day.get("label") if isinstance(day, Mapping) else None

    for position, group in enumerate(groups):
        previous = groups[position - 1] if position else None
        ends = ends_of(group)
        new_day = previous is None or (
            day_of(group) is not None and day_of(previous) != day_of(group)
        )
        if new_day:
            if previous is not None:
                transitions.append(
                    {"fromGroupId": previous["id"], "toGroupId": group["id"], "mode": "day_break"}
                )
                previous_ends = ends_of(previous)
                if previous_ends.get("end") is None:
                    candidates = near(previous, "any")
                    if candidates:
                        previous_ends["end"] = station_end(candidates[0][1])
            if ends.get("start") is None:
                candidates = near(group, "any")
                if candidates:
                    ends["start"] = station_end(candidates[0][1])
                else:
                    warnings.append(f"{group.get('name')}: no station within {max_access_meters:.0f} m.")
            continue
        gap = min(
            haversine_meters(*a, *b) for a in coordinates(previous) for b in coordinates(group)
        )
        if gap <= walk_threshold_meters:
            transitions.append(
                {
                    "fromGroupId": previous["id"],
                    "toGroupId": group["id"],
                    "mode": "walk",
                    "straightMeters": round(gap, 1),
                }
            )
            if ends.get("start") is None and ends_of(previous).get("end") is None:
                ends["start"] = {"kind": "previous-group"}
            continue
        exits = near(previous)
        entries = near(group)
        best: tuple[int, float, dict[str, Any], dict[str, Any], list[str]] | None = None
        for exit_distance, exit_station in exits:
            for entry_distance, entry_station in entries:
                shared = sorted(set(exit_station["lines"]) & set(entry_station["lines"]))
                key = (0 if shared else 1, exit_distance + entry_distance)
                if best is None or key < (best[0], best[1]):
                    best = (key[0], key[1], exit_station, entry_station, shared)
        transition: dict[str, Any] = {
            "fromGroupId": previous["id"],
            "toGroupId": group["id"],
            "straightMeters": round(gap, 1),
            "source": {
                "provider": "openstreetmap_overpass",
                "scope": "geography_only",
            },
        }
        if best is None:
            transition["mode"] = "transit_unknown"
            warnings.append(
                f"{previous.get('name')} → {group.get('name')}: no station pair within "
                f"{max_access_meters:.0f} m."
            )
        else:
            _, _, exit_station, entry_station, shared = best
            transition.update(
                {
                    "mode": "rail_candidate" if shared else "transit_unknown",
                    "fromStation": exit_station["label"],
                    "toStation": entry_station["label"],
                    "lines": shared,
                    "fromStationLines": exit_station["lines"],
                    "toStationLines": entry_station["lines"],
                }
            )
            previous_ends = ends_of(previous)
            if previous_ends.get("end") is None:
                previous_ends["end"] = station_end(exit_station)
            if ends.get("start") is None:
                ends["start"] = station_end(entry_station)
        transitions.append(transition)
    if groups:
        last_ends = ends_of(groups[-1])
        if last_ends.get("end") is None:
            candidates = near(groups[-1], "any")
            if candidates:
                last_ends["end"] = station_end(candidates[0][1])
    for group in groups:
        ends = group.get("routeEnds")
        if isinstance(ends, dict) and ends.get("start") is None and ends.get("end") is None:
            group.pop("routeEnds", None)
    changed = [
        group["id"]
        for group in groups
        if before.get(group["id"]) != group.get("routeEnds")
    ]
    for group in groups:
        if group["id"] in changed:
            group.pop("routeEvaluation", None)
            group.pop("access", None)
    proposal["transitions"] = transitions
    proposal["stationAssist"] = {
        **empty_station_assist(),
        "status": "planned",
        "plannedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "walkThresholdMeters": walk_threshold_meters,
        "maxAccessMeters": max_access_meters,
        "stations": [
            {
                "label": station["label"],
                "name": station.get("name"),
                "operator": station.get("operator"),
                "latitude": float(station["latitude"]),
                "longitude": float(station["longitude"]),
                "lines": station.get("lines", []),
            }
            for station in sorted(used.values(), key=lambda item: item["label"])
        ],
        "source": dict(source or {}),
    }
    review = proposal.setdefault("review", {})
    review["routesConfirmed"] = False
    review["routesConfirmedAt"] = None
    return {
        "changedGroupIds": changed,
        "transitions": {
            mode: sum(1 for item in transitions if item["mode"] == mode)
            for mode in sorted({item["mode"] for item in transitions})
        },
        "warnings": warnings,
    }


def draft_notes_spec(
    proposal: Mapping[str, Any],
    *,
    checked_at: str,
    timezone_name: str,
    title: str = "巡礼行程建议",
) -> dict[str, Any]:
    """Return a journey-notes skeleton built from station transitions.

    Durations and fares stay empty: fill them from official sources or the
    user before ``prepare-notes``; the renderer refuses non-walking segments
    without a named source.
    """
    groups = {
        group["id"]: group
        for group in proposal.get("groups", [])
        if isinstance(group, Mapping)
    }
    segments: list[dict[str, Any]] = []
    summary: list[str] = []
    day_ranges: dict[str, list[str]] = {}
    for group in groups.values():
        day = group.get("day")
        if isinstance(day, Mapping) and group.get("kind", "route") == "route":
            day_ranges.setdefault(str(day.get("label")), []).append(group["id"])
    for label, ids in day_ranges.items():
        first = groups[ids[0]].get("routeEnds") or {}
        last = groups[ids[-1]].get("routeEnds") or {}
        start = (first.get("start") or {}).get("name") if isinstance(first, Mapping) else None
        finish = (last.get("end") or {}).get("name") if isinstance(last, Mapping) else None
        parts = [label]
        if start:
            parts.append(f"从 {start} 出发")
        if finish:
            parts.append(f"在 {finish} 结束")
        summary.append("，".join(parts))
    for transition in proposal.get("transitions", []):
        if not isinstance(transition, Mapping) or transition.get("mode") not in {
            "rail_candidate",
            "transit_unknown",
        }:
            continue
        origin = groups.get(transition.get("fromGroupId"), {})
        target = groups.get(transition.get("toGroupId"), {})
        from_station = transition.get("fromStation") or "（待确认）"
        to_station = transition.get("toStation") or "（待确认）"
        lines = list(transition.get("lines") or [])
        segment: dict[str, Any] = {
            "from": f"{origin.get('name')}（{from_station}）",
            "to": f"{target.get('name')}（{to_station}）",
            "mode": "rail" if lines else "other",
            "instructions": [f"{from_station} 乘车，{to_station} 下车"],
            "cautions": [] if lines else ["两站不在同一条已知线路上，请确认换乘方式"],
            "actionItems": ["出发前在运营商官网查询时刻、所需时间与票价"],
            "source": {
                "provider": "OpenStreetMap（仅车站与线路的地理信息）",
                "checkedAt": checked_at,
            },
            "confidence": "low",
        }
        if lines:
            segment["service"] = " / ".join(lines)
        segments.append(segment)
    return {
        "schemaVersion": 1,
        "title": title,
        "timezone": timezone_name,
        "checkedAt": checked_at,
        "includeAreaWalking": True,
        "summaryNotes": summary,
        "segments": segments,
        "globalCautions": [],
        "actionItems": [],
    }


def _merge_boxes(
    boxes: Sequence[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    merged: list[list[float]] = []
    for south, west, north, east in sorted(boxes):
        for box in merged:
            if not (north < box[0] or south > box[2] or east < box[1] or west > box[3]):
                box[0], box[1] = min(box[0], south), min(box[1], west)
                box[2], box[3] = max(box[2], north), max(box[3], east)
                break
        else:
            merged.append([south, west, north, east])
    return [tuple(box) for box in merged]


def _normalize(value: str) -> str:
    return "".join(
        char for char in value.casefold() if char.isalnum() and char not in {"駅", "站"}
    )
