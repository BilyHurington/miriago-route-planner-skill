"""Planning proposal generation, validation, and application."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .clustering import (
    GeoPoint,
    PlanningProfile,
    bundle_points,
    cluster_metrics,
    cluster_points,
    distance_matrix,
    medoid_index,
    nearest_items,
    order_clusters,
    order_open_path,
    path_length,
)
from .journey_notes import (
    JourneyNotesError,
    merge_managed_journey_notes,
    render_journey_notes,
    route_fingerprint,
    validate_journey_notes_spec,
)
from .package_io import LoadedPackage, PackageError, has_valid_coordinate

PROPOSAL_SCHEMA_VERSION = 5
PLACEHOLDER_PREFIXES = ("候选片区", "区域 ", "片区 ")
GROUP_KINDS = frozenset({"route", "restricted", "unverified"})
EXCLUSION_KINDS = frozenset({"time_tradeoff", "curation"})
END_KINDS = frozenset({"station", "location", "point", "previous-group"})
TRANSITION_MODES = frozenset({"walk", "rail_candidate", "transit_unknown", "day_break"})
NETWORK_BACKENDS = frozenset({"valhalla", "osm"})
MODULES = frozenset(
    {
        "osm-network",
        "day-route",
        "special-groups",
        "stations-assist",
        "dedupe",
        "verify",
        "classify",
        "review-preview",
        "review-server",
    }
)


class ProposalError(ValueError):
    """Raised when a proposal is incomplete or inconsistent with its source."""


def generate_proposal(
    package: LoadedPackage,
    profile: PlanningProfile,
    *,
    stations: Sequence[dict[str, Any]] | None = None,
    station_source: Mapping[str, Any] | None = None,
    special_groups: Sequence[Mapping[str, Any]] = (),
    modules: Sequence[str] = (),
) -> dict[str, Any]:
    points = package.plan["points"]
    special_ids = {
        point_id
        for special in special_groups
        for point_id in special.get("pointIds", [])
    }
    original_order = {
        point["id"]: index
        for index, point in enumerate(points)
        if isinstance(point.get("id"), str)
    }
    valid_points = [
        point
        for point in points
        if has_valid_coordinate(point) and point["id"] not in special_ids
    ]
    invalid_ids = [point["id"] for point in points if not has_valid_coordinate(point)]
    raw_stops = [
        GeoPoint(
            key=point["id"],
            latitude=float(point["latitude"]),
            longitude=float(point["longitude"]),
            point_ids=(point["id"],),
        )
        for point in valid_points
    ]
    stops = bundle_points(raw_stops, profile.bundle_distance_meters)
    stop_matrix = distance_matrix(stops)
    clusters, diagnostics = cluster_points(stops, profile)
    cluster_order = order_clusters(clusters, stops, stop_matrix)
    point_by_id = {point["id"]: point for point in points}
    station_values = list(stations or [])

    groups: list[dict[str, Any]] = []
    for order_index, cluster_index in enumerate(cluster_order):
        cluster = clusters[cluster_index]
        stop_path = order_open_path(cluster, stop_matrix)
        ordered_point_ids = [
            point_id
            for stop_index in stop_path
            for point_id in sorted(
                stops[stop_index].point_ids,
                key=lambda value: original_order[value],
            )
        ]
        medoid_stop = medoid_index(cluster, stop_matrix)
        medoid = stops[medoid_stop]
        anchor_point_id = medoid.point_ids[0]
        anchor_point = point_by_id[anchor_point_id]
        metrics = cluster_metrics(cluster, stops, stop_matrix)
        direct_path = path_length(stop_path, stop_matrix)
        point_names = [
            str(point_by_id[point_id].get("name") or point_id)
            for point_id in ordered_point_ids
        ]
        station_candidates = nearest_items(
            medoid.latitude,
            medoid.longitude,
            station_values,
            limit=5,
        )
        group_id = _stable_group_id(package.plan["id"], ordered_point_ids)
        confidence, reasons = _confidence(metrics, profile, station_candidates)
        groups.append(
            {
                "id": group_id,
                "name": f"候选片区 {order_index + 1:02d}",
                "pointIds": ordered_point_ids,
                "pointNames": point_names,
                "anchor": {
                    "name": str(anchor_point.get("name") or anchor_point_id),
                    "latitude": float(anchor_point["latitude"]),
                    "longitude": float(anchor_point["longitude"]),
                    "pointId": anchor_point_id,
                    "kind": "point",
                },
                "medoid": {
                    "latitude": medoid.latitude,
                    "longitude": medoid.longitude,
                    "pointId": medoid.point_ids[0],
                },
                "metrics": {
                    **metrics,
                    "directPathMeters": round(direct_path, 1),
                    "roughWalkingMeters": round(direct_path * 1.25, 1),
                    "walkingEstimateMethod": "haversine_path_x_1.25",
                },
                "nearbyStations": station_candidates,
                "confidence": {
                    "level": confidence,
                    "reasons": reasons,
                },
                "note": _default_note(metrics, station_candidates),
                "warnings": [],
                "kind": "route",
            }
        )
    point_by_id = {point["id"]: point for point in points}
    for special in special_groups:
        groups.append(
            build_group(
                package.plan["id"],
                str(special["name"]),
                list(special["pointIds"]),
                point_by_id,
                kind=str(special.get("kind") or "restricted"),
                note=special.get("note"),
            )
        )

    return {
        "schemaVersion": PROPOSAL_SCHEMA_VERSION,
        "modules": sorted(set(modules)),
        "skill": {
            "name": "plan-miriago-route",
            "version": __version__,
        },
        "createdAt": _now(),
        "review": {
            "areasConfirmed": False,
            "areasConfirmedAt": None,
            "routesConfirmed": False,
            "routesConfirmedAt": None,
            "curationConfirmed": False,
            "curationConfirmedAt": None,
        },
        "routePolicy": empty_route_policy(),
        "journeyNotes": {
            "status": "not_requested",
            "preparedAt": None,
            "confirmedAt": None,
            "routeFingerprint": None,
            "spec": None,
            "markdown": None,
        },
        "input": {
            "path": str(package.path),
            "sha256": package.source_sha256,
            "planId": package.plan["id"],
            "planName": package.plan.get("name"),
        },
        "profile": {
            "name": profile.name,
            "bundleDistanceMeters": profile.bundle_distance_meters,
            "hardLinkMeters": profile.hard_link_meters,
            "maxRadiusP90Meters": profile.max_radius_p90_meters,
            "maxDiameterMeters": profile.max_diameter_meters,
            "maxPoints": profile.max_points,
        },
        "groups": groups,
        "excludedPoints": [],
        "unassignedPointIds": invalid_ids,
        "constraints": {"via": []},
        "transitions": [],
        "stationAssist": empty_station_assist(),
        "reviewHints": {},
        "dataSources": {
            "geometry": {
                "provider": "local",
                "method": "haversine_adaptive_mst",
            },
            "stations": dict(station_source or {"provider": "not_requested"}),
            "walking": {
                "provider": "local",
                "method": "haversine_path_x_1.25",
                "status": "rough_estimate",
            },
        },
        "diagnostics": diagnostics,
        "warnings": list(package.warnings),
    }


def proposal_shell(
    package: LoadedPackage,
    *,
    groups: list[dict[str, Any]],
    profile: Mapping[str, Any],
    data_sources: Mapping[str, Any],
    modules: Sequence[str] = (),
    constraints: Mapping[str, Any] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """Wrap planner-built groups in a draft proposal with closed gates."""
    return {
        "schemaVersion": PROPOSAL_SCHEMA_VERSION,
        "modules": sorted(set(modules)),
        "skill": {"name": "plan-miriago-route", "version": __version__},
        "createdAt": _now(),
        "review": {
            "areasConfirmed": False,
            "areasConfirmedAt": None,
            "routesConfirmed": False,
            "routesConfirmedAt": None,
            "curationConfirmed": False,
            "curationConfirmedAt": None,
        },
        "routePolicy": empty_route_policy(),
        "journeyNotes": {
            "status": "not_requested",
            "preparedAt": None,
            "confirmedAt": None,
            "routeFingerprint": None,
            "spec": None,
            "markdown": None,
        },
        "input": {
            "path": str(package.path),
            "sha256": package.source_sha256,
            "planId": package.plan["id"],
            "planName": package.plan.get("name"),
        },
        "profile": dict(profile),
        "groups": groups,
        "excludedPoints": [],
        "unassignedPointIds": [
            point["id"]
            for point in package.plan["points"]
            if not has_valid_coordinate(point)
        ],
        "constraints": dict(constraints or {"via": []}),
        "transitions": [],
        "stationAssist": empty_station_assist(),
        "reviewHints": {},
        "dataSources": dict(data_sources),
        "diagnostics": dict(diagnostics or {}),
        "warnings": list(warnings) + list(package.warnings),
    }


def load_proposal(path: str | Path) -> dict[str, Any]:
    proposal_path = Path(path).expanduser().resolve()
    try:
        value = json.loads(
            proposal_path.read_text(encoding="utf-8-sig"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProposalError(f"Cannot read proposal {proposal_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProposalError("Proposal must be a JSON object.")
    return upgrade_proposal(value)


def upgrade_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """Bring a schema-4 proposal to schema 5 without touching its decisions.

    Version 5 only adds optional modules, so every confirmation recorded in a
    version-4 proposal stays valid.
    """
    if proposal.get("schemaVersion") != 4:
        return proposal
    proposal["schemaVersion"] = PROPOSAL_SCHEMA_VERSION
    proposal.setdefault("modules", [])
    review = proposal.setdefault("review", {})
    if isinstance(review, dict):
        review.setdefault("curationConfirmed", False)
        review.setdefault("curationConfirmedAt", None)
    policy = proposal.get("routePolicy")
    if isinstance(policy, dict):
        policy.setdefault(
            "networkBackend",
            "valhalla" if policy.get("useRoadNetwork") is True else None,
        )
        policy.setdefault("stationsAssist", False if policy.get("confirmed") else None)
    proposal.setdefault("constraints", {"via": []})
    proposal.setdefault("transitions", [])
    proposal.setdefault("stationAssist", empty_station_assist())
    proposal.setdefault("reviewHints", {})
    for group in proposal.get("groups", []):
        if isinstance(group, dict):
            group.setdefault("kind", "route")
    # Exclusions without ``kind`` keep meaning time_tradeoff; leaving them
    # untouched keeps confirmed journey-note fingerprints valid.
    return proposal


def empty_route_policy() -> dict[str, Any]:
    return {
        "confirmed": False,
        "confirmedAt": None,
        "useRoadNetwork": None,
        "networkBackend": None,
        "adjustForTime": None,
        "writePlanNotes": None,
        "stationsAssist": None,
        "timeBudgetMinutes": None,
        "tradeoffNotes": None,
    }


def empty_station_assist() -> dict[str, Any]:
    return {"status": "not_requested", "plannedAt": None, "stations": []}


def build_group(
    plan_id: str,
    name: str,
    point_ids: Sequence[str],
    point_by_id: Mapping[str, Mapping[str, Any]],
    *,
    kind: str = "route",
    note: str | None = None,
    anchor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a proposal group from an ordered point list."""
    if not point_ids:
        raise ProposalError(f"Group {name!r} has no points.")
    geo_points = [
        GeoPoint(
            key=point_id,
            latitude=float(point_by_id[point_id]["latitude"]),
            longitude=float(point_by_id[point_id]["longitude"]),
            point_ids=(point_id,),
        )
        for point_id in point_ids
    ]
    matrix = distance_matrix(geo_points)
    indices = list(range(len(geo_points)))
    metrics = cluster_metrics(indices, geo_points, matrix)
    direct = path_length(indices, matrix)
    medoid = geo_points[medoid_index(indices, matrix)]
    medoid_point = point_by_id[medoid.key]
    return {
        "id": _stable_group_id(plan_id, point_ids),
        "name": name,
        "kind": kind,
        "pointIds": list(point_ids),
        "pointNames": [
            str(point_by_id[point_id].get("name") or point_id) for point_id in point_ids
        ],
        "anchor": dict(anchor)
        if anchor
        else {
            "name": str(medoid_point.get("name") or medoid.key),
            "latitude": float(medoid_point["latitude"]),
            "longitude": float(medoid_point["longitude"]),
            "pointId": medoid.key,
            "kind": "point",
        },
        "medoid": {
            "latitude": medoid.latitude,
            "longitude": medoid.longitude,
            "pointId": medoid.key,
        },
        "metrics": {
            **metrics,
            "directPathMeters": round(direct, 1),
            "roughWalkingMeters": round(direct * 1.25, 1),
            "walkingEstimateMethod": "haversine_path_x_1.25",
        },
        "nearbyStations": [],
        "confidence": {"level": "reviewed", "reasons": []},
        "note": note,
        "warnings": [],
    }


