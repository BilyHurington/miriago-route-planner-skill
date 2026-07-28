#!/usr/bin/env python3
"""Command-line workflow for planning MiriaGo v2 packages."""

from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miriago_planner import __version__
from miriago_planner.bangumi_anitabi import (
    SUBJECT_TYPES,
    SourceImportError,
    collect_anitabi_source,
    search_bangumi,
)
from miriago_planner.clustering import profile_with_overrides
from miriago_planner.google_my_maps import collect_google_my_maps_source
from miriago_planner.journey_notes import (
    JourneyNotesError,
    load_journey_notes_spec,
    merge_managed_journey_notes,
    render_journey_notes,
    route_fingerprint,
)
from miriago_planner.network_io import NetworkError
from miriago_planner.package_io import (
    DERIVED_ENTRIES,
    PackageError,
    create_package,
    load_package,
    package_summary,
    rebuild_package,
)
from miriago_planner.proposal import (
    ProposalError,
    apply_proposal,
    ensure_source_not_output,
    exclude_points_for_time,
    generate_proposal,
    load_proposal,
    proposal_summary,
    record_group_route_evaluation,
    record_route_policy,
    save_proposal,
    update_group_with_walking_matrix,
    update_walking_source_summary,
    validate_proposal,
)
from miriago_planner.public_services import (
    DEFAULT_OVERPASS_ENDPOINT,
    DEFAULT_VALHALLA_ENDPOINT,
    JsonCache,
    PublicServiceError,
    fetch_stations,
    fetch_walking_matrix,
    optimize_walking_order,
    station_boxes_from_proposal,
)
from miriago_planner.reference_images import (
    DEFAULT_IMAGE_DOWNLOAD_WORKERS,
    MAX_IMAGE_DOWNLOAD_WORKERS,
    ReferenceImageError,
    bundle_reference_images,
)
from miriago_planner.source_import import (
    ImportProposalError,
    apply_import_proposal,
    build_import_proposal,
    import_proposal_summary,
    load_import_proposal,
    save_import_proposal,
    validate_import_proposal,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Plan MiriaGo v2 .sjhplan packages without modifying the input.")
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser(
        "search-work",
        help="Search Bangumi by work name before selecting an Anitabi import ID.",
    )
    search_parser.add_argument("keyword")
    search_parser.add_argument(
        "--type",
        action="append",
        choices=tuple(SUBJECT_TYPES),
        dest="subject_types",
        help="Bangumi subject type; repeat to select multiple types.",
    )
    search_parser.add_argument("--limit", type=int, default=12)
    search_parser.set_defaults(handler=_search_work)

    import_parser = subparsers.add_parser(
        "prepare-import",
        help="Collect sources into a review-gated import proposal.",
    )
    import_parser.add_argument(
        "--input",
        help="Existing MiriaGo v2 package to extend; omit to create a new plan.",
    )
    import_parser.add_argument(
        "--anitabi-bangumi-id",
        action="append",
        type=int,
        default=[],
    )
    import_parser.add_argument(
        "--my-maps",
        action="append",
        default=[],
        help="Public Google My Maps URL or local KML/KMZ; repeat as needed.",
    )
    import_parser.add_argument("--plan-name")
    import_parser.add_argument("--area")
    import_parser.add_argument("--output", required=True)
    import_parser.add_argument("--force", action="store_true")
    import_parser.set_defaults(handler=_prepare_import)

    validate_import_parser = subparsers.add_parser(
        "validate-import",
        help="Validate an import proposal and its source binding.",
    )
    validate_import_parser.add_argument("proposal")
    validate_import_parser.add_argument("--input")
    validate_import_parser.add_argument("--draft", action="store_true")
    validate_import_parser.set_defaults(handler=_validate_import)

    confirm_import_parser = subparsers.add_parser(
        "confirm-import",
        help="Record explicit user approval of a reviewed import proposal.",
    )
    confirm_import_parser.add_argument("proposal")
    confirm_import_parser.add_argument("--input")
    confirm_import_parser.add_argument("--output", required=True)
    confirm_import_parser.add_argument("--force", action="store_true")
    confirm_import_parser.set_defaults(handler=_confirm_import)

    apply_import_parser = subparsers.add_parser(
        "apply-import",
        help="Apply only an explicitly confirmed import proposal.",
    )
    apply_import_parser.add_argument("proposal")
    apply_import_parser.add_argument("--input")
    apply_import_parser.add_argument("--output", required=True)
    _add_reference_image_policy(apply_import_parser)
    apply_import_parser.add_argument("--force", action="store_true")
    apply_import_parser.set_defaults(handler=_apply_import)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect and validate a MiriaGo package."
    )
    inspect_parser.add_argument("input")
    inspect_parser.set_defaults(handler=_inspect)

    propose_parser = subparsers.add_parser(
        "propose", help="Create a deterministic draft grouping proposal."
    )
    propose_parser.add_argument("input")
    propose_parser.add_argument("--output", required=True)
    propose_parser.add_argument(
        "--profile",
        choices=("compact", "balanced", "broad"),
        default="balanced",
    )
    propose_parser.add_argument("--max-points", type=int)
    propose_parser.add_argument("--max-radius-meters", type=float)
    propose_parser.add_argument("--hard-link-meters", type=float)
    propose_parser.add_argument(
        "--fetch-stations",
        action="store_true",
        help="Bulk-fetch nearby public transport candidates through Overpass.",
    )
    propose_parser.add_argument(
        "--overpass-endpoint",
        default=DEFAULT_OVERPASS_ENDPOINT,
    )
    propose_parser.add_argument("--cache-dir")
    propose_parser.add_argument("--force", action="store_true")
    propose_parser.set_defaults(handler=_propose)

    proposal_parser = subparsers.add_parser(
        "validate-proposal",
        help="Validate proposal membership and source integrity.",
    )
    proposal_parser.add_argument("input")
    proposal_parser.add_argument("proposal")
    proposal_parser.add_argument(
        "--draft",
        action="store_true",
        help="Permit generated placeholder names.",
    )
    proposal_parser.add_argument(
        "--stage",
        choices=("draft", "areas", "route-policy", "final"),
        help="Validation gate; defaults to final unless --draft is used.",
    )
    proposal_parser.set_defaults(handler=_validate_proposal)

    confirm_parser = subparsers.add_parser(
        "confirm-proposal",
        help="Record explicit user approval at the area or route gate.",
    )
    confirm_parser.add_argument("input")
    confirm_parser.add_argument("proposal")
    confirm_parser.add_argument("--stage", choices=("areas", "routes"), required=True)
    confirm_parser.add_argument("--output", required=True)
    confirm_parser.add_argument("--force", action="store_true")
    confirm_parser.set_defaults(handler=_confirm_proposal)

    route_policy_parser = subparsers.add_parser(
        "confirm-route-policy",
        help=(
            "Record explicit road-network, time-adjustment, and plan-note choices "
            "after area approval."
        ),
    )
    route_policy_parser.add_argument("input")
    route_policy_parser.add_argument("proposal")
    route_policy_parser.add_argument(
        "--road-network",
        choices=("yes", "no"),
        required=True,
        help="Explicit choice; yes is recommended but is never inferred.",
    )
    route_policy_parser.add_argument(
        "--time-adjustment",
        choices=("yes", "no"),
        required=True,
        help="Allow route-time tradeoffs, including explicit point exclusions.",
    )
    route_policy_parser.add_argument(
        "--plan-notes",
        choices=("yes", "no"),
        required=True,
        help="Write reviewed travel segments, walking estimates, and cautions to memo.",
    )
    route_policy_parser.add_argument("--time-budget-minutes", type=float)
    route_policy_parser.add_argument("--tradeoff-notes")
    route_policy_parser.add_argument("--output", required=True)
    route_policy_parser.add_argument("--force", action="store_true")
    route_policy_parser.set_defaults(handler=_confirm_route_policy)

    exclude_parser = subparsers.add_parser(
        "exclude-points",
        help="Record explicitly approved point exclusions without deleting source data.",
    )
    exclude_parser.add_argument("input")
    exclude_parser.add_argument("proposal")
    exclude_parser.add_argument(
        "--exclude-point",
        action="append",
        nargs=2,
        required=True,
        metavar=("POINT_ID", "REASON"),
        help="Point ID and user-approved reason; repeat as needed.",
    )
    exclude_parser.add_argument("--output", required=True)
    exclude_parser.add_argument("--force", action="store_true")
    exclude_parser.set_defaults(handler=_exclude_points)

    prepare_notes_parser = subparsers.add_parser(
        "prepare-notes",
        help="Render structured journey notes for explicit review before package write.",
    )
    prepare_notes_parser.add_argument("input")
    prepare_notes_parser.add_argument("proposal")
    prepare_notes_parser.add_argument("notes")
    prepare_notes_parser.add_argument("--output", required=True)
    prepare_notes_parser.add_argument("--force", action="store_true")
    prepare_notes_parser.set_defaults(handler=_prepare_notes)

    confirm_notes_parser = subparsers.add_parser(
        "confirm-notes",
        help="Record explicit approval of rendered journey notes.",
    )
    confirm_notes_parser.add_argument("input")
    confirm_notes_parser.add_argument("proposal")
    confirm_notes_parser.add_argument("--output", required=True)
    confirm_notes_parser.add_argument("--force", action="store_true")
    confirm_notes_parser.set_defaults(handler=_confirm_notes)

    refine_parser = subparsers.add_parser(
        "refine",
        help="Optionally refine small group orders with public Valhalla walking data.",
    )
    refine_parser.add_argument("input")
    refine_parser.add_argument("proposal")
    refine_parser.add_argument("--output", required=True)
    refine_parser.add_argument(
        "--group-id",
        action="append",
        required=True,
        help="Group to refine; repeat to select more than one group.",
    )
    refine_parser.add_argument(
        "--valhalla-endpoint",
        default=DEFAULT_VALHALLA_ENDPOINT,
    )
    refine_parser.add_argument("--cache-dir")
    refine_parser.add_argument("--force", action="store_true")
    refine_parser.set_defaults(handler=_refine)

    apply_parser = subparsers.add_parser(
        "apply", help="Apply a completed proposal to a new .sjhplan package."
    )
    apply_parser.add_argument("input")
    apply_parser.add_argument("proposal")
    apply_parser.add_argument("--output", required=True)
    _add_reference_image_policy(apply_parser)
    apply_parser.add_argument("--force", action="store_true")
    apply_parser.set_defaults(handler=_apply)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate a package and optionally compare preserved entries."
    )
    validate_parser.add_argument("input")
    validate_parser.add_argument(
        "--source",
        help="Original package to verify non-derived ZIP entries byte-for-byte.",
    )
    validate_parser.set_defaults(handler=_validate_package)
    return parser


