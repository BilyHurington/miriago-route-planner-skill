"""Build a compact, interactive route preview without mutating plan data."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .journey_notes import route_fingerprint
from .package_io import LoadedPackage
from .proposal import validate_proposal
from .public_services import (
    DEFAULT_VALHALLA_ROUTE_ENDPOINT,
    JsonCache,
    PublicServiceError,
    WalkingRoute,
    fetch_walking_route,
)

GEOMETRY_MODES = frozenset({"auto", "road-network", "straight"})
PREVIEW_MODES = frozenset({"compact", "review"})
OFFLINE_PROVIDERS = frozenset({"openstreetmap_walknet"})
KIND_LABELS = {"restricted": "限定开放（不规划路线）", "unverified": "位置待考证（不规划路线）"}
COMPACT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' blob:; style-src 'unsafe-inline'; "
    "img-src data: blob: https:; connect-src https:; worker-src blob:; font-src data: https:;"
)
REVIEW_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' blob:; style-src 'unsafe-inline'; "
    "img-src data: blob: https: 'self' file:; connect-src https: 'self'; worker-src blob:; "
    "font-src data: https:;"
)
OPENFREEMAP_STYLE_URL = "https://tiles.openfreemap.org/styles/positron"
GROUP_COLORS = (
    "#2563eb",
    "#c026d3",
    "#059669",
    "#d97706",
    "#0891b2",
    "#7c3aed",
    "#be123c",
    "#4d7c0f",
    "#0f766e",
    "#c2410c",
)
ASSET_DIRECTORY = Path(__file__).resolve().parents[2] / "assets" / "route-preview"


class RoutePreviewError(ValueError):
    """Raised when an interactive route preview cannot be generated."""


@dataclass(frozen=True)
class RoutePreviewResult:
    output: Path
    groups: int
    routed_groups: int
    fallback_groups: int
    excluded_points: int
    unlocated_points: int
    warnings: tuple[str, ...]


def render_route_preview(
    package: LoadedPackage,
    proposal: Mapping[str, Any],
    *,
    proposal_path: str | Path,
    proposal_sha256: str,
    output: str | Path,
    geometry_mode: str = "auto",
    no_basemap: bool = False,
    route_endpoint: str = DEFAULT_VALHALLA_ROUTE_ENDPOINT,
    cache: JsonCache | None = None,
    force: bool = False,
    route_fetcher: Callable[..., WalkingRoute] = fetch_walking_route,
    progress: Callable[[dict[str, Any]], None] | None = None,
    request_delay_seconds: float = 0.25,
    mode: str = "compact",
    geometry_file: Mapping[str, Any] | None = None,
) -> RoutePreviewResult:
    if geometry_mode not in GEOMETRY_MODES:
        raise RoutePreviewError(f"Unknown route preview geometry mode: {geometry_mode}")
    if mode not in PREVIEW_MODES:
        raise RoutePreviewError(f"Unknown route preview mode: {mode}")
    stored_geometry = (geometry_file or {}).get("groups") or {}
    validate_proposal(package, proposal, final=False, stage="draft")
    route_policy = proposal.get("routePolicy")
    road_network_approved = (
        isinstance(route_policy, Mapping)
        and route_policy.get("confirmed") is True
        and route_policy.get("useRoadNetwork") is True
    )
    if geometry_mode == "road-network" and not road_network_approved:
        raise RoutePreviewError(
            "Road-network preview requires an explicitly confirmed road-network policy."
        )

    target = Path(output).expanduser().resolve()
    proposal_source = Path(proposal_path).expanduser().resolve()
    if target in {package.path, proposal_source}:
        raise RoutePreviewError(
            "Preview output must not replace the input package or proposal."
        )
    if target.exists() and not force:
        raise RoutePreviewError(f"Route preview output already exists: {target}")

    point_by_id = {
        str(point["id"]): point
        for point in package.plan.get("points", [])
        if isinstance(point, Mapping) and isinstance(point.get("id"), str)
    }
    raw_groups = [
        group for group in proposal.get("groups", []) if isinstance(group, Mapping)
    ]
    route_jobs = [
        group
        for group in raw_groups
        if _stored_route(group, stored_geometry) is None
        and _should_fetch_route(group, geometry_mode, road_network_approved)
    ]
    _report(
        progress,
        {
            "event": "route_preview_start",
            "groups": len(raw_groups),
            "routeGeometryJobs": len(route_jobs),
            "geometryMode": geometry_mode,
        },
    )

    groups: list[dict[str, Any]] = []
    route_features: list[dict[str, Any]] = []
    warnings: list[str] = []
    routed_groups = 0
    fallback_groups = 0
    completed_jobs = 0
    cache_value = cache or JsonCache()

    color_offset = _color_offset(str(package.plan.get("id") or ""))
    for group_index, raw_group in enumerate(raw_groups):
        group_id = str(raw_group.get("id") or "")
        group_name = str(raw_group.get("name") or group_id)
        color = GROUP_COLORS[(color_offset + group_index) % len(GROUP_COLORS)]
        points = _group_points(raw_group, point_by_id, group_name)
        straight_coordinates = [
            [point["longitude"], point["latitude"]] for point in points
        ]
        evaluation = raw_group.get("routeEvaluation")
        evaluation = evaluation if isinstance(evaluation, Mapping) else {}
        evaluation_status = evaluation.get("status")
        display_status, status_label = _fallback_status(evaluation_status)
        kind = str(raw_group.get("kind") or "route")
        if kind in KIND_LABELS:
            display_status, status_label = "special", KIND_LABELS[kind]
        route_coordinates = straight_coordinates
        stored = _stored_route(raw_group, stored_geometry) if geometry_mode != "straight" else None
        if stored is not None and kind == "route":
            route_coordinates = stored["coordinates"]
            if evaluation_status == "routed":
                display_status, status_label = "routed", "步行路网"
                routed_groups += 1
            else:
                display_status, status_label = "planned", "规划路线（尚未正式评估）"
        elif (
            kind == "route"
            and evaluation_status == "routed"
            and evaluation.get("provider") in OFFLINE_PROVIDERS
        ):
            status_label = "已按离线路网评估（缺少线形文件）"

        if stored is None and kind == "route" and _should_fetch_route(
            raw_group, geometry_mode, road_network_approved
        ):
            try:
                route = route_fetcher(
                    [
                        {
                            "latitude": point["latitude"],
                            "longitude": point["longitude"],
                        }
                        for point in points
                    ],
                    endpoint=route_endpoint,
                    cache=cache_value,
                )
            except PublicServiceError as exc:
                display_status = "failed"
                status_label = "路网线形不可用"
                warnings.append(f"{group_name}: {exc}")
                event = {
                    "event": "route_geometry_progress",
                    "completed": completed_jobs + 1,
                    "total": len(route_jobs),
                    "status": "failed",
                    "groupId": group_id,
                    "groupName": group_name,
                    "error": str(exc),
                }
            else:
                route_coordinates = [list(item) for item in route.geojson_coordinates]
                display_status = "routed"
                status_label = "步行路网"
                routed_groups += 1
                event = {
                    "event": "route_geometry_progress",
                    "completed": completed_jobs + 1,
                    "total": len(route_jobs),
                    "status": "routed",
                    "groupId": group_id,
                    "groupName": group_name,
                    "cacheHit": route.cache_hit,
                }
            completed_jobs += 1
            _report(progress, event)
            if completed_jobs < len(route_jobs) and request_delay_seconds > 0:
                time.sleep(request_delay_seconds)

        if len(route_coordinates) >= 2 and kind == "route":
            route_features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "groupId": group_id,
                        "status": display_status,
                        "color": color,
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": route_coordinates,
                    },
                }
            )
        if display_status != "routed":
            fallback_groups += 1
        day = raw_group.get("day") if isinstance(raw_group.get("day"), Mapping) else None
        groups.append(
            {
                "id": group_id,
                "name": group_name,
                "color": color,
                "kind": kind,
                "day": {"label": day.get("label"), "theme": day.get("theme")} if day else None,
                "status": display_status,
                "statusLabel": status_label,
                "distanceMeters": _distance(raw_group),
                "durationSeconds": _duration(raw_group),
                "quality": _quality(raw_group),
                "points": points,
            }
        )

    excluded_points = _excluded_points(proposal, point_by_id)
    unlocated_points = _unlocated_points(proposal, point_by_id)
    model = {
        "schemaVersion": 1,
        "generatedAt": _now(),
        "plan": {
            "name": str(package.plan.get("name") or "MiriaGo 路线预览"),
            "area": str(package.plan.get("area") or ""),
        },
        "integrity": {
            "inputSha256": package.source_sha256,
            "proposalSha256": proposal_sha256,
            "routeFingerprint": route_fingerprint(proposal),
        },
        "map": {
            "noBasemap": no_basemap,
            "styleUrl": OPENFREEMAP_STYLE_URL,
        },
        "groups": groups,
        "routes": {
            "type": "FeatureCollection",
            "features": route_features,
        },
        "excludedPoints": excluded_points,
        "unlocatedPoints": unlocated_points,
        "stations": _stations(proposal),
        "transitions": _transitions(proposal),
        "mode": mode,
    }
    if mode == "review":
        model["review"] = _review_data(package, proposal, target, point_by_id)
    html = _render_html(model)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(html, encoding="utf-8")
        temporary.replace(target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise

    _report(
        progress,
        {
            "event": "route_preview_complete",
            "groups": len(groups),
            "routedGroups": routed_groups,
            "fallbackGroups": fallback_groups,
            "excludedPoints": len(excluded_points),
            "unlocatedPoints": len(unlocated_points),
            "output": str(target),
        },
    )
    return RoutePreviewResult(
        output=target,
        groups=len(groups),
        routed_groups=routed_groups,
        fallback_groups=fallback_groups,
        excluded_points=len(excluded_points),
        unlocated_points=len(unlocated_points),
        warnings=tuple(warnings),
    )


def _stored_route(
    group: Mapping[str, Any],
    stored: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    item = stored.get(str(group.get("id")))
    if not isinstance(item, Mapping):
        return None
    if item.get("pointIds") != group.get("pointIds"):
        return None
    if (item.get("routeEnds") or None) != (group.get("routeEnds") or None):
        return None
    coordinates = item.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    return item


def _quality(group: Mapping[str, Any]) -> dict[str, Any] | None:
    quality = group.get("quality")
    if not isinstance(quality, Mapping):
        return None
    return {
        "crossings": quality.get("crossings", 0),
        "spurs": len(quality.get("spurs") or []),
        "backtrackRatio": quality.get("backtrackRatio"),
    }


def _stations(proposal: Mapping[str, Any]) -> list[dict[str, Any]]:
    stations: dict[str, dict[str, Any]] = {}
    state = proposal.get("stationAssist")
    if isinstance(state, Mapping):
        for item in state.get("stations", []) or []:
            if isinstance(item, Mapping) and item.get("label"):
                stations[str(item["label"])] = {
                    "name": str(item["label"]),
                    "latitude": float(item["latitude"]),
                    "longitude": float(item["longitude"]),
                    "lines": list(item.get("lines") or []),
                }
    for group in proposal.get("groups", []):
        ends = group.get("routeEnds") if isinstance(group, Mapping) else None
        if not isinstance(ends, Mapping):
            continue
        for side in ("start", "end"):
            end = ends.get(side)
            if isinstance(end, Mapping) and end.get("kind") == "station":
                stations.setdefault(
                    str(end["name"]),
                    {
                        "name": str(end["name"]),
                        "latitude": float(end["latitude"]),
                        "longitude": float(end["longitude"]),
                        "lines": [],
                    },
                )
    return sorted(stations.values(), key=lambda item: item["name"])


def _transitions(proposal: Mapping[str, Any]) -> dict[str, Any]:
    stations = {item["name"]: item for item in _stations(proposal)}
    features = []
    for item in proposal.get("transitions", []) or []:
        if not isinstance(item, Mapping) or item.get("mode") not in {
            "rail_candidate",
            "transit_unknown",
        }:
            continue
        origin = stations.get(str(item.get("fromStation")))
        target = stations.get(str(item.get("toStation")))
        if origin is None or target is None:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "mode": item["mode"],
                    "label": f"{origin['name']} → {target['name']}",
                    "lines": list(item.get("lines") or []),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [origin["longitude"], origin["latitude"]],
                        [target["longitude"], target["latitude"]],
                    ],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _review_data(
    package: LoadedPackage,
    proposal: Mapping[str, Any],
    target: Path,
    point_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Point metadata and exported thumbnails for the interactive review page."""
    assets = target.with_name(f"{target.stem}-assets") / "thumbs"
    works = {
        work.get("id"): str(work.get("title") or work.get("name") or "")
        for work in package.plan.get("works", [])
        if isinstance(work, Mapping)
    }
    hints = proposal.get("reviewHints") if isinstance(proposal.get("reviewHints"), Mapping) else {}
    ids = [
        point_id
        for group in proposal.get("groups", [])
        if isinstance(group, Mapping)
        for point_id in group.get("pointIds", [])
    ] + [
        str(item.get("pointId"))
        for item in proposal.get("excludedPoints", [])
        if isinstance(item, Mapping)
    ]
    points: dict[str, Any] = {}
    for point_id in ids:
        point = point_by_id.get(point_id)
        if point is None:
            continue
        thumb = None
        asset = point.get("referenceThumbnailAsset")
        if isinstance(asset, str) and asset in package.entries:
            suffix = Path(asset).suffix or ".jpg"
            name = "".join(char if char.isalnum() or char in "-_" else "_" for char in point_id) + suffix
            assets.mkdir(parents=True, exist_ok=True)
            destination = assets / name
            data = package.entries[asset]
            if not destination.exists() or destination.read_bytes() != data:
                destination.write_bytes(data)
            thumb = f"{assets.parent.name}/thumbs/{name}"
        full_asset = next(
            (
                value
                for value in (point.get("referenceFullReferenceAsset"), point.get("userReferenceAsset"))
                if isinstance(value, str) and value in package.entries
            ),
            None,
        )
        anitabi = None
        work_id = str(point.get("workId") or "")
        if point.get("source") == "anitabi" and work_id.startswith("bangumi-"):
            anitabi = {"bangumiId": work_id.removeprefix("bangumi-"), "pointId": point.get("sourceId")}
        hint = hints.get(point_id) if isinstance(hints, Mapping) else None
        points[point_id] = {
            "thumb": thumb,
            "fullAsset": full_asset,
            "remote": point.get("referenceImageUrl") if isinstance(point.get("referenceImageUrl"), str) else None,
            "work": works.get(point.get("workId"), ""),
            "episode": str(point.get("episodeLabel") or ""),
            "anitabi": anitabi,
            "sourceUrl": point.get("sourceUrl") if isinstance(point.get("sourceUrl"), str) else None,
            "hint": dict(hint) if isinstance(hint, Mapping) else None,
        }
    excluded = {
        str(item.get("pointId")): item.get("kind", "time_tradeoff")
        for item in proposal.get("excludedPoints", [])
        if isinstance(item, Mapping)
    }
    return {
        "points": points,
        "excludedKinds": excluded,
        "storageKey": f"miriago-review:{package.plan.get('id')}",
    }