def save_proposal(
    proposal: Mapping[str, Any],
    path: str | Path,
    *,
    force: bool = False,
) -> Path:
    target = Path(path).expanduser().resolve()
    if target.exists() and not force:
        raise ProposalError(f"Proposal output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def validate_proposal(
    package: LoadedPackage,
    proposal: Mapping[str, Any],
    *,
    final: bool,
    stage: str | None = None,
) -> list[str]:
    validation_stage = stage or ("final" if final else "draft")
    if validation_stage not in {"draft", "areas", "route-policy", "final"}:
        raise ProposalError(f"Unknown proposal validation stage: {validation_stage}")
    reviewed_areas = validation_stage in {"areas", "route-policy", "final"}
    policy_required = validation_stage in {"route-policy", "final"}
    errors: list[str] = []
    warnings: list[str] = []
    if proposal.get("schemaVersion") != PROPOSAL_SCHEMA_VERSION:
        errors.append(f"proposal.schemaVersion must be {PROPOSAL_SCHEMA_VERSION}.")
    input_info = proposal.get("input")
    if not isinstance(input_info, dict):
        errors.append("proposal.input must be an object.")
    else:
        if input_info.get("sha256") != package.source_sha256:
            errors.append("Proposal source hash does not match the input package.")
        if input_info.get("planId") != package.plan["id"]:
            errors.append("Proposal planId does not match the input package.")
    modules = proposal.get("modules", [])
    if not isinstance(modules, list) or any(item not in MODULES for item in modules):
        errors.append(f"proposal.modules must be a list drawn from {sorted(MODULES)}.")

    points = package.plan["points"]
    valid_ids = {point["id"] for point in points if has_valid_coordinate(point)}
    point_by_id = {point["id"]: point for point in points}
    invalid_ids = {point["id"] for point in points if not has_valid_coordinate(point)}
    raw_groups = proposal.get("groups")
    if not isinstance(raw_groups, list):
        errors.append("proposal.groups must be a list.")
        raw_groups = []
    group_ids: set[str] = set()
    group_names: set[str] = set()
    assigned: list[str] = []
    for index, group in enumerate(raw_groups):
        context = f"groups[{index}]"
        if not isinstance(group, dict):
            errors.append(f"{context} must be an object.")
            continue
        group_id = group.get("id")
        if not isinstance(group_id, str) or not group_id.strip():
            errors.append(f"{context}.id must be a non-empty string.")
        elif group_id in group_ids:
            errors.append(f"Duplicate group ID: {group_id}")
        else:
            group_ids.add(group_id)
        name = group.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{context}.name must be a non-empty string.")
        else:
            normalized_name = name.strip().casefold()
            if normalized_name in group_names:
                errors.append(f"Duplicate group name: {name}")
            group_names.add(normalized_name)
            if reviewed_areas and _is_placeholder_name(name):
                errors.append(
                    f"{context}.name is still a generated placeholder: {name!r}"
                )
        point_ids = group.get("pointIds")
        if not isinstance(point_ids, list) or not point_ids:
            errors.append(f"{context}.pointIds must be a non-empty list.")
            point_ids = []
        local_ids: set[str] = set()
        for point_id in point_ids:
            if not isinstance(point_id, str):
                errors.append(f"{context}.pointIds contains a non-string value.")
            elif point_id not in valid_ids:
                errors.append(
                    f"{context}.pointIds contains an unknown or unplannable ID: "
                    f"{point_id!r}"
                )
            elif point_id in local_ids:
                errors.append(f"{context} repeats point ID {point_id!r}.")
            else:
                local_ids.add(point_id)
                assigned.append(point_id)
        anchor = group.get("anchor")
        note = group.get("note")
        if note is not None and not isinstance(note, str):
            errors.append(f"{context}.note must be a string or null.")
        kind = group.get("kind", "route")
        if kind not in GROUP_KINDS:
            errors.append(f"{context}.kind must be one of {sorted(GROUP_KINDS)}.")
        _validate_day(group.get("day"), context, errors)
        _validate_route_ends(
            group.get("routeEnds"),
            local_ids,
            context,
            errors,
            first_group=index == 0,
        )
        if anchor is None and reviewed_areas:
            errors.append(f"{context}.anchor is required for a final proposal.")
        elif anchor is not None:
            _validate_anchor(
                anchor,
                local_ids,
                point_by_id,
                context,
                errors,
            )

    assigned_set = set(assigned)
    duplicated = sorted(
        point_id for point_id in assigned_set if assigned.count(point_id) > 1
    )
    if duplicated:
        errors.append(f"Points assigned to more than one group: {duplicated}")
    excluded = proposal.get("excludedPoints")
    excluded_ids: list[str] = []
    if not isinstance(excluded, list):
        errors.append("proposal.excludedPoints must be a list.")
        excluded = []
    for index, exclusion in enumerate(excluded):
        context = f"excludedPoints[{index}]"
        if not isinstance(exclusion, Mapping):
            errors.append(f"{context} must be an object.")
            continue
        point_id = exclusion.get("pointId")
        if not isinstance(point_id, str) or not point_id.strip():
            errors.append(f"{context}.pointId must be a non-empty string.")
        elif point_id not in valid_ids:
            errors.append(f"{context}.pointId is unknown or unplannable: {point_id!r}")
        else:
            excluded_ids.append(point_id)
        reason = exclusion.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{context}.reason must be a non-empty string.")
        previous_group_id = exclusion.get("previousGroupId")
        if not isinstance(previous_group_id, str) or not previous_group_id.strip():
            errors.append(f"{context}.previousGroupId must be a non-empty string.")
        excluded_at = exclusion.get("excludedAt")
        if not isinstance(excluded_at, str) or not excluded_at.strip():
            errors.append(f"{context}.excludedAt must be a non-empty string.")
        if exclusion.get("kind", "time_tradeoff") not in EXCLUSION_KINDS:
            errors.append(f"{context}.kind must be one of {sorted(EXCLUSION_KINDS)}.")
    duplicate_exclusions = sorted(
        point_id for point_id in set(excluded_ids) if excluded_ids.count(point_id) > 1
    )
    if duplicate_exclusions:
        errors.append(f"Points excluded more than once: {duplicate_exclusions}")
    excluded_set = set(excluded_ids)
    overlap = sorted(assigned_set & excluded_set)
    if overlap:
        errors.append(f"Points cannot be both assigned and excluded: {overlap}")
    missing = sorted(valid_ids - assigned_set - excluded_set)
    if missing:
        errors.append(f"Plannable points not assigned or explicitly excluded: {missing}")

    unassigned = proposal.get("unassignedPointIds")
    if not isinstance(unassigned, list) or any(
        not isinstance(value, str) for value in unassigned
    ):
        errors.append("proposal.unassignedPointIds must be a list of strings.")
    elif set(unassigned) != invalid_ids:
        errors.append(
            "unassignedPointIds must contain exactly the points without valid coordinates."
        )
    if not raw_groups and valid_ids - excluded_set:
        errors.append("Proposal has no groups for its plannable points.")
    route_policy = proposal.get("routePolicy")
    if not isinstance(route_policy, Mapping):
        errors.append("proposal.routePolicy must be an object.")
        route_policy = {}
    policy_confirmed = route_policy.get("confirmed") is True
    use_road_network = route_policy.get("useRoadNetwork")
    adjust_for_time = route_policy.get("adjustForTime")
    write_plan_notes = route_policy.get("writePlanNotes")
    if not isinstance(route_policy.get("confirmed"), bool):
        errors.append("routePolicy.confirmed must be a boolean.")
    if use_road_network is not None and not isinstance(use_road_network, bool):
        errors.append("routePolicy.useRoadNetwork must be a boolean or null.")
    if adjust_for_time is not None and not isinstance(adjust_for_time, bool):
        errors.append("routePolicy.adjustForTime must be a boolean or null.")
    if write_plan_notes is not None and not isinstance(write_plan_notes, bool):
        errors.append("routePolicy.writePlanNotes must be a boolean or null.")
    budget = route_policy.get("timeBudgetMinutes")
    if budget is not None and (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or not math.isfinite(float(budget))
        or float(budget) <= 0
    ):
        errors.append("routePolicy.timeBudgetMinutes must be a positive number or null.")
    if budget is not None and adjust_for_time is not True:
        errors.append("A time budget requires routePolicy.adjustForTime to be true.")
    tradeoff_notes = route_policy.get("tradeoffNotes")
    if tradeoff_notes is not None and not isinstance(tradeoff_notes, str):
        errors.append("routePolicy.tradeoffNotes must be a string or null.")
    time_excluded = [
        item
        for item in excluded
        if isinstance(item, Mapping) and item.get("kind", "time_tradeoff") == "time_tradeoff"
    ]
    curation_excluded = [
        item
        for item in excluded
        if isinstance(item, Mapping) and item.get("kind") == "curation"
    ]
    if time_excluded and adjust_for_time is not True:
        errors.append(
            "Excluded points require explicit approval of time-based route adjustments."
        )
    network_backend = route_policy.get("networkBackend")
    if network_backend is not None and network_backend not in NETWORK_BACKENDS:
        errors.append(f"routePolicy.networkBackend must be one of {sorted(NETWORK_BACKENDS)}.")
    if network_backend is not None and use_road_network is not True:
        errors.append("routePolicy.networkBackend requires useRoadNetwork to be true.")
    stations_assist = route_policy.get("stationsAssist")
    if stations_assist is not None and not isinstance(stations_assist, bool):
        errors.append("routePolicy.stationsAssist must be a boolean or null.")
    _validate_transitions(proposal, group_ids, errors)
    if policy_confirmed:
        if not isinstance(use_road_network, bool):
            errors.append(
                "A confirmed route policy requires an explicit road-network choice."
            )
        if not isinstance(adjust_for_time, bool):
            errors.append(
                "A confirmed route policy requires an explicit time-adjustment choice."
            )
        if not isinstance(write_plan_notes, bool):
            errors.append(
                "A confirmed route policy requires an explicit plan-notes choice."
            )
        confirmed_at = route_policy.get("confirmedAt")
        if not isinstance(confirmed_at, str) or not confirmed_at.strip():
            errors.append("A confirmed route policy requires routePolicy.confirmedAt.")
    if policy_required and not policy_confirmed:
        errors.append(
            "Route policy gate is closed: record explicit road-network and "
            "time-adjustment and plan-note choices before planning routes."
        )
    _validate_journey_notes_state(
        proposal,
        write_plan_notes=write_plan_notes,
        validation_stage=validation_stage,
        errors=errors,
    )
    review = proposal.get("review")
    if not isinstance(review, Mapping):
        errors.append("proposal.review must be an object.")
    else:
        if reviewed_areas and review.get("areasConfirmed") is not True:
            errors.append(
                "Area review gate is closed: set review.areasConfirmed only after "
                "explicit user confirmation."
            )
        if validation_stage == "final" and review.get("routesConfirmed") is not True:
            errors.append(
                "Route review gate is closed: set review.routesConfirmed only after "
                "explicit user confirmation."
            )
        if (
            reviewed_areas
            and curation_excluded
            and review.get("curationConfirmed") is not True
        ):
            errors.append(
                "Curation gate is closed: confirm the reviewed point removals with "
                "confirm-curation before area approval."
            )
    station_state = proposal.get("stationAssist", empty_station_assist())
    if not isinstance(station_state, Mapping) or station_state.get("status") not in {
        "not_requested",
        "planned",
    }:
        errors.append("stationAssist.status must be not_requested or planned.")
    elif (
        validation_stage == "final"
        and stations_assist is True
        and station_state.get("status") != "planned"
    ):
        errors.append(
            "Station assistance was requested at the route policy gate; run "
            "assist-stations before route confirmation."
        )
    if validation_stage == "final" and use_road_network is True:
        _validate_route_evaluations(raw_groups, errors)
    if validation_stage == "draft":
        warnings.append(
            "Draft validation permits generated group names and does not pass review gates."
        )
    if errors:
        raise ProposalError("\n".join(errors))
    return warnings