def _add_reference_image_policy(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--full-reference-images",
        choices=("include", "omit"),
        required=True,
        help=(
            "Required explicit export choice for Anitabi full-resolution images. "
            "Thumbnails and non-Anitabi external full images are always bundled."
        ),
    )
    parser.add_argument(
        "--image-download-workers",
        type=_image_download_worker_count,
        default=DEFAULT_IMAGE_DOWNLOAD_WORKERS,
        metavar="N",
        help=(
            "Concurrent reference-image downloads "
            f"(default: {DEFAULT_IMAGE_DOWNLOAD_WORKERS}; "
            f"range: 1-{MAX_IMAGE_DOWNLOAD_WORKERS})."
        ),
    )


def _image_download_worker_count(value: str) -> int:
    try:
        worker_count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= worker_count <= MAX_IMAGE_DOWNLOAD_WORKERS:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_IMAGE_DOWNLOAD_WORKERS}"
        )
    return worker_count


def _search_work(args: argparse.Namespace) -> dict[str, Any]:
    candidates = search_bangumi(
        args.keyword,
        subject_types=args.subject_types or ("anime", "game"),
        limit=args.limit,
    )
    return {
        "status": "bangumi_selection_required",
        "keyword": args.keyword,
        "candidates": candidates,
        "nextGate": "Present candidates and wait for the user to select a Bangumi ID.",
    }


