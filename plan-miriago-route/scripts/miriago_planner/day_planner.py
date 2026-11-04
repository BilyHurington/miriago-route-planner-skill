"""Day-based route areas: one continuous walk per pool, cut into days and areas.

A pool is a set of points walked as one open path (for example a town centre).
The path is optimised on the chosen walking backend, optionally split into
several days at station-friendly gaps, and every day is cut into contiguous
areas of a target duration with an exact dynamic programme. Adjacent areas
share their boundary, so the plan has no crossings between areas and no
detours back to a single far point.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .clustering import haversine_meters
from .package_io import LoadedPackage, has_valid_coordinate
from .proposal import ProposalError, build_group, proposal_shell
from .public_services import PublicServiceError
from .route_refine import stop_site, stops_for_points, via_rules_for
from .routing import backtrack_ratio, contiguous_cuts, count_crossings, find_spurs
from .walking import EndSpec, SequenceResult, Site, sequence_stops

CONFIG_SCHEMA_VERSION = 1
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
SAME_LANDMARK_PENALTY_SECONDS = 1_800
DEFAULTS: dict[str, Any] = {
    "bundleMeters": 30,
    "landmarkMeters": 120,
    "landmarkGenericNames": [],
    "speedMetersPerSecond": 1.2,
    "shootSecondsPerPoint": 60,
    "stopOverheadSeconds": 60,
    "areaTargetMinutes": 60,
    "maxAreasPerDay": 12,
    "gapBonus": 1.0,
    "localCorrectionMeters": 150,
    "restarts": 64,
    "stationSearchMeters": 1_500,
    "dayStationWeight": 1.0,
    "nameFormat": "{day}-{index} {title}",
    "uncovered": "error",
}


class DayPlanError(ProposalError):
    """Raised when a day-route configuration cannot be planned."""


def load_config(path: str | Path) -> dict[str, Any]:
    try:
        config = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DayPlanError(f"Cannot read day-route config {path}: {exc}") from exc
    if not isinstance(config, dict) or config.get("schemaVersion") != CONFIG_SCHEMA_VERSION:
        raise DayPlanError(f"Day-route config must be an object with schemaVersion {CONFIG_SCHEMA_VERSION}.")
    pools = config.get("pools")
    if not isinstance(pools, list) or not pools:
        raise DayPlanError("Day-route config requires a non-empty pools list.")
    for index, pool in enumerate(pools):
        if not isinstance(pool, Mapping) or not isinstance(pool.get("select"), Mapping):
            raise DayPlanError(f"pools[{index}].select must be an object.")
        days = pool.get("days")
        if not isinstance(days, list) or not days or any(
            not isinstance(day, Mapping) or not str(day.get("label") or "").strip() for day in days
        ):
            raise DayPlanError(f"pools[{index}].days must list objects with a label.")
    for index, special in enumerate(config.get("specialGroups", [])):
        if (
            not isinstance(special, Mapping)
            or special.get("kind") not in {"restricted", "unverified"}
            or not str(special.get("name") or "").strip()
            or not isinstance(special.get("select"), Mapping)
        ):
            raise DayPlanError(
                f"specialGroups[{index}] needs kind restricted|unverified, a name, and select."
            )
    if config.get("uncovered", "error") not in {"error", "keep-base-groups"}:
        raise DayPlanError("uncovered must be error or keep-base-groups.")
    return config


def select_points(
    selector: Mapping[str, Any],
    candidates: Sequence[str],
    point_by_id: Mapping[str, Mapping[str, Any]],
    base_groups: Mapping[str, Sequence[str]],
) -> list[str]:
    """Resolve a selector against still-unassigned point IDs.

    Supported keys (combined as a union, then narrowed by ``works``):
    ``groups`` (base-proposal group names or IDs), ``pointIds``, ``bbox``
    ``[south, west, north, east]``, ``nameContains`` (list), ``remaining``.
    """
    available = list(candidates)
    available_set = set(available)
    chosen: set[str] = set()
    for name in selector.get("groups", []) or []:
        if name not in base_groups:
            raise DayPlanError(f"Unknown base group in selector: {name!r}")
        chosen.update(point_id for point_id in base_groups[name] if point_id in available_set)
    chosen.update(point_id for point_id in selector.get("pointIds", []) or [] if point_id in available_set)
    bbox = selector.get("bbox")
    if bbox:
        south, west, north, east = (float(value) for value in bbox)
        chosen.update(
            point_id
            for point_id in available
            if south <= float(point_by_id[point_id]["latitude"]) <= north
            and west <= float(point_by_id[point_id]["longitude"]) <= east
        )
    needles = selector.get("nameContains") or []
    if isinstance(needles, str):
        needles = [needles]
    for needle in needles:
        chosen.update(
            point_id
            for point_id in available
            if needle and needle in str(point_by_id[point_id].get("name") or "")
        )
    if selector.get("remaining"):
        chosen.update(available)
    works = selector.get("works")
    if works:
        chosen = {point_id for point_id in chosen if point_by_id[point_id].get("workId") in set(works)}
    return [point_id for point_id in available if point_id in chosen]


def plan_days(
    package: LoadedPackage,
    config: Mapping[str, Any],
    *,
    backend: Any,
    station_provider: Callable[[list[tuple[float, float]]], Sequence[Mapping[str, Any]]] | None = None,
    station_source: Mapping[str, Any] | None = None,
    base_proposal: Mapping[str, Any] | None = None,
    modules: Sequence[str] = (),
    geometry: dict[str, Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    settings = {**DEFAULTS, **{key: value for key, value in config.items() if key in DEFAULTS}}
    points = package.plan["points"]
    point_by_id = {point["id"]: point for point in points}
    base_groups: dict[str, list[str]] = {}
    curated: list[dict[str, Any]] = []
    if base_proposal:
        for group in base_proposal.get("groups", []):
            if isinstance(group, Mapping):
                base_groups[str(group.get("name"))] = list(group.get("pointIds", []))
                base_groups[str(group.get("id"))] = list(group.get("pointIds", []))
        curated = [
            dict(item)
            for item in base_proposal.get("excludedPoints", [])
            if isinstance(item, Mapping) and item.get("kind") == "curation"
        ]
    excluded_ids = {item["pointId"] for item in curated}
    remaining = [
        point["id"]
        for point in points
        if has_valid_coordinate(point) and point["id"] not in excluded_ids
    ]
    groups: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []

    specials: list[dict[str, Any]] = []
    for special in config.get("specialGroups", []):
        ids = select_points(special["select"], remaining, point_by_id, base_groups)
        if not ids:
            continue
        remaining = [point_id for point_id in remaining if point_id not in set(ids)]
        specials.append(
            build_group(
                package.plan["id"],
                str(special["name"]),
                ids,
                point_by_id,
                kind=str(special["kind"]),
                note=special.get("note"),
            )
        )

    via_rules = [dict(rule) for rule in config.get("via", [])]
    day_counter: dict[str, int] = {}
    day_index: dict[str, int] = {}
    for pool_index, pool in enumerate(config["pools"]):
        ids = select_points(pool["select"], remaining, point_by_id, base_groups)
        if not ids:
            raise DayPlanError(f"pools[{pool_index}] selects no unassigned points.")
        remaining = [point_id for point_id in remaining if point_id not in set(ids)]
        if progress is not None:
            progress({"event": "day_plan_progress", "pool": pool_index + 1, "total": len(config["pools"]), "points": len(ids)})
        stops = _landmark_merge(
            stops_for_points(ids, point_by_id, float(settings["bundleMeters"])),
            point_by_id,
            float(settings["landmarkMeters"]),
            set(settings["landmarkGenericNames"]),
        )
        sites = [stop_site(stop, point_by_id) for stop in stops]
        wants_stations = len(pool["days"]) > 1 or any(
            str(pool.get(side) or "").startswith(("auto", "station:")) for side in ("start", "end")
        )
        station_sites = []
        if wants_stations and station_provider is not None:
            station_sites = [
                (str(station["label"]), Site(float(station["latitude"]), float(station["longitude"])))
                for station in station_provider([site.coordinate for site in sites])
            ]
        start = _end_spec(pool.get("start"), stops, point_by_id, station_sites)
        end = _end_spec(pool.get("end"), stops, point_by_id, station_sites)
        via = via_rules_for(stops, point_by_id, via_rules)
        result = sequence_stops(
            backend,
            sites,
            start=start,
            end=end,
            via=via,
            restarts=int(settings["restarts"]),
            local_correction_meters=float(settings["localCorrectionMeters"]),
            station_candidates=station_sites if len(pool["days"]) > 1 else (),
        )
        ordered = [stops[index] for index in result.order]
        speed = float(pool.get("speedMetersPerSecond") or settings["speedMetersPerSecond"])
        stop_seconds = [
            len(stop) * float(settings["shootSecondsPerPoint"]) + float(settings["stopOverheadSeconds"])
            for stop in ordered
        ]
        gap_seconds = [meters / speed for meters in result.inner_meters]
        days = list(pool["days"])
        nearest = [result.nearest_candidate[index] for index in result.order]
        day_cuts: list[int] = []
        if len(days) > 1:
            penalty = [
                (nearest[position][0] + nearest[position + 1][0] - result.inner_meters[position])
                / speed
                * float(settings["dayStationWeight"])
                if station_sites
                else 0.0
                for position in range(len(ordered) - 1)
            ]
            day_cuts = contiguous_cuts(
                stop_seconds,
                gap_seconds,
                len(days),
                cut_penalty=penalty,
                gap_bonus=float(settings["gapBonus"]),
            )
        bounds = [0, *day_cuts, len(ordered)]
        for day_position, (first, last) in enumerate(zip(bounds, bounds[1:])):
            day = days[day_position]
            label = str(day["label"])
            if label not in day_index:
                day_index[label] = len(day_index)
            day_start = _day_end_info(
                result, start, first, day_position == 0, nearest, stops, side="start"
            )
            day_end = _day_end_info(
                result, end, last - 1, day_position == len(days) - 1, nearest, stops, side="end"
            )
            span_seconds = stop_seconds[first:last]
            span_gaps = gap_seconds[first : last - 1]
            target = float(pool.get("areaTargetMinutes") or day.get("areaTargetMinutes") or settings["areaTargetMinutes"]) * 60
            total = sum(span_seconds) + sum(span_gaps)
            fixed = day.get("areas") or pool.get("areas")
            if pool.get("split") is False:
                count = 1
            elif fixed:
                count = int(fixed)
            else:
                count = round(total / target) if target > 0 else 1
            count = max(1, min(count, int(settings["maxAreasPerDay"]), last - first))
            names = [_base_names(stop, point_by_id) for stop in ordered[first:last]]
            landmark_penalty = [
                SAME_LANDMARK_PENALTY_SECONDS
                if _share_landmark(names[position], names[position + 1], set(settings["landmarkGenericNames"]))
                else 0.0
                for position in range(len(names) - 1)
            ]
            area_cuts = contiguous_cuts(
                span_seconds,
                span_gaps,
                count,
                cut_penalty=landmark_penalty,
                gap_bonus=float(settings["gapBonus"]),
            )
            area_bounds = [first, *[first + cut for cut in area_cuts], last]
            for area_position, (a, b) in enumerate(zip(area_bounds, area_bounds[1:])):
                day_counter[label] = day_counter.get(label, 0) + 1
                number = day_counter[label]
                key = f"{label}-{CIRCLED[number - 1] if number <= len(CIRCLED) else number}"
                title = (config.get("names") or {}).get(key) or _title(ordered[a:b], point_by_id)
                name = str(settings["nameFormat"]).format(day=label, index=key.split("-", 1)[1], title=title, key=key)
                walk_legs = list(result.inner_meters[a : b - 1])
                leg_indices = [result.order[position] for position in range(a, b)]
                ends: dict[str, Any] = {"start": None, "end": None}
                if area_position == 0:
                    if day_start is not None:
                        ends["start"] = day_start["end"]
                        walk_legs.insert(0, day_start["meters"])
                else:
                    ends["start"] = {"kind": "previous-group"}
                    walk_legs.insert(0, result.inner_meters[a - 1])
                if area_position == len(area_bounds) - 2 and day_end is not None:
                    ends["end"] = day_end["end"]
                    walk_legs.append(day_end["meters"])
                if ends["start"] is None and ends["end"] is None:
                    ends_value = None
                else:
                    ends_value = ends
                walk = sum(walk_legs)
                minutes = (walk / speed + sum(stop_seconds[a:b])) / 60
                point_ids = [point_id for stop in ordered[a:b] for point_id in stop]
                summary = (
                    f"【路线规划】{b - a} 个机位，步行约 {walk / 1000:.1f} km / {round(walk / speed / 60)} 分钟，"
                    f"含拍摄约 {round(minutes)} 分钟（{backend.provider}，按 {speed * 3.6:.1f} km/h；"
                    f"拍摄按每点 {round(float(settings['shootSecondsPerPoint']) / 60, 1):g} 分钟、"
                    f"每机位另加 {round(float(settings['stopOverheadSeconds']) / 60, 1):g} 分钟估算）。"
                )
                note_lines = [
                    summary,
                    f"【{label}】{day.get('theme') or ''}".rstrip(),
                ]
                if area_position == 0 and day_start is not None and day_start.get("label"):
                    note_lines.append(f"当天从 {day_start['label']} 出发。")
                if area_position == len(area_bounds) - 2 and day_end is not None and day_end.get("label"):
                    note_lines.append(f"当天在 {day_end['label']} 结束。")
                extra = (config.get("notes") or {}).get(key)
                if extra:
                    note_lines.append(str(extra))
                anchor = None
                if ends_value and isinstance(ends_value.get("start"), Mapping) and ends_value["start"].get("kind") == "station":
                    station = ends_value["start"]
                    anchor = {
                        "name": station["name"],
                        "latitude": station["latitude"],
                        "longitude": station["longitude"],
                        "pointId": None,
                        "kind": "station",
                    }
                group = build_group(
                    package.plan["id"],
                    name,
                    point_ids,
                    point_by_id,
                    note="\n".join(note_lines),
                    anchor=anchor,
                )
                group["day"] = {"label": label, "index": day_index[label], "theme": day.get("theme")}
                if ends_value:
                    group["routeEnds"] = ends_value
                stop_coords = [(sites[index].longitude, sites[index].latitude) for index in leg_indices]
                spurs = find_spurs(
                    list(range(b - a)),
                    [[result.stop_matrix[i][j] for j in leg_indices] for i in leg_indices],
                    [len(stop) for stop in ordered[a:b]],
                )
                quality: dict[str, Any] = {
                    "crossings": count_crossings(stop_coords),
                    "spurs": [
                        {"pointIds": ordered[a + item["position"]], "detourMeters": item["detourMeters"]}
                        for item in spurs
                    ],
                }
                group["metrics"].update(
                    {
                        "plannedWalkingMeters": round(walk, 1),
                        "plannedMinutes": round(minutes, 1),
                        "plannedStopCount": b - a,
                        "plannedProvider": backend.provider,
                    }
                )
                if geometry is not None:
                    coordinates = _area_polyline(backend, result, a, b, area_position, day_start, day_end, len(area_bounds) - 2)
                    quality["backtrackRatio"] = backtrack_ratio([[tuple(item) for item in coordinates]])
                    geometry.setdefault("groups", {})[group["id"]] = {
                        "pointIds": point_ids,
                        "routeEnds": ends_value,
                        "provider": backend.provider,
                        "planned": True,
                        "coordinates": [[round(lon, 6), round(lat, 6)] for lon, lat in coordinates],
                    }
                group["quality"] = quality
                groups.append(group)
                report.append(
                    {
                        "name": name,
                        "points": len(point_ids),
                        "stops": b - a,
                        "walkMeters": round(walk),
                        "totalMinutes": round(minutes),
                        **{key: value for key, value in quality.items() if key != "spurs"},
                        "spurs": len(quality["spurs"]),
                    }
                )
    warnings: list[str] = []
    if remaining:
        if config.get("uncovered", "error") == "keep-base-groups" and base_proposal:
            left = set(remaining)
            for base in base_proposal.get("groups", []):
                if not isinstance(base, Mapping):
                    continue
                ids = [point_id for point_id in base.get("pointIds", []) if point_id in left]
                if ids:
                    kept = build_group(
                        package.plan["id"],
                        str(base.get("name")),
                        ids,
                        point_by_id,
                        kind=str(base.get("kind") or "route"),
                        note=base.get("note"),
                    )
                    groups.append(kept)
                    left -= set(ids)
            if left:
                raise DayPlanError(f"Points not covered by any pool or base group: {sorted(left)}")
            warnings.append(f"{len(remaining)} points kept their base-proposal groups.")
        else:
            raise DayPlanError(
                "Points not covered by any pool or special group: "
                f"{sorted(remaining)[:20]}{' …' if len(remaining) > 20 else ''} "
                f"({len(remaining)} total). Add a pool with select.remaining or set "
                "uncovered to keep-base-groups."
            )
    groups.extend(specials)
    _ensure_unique_names(groups)
    proposal = proposal_shell(
        package,
        groups=groups,
        profile={
            "name": "day-route",
            "bundleDistanceMeters": float(settings["bundleMeters"]),
            "landmarkMeters": float(settings["landmarkMeters"]),
            "areaTargetMinutes": float(settings["areaTargetMinutes"]),
            "speedMetersPerSecond": float(settings["speedMetersPerSecond"]),
        },
        data_sources={
            "geometry": {"provider": backend.provider, "method": "day_route_open_path_dp"},
            "stations": dict(station_source or {"provider": "not_requested"}),
            "walking": {
                "provider": "local",
                "method": "haversine_path_x_1.25",
                "status": "rough_estimate",
                "planning": backend.provider,
            },
        },
        modules=sorted({*modules, "day-route"}),
        constraints={"via": via_rules},
        warnings=warnings,
    )
    if curated:
        proposal["excludedPoints"] = curated
        review = (base_proposal or {}).get("review") or {}
        proposal["review"]["curationConfirmed"] = review.get("curationConfirmed") is True
        proposal["review"]["curationConfirmedAt"] = review.get("curationConfirmedAt")
    return proposal, report


def _end_spec(
    value: Any,
    stops: Sequence[Sequence[str]],
    point_by_id: Mapping[str, Mapping[str, Any]],
    stations: Sequence[tuple[str, Site]],
) -> EndSpec:
    if value in (None, "", "free"):
        return EndSpec()
    text = str(value)
    if text == "auto":
        if not stations:
            raise DayPlanError(
                "start/end auto found no station candidates near the pool "
                "(Overpass unavailable or no rail station within stationSearchMeters)."
            )
        return EndSpec(kind="auto", candidates=tuple(stations))
    kind, _, target = text.partition(":")
    if kind == "station":
        for label, site in stations:
            if label == target or label.split("（", 1)[0] == target:
                return EndSpec(kind="site", site=site, label=label)
        raise DayPlanError(f"Unknown station {target!r}; available: {[label for label, _ in stations][:20]}")
    if kind in {"point", "name"}:
        best: tuple[int, int] | None = None
        for index, stop in enumerate(stops):
            hits = sum(
                1
                for point_id in stop
                if (kind == "point" and point_id == target)
                or (kind == "name" and target in str(point_by_id[point_id].get("name") or ""))
            )
            if hits and (best is None or hits > best[0]):
                best = (hits, index)
        if best is None:
            raise DayPlanError(f"No stop matches {text!r} in this pool.")
        return EndSpec(kind="stop", stop=best[1])
    if kind == "location":
        latitude, _, longitude = target.partition(",")
        return EndSpec(kind="site", site=Site(float(latitude), float(longitude)), label=text)
    raise DayPlanError(f"Unsupported start/end value: {text!r}")


def _day_end_info(
    result: SequenceResult,
    spec: EndSpec,
    position: int,
    pool_edge: bool,
    nearest: Sequence[tuple[float, str | None]],
    stops: Sequence[Sequence[str]],
    *,
    side: str,
) -> dict[str, Any] | None:
    """Describe how a day starts/ends: the pool's own end, or the nearest station."""
    if pool_edge and spec.kind in {"site", "auto"}:
        label = result.start_label if side == "start" else result.end_label
        site = result.start_site if side == "start" else result.end_site
        meters = result.start_meters if side == "start" else result.end_meters
        if site is None:
            return None
        is_location = spec.kind == "site" and (label or "").startswith("location:")
        return {
            "label": label,
            "meters": meters,
            "site": site,
            "end": {
                "kind": "location" if is_location else "station",
                "name": label or "",
                "latitude": site.latitude,
                "longitude": site.longitude,
            },
        }
    if pool_edge and spec.kind == "stop":
        stop = stops[result.order[position]]
        return {
            "label": None,
            "meters": 0.0,
            "site": None,
            "end": {"kind": "point", "pointId": stop[0]},
        }
    if pool_edge:
        return None
    meters, label = nearest[position]
    if label is None or label not in result.candidate_sites:
        return None
    site = result.candidate_sites[label]
    return {
        "label": label,
        "meters": meters,
        "site": site,
        "end": {"kind": "station", "name": label, "latitude": site.latitude, "longitude": site.longitude},
    }