def _validate_route_evaluations(
    groups: Sequence[Any],
    errors: list[str],
) -> None:
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping) or group.get("kind", "route") != "route":
            continue
        context = f"groups[{index}].routeEvaluation"
        evaluation = group.get("routeEvaluation")
        if not isinstance(evaluation, Mapping):
            errors.append(
                f"{context} is required because road-network evaluation was requested."
            )
            continue
        status = evaluation.get("status")
        if status not in {"routed", "skipped", "failed"}:
            errors.append(f"{context}.status must be routed, skipped, or failed.")
        provider = evaluation.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            errors.append(f"{context}.provider must be a non-empty string.")
        evaluated_at = evaluation.get("evaluatedAt")
        if not isinstance(evaluated_at, str) or not evaluated_at.strip():
            errors.append(f"{context}.evaluatedAt must be a non-empty string.")
        if evaluation.get("pointIds") != group.get("pointIds"):
            errors.append(f"{context}.pointIds must match the current route order.")
        if (
            status == "routed"
            and group.get("routeEnds")
            and evaluation.get("routeEnds") != group.get("routeEnds")
        ):
            errors.append(
                f"{context} is stale: the group's start or end changed after routing."
            )
        if status == "routed":
            metrics = group.get("metrics")
            if not isinstance(metrics, Mapping):
                errors.append(f"groups[{index}].metrics must contain routed evidence.")
                continue
            for field in ("walkingPathMeters", "walkingDurationSeconds"):
                value = metrics.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) < 0
                ):
                    errors.append(
                        f"groups[{index}].metrics.{field} must be a non-negative number."
                    )
        elif status in {"skipped", "failed"}:
            reason = evaluation.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{context}.reason is required for {status} evaluation.")