def _prepare_import(args: argparse.Namespace) -> dict[str, Any]:
    base = load_package(args.input) if args.input else None
    sources = [
        collect_anitabi_source(bangumi_id)
        for bangumi_id in args.anitabi_bangumi_id
    ]
    sources.extend(
        collect_google_my_maps_source(source) for source in args.my_maps
    )
    proposal = build_import_proposal(
        sources,
        base=base,
        plan_name=args.plan_name,
        area=args.area,
    )
    output = save_import_proposal(proposal, args.output, force=args.force)
    return {
        "status": "import_review_required",
        "output": str(output),
        **import_proposal_summary(proposal),
        "nextGate": (
            "Review sources, workMappings, selected points, duplicates, and warnings; "
            "then ask the user for explicit confirmation."
        ),
    }


def _validate_import(args: argparse.Namespace) -> dict[str, Any]:
    base = load_package(args.input) if args.input else None
    proposal = load_import_proposal(args.proposal)
    warnings = validate_import_proposal(base, proposal, final=not args.draft)
    return {
        "status": "valid",
        "mode": "draft" if args.draft else "final",
        **import_proposal_summary(proposal),
        "validationWarnings": warnings,
    }


def _confirm_import(args: argparse.Namespace) -> dict[str, Any]:
    base = load_package(args.input) if args.input else None
    proposal = load_import_proposal(args.proposal)
    validate_import_proposal(base, proposal, final=False)
    result = deepcopy(proposal)
    result["review"]["confirmed"] = True
    result["review"]["confirmedAt"] = _now()
    validate_import_proposal(base, result, final=True)
    output = save_import_proposal(result, args.output, force=args.force)
    return {
        "status": "import_confirmed",
        "output": str(output),
        **import_proposal_summary(result),
    }