def _group_points(
    group: Mapping[str, Any],
    point_by_id: Mapping[str, Mapping[str, Any]],
    group_name: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for order_index, point_id in enumerate(group.get("pointIds", [])):
        point = point_by_id[str(point_id)]
        result.append(
            {
                "id": str(point_id),
                "name": str(point.get("name") or point_id),
                "groupName": group_name,
                "order": order_index + 1,
                "latitude": float(point["latitude"]),
                "longitude": float(point["longitude"]),
            }
        )
    return result


def _excluded_points(
    proposal: Mapping[str, Any],
    point_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in proposal.get("excludedPoints", []):
        if not isinstance(item, Mapping):
            continue
        point_id = item.get("pointId")
        point = point_by_id.get(str(point_id))
        if point is None:
            continue
        result.append(
            {
                "id": str(point_id),
                "name": str(point.get("name") or point_id),
                "latitude": float(point["latitude"]),
                "longitude": float(point["longitude"]),
            }
        )
    return result


def _unlocated_points(
    proposal: Mapping[str, Any],
    point_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "id": str(point_id),
            "name": str(point_by_id.get(str(point_id), {}).get("name") or point_id),
        }
        for point_id in proposal.get("unassignedPointIds", [])
    ]


def _should_fetch_route(
    group: Mapping[str, Any],
    geometry_mode: str,
    road_network_approved: bool,
) -> bool:
    if geometry_mode == "straight" or not road_network_approved:
        return False
    evaluation = group.get("routeEvaluation")
    return (
        isinstance(evaluation, Mapping)
        and evaluation.get("status") == "routed"
        and evaluation.get("provider") not in OFFLINE_PROVIDERS
        and isinstance(group.get("pointIds"), list)
        and len(group["pointIds"]) >= 2
    )


def _fallback_status(evaluation_status: Any) -> tuple[str, str]:
    if evaluation_status == "failed":
        return "failed", "路网评估失败"
    if evaluation_status == "skipped":
        return "skipped", "路网评估跳过"
    if evaluation_status == "routed":
        return "approximate", "访问顺序连线"
    return "approximate", "访问顺序连线"


def _distance(group: Mapping[str, Any]) -> float | None:
    metrics = group.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    evaluation = group.get("routeEvaluation")
    value = (
        metrics.get("walkingPathMeters")
        if isinstance(evaluation, Mapping) and evaluation.get("status") == "routed"
        else metrics.get("roughWalkingMeters")
    )
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _duration(group: Mapping[str, Any]) -> float | None:
    metrics = group.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get("walkingDurationSeconds")
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _color_offset(plan_id: str) -> int:
    digest = hashlib.sha256(plan_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % len(GROUP_COLORS)


def _render_html(model: Mapping[str, Any]) -> str:
    review = model.get("mode") == "review"
    try:
        template = (ASSET_DIRECTORY / "template.html").read_text(encoding="utf-8")
        application_css = (ASSET_DIRECTORY / "route-preview.css").read_text(
            encoding="utf-8"
        )
        application_js = (ASSET_DIRECTORY / "route-preview.js").read_text(
            encoding="utf-8"
        )
        maplibre_css = (ASSET_DIRECTORY / "vendor/maplibre-gl.css").read_text(
            encoding="utf-8"
        )
        maplibre_js = (ASSET_DIRECTORY / "vendor/maplibre-gl.js").read_text(
            encoding="utf-8"
        )
        if review:
            application_css += "\n" + (ASSET_DIRECTORY / "route-preview-review.css").read_text(
                encoding="utf-8"
            )
            application_js = (
                (ASSET_DIRECTORY / "route-preview-review.js").read_text(encoding="utf-8")
                + "\n"
                + application_js
            )
    except OSError as exc:
        raise RoutePreviewError(f"Cannot load route-preview assets: {exc}") from exc
    maplibre_js = _escape_inline_script(maplibre_js)
    application_js = _escape_inline_script(application_js)
    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
    payload = (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    replacements = {
        "__CONTENT_SECURITY_POLICY__": REVIEW_CSP if review else COMPACT_CSP,
        "/*__MAPLIBRE_CSS__*/": maplibre_css,
        "/*__APPLICATION_CSS__*/": application_css,
        "/*__MAPLIBRE_JS__*/": maplibre_js,
        "/*__APPLICATION_JS__*/": application_js,
        "__PREVIEW_DATA__": payload,
    }
    for placeholder, value in replacements.items():
        if placeholder not in template:
            raise RoutePreviewError(f"Route-preview template is missing {placeholder}.")
        template = template.replace(placeholder, value)
    return template


def _escape_inline_script(value: str) -> str:
    return re.sub(r"</script", r"<\\/script", value, flags=re.IGNORECASE)


def _report(
    callback: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
) -> None:
    if callback is not None:
        callback(event)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
