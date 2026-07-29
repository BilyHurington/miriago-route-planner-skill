"""Build a compact, interactive route preview without mutating plan data."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

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
) -> RoutePreviewResult:
    if geometry_mode not in GEOMETRY_MODES:
        raise RoutePreviewError(f"Unknown route preview geometry mode: {geometry_mode}")
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
        if _should_fetch_route(group, geometry_mode, road_network_approved)
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
        route_coordinates = straight_coordinates

        if _should_fetch_route(raw_group, geometry_mode, road_network_approved):
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

        if len(route_coordinates) >= 2:
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
        groups.append(
            {
                "id": group_id,
                "name": group_name,
                "color": color,
                "status": display_status,
                "statusLabel": status_label,
                "distanceMeters": _distance(raw_group),
                "durationSeconds": _duration(raw_group),
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
    }
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
        and isinstance(group.get("pointIds"), list)
        and len(group["pointIds"]) >= 2
    )


def _fallback_status(evaluation_status: Any) -> tuple[str, str]:
    if evaluation_status == "failed":
        return "failed", "路网评估失败"
    if evaluation_status == "skipped":
        return "skipped", "路网评估跳过"
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
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
