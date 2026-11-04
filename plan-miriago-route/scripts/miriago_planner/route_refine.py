"""Road-network refinement of confirmed areas with any walking backend."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .clustering import GeoPoint, bundle_points
from .proposal import (
    record_group_route_evaluation,
    update_group_with_walking_matrix,
    update_walking_source_summary,
)
from .public_services import MAX_WALKING_MATRIX_POINTS, PublicServiceError
from .routing import backtrack_ratio, count_crossings, find_spurs
from .walking import EndSpec, Site, route_polyline, sequence_stops

DEFAULT_BUNDLE_METERS = 15.0


def stops_for_points(
    point_ids: Sequence[str],
    point_by_id: Mapping[str, Mapping[str, Any]],
    bundle_meters: float,
) -> list[list[str]]:
    """Bundle a group's points into stops, keeping the group order inside each."""
    order = {point_id: index for index, point_id in enumerate(point_ids)}
    bundled = bundle_points(
        [
            GeoPoint(
                key=point_id,
                latitude=float(point_by_id[point_id]["latitude"]),
                longitude=float(point_by_id[point_id]["longitude"]),
                point_ids=(point_id,),
            )
            for point_id in point_ids
        ],
        bundle_meters,
    )
    stops = [sorted(stop.point_ids, key=order.get) for stop in bundled]
    return sorted(stops, key=lambda members: order[members[0]])


def stop_site(stop: Sequence[str], point_by_id: Mapping[str, Mapping[str, Any]]) -> Site:
    return Site(
        sum(float(point_by_id[point_id]["latitude"]) for point_id in stop) / len(stop),
        sum(float(point_by_id[point_id]["longitude"]) for point_id in stop) / len(stop),
    )