def _area_polyline(
    backend: Any,
    result: SequenceResult,
    first: int,
    last: int,
    area_position: int,
    day_start: Mapping[str, Any] | None,
    day_end: Mapping[str, Any] | None,
    last_area: int,
) -> list[list[float]]:
    indices = [result.order[position] for position in range(first, last)]
    if area_position > 0:
        indices.insert(0, result.order[first - 1])
    sites = [result.sites[index] for index in indices]
    if area_position == 0 and day_start and day_start.get("site") is not None:
        sites.insert(0, day_start["site"])
    if area_position == last_area and day_end and day_end.get("site") is not None:
        sites.append(day_end["site"])
    via_by_site = {result.sites[stop]: result.sites[via] for stop, via in result.via.items()}
    expanded: list[Site] = []
    for position, site in enumerate(sites):
        if position:
            via_site = via_by_site.get(site) or via_by_site.get(sites[position - 1])
            if via_site is not None:
                expanded.append(via_site)
        expanded.append(site)
    try:
        return backend.polyline(expanded, None)
    except (PublicServiceError, OSError, ValueError):  # geometry is only a review aid
        return [[site.longitude, site.latitude] for site in expanded]


def _landmark_merge(
    stops: list[list[str]],
    point_by_id: Mapping[str, Mapping[str, Any]],
    limit_meters: float,
    generic: set[str],
) -> list[list[str]]:
    """Merge stops of the same landmark so one place is visited once."""
    if limit_meters <= 0:
        return stops
    merged = [list(stop) for stop in stops]
    changed = True
    while changed:
        changed = False
        for left in range(len(merged)):
            for right in range(left + 1, len(merged)):
                a, b = merged[left], merged[right]
                if not _share_landmark(_base_names(a, point_by_id), _base_names(b, point_by_id), generic):
                    continue
                if max(
                    haversine_meters(
                        float(point_by_id[p]["latitude"]),
                        float(point_by_id[p]["longitude"]),
                        float(point_by_id[q]["latitude"]),
                        float(point_by_id[q]["longitude"]),
                    )
                    for p in a
                    for q in b
                ) <= limit_meters:
                    merged[left] = a + b
                    del merged[right]
                    changed = True
                    break
            if changed:
                break
    return merged