def _validate_day(value: Any, context: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        errors.append(f"{context}.day must be an object or null.")
        return
    label = value.get("label")
    if not isinstance(label, str) or not label.strip():
        errors.append(f"{context}.day.label must be a non-empty string.")
    index = value.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        errors.append(f"{context}.day.index must be a non-negative integer.")
    theme = value.get("theme")
    if theme is not None and not isinstance(theme, str):
        errors.append(f"{context}.day.theme must be a string or null.")


def _validate_route_ends(
    value: Any,
    point_ids: set[str],
    context: str,
    errors: list[str],
    *,
    first_group: bool,
) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        errors.append(f"{context}.routeEnds must be an object or null.")
        return
    for side in ("start", "end"):
        end = value.get(side)
        if end is None:
            continue
        where = f"{context}.routeEnds.{side}"
        if not isinstance(end, Mapping) or end.get("kind") not in END_KINDS:
            errors.append(f"{where}.kind must be one of {sorted(END_KINDS)}.")
            continue
        kind = end["kind"]
        if kind == "point" and end.get("pointId") not in point_ids:
            errors.append(f"{where}.pointId must be a point of the group.")
        if kind in {"station", "location"}:
            for field in ("latitude", "longitude"):
                number = end.get(field)
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                ):
                    errors.append(f"{where}.{field} must be a number.")
            if not isinstance(end.get("name"), str) or not end["name"].strip():
                errors.append(f"{where}.name must be a non-empty string.")
        if kind == "previous-group" and (side != "start" or first_group):
            errors.append(f"{where}: previous-group is only valid as a later group's start.")