def _apply_import(args: argparse.Namespace) -> dict[str, Any]:
    base = load_package(args.input) if args.input else None
    proposal = load_import_proposal(args.proposal)
    plan_root = apply_import_proposal(base, proposal)
    image_bundle = bundle_reference_images(
        plan_root,
        existing_entries=base.entries if base else None,
        include_full_reference_images=args.full_reference_images == "include",
        max_workers=args.image_download_workers,
        progress=_emit_image_download_progress,
    )
    plan_root = image_bundle.plan_root
    source_ids = [
        item.get("sourceId")
        for item in proposal.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("sourceId"), str)
    ]
    if base is None:
        output = create_package(
            plan_root,
            args.output,
            planner_version=__version__,
            import_metadata={
                "sources": source_ids,
                "referenceImages": image_bundle.summary,
            },
            additional_entries=image_bundle.entries,
            force=args.force,
        )
        preserved_input = None
    else:
        ensure_source_not_output(base.path, Path(args.output))
        output = rebuild_package(
            base,
            plan_root,
            args.output,
            planner_version=__version__,
            operation="imported",
            operation_metadata={
                "sources": source_ids,
                "referenceImages": image_bundle.summary,
            },
            additional_entries=image_bundle.entries,
            force=args.force,
        )
        preserved_input = str(base.path)
    package = load_package(output)
    return {
        "status": "imported_package_created",
        "output": str(output),
        "inputPreserved": preserved_input,
        "sha256": package.source_sha256,
        "counts": package_summary(package)["counts"],
        "referenceImages": image_bundle.summary,
    }


def _inspect(args: argparse.Namespace) -> dict[str, Any]:
    return package_summary(load_package(args.input))