def via_rules_for(
    stops: Sequence[Sequence[str]],
    point_by_id: Mapping[str, Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
) -> dict[int, Site]:
    """Map stop index → forced via site from ``constraints.via`` rules."""
    result: dict[int, Site] = {}
    for rule in rules:
        target_ids = set(rule.get("pointIds") or [])
        needle = rule.get("nameContains")
        best: tuple[int, int] | None = None
        for index, stop in enumerate(stops):
            hits = sum(
                1
                for point_id in stop
                if point_id in target_ids
                or (
                    isinstance(needle, str)
                    and needle
                    and needle in str(point_by_id[point_id].get("name") or "")
                )
            )
            if hits and (best is None or hits > best[0]):
                best = (hits, index)
        if best is not None:
            result[best[1]] = Site(float(rule["latitude"]), float(rule["longitude"]))
    return result


def end_spec(
    end: Mapping[str, Any] | None,
    stops: Sequence[Sequence[str]],
    previous_site: Site | None,
    previous_name: str | None,
) -> EndSpec:
    if not end:
        return EndSpec()
    kind = end.get("kind")
    if kind in {"station", "location"}:
        return EndSpec(
            kind="site",
            site=Site(float(end["latitude"]), float(end["longitude"])),
            label=str(end.get("name") or ""),
        )
    if kind == "point":
        for index, stop in enumerate(stops):
            if end.get("pointId") in stop:
                return EndSpec(kind="stop", stop=index)
        return EndSpec()
    if kind == "previous-group" and previous_site is not None:
        return EndSpec(kind="site", site=previous_site, label=f"上一片区：{previous_name}")
    return EndSpec()


def refine_groups(
    package: Any,
    proposal: dict[str, Any],
    *,
    group_ids: set[str],
    backend: Any,
    restarts: int = 48,
    progress: Callable[[dict[str, Any]], None] | None = None,
    geometry: dict[str, Any] | None = None,
    spur_threshold_meters: float = 400,
) -> dict[str, Any]:
    point_by_id = {point["id"]: point for point in package.plan["points"]}
    bundle_meters = float(
        (proposal.get("profile") or {}).get("bundleDistanceMeters") or DEFAULT_BUNDLE_METERS
    )
    via_rules = list((proposal.get("constraints") or {}).get("via") or [])
    provider = backend.provider
    refined: list[str] = []
    skipped: list[dict[str, str]] = []
    quality_flags: list[dict[str, Any]] = []
    previous_site: Site | None = None
    previous_name: str | None = None
    previous_day: Any = None
    groups = [group for group in proposal.get("groups", []) if isinstance(group, dict)]
    selected = [group for group in groups if group["id"] in group_ids]
    for group in groups:
        kind = group.get("kind", "route")
        day = (group.get("day") or {}).get("label") if isinstance(group.get("day"), Mapping) else None
        if day != previous_day:
            previous_site, previous_name = None, None
        previous_day = day
        if group["id"] not in group_ids:
            if kind == "route" and group.get("pointIds"):
                previous_site = stop_site([group["pointIds"][-1]], point_by_id)
                previous_name = group.get("name")
            continue
        if progress is not None:
            progress(
                {
                    "event": "refine_progress",
                    "completed": len(refined) + len(skipped),
                    "total": len(selected),
                    "groupName": group.get("name"),
                }
            )
        if kind != "route":
            reason = f"non_route_group:{kind}"
            skipped.append({"groupId": group["id"], "reason": reason})
            record_group_route_evaluation(
                proposal, group["id"], status="skipped", provider=provider, reason=reason
            )
            continue
        point_ids = list(group.get("pointIds", []))
        ends = group.get("routeEnds") if isinstance(group.get("routeEnds"), Mapping) else {}
        has_external = any(
            isinstance(ends.get(side), Mapping)
            and ends[side].get("kind") in {"station", "location", "previous-group"}
            for side in ("start", "end")
        )
        if len(point_ids) < 2 and not has_external:
            reason = "fewer_than_2_points"
            skipped.append({"groupId": group["id"], "reason": reason})
            record_group_route_evaluation(
                proposal, group["id"], status="skipped", provider=provider, reason=reason
            )
            previous_site = stop_site(point_ids, point_by_id) if point_ids else previous_site
            previous_name = group.get("name")
            continue
        stops = stops_for_points(point_ids, point_by_id, bundle_meters)
        sites = [stop_site(stop, point_by_id) for stop in stops]
        via = via_rules_for(stops, point_by_id, via_rules)
        if backend.name == "valhalla" and len(sites) + len(via) + 2 > MAX_WALKING_MATRIX_POINTS:
            reason = f"more_than_{MAX_WALKING_MATRIX_POINTS - 2}_stops_use_osm_backend"
            skipped.append({"groupId": group["id"], "reason": reason})
            record_group_route_evaluation(
                proposal, group["id"], status="skipped", provider=provider, reason=reason
            )
            previous_site = sites[-1]
            previous_name = group.get("name")
            continue
        start = end_spec(ends.get("start"), stops, previous_site, previous_name)
        finish = end_spec(ends.get("end"), stops, None, None)
        try:
            result = sequence_stops(
                backend,
                sites,
                start=start,
                end=finish,
                via=via,
                restarts=restarts,
            )
        except PublicServiceError as exc:
            reason = str(exc)
            skipped.append({"groupId": group["id"], "reason": reason})
            record_group_route_evaluation(
                proposal, group["id"], status="failed", provider=provider, reason=reason
            )
            previous_site = sites[-1]
            previous_name = group.get("name")
            continue
        ordered_ids = [point_id for index in result.order for point_id in stops[index]]
        distance = result.total_meters
        access: dict[str, Any] = {}
        start_end = ends.get("start") if isinstance(ends.get("start"), Mapping) else None
        finish_end = ends.get("end") if isinstance(ends.get("end"), Mapping) else None
        if start_end and start_end.get("kind") == "station":
            access["entry"] = {
                "station": start_end.get("name"),
                "walkMeters": round(result.start_meters, 1),
            }
        if finish_end and finish_end.get("kind") == "station":
            access["exit"] = {
                "station": finish_end.get("name"),
                "walkMeters": round(result.end_meters, 1),
            }
        ordered_sites = [sites[index] for index in result.order]
        weights = [len(stop) for stop in stops]
        spurs = find_spurs(
            result.order,
            result.stop_matrix,
            weights,
            threshold_meters=spur_threshold_meters,
        )
        quality: dict[str, Any] = {
            "crossings": count_crossings([(site.longitude, site.latitude) for site in ordered_sites]),
            "spurs": [
                {
                    "pointIds": stops[result.order[item["position"]]],
                    "detourMeters": item["detourMeters"],
                }
                for item in spurs
            ],
        }
        if geometry is not None:
            coordinates = route_polyline(backend, result)
            quality["backtrackRatio"] = backtrack_ratio([[tuple(item) for item in coordinates]])
            geometry.setdefault("groups", {})[group["id"]] = {
                "pointIds": ordered_ids,
                "routeEnds": ends or None,
                "provider": provider,
                "coordinates": [[round(lon, 6), round(lat, 6)] for lon, lat in coordinates],
            }
        if quality["crossings"] or quality["spurs"]:
            quality_flags.append({"groupId": group["id"], "groupName": group.get("name"), **quality})
        update_group_with_walking_matrix(
            proposal,
            group["id"],
            ordered_ids,
            distance_meters=distance,
            duration_seconds=distance * result.seconds_per_meter,
            provider=provider,
            extra_metrics={
                "routedStopCount": len(stops),
                "accessMeters": round(result.start_meters + result.end_meters, 1),
            },
            access=access or None,
            quality=quality,
        )
        if result.warnings:
            group.setdefault("warnings", []).extend(
                warning for warning in result.warnings if warning not in group.get("warnings", [])
            )
        refined.append(group["id"])
        previous_site = sites[result.order[-1]]
        previous_name = group.get("name")
    update_walking_source_summary(proposal, provider=provider)
    return {"refinedGroupIds": refined, "skipped": skipped, "qualityFlags": quality_flags}