def _validate_transitions(
    proposal: Mapping[str, Any],
    group_ids: set[str],
    errors: list[str],
) -> None:
    transitions = proposal.get("transitions", [])
    if not isinstance(transitions, list):
        errors.append("proposal.transitions must be a list.")
        return
    for index, item in enumerate(transitions):
        context = f"transitions[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{context} must be an object.")
            continue
        for field in ("fromGroupId", "toGroupId"):
            if item.get(field) not in group_ids:
                errors.append(f"{context}.{field} must reference a current group.")
        if item.get("mode") not in TRANSITION_MODES:
            errors.append(f"{context}.mode must be one of {sorted(TRANSITION_MODES)}.")


def _validate_journey_notes_state(
    proposal: Mapping[str, Any],
    *,
    write_plan_notes: Any,
    validation_stage: str,
    errors: list[str],
) -> None:
    state = proposal.get("journeyNotes")
    if not isinstance(state, Mapping):
        errors.append("proposal.journeyNotes must be an object.")
        return
    status = state.get("status")
    if status not in {"not_requested", "draft", "confirmed"}:
        errors.append(
            "journeyNotes.status must be not_requested, draft, or confirmed."
        )
        return
    if status == "not_requested":
        for field in (
            "preparedAt",
            "confirmedAt",
            "routeFingerprint",
            "spec",
            "markdown",
        ):
            if state.get(field) is not None:
                errors.append(
                    f"journeyNotes.{field} must be null when notes are not requested."
                )
    else:
        prepared_at = state.get("preparedAt")
        if not isinstance(prepared_at, str) or not prepared_at.strip():
            errors.append("Prepared journey notes require journeyNotes.preparedAt.")
        fingerprint = state.get("routeFingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            errors.append("Prepared journey notes require a route fingerprint.")
        elif fingerprint != route_fingerprint(proposal):
            errors.append(
                "Journey notes are stale because the reviewed route has changed."
            )
        spec = state.get("spec")
        markdown = state.get("markdown")
        if not isinstance(spec, Mapping):
            errors.append("Prepared journey notes require a structured spec.")
        else:
            try:
                validate_journey_notes_spec(spec)
                expected_markdown = render_journey_notes(proposal, spec)
                if markdown != expected_markdown:
                    errors.append(
                        "journeyNotes.markdown does not match its structured spec."
                    )
            except JourneyNotesError as exc:
                errors.append(f"Invalid journey notes: {exc}")
        if not isinstance(markdown, str) or not markdown.strip():
            errors.append("Prepared journey notes require rendered Markdown.")
        confirmed_at = state.get("confirmedAt")
        if status == "confirmed":
            if not isinstance(confirmed_at, str) or not confirmed_at.strip():
                errors.append("Confirmed journey notes require journeyNotes.confirmedAt.")
        elif confirmed_at is not None:
            errors.append("Draft journey notes must not have confirmedAt.")
    if validation_stage == "final":
        if write_plan_notes is True and status != "confirmed":
            errors.append(
                "Journey-note review gate is closed: confirm the rendered plan memo."
            )
        if write_plan_notes is False and status != "not_requested":
            errors.append(
                "Journey notes must remain not_requested when plan-note writing was declined."
            )


def _validate_anchor(
    anchor: Any,
    point_ids: set[str],
    point_by_id: Mapping[str, Mapping[str, Any]],
    context: str,
    errors: list[str],
) -> None:
    if not isinstance(anchor, dict):
        errors.append(f"{context}.anchor must be an object or null.")
        return
    name = anchor.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{context}.anchor.name must be a non-empty string.")
    latitude = anchor.get("latitude")
    longitude = anchor.get("longitude")
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, (int, float))
        or not math.isfinite(float(latitude))
        or not -90 <= float(latitude) <= 90
    ):
        errors.append(f"{context}.anchor.latitude is invalid.")
    if (
        isinstance(longitude, bool)
        or not isinstance(longitude, (int, float))
        or not math.isfinite(float(longitude))
        or not -180 <= float(longitude) <= 180
    ):
        errors.append(f"{context}.anchor.longitude is invalid.")
    anchor_point_id = anchor.get("pointId")
    if anchor_point_id is not None and anchor_point_id not in point_ids:
        errors.append(f"{context}.anchor.pointId must be null or a point in the group.")
    kind = anchor.get("kind")
    if kind not in {"point", "station", "custom"}:
        errors.append(f"{context}.anchor.kind must be point, station, or custom.")
    elif kind == "point":
        if not isinstance(anchor_point_id, str) or anchor_point_id not in point_ids:
            errors.append(f"{context}.anchor.pointId is required for a point anchor.")
        else:
            source_point = point_by_id[anchor_point_id]
            expected_name = str(source_point.get("name") or anchor_point_id)
            if name != expected_name:
                errors.append(
                    f"{context}.anchor.name must match point {anchor_point_id!r}: "
                    f"{expected_name!r}."
                )
            if (
                isinstance(latitude, (int, float))
                and not isinstance(latitude, bool)
                and abs(float(latitude) - float(source_point["latitude"])) > 1e-7
            ):
                errors.append(f"{context}.anchor.latitude does not match its point.")
            if (
                isinstance(longitude, (int, float))
                and not isinstance(longitude, bool)
                and abs(float(longitude) - float(source_point["longitude"])) > 1e-7
            ):
                errors.append(f"{context}.anchor.longitude does not match its point.")
    elif anchor_point_id is not None:
        errors.append(f"{context}.anchor.pointId must be null for {kind} anchors.")