def _base_names(stop: Sequence[str], point_by_id: Mapping[str, Mapping[str, Any]]) -> set[str]:
    return {
        re.split(r"[ 　（(・]", str(point_by_id[point_id].get("name") or "").replace("\u200b", ""))[0]
        for point_id in stop
    }


def _share_landmark(left: set[str], right: set[str], generic: set[str]) -> bool:
    for x in left:
        for y in right:
            length = 0
            while length < min(len(x), len(y)) and x[length] == y[length]:
                length += 1
            if length >= 3 and x[:length] not in generic:
                return True
    return False


def _title(stops: Sequence[Sequence[str]], point_by_id: Mapping[str, Mapping[str, Any]]) -> str:
    counter: Counter[str] = Counter()
    for stop in stops:
        for point_id in stop:
            name = re.split(r"[ 　（(]", str(point_by_id[point_id].get("name") or "").replace("\u200b", ""))[0]
            if name:
                counter[name] += 1
    return "・".join(name for name, _ in counter.most_common(3)) or "片区"


def _ensure_unique_names(groups: list[dict[str, Any]]) -> None:
    seen: dict[str, int] = {}
    for group in groups:
        key = group["name"].strip().casefold()
        if key in seen:
            seen[key] += 1
            group["name"] = f"{group['name']}（{seen[key]}）"
        else:
            seen[key] = 1


__all__ = ["DayPlanError", "load_config", "plan_days", "select_points"]