def _propose(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    ensure_source_not_output(package.path, Path(args.output))
    profile = profile_with_overrides(
        args.profile,
        max_points=args.max_points,
        max_radius_meters=args.max_radius_meters,
        hard_link_meters=args.hard_link_meters,
    )
    proposal = generate_proposal(package, profile)
    if args.fetch_stations:
        result = fetch_stations(
            station_boxes_from_proposal(proposal),
            endpoint=args.overpass_endpoint,
            cache=JsonCache(args.cache_dir),
        )
        proposal = generate_proposal(
            package,
            profile,
            stations=result.stations,
            station_source=result.source,
        )
        proposal["warnings"].extend(result.warnings)
    validate_proposal(package, proposal, final=False)
    output = save_proposal(proposal, args.output, force=args.force)
    return {
        "status": "draft_proposal_created",
        "output": str(output),
        **proposal_summary(proposal),
        "warnings": proposal.get("warnings", []),
    }


def _validate_proposal(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal = load_proposal(args.proposal)
    stage = "draft" if args.draft else (args.stage or "final")
    warnings = validate_proposal(
        package,
        proposal,
        final=stage == "final",
        stage=stage,
    )
    return {
        "status": "valid",
        "mode": stage,
        **proposal_summary(proposal),
        "warnings": warnings,
    }


def _confirm_proposal(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal = load_proposal(args.proposal)
    result = deepcopy(proposal)
    if args.stage == "areas":
        validate_proposal(package, result, final=False, stage="draft")
        result["review"]["areasConfirmed"] = True
        result["review"]["areasConfirmedAt"] = _now()
        result["review"]["routesConfirmed"] = False
        result["review"]["routesConfirmedAt"] = None
        result["routePolicy"] = {
            "confirmed": False,
            "confirmedAt": None,
            "useRoadNetwork": None,
            "adjustForTime": None,
            "writePlanNotes": None,
            "timeBudgetMinutes": None,
            "tradeoffNotes": None,
        }
        result["journeyNotes"] = {
            "status": "not_requested",
            "preparedAt": None,
            "confirmedAt": None,
            "routeFingerprint": None,
            "spec": None,
            "markdown": None,
        }
        validate_proposal(package, result, final=False, stage="areas")
        status = "areas_confirmed"
    else:
        validate_proposal(package, result, final=False, stage="route-policy")
        result["review"]["routesConfirmed"] = True
        result["review"]["routesConfirmedAt"] = _now()
        validate_proposal(package, result, final=True, stage="final")
        status = "routes_confirmed"
    output = save_proposal(result, args.output, force=args.force)
    return {
        "status": status,
        "output": str(output),
        **proposal_summary(result),
    }


def _confirm_route_policy(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="areas")
    result = deepcopy(proposal)
    record_route_policy(
        result,
        use_road_network=args.road_network == "yes",
        adjust_for_time=args.time_adjustment == "yes",
        write_plan_notes=args.plan_notes == "yes",
        time_budget_minutes=args.time_budget_minutes,
        tradeoff_notes=args.tradeoff_notes,
    )
    validate_proposal(package, result, final=False, stage="route-policy")
    output = save_proposal(result, args.output, force=args.force)
    return {
        "status": "route_policy_confirmed",
        "output": str(output),
        **proposal_summary(result),
    }


def _exclude_points(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="route-policy")
    result = deepcopy(proposal)
    exclude_points_for_time(
        package,
        result,
        [(point_id, reason) for point_id, reason in args.exclude_point],
    )
    validate_proposal(package, result, final=False, stage="route-policy")
    output = save_proposal(result, args.output, force=args.force)
    return {
        "status": "route_tradeoffs_recorded",
        "output": str(output),
        **proposal_summary(result),
    }


def _prepare_notes(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal = load_proposal(args.proposal)
    result = deepcopy(proposal)
    result["journeyNotes"] = {
        "status": "not_requested",
        "preparedAt": None,
        "confirmedAt": None,
        "routeFingerprint": None,
        "spec": None,
        "markdown": None,
    }
    validate_proposal(package, result, final=False, stage="route-policy")
    if result["routePolicy"].get("writePlanNotes") is not True:
        raise ProposalError(
            "Plan-note writing was declined at the route policy gate."
        )
    spec = load_journey_notes_spec(args.notes)
    markdown = render_journey_notes(result, spec)
    memo_preview = merge_managed_journey_notes(
        str(package.plan.get("memo") or ""),
        markdown,
    )
    result["journeyNotes"] = {
        "status": "draft",
        "preparedAt": _now(),
        "confirmedAt": None,
        "routeFingerprint": route_fingerprint(result),
        "spec": spec,
        "markdown": markdown,
    }
    result["review"]["routesConfirmed"] = False
    result["review"]["routesConfirmedAt"] = None
    validate_proposal(package, result, final=False, stage="route-policy")
    output = save_proposal(result, args.output, force=args.force)
    return {
        "status": "journey_notes_review_required",
        "output": str(output),
        "markdown": markdown,
        "memoPreview": memo_preview,
        "nextGate": "Present the full memo preview and wait for explicit approval.",
    }


def _confirm_notes(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="route-policy")
    if proposal["routePolicy"].get("writePlanNotes") is not True:
        raise ProposalError(
            "Plan-note writing was declined at the route policy gate."
        )
    if proposal["journeyNotes"].get("status") != "draft":
        raise ProposalError("Journey notes must be prepared before confirmation.")
    result = deepcopy(proposal)
    result["journeyNotes"]["status"] = "confirmed"
    result["journeyNotes"]["confirmedAt"] = _now()
    result["review"]["routesConfirmed"] = False
    result["review"]["routesConfirmedAt"] = None
    validate_proposal(package, result, final=False, stage="route-policy")
    output = save_proposal(result, args.output, force=args.force)
    return {
        "status": "journey_notes_confirmed",
        "output": str(output),
        **proposal_summary(result),
    }


def _refine(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    ensure_source_not_output(package.path, Path(args.output))
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="route-policy")
    route_policy = proposal["routePolicy"]
    if route_policy.get("useRoadNetwork") is not True:
        raise ProposalError(
            "Road-network evaluation was declined at the route policy gate."
        )
    result = deepcopy(proposal)
    point_by_id = {point["id"]: point for point in package.plan["points"]}
    selected = set(args.group_id)
    known = {group["id"] for group in result["groups"]}
    unknown = sorted(selected - known)
    if unknown:
        raise ProposalError(f"Unknown --group-id values: {unknown}")
    cache = JsonCache(args.cache_dir)
    refined: list[str] = []
    skipped: list[dict[str, str]] = []
    eligible = [group for group in result["groups"] if group["id"] in selected]
    for index, group in enumerate(eligible):
        point_ids = group["pointIds"]
        if len(point_ids) < 2:
            reason = "fewer_than_2_points"
            skipped.append({"groupId": group["id"], "reason": reason})
            record_group_route_evaluation(
                result,
                group["id"],
                status="skipped",
                provider="fossgis_valhalla_pedestrian_matrix",
                reason=reason,
            )
            continue
        if len(point_ids) > 25:
            reason = "more_than_25_points"
            skipped.append({"groupId": group["id"], "reason": reason})
            record_group_route_evaluation(
                result,
                group["id"],
                status="skipped",
                provider="fossgis_valhalla_pedestrian_matrix",
                reason=reason,
            )
            continue
        locations = [
            {
                "latitude": point_by_id[point_id]["latitude"],
                "longitude": point_by_id[point_id]["longitude"],
            }
            for point_id in point_ids
        ]
        try:
            matrix = fetch_walking_matrix(
                locations,
                endpoint=args.valhalla_endpoint,
                cache=cache,
            )
            ordered, distance, duration = optimize_walking_order(point_ids, matrix)
        except PublicServiceError as exc:
            reason = str(exc)
            skipped.append({"groupId": group["id"], "reason": reason})
            record_group_route_evaluation(
                result,
                group["id"],
                status="failed",
                provider="fossgis_valhalla_pedestrian_matrix",
                reason=reason,
            )
            continue
        update_group_with_walking_matrix(
            result,
            group["id"],
            ordered,
            distance_meters=distance,
            duration_seconds=duration,
            provider="fossgis_valhalla_pedestrian_matrix",
        )
        refined.append(group["id"])
        if index + 1 < len(eligible):
            time.sleep(0.25)
    update_walking_source_summary(
        result,
        provider="fossgis_valhalla_pedestrian_matrix",
    )
    output = save_proposal(result, args.output, force=args.force)
    return {
        "status": "walking_refinement_finished",
        "output": str(output),
        "refinedGroupIds": refined,
        "skipped": skipped,
    }


def _apply(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal = load_proposal(args.proposal)
    plan_root = apply_proposal(package, proposal)
    image_bundle = bundle_reference_images(
        plan_root,
        existing_entries=package.entries,
        include_full_reference_images=args.full_reference_images == "include",
        max_workers=args.image_download_workers,
        progress=_emit_image_download_progress,
    )
    output = rebuild_package(
        package,
        image_bundle.plan_root,
        args.output,
        planner_version=__version__,
        operation_metadata={
            "referenceImages": image_bundle.summary,
            "journeyNotes": {
                "status": proposal.get("journeyNotes", {}).get("status"),
                "routeFingerprint": proposal.get("journeyNotes", {}).get(
                    "routeFingerprint"
                ),
            },
        },
        additional_entries=image_bundle.entries,
        force=args.force,
    )
    rebuilt = load_package(output)
    return {
        "status": "package_created",
        "output": str(output),
        "sha256": rebuilt.source_sha256,
        "inputPreserved": str(package.path),
        "counts": package_summary(rebuilt)["counts"],
        "referenceImages": image_bundle.summary,
        "journeyNotes": proposal.get("journeyNotes", {}).get("status"),
    }


def _validate_package(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    result = package_summary(package)
    result["status"] = "valid"
    if args.source:
        source = load_package(args.source)
        differences: list[str] = []
        for name, data in source.entries.items():
            if name not in DERIVED_ENTRIES and package.entries.get(name) != data:
                differences.append(name)
        added_non_derived = sorted(
            name
            for name in package.entries
            if name not in source.entries and name not in DERIVED_ENTRIES
        )
        if differences:
            raise PackageError(
                "Preserved ZIP entries differ from source: "
                f"{sorted(differences)}"
            )
        result["sourceComparison"] = {
            "source": str(source.path),
            "preservedEntries": len(
                [name for name in source.entries if name not in DERIVED_ENTRIES]
            ),
            "addedEntries": added_non_derived,
            "status": "byte_identical",
        }
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _emit_image_download_progress(event: dict[str, Any]) -> None:
    print(
        json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except (
        ImportProposalError,
        JourneyNotesError,
        NetworkError,
        PackageError,
        ProposalError,
        PublicServiceError,
        ReferenceImageError,
        SourceImportError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