def apply_proposal(
    package: LoadedPackage,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    validate_proposal(package, proposal, final=True)
    result = deepcopy(package.plan_root)
    plan = result["plan"]
    journey_notes = proposal["journeyNotes"]
    if journey_notes["status"] == "confirmed":
        plan["memo"] = merge_managed_journey_notes(
            str(plan.get("memo") or ""),
            journey_notes["markdown"],
        )
    created_at = _now()
    new_groups: list[dict[str, Any]] = []
    assignments: dict[str, tuple[str, int]] = {}
    for group_index, proposal_group in enumerate(proposal["groups"]):
        anchor = proposal_group.get("anchor") or {}
        new_groups.append(
            {
                "id": proposal_group["id"],
                "name": proposal_group["name"].strip(),
                "orderIndex": group_index,
                "orderMode": "manual",
                "anchorName": anchor.get("name"),
                "anchorLatitude": anchor.get("latitude"),
                "anchorLongitude": anchor.get("longitude"),
                "anchorPointId": anchor.get("pointId"),
                "note": proposal_group.get("note"),
                "createdAt": created_at,
            }
        )
        for point_index, point_id in enumerate(proposal_group["pointIds"]):
            assignments[point_id] = (proposal_group["id"], point_index)
    plan["groups"] = new_groups
    for point in plan["points"]:
        assignment = assignments.get(point["id"])
        if assignment is None:
            point["groupId"] = None
            point["groupOrderIndex"] = None
        else:
            point["groupId"], point["groupOrderIndex"] = assignment
    current_point_id = plan.get("currentPointId")
    if current_point_id in assignments:
        plan["currentGroupId"] = assignments[current_point_id][0]
    else:
        plan["currentGroupId"] = None
    return result


def record_route_policy(
    proposal: dict[str, Any],
    *,
    use_road_network: bool,
    adjust_for_time: bool,
    write_plan_notes: bool = False,
    time_budget_minutes: float | None = None,
    tradeoff_notes: str | None = None,
    network_backend: str | None = None,
    stations_assist: bool = False,
) -> None:
    if network_backend is not None and network_backend not in NETWORK_BACKENDS:
        raise ProposalError(f"Unknown network backend: {network_backend}")
    if network_backend is not None and not use_road_network:
        raise ProposalError("--network-backend requires --road-network yes.")
    if use_road_network and network_backend is None:
        network_backend = "valhalla"
    if time_budget_minutes is not None and time_budget_minutes <= 0:
        raise ProposalError("--time-budget-minutes must be positive.")
    if time_budget_minutes is not None and not adjust_for_time:
        raise ProposalError(
            "--time-budget-minutes requires --time-adjustment yes."
        )
    if (
        any(
            isinstance(item, Mapping) and item.get("kind", "time_tradeoff") == "time_tradeoff"
            for item in proposal.get("excludedPoints", [])
        )
        and not adjust_for_time
    ):
        raise ProposalError(
            "Cannot decline time adjustments while the proposal has excluded points."
        )
    proposal["routePolicy"] = {
        "confirmed": True,
        "confirmedAt": _now(),
        "useRoadNetwork": use_road_network,
        "networkBackend": network_backend,
        "adjustForTime": adjust_for_time,
        "writePlanNotes": write_plan_notes,
        "stationsAssist": stations_assist,
        "timeBudgetMinutes": time_budget_minutes,
        "tradeoffNotes": tradeoff_notes.strip() if tradeoff_notes else None,
    }
    proposal["stationAssist"] = empty_station_assist()
    proposal["transitions"] = []
    proposal["journeyNotes"] = {
        "status": "not_requested",
        "preparedAt": None,
        "confirmedAt": None,
        "routeFingerprint": None,
        "spec": None,
        "markdown": None,
    }
    for group in proposal.get("groups", []):
        if not isinstance(group, dict):
            continue
        group.pop("routeEvaluation", None)
        group.pop("access", None)
        group.pop("quality", None)
        _clear_routed_metrics(group)
    proposal.setdefault("dataSources", {})["walking"] = {
        "provider": "local",
        "method": "haversine_path_x_1.25",
        "status": "rough_estimate",
    }
    _reset_route_confirmation(proposal)


def exclude_points_for_time(
    package: LoadedPackage,
    proposal: dict[str, Any],
    exclusions: Sequence[tuple[str, str]],
) -> None:
    route_policy = proposal.get("routePolicy")
    if not isinstance(route_policy, Mapping) or route_policy.get("confirmed") is not True:
        raise ProposalError("Route policy gate is closed.")
    if route_policy.get("adjustForTime") is not True:
        raise ProposalError(
            "Point exclusions require --time-adjustment yes at the route policy gate."
        )
    _exclude_points(package, proposal, exclusions, kind="time_tradeoff")
    if route_policy.get("useRoadNetwork") is True:
        walking_source = proposal.get("dataSources", {}).get("walking", {})
        provider = (
            walking_source.get("provider")
            if isinstance(walking_source, Mapping)
            else None
        )
        update_walking_source_summary(
            proposal,
            provider=provider or "fossgis_valhalla_pedestrian_matrix",
        )


def exclude_points_for_curation(
    package: LoadedPackage,
    proposal: dict[str, Any],
    exclusions: Sequence[tuple[str, str]],
) -> None:
    """Record reviewed duplicate/unwanted points; the curation gate reopens."""
    if exclusions:
        _exclude_points(package, proposal, exclusions, kind="curation")
    review = proposal.setdefault("review", {})
    review["curationConfirmed"] = False
    review["curationConfirmedAt"] = None


def restore_curated_points(
    package: LoadedPackage,
    proposal: dict[str, Any],
    point_ids: Sequence[str],
) -> list[str]:
    """Undo curation exclusions, reinserting points at their former position."""
    wanted = set(point_ids)
    point_by_id = {point["id"]: point for point in package.plan["points"]}
    groups = {
        group["id"]: group
        for group in proposal.get("groups", [])
        if isinstance(group, dict)
    }
    restored: list[str] = []
    remaining: list[Any] = []
    for item in proposal.get("excludedPoints", []):
        if (
            not isinstance(item, Mapping)
            or item.get("kind") != "curation"
            or item.get("pointId") not in wanted
        ):
            remaining.append(item)
            continue
        group = groups.get(item.get("previousGroupId"))
        if group is None:
            remaining.append(item)
            continue
        position = min(int(item.get("previousOrderIndex") or 0), len(group["pointIds"]))
        group["pointIds"].insert(position, item["pointId"])
        _refresh_group_after_membership_change(group, point_by_id)
        restored.append(item["pointId"])
    if restored:
        proposal["excludedPoints"] = remaining
        review = proposal.setdefault("review", {})
        review["curationConfirmed"] = False
        review["curationConfirmedAt"] = None
        _reset_route_confirmation(proposal)
    return restored


def _exclude_points(
    package: LoadedPackage,
    proposal: dict[str, Any],
    exclusions: Sequence[tuple[str, str]],
    *,
    kind: str,
) -> None:
    if not exclusions:
        raise ProposalError("At least one point exclusion is required.")

    requested_ids = [point_id for point_id, _ in exclusions]
    duplicates = sorted(
        point_id for point_id in set(requested_ids) if requested_ids.count(point_id) > 1
    )
    if duplicates:
        raise ProposalError(f"Duplicate point exclusion IDs: {duplicates}")
    point_by_id = {point["id"]: point for point in package.plan["points"]}
    existing_ids = {
        item.get("pointId")
        for item in proposal.get("excludedPoints", [])
        if isinstance(item, Mapping)
    }
    unknown = sorted(point_id for point_id in requested_ids if point_id not in point_by_id)
    if unknown:
        raise ProposalError(f"Unknown point exclusion IDs: {unknown}")
    unplannable = sorted(
        point_id
        for point_id in requested_ids
        if not has_valid_coordinate(point_by_id[point_id])
    )
    if unplannable:
        raise ProposalError(f"Cannot exclude unplannable point IDs: {unplannable}")
    already_excluded = sorted(set(requested_ids) & existing_ids)
    if already_excluded:
        raise ProposalError(f"Points are already excluded: {already_excluded}")
    empty_reasons = sorted(
        point_id for point_id, reason in exclusions if not reason.strip()
    )
    if empty_reasons:
        raise ProposalError(f"Point exclusions require reasons: {empty_reasons}")

    group_by_point: dict[str, tuple[dict[str, Any], int]] = {}
    for group in proposal.get("groups", []):
        if not isinstance(group, dict):
            continue
        for order_index, point_id in enumerate(group.get("pointIds", [])):
            group_by_point[point_id] = (group, order_index)
    not_assigned = sorted(point_id for point_id in requested_ids if point_id not in group_by_point)
    if not_assigned:
        raise ProposalError(f"Points are not assigned to a route: {not_assigned}")

    excluded_at = _now()
    exclusions_by_group: dict[str, set[str]] = {}
    for point_id, reason in exclusions:
        group, order_index = group_by_point[point_id]
        group_id = group["id"]
        exclusions_by_group.setdefault(group_id, set()).add(point_id)
        proposal.setdefault("excludedPoints", []).append(
            {
                "pointId": point_id,
                "pointName": str(point_by_id[point_id].get("name") or point_id),
                "reason": reason.strip(),
                "kind": kind,
                "previousGroupId": group_id,
                "previousOrderIndex": order_index,
                "excludedAt": excluded_at,
            }
        )

    retained_groups: list[dict[str, Any]] = []
    for group in proposal.get("groups", []):
        if not isinstance(group, dict):
            continue
        removed_ids = exclusions_by_group.get(group.get("id"), set())
        if not removed_ids:
            retained_groups.append(group)
            continue
        group["pointIds"] = [
            point_id for point_id in group["pointIds"] if point_id not in removed_ids
        ]
        if not group["pointIds"]:
            continue
        _refresh_group_after_membership_change(group, point_by_id)
        retained_groups.append(group)
    proposal["groups"] = retained_groups
    retained_ids = {group["id"] for group in retained_groups}
    proposal["transitions"] = [
        item
        for item in proposal.get("transitions", [])
        if isinstance(item, Mapping)
        and item.get("fromGroupId") in retained_ids
        and item.get("toGroupId") in retained_ids
    ]
    for position, group in enumerate(retained_groups):
        ends = group.get("routeEnds")
        if (
            position == 0
            and isinstance(ends, dict)
            and isinstance(ends.get("start"), Mapping)
            and ends["start"].get("kind") == "previous-group"
        ):
            ends["start"] = None
    _reset_route_confirmation(proposal)


def update_group_with_walking_matrix(
    proposal: dict[str, Any],
    group_id: str,
    ordered_point_ids: list[str],
    *,
    distance_meters: float,
    duration_seconds: float,
    provider: str,
    extra_metrics: Mapping[str, Any] | None = None,
    access: Mapping[str, Any] | None = None,
    quality: Mapping[str, Any] | None = None,
) -> None:
    group = next(
        (item for item in proposal.get("groups", []) if item.get("id") == group_id),
        None,
    )
    if group is None:
        raise ProposalError(f"Unknown proposal group ID: {group_id}")
    if set(ordered_point_ids) != set(group.get("pointIds", [])):
        raise ProposalError("Walking refinement changed the group's point membership.")
    old_ids = group.get("pointIds", [])
    old_names = group.get("pointNames", [])
    if (
        isinstance(old_ids, list)
        and isinstance(old_names, list)
        and len(old_ids) == len(old_names)
    ):
        name_by_id = dict(zip(old_ids, old_names))
        group["pointNames"] = [
            name_by_id.get(point_id, point_id) for point_id in ordered_point_ids
        ]
    group["pointIds"] = ordered_point_ids
    metrics = group.setdefault("metrics", {})
    metrics["walkingPathMeters"] = round(distance_meters, 1)
    metrics["walkingDurationSeconds"] = round(duration_seconds, 1)
    metrics["walkingEstimateMethod"] = provider
    metrics.update(dict(extra_metrics or {}))
    evaluation: dict[str, Any] = {
        "status": "routed",
        "provider": provider,
        "pointIds": list(ordered_point_ids),
        "evaluatedAt": _now(),
    }
    if group.get("routeEnds"):
        evaluation["routeEnds"] = deepcopy(group["routeEnds"])
    group["routeEvaluation"] = evaluation
    if access:
        group["access"] = dict(access)
    else:
        group.pop("access", None)
    if quality is not None:
        group["quality"] = dict(quality)
    _reset_route_confirmation(proposal)


def record_group_route_evaluation(
    proposal: dict[str, Any],
    group_id: str,
    *,
    status: str,
    provider: str,
    reason: str,
) -> None:
    if status not in {"skipped", "failed"}:
        raise ProposalError("Route evaluation status must be skipped or failed.")
    group = next(
        (item for item in proposal.get("groups", []) if item.get("id") == group_id),
        None,
    )
    if group is None:
        raise ProposalError(f"Unknown proposal group ID: {group_id}")
    group["routeEvaluation"] = {
        "status": status,
        "provider": provider,
        "pointIds": list(group.get("pointIds", [])),
        "reason": reason,
        "evaluatedAt": _now(),
    }
    _reset_route_confirmation(proposal)


def update_walking_source_summary(
    proposal: dict[str, Any],
    *,
    provider: str,
) -> None:
    groups = [group for group in proposal.get("groups", []) if isinstance(group, Mapping)]
    evaluated = sum(
        1 for group in groups if isinstance(group.get("routeEvaluation"), Mapping)
    )
    proposal.setdefault("dataSources", {})["walking"] = {
        "provider": provider,
        "method": "pedestrian_many_to_many_matrix",
        "status": "evaluated" if evaluated == len(groups) else "partial",
        "evaluatedGroups": evaluated,
        "totalGroups": len(groups),
    }


def _refresh_group_after_membership_change(
    group: dict[str, Any],
    point_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    point_ids = group["pointIds"]
    geo_points = [
        GeoPoint(
            key=point_id,
            latitude=float(point_by_id[point_id]["latitude"]),
            longitude=float(point_by_id[point_id]["longitude"]),
            point_ids=(point_id,),
        )
        for point_id in point_ids
    ]
    matrix = distance_matrix(geo_points)
    indices = list(range(len(geo_points)))
    metrics = group.setdefault("metrics", {})
    metrics.update(cluster_metrics(indices, geo_points, matrix))
    direct_path = path_length(indices, matrix)
    metrics["directPathMeters"] = round(direct_path, 1)
    metrics["roughWalkingMeters"] = round(direct_path * 1.25, 1)
    _clear_routed_metrics(group)
    group.pop("access", None)
    group.pop("quality", None)
    group["pointNames"] = [
        str(point_by_id[point_id].get("name") or point_id) for point_id in point_ids
    ]
    medoid = geo_points[medoid_index(indices, matrix)]
    group["medoid"] = {
        "latitude": medoid.latitude,
        "longitude": medoid.longitude,
        "pointId": medoid.key,
    }
    anchor = group.get("anchor")
    if (
        isinstance(anchor, Mapping)
        and anchor.get("kind") == "point"
        and anchor.get("pointId") not in point_ids
    ):
        source_point = point_by_id[medoid.key]
        group["anchor"] = {
            "name": str(source_point.get("name") or medoid.key),
            "latitude": float(source_point["latitude"]),
            "longitude": float(source_point["longitude"]),
            "pointId": medoid.key,
            "kind": "point",
        }
        group.setdefault("warnings", []).append(
            "Anchor was excluded; a remaining medoid point was selected for review."
        )
    group.pop("routeEvaluation", None)


def _clear_routed_metrics(group: dict[str, Any]) -> None:
    metrics = group.get("metrics")
    if not isinstance(metrics, dict):
        return
    metrics.pop("walkingPathMeters", None)
    metrics.pop("walkingDurationSeconds", None)
    metrics.pop("accessMeters", None)
    metrics.pop("routedStopCount", None)
    metrics["walkingEstimateMethod"] = "haversine_path_x_1.25"


def _reset_route_confirmation(proposal: dict[str, Any]) -> None:
    review = proposal.setdefault("review", {})
    review["routesConfirmed"] = False
    review["routesConfirmedAt"] = None
    journey_notes = proposal.get("journeyNotes")
    if (
        isinstance(journey_notes, Mapping)
        and journey_notes.get("status") != "not_requested"
    ):
        proposal["journeyNotes"] = {
            "status": "not_requested",
            "preparedAt": None,
            "confirmedAt": None,
            "routeFingerprint": None,
            "spec": None,
            "markdown": None,
        }


def _stable_group_id(plan_id: str, point_ids: Sequence[str]) -> str:
    material = "\0".join([plan_id, *sorted(point_ids)]).encode("utf-8")
    return f"planned-{hashlib.sha256(material).hexdigest()[:16]}"


def _confidence(
    metrics: Mapping[str, Any],
    profile: PlanningProfile,
    stations: Sequence[Mapping[str, Any]],
) -> tuple[str, list[str]]:
    radius = float(metrics["radiusP90Meters"])
    point_count = int(metrics["pointCount"])
    score = 0
    reasons: list[str] = []
    if radius <= profile.max_radius_p90_meters * 0.65:
        score += 1
        reasons.append("片区紧凑")
    elif radius >= profile.max_radius_p90_meters * 0.9:
        score -= 1
        reasons.append("接近片区半径上限")
    if point_count >= 3:
        score += 1
        reasons.append("有多个点位互相支撑")
    else:
        score -= 1
        reasons.append("孤立或点位很少")
    if stations and float(stations[0].get("directDistanceMeters", 1e9)) <= 1_500:
        score += 1
        reasons.append("附近找到公开交通站点候选")
    if score >= 2:
        return "high", reasons
    if score <= -1:
        return "low", reasons
    return "medium", reasons


def _default_note(
    metrics: Mapping[str, Any],
    stations: Sequence[Mapping[str, Any]],
) -> str:
    parts = [
        f"{metrics['pointCount']} 个点位",
        f"P90 半径约 {round(float(metrics['radiusP90Meters']))} 米",
    ]
    if stations:
        parts.append(f"最近站点候选：{stations[0].get('name', '未命名站')}")
    return "；".join(parts)


def _is_placeholder_name(name: str) -> bool:
    stripped = name.strip()
    return any(stripped.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def proposal_summary(proposal: Mapping[str, Any]) -> dict[str, Any]:
    groups = proposal.get("groups", [])
    review = proposal.get("review")
    route_policy = proposal.get("routePolicy")
    return {
        "groups": len(groups) if isinstance(groups, list) else 0,
        "assignedPoints": sum(
            len(group.get("pointIds", []))
            for group in groups
            if isinstance(group, dict) and isinstance(group.get("pointIds"), list)
        )
        if isinstance(groups, list)
        else 0,
        "unassignedPoints": len(proposal.get("unassignedPointIds", []))
        if isinstance(proposal.get("unassignedPointIds"), list)
        else 0,
        "excludedPoints": len(proposal.get("excludedPoints", []))
        if isinstance(proposal.get("excludedPoints"), list)
        else 0,
        "areasConfirmed": review.get("areasConfirmed") is True
        if isinstance(review, Mapping)
        else False,
        "routesConfirmed": review.get("routesConfirmed") is True
        if isinstance(review, Mapping)
        else False,
        "routePolicyConfirmed": route_policy.get("confirmed") is True
        if isinstance(route_policy, Mapping)
        else False,
        "useRoadNetwork": route_policy.get("useRoadNetwork")
        if isinstance(route_policy, Mapping)
        else None,
        "adjustForTime": route_policy.get("adjustForTime")
        if isinstance(route_policy, Mapping)
        else None,
        "writePlanNotes": route_policy.get("writePlanNotes")
        if isinstance(route_policy, Mapping)
        else None,
        "journeyNotesStatus": proposal.get("journeyNotes", {}).get("status")
        if isinstance(proposal.get("journeyNotes"), Mapping)
        else None,
        "modules": proposal.get("modules", []),
        "groupKinds": _count_by(
            group.get("kind", "route") for group in groups if isinstance(group, Mapping)
        )
        if isinstance(groups, list)
        else {},
        "exclusionKinds": _count_by(
            item.get("kind", "time_tradeoff")
            for item in proposal.get("excludedPoints", [])
            if isinstance(item, Mapping)
        ),
        "curationConfirmed": review.get("curationConfirmed") is True
        if isinstance(review, Mapping)
        else False,
        "networkBackend": route_policy.get("networkBackend")
        if isinstance(route_policy, Mapping)
        else None,
        "stationsAssist": route_policy.get("stationsAssist")
        if isinstance(route_policy, Mapping)
        else None,
        "stationAssistStatus": proposal.get("stationAssist", {}).get("status")
        if isinstance(proposal.get("stationAssist"), Mapping)
        else None,
    }


def _count_by(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def ensure_source_not_output(
    source: Path,
    output: Path,
) -> None:
    if source.expanduser().resolve() == output.expanduser().resolve():
        raise PackageError("Input and output paths must be different.")
