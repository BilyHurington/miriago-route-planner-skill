#!/usr/bin/env python3
"""Command-line workflow for planning MiriaGo v2 packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
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
from miriago_planner.curation import (
    CurationError,
    ThumbnailSource,
    apply_dedupe,
    apply_verify,
    draft_points,
    merge_classify,
    unverified_special_group,
    write_classify_batches,
    write_dedupe_batch,
    write_verify_batches,
)
from miriago_planner.day_planner import load_config, plan_days, select_points
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
    has_valid_coordinate,
    load_package,
    package_summary,
    rebuild_package,
)
from miriago_planner.proposal import (
    MODULES,
    ProposalError,
    apply_proposal,
    empty_route_policy,
    empty_station_assist,
    ensure_source_not_output,
    exclude_points_for_curation,
    exclude_points_for_time,
    generate_proposal,
    load_proposal,
    proposal_summary,
    record_route_policy,
    restore_curated_points,
    save_proposal,
    validate_proposal,
)
from miriago_planner.public_services import (
    DEFAULT_OVERPASS_ENDPOINT,
    DEFAULT_VALHALLA_ENDPOINT,
    DEFAULT_VALHALLA_ROUTE_ENDPOINT,
    JsonCache,
    PublicServiceError,
    fetch_stations,
    station_boxes_from_proposal,
)
from miriago_planner.reference_images import (
    DEFAULT_IMAGE_DOWNLOAD_WORKERS,
    MAX_IMAGE_DOWNLOAD_WORKERS,
    ReferenceImageError,
    bundle_reference_images,
)
from miriago_planner.review_server import ReviewStoreError, load_decisions, serve_review
from miriago_planner.route_preview import (
    GEOMETRY_MODES,
    PREVIEW_MODES,
    RoutePreviewError,
    render_route_preview,
)
from miriago_planner.route_refine import refine_groups
from miriago_planner.source_import import (
    ImportProposalError,
    apply_import_proposal,
    build_import_proposal,
    import_proposal_summary,
    load_import_proposal,
    save_import_proposal,
    validate_import_proposal,
)
from miriago_planner.stations import (
    assist_stations,
    draft_notes_spec,
    fetch_station_lines,
    station_candidates,
    stations_from_files,
)
from miriago_planner.walking import BACKENDS, make_backend


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
        "--bundle-meters",
        type=float,
        help="Merge scenes within this distance into one stop (complete linkage).",
    )
    propose_parser.add_argument(
        "--special-groups",
        help="JSON list of restricted/unverified groups kept out of route planning.",
    )
    _add_modules(propose_parser)
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

    plan_days_parser = subparsers.add_parser(
        "plan-days",
        help="Optional module: plan day-based route areas from a pool config.",
    )
    plan_days_parser.add_argument("input")
    plan_days_parser.add_argument("--config", required=True)
    plan_days_parser.add_argument("--output", required=True)
    plan_days_parser.add_argument(
        "--base-proposal",
        help="Earlier proposal whose group names selectors may use and whose curation is kept.",
    )
    plan_days_parser.add_argument("--geometry-output")
    _add_network_arguments(plan_days_parser)
    _add_modules(plan_days_parser)
    plan_days_parser.add_argument("--force", action="store_true")
    plan_days_parser.set_defaults(handler=_plan_days)

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
    route_policy_parser.add_argument(
        "--network-backend",
        choices=tuple(sorted({"valhalla", "osm"})),
        help="Road-network source when --road-network yes (default: valhalla).",
    )
    route_policy_parser.add_argument(
        "--stations-assist",
        choices=("yes", "no"),
        default="no",
        help="Plan station entries/exits and area transitions (stations-assist module).",
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
        help="Order every area on the approved walking network (Valhalla or offline OSM).",
    )
    refine_parser.add_argument("input")
    refine_parser.add_argument("proposal")
    refine_parser.add_argument("--output", required=True)
    refine_parser.add_argument(
        "--group-id",
        action="append",
        default=[],
        help="Group to refine; repeat to select more than one group.",
    )
    refine_parser.add_argument(
        "--all-groups",
        action="store_true",
        help="Refine every current group, in plan order.",
    )
    refine_parser.add_argument(
        "--backend",
        choices=("valhalla", "osm"),
        help="Defaults to the backend approved at the route policy gate.",
    )
    refine_parser.add_argument("--restarts", type=int, default=48)
    refine_parser.add_argument("--geometry-output")
    _add_network_arguments(refine_parser, backend=False)
    refine_parser.add_argument("--force", action="store_true")
    refine_parser.set_defaults(handler=_refine)

    preview_parser = subparsers.add_parser(
        "render-preview",
        help="Render a single-file interactive area and route preview.",
    )
    preview_parser.add_argument("input")
    preview_parser.add_argument("proposal")
    preview_parser.add_argument("--output", required=True)
    preview_parser.add_argument(
        "--geometry",
        choices=tuple(sorted(GEOMETRY_MODES)),
        default="auto",
        help=(
            "Route geometry mode. auto uses Valhalla only for explicitly approved "
            "and successfully evaluated groups."
        ),
    )
    preview_parser.add_argument(
        "--mode",
        choices=tuple(sorted(PREVIEW_MODES)),
        default="compact",
        help="review adds images, links, decisions, and quick classification.",
    )
    preview_parser.add_argument(
        "--geometry-file",
        help="Route shapes written by plan-days or refine --geometry-output.",
    )
    preview_parser.add_argument(
        "--no-basemap",
        action="store_true",
        help="Render routes and points without loading an online basemap.",
    )
    preview_parser.add_argument(
        "--valhalla-route-endpoint",
        default=DEFAULT_VALHALLA_ROUTE_ENDPOINT,
    )
    preview_parser.add_argument("--cache-dir")
    preview_parser.add_argument("--force", action="store_true")
    preview_parser.set_defaults(handler=_render_preview)

    stations_parser = subparsers.add_parser(
        "assist-stations",
        help="Optional module: choose station entries/exits and classify transitions.",
    )
    stations_parser.add_argument("input")
    stations_parser.add_argument("proposal")
    stations_parser.add_argument("--output", required=True)
    stations_parser.add_argument("--walk-threshold-meters", type=float, default=1_200)
    stations_parser.add_argument("--max-access-meters", type=float, default=1_500)
    stations_parser.add_argument(
        "--station-file",
        action="append",
        default=[],
        help="Local Overpass JSON with stations (repeatable); skips the station download.",
    )
    stations_parser.add_argument("--overpass-endpoint", default=DEFAULT_OVERPASS_ENDPOINT)
    stations_parser.add_argument("--cache-dir")
    stations_parser.add_argument("--force", action="store_true")
    stations_parser.set_defaults(handler=_assist_stations)

    notes_spec_parser = subparsers.add_parser(
        "draft-notes-spec",
        help="Write a journey-notes JSON skeleton from station transitions.",
    )
    notes_spec_parser.add_argument("input")
    notes_spec_parser.add_argument("proposal")
    notes_spec_parser.add_argument("--checked-at", required=True)
    notes_spec_parser.add_argument("--timezone", required=True)
    notes_spec_parser.add_argument("--title", default="巡礼行程建议")
    notes_spec_parser.add_argument("--output", required=True)
    notes_spec_parser.add_argument("--force", action="store_true")
    notes_spec_parser.set_defaults(handler=_draft_notes_spec)

    serve_parser = subparsers.add_parser(
        "serve-review",
        help="Optional module: serve a review preview with a temporary decision store.",
    )
    serve_parser.add_argument("preview")
    serve_parser.add_argument("--db", required=True)
    serve_parser.add_argument("--package", help="Serve full reference images from this package.")
    serve_parser.add_argument("--port", type=int, default=8791)
    serve_parser.set_defaults(handler=_serve_review)

    apply_review_parser = subparsers.add_parser(
        "apply-review",
        help="Turn stored keep/delete decisions into a curation draft proposal.",
    )
    apply_review_parser.add_argument("input")
    apply_review_parser.add_argument("proposal")
    source = apply_review_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db")
    source.add_argument("--decisions")
    apply_review_parser.add_argument("--reason", default="人工审核删除（重复或不需要的画面）")
    apply_review_parser.add_argument("--output", required=True)
    apply_review_parser.add_argument("--force", action="store_true")
    apply_review_parser.set_defaults(handler=_apply_review)

    confirm_curation_parser = subparsers.add_parser(
        "confirm-curation",
        help="Record explicit approval of reviewed point removals.",
    )
    confirm_curation_parser.add_argument("input")
    confirm_curation_parser.add_argument("proposal")
    confirm_curation_parser.add_argument("--output", required=True)
    confirm_curation_parser.add_argument("--force", action="store_true")
    confirm_curation_parser.set_defaults(handler=_confirm_curation)

    dedupe_parser = subparsers.add_parser(
        "dedupe-candidates",
        help="Optional module: same-shot candidate groups and a picker page for an import draft.",
    )
    dedupe_parser.add_argument("draft")
    dedupe_parser.add_argument("--output-dir", required=True)
    dedupe_parser.add_argument("--meters", type=float, default=30)
    _add_thumbnail_arguments(dedupe_parser)
    dedupe_parser.set_defaults(handler=_dedupe_candidates)

    apply_dedupe_parser = subparsers.add_parser(
        "apply-dedupe", help="Apply dedupe decisions to a new import draft."
    )
    apply_dedupe_parser.add_argument("draft")
    apply_dedupe_parser.add_argument("--candidates", required=True)
    apply_dedupe_parser.add_argument("--decisions", required=True)
    apply_dedupe_parser.add_argument("--output", required=True)
    apply_dedupe_parser.add_argument("--force", action="store_true")
    apply_dedupe_parser.set_defaults(handler=_apply_dedupe)

    verify_parser = subparsers.add_parser(
        "verify-batches",
        help="Optional module: satellite/street-view check batches for an import draft.",
    )
    verify_parser.add_argument("draft")
    verify_parser.add_argument("--output-dir", required=True)
    verify_parser.add_argument("--meters", type=float, default=15)
    verify_parser.add_argument("--batch-points", type=int, default=120)
    _add_thumbnail_arguments(verify_parser)
    verify_parser.set_defaults(handler=_verify_batches)

    apply_verify_parser = subparsers.add_parser(
        "apply-verify", help="Apply verify.json results to a new import draft."
    )
    apply_verify_parser.add_argument("draft")
    apply_verify_parser.add_argument("--batches-dir", required=True)
    apply_verify_parser.add_argument("--output", required=True)
    apply_verify_parser.add_argument(
        "--special-groups-output",
        help="Write an unverified special-group file for propose/plan-days.",
    )
    apply_verify_parser.add_argument("--force", action="store_true")
    apply_verify_parser.set_defaults(handler=_apply_verify)

    classify_parser = subparsers.add_parser(
        "classify-batches",
        help="Optional module: keep/optional/drop classification batches by area.",
    )
    classify_parser.add_argument("input")
    classify_parser.add_argument("proposal")
    classify_parser.add_argument("--output-dir", required=True)
    classify_parser.add_argument("--meters", type=float, default=30)
    classify_parser.add_argument("--batch-points", type=int, default=250)
    _add_thumbnail_arguments(classify_parser)
    classify_parser.set_defaults(handler=_classify_batches)

    merge_classify_parser = subparsers.add_parser(
        "merge-classify", help="Merge classify.json results into proposal.reviewHints."
    )
    merge_classify_parser.add_argument("input")
    merge_classify_parser.add_argument("proposal")
    merge_classify_parser.add_argument("--batches-dir", required=True)
    merge_classify_parser.add_argument("--output", required=True)
    merge_classify_parser.add_argument("--force", action="store_true")
    merge_classify_parser.set_defaults(handler=_merge_classify)

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


def _add_modules(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--modules",
        default="",
        help=f"Comma-separated optional modules chosen at the input gate: {', '.join(sorted(MODULES))}.",
    )


def _parse_modules(value: str) -> list[str]:
    modules = sorted({item.strip() for item in value.split(",") if item.strip()})
    unknown = sorted(set(modules) - MODULES)
    if unknown:
        raise ValueError(f"Unknown modules: {unknown}; choose from {sorted(MODULES)}.")
    return modules


def _add_network_arguments(parser: argparse.ArgumentParser, *, backend: bool = True) -> None:
    if backend:
        parser.add_argument(
            "--backend",
            choices=BACKENDS,
            help="Override the config's network backend.",
        )
    parser.add_argument(
        "--walknet-file",
        action="append",
        default=[],
        help="Local Overpass JSON walking network (repeatable); skips the download.",
    )
    parser.add_argument("--water-file", action="append", default=[])
    parser.add_argument("--overpass-endpoint", default=DEFAULT_OVERPASS_ENDPOINT)
    parser.add_argument("--valhalla-endpoint", default=DEFAULT_VALHALLA_ENDPOINT)
    parser.add_argument("--valhalla-route-endpoint", default=DEFAULT_VALHALLA_ROUTE_ENDPOINT)
    parser.add_argument("--cache-dir")


def _add_thumbnail_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--package", help="Take thumbnails from this package's assets.")
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Never download thumbnails; use package assets or skip images.",
    )
    parser.add_argument("--cache-dir")
    parser.add_argument("--force", action="store_true")


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
        bundle_distance_meters=args.bundle_meters,
    )
    modules = _parse_modules(args.modules)
    specials = _special_groups(package, args.special_groups)
    if specials:
        modules = sorted({*modules, "special-groups"})
    proposal = generate_proposal(package, profile, special_groups=specials, modules=modules)
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
            special_groups=specials,
            modules=modules,
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
        result["routePolicy"] = empty_route_policy()
        result["transitions"] = []
        result["stationAssist"] = empty_station_assist()
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
        network_backend=args.network_backend,
        stations_assist=args.stations_assist == "yes",
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
    approved = route_policy.get("networkBackend") or "valhalla"
    backend_name = args.backend or approved
    if backend_name != approved:
        raise ProposalError(
            f"The route policy approved the {approved} backend, not {backend_name}."
        )
    result = deepcopy(proposal)
    known = [group["id"] for group in result["groups"]]
    selected = set(known) if args.all_groups else set(args.group_id)
    if not selected:
        raise ProposalError("Pass --all-groups or at least one --group-id.")
    unknown = sorted(selected - set(known))
    if unknown:
        raise ProposalError(f"Unknown --group-id values: {unknown}")
    point_by_id = {point["id"]: point for point in package.plan["points"]}
    locations = _group_locations(result, point_by_id, selected)
    backend = make_backend(
        backend_name,
        locations=locations,
        walknet_files=args.walknet_file,
        water_files=args.water_file,
        overpass_endpoint=args.overpass_endpoint,
        valhalla_endpoint=args.valhalla_endpoint,
        valhalla_route_endpoint=args.valhalla_route_endpoint,
        cache=JsonCache(args.cache_dir),
        progress=_emit_progress,
    )
    geometry: dict[str, Any] | None = {"schemaVersion": 1, "groups": {}} if args.geometry_output else None
    summary = refine_groups(
        package,
        result,
        group_ids=selected,
        backend=backend,
        restarts=args.restarts,
        progress=_emit_progress,
        geometry=geometry,
    )
    if backend_name == "osm":
        result.setdefault("dataSources", {})["walking"]["network"] = backend.loaded.source
    output = save_proposal(result, args.output, force=args.force)
    geometry_path = _write_geometry(geometry, args.geometry_output, args.force)
    return {
        "status": "walking_refinement_finished",
        "output": str(output),
        "backend": backend_name,
        "geometryOutput": geometry_path,
        **summary,
    }


def _render_preview(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal_path = Path(args.proposal).expanduser().resolve()
    proposal = load_proposal(proposal_path)
    proposal_sha256 = hashlib.sha256(proposal_path.read_bytes()).hexdigest()
    result = render_route_preview(
        package,
        proposal,
        proposal_path=proposal_path,
        proposal_sha256=proposal_sha256,
        output=args.output,
        geometry_mode=args.geometry,
        no_basemap=args.no_basemap,
        route_endpoint=args.valhalla_route_endpoint,
        cache=JsonCache(args.cache_dir),
        force=args.force,
        progress=_emit_route_preview_progress,
        mode=args.mode,
        geometry_file=_read_json_file(args.geometry_file) if args.geometry_file else None,
    )
    return {
        "status": "route_preview_created",
        "output": str(result.output),
        "geometryMode": args.geometry,
        "mode": args.mode,
        "noBasemap": args.no_basemap,
        "groups": result.groups,
        "routedGroups": result.routed_groups,
        "fallbackGroups": result.fallback_groups,
        "excludedPoints": result.excluded_points,
        "unlocatedPoints": result.unlocated_points,
        "warnings": list(result.warnings),
    }


def _plan_days(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    ensure_source_not_output(package.path, Path(args.output))
    config = load_config(args.config)
    base = load_proposal(args.base_proposal) if args.base_proposal else None
    if base is not None:
        validate_proposal(package, base, final=False, stage="draft")
    network = config.get("network") if isinstance(config.get("network"), dict) else {}
    backend_name = args.backend or network.get("backend") or "straight"
    valid = [point for point in package.plan["points"] if has_valid_coordinate(point)]
    locations = [(float(point["latitude"]), float(point["longitude"])) for point in valid]
    cache = JsonCache(args.cache_dir)
    warnings: list[str] = []
    station_state: dict[str, Any] = {"source": None}

    station_files = list((config.get("stations") or {}).get("files") or [])

    def station_provider(pool_locations: list[tuple[float, float]]) -> list[dict[str, Any]]:
        if station_files:
            station_state["source"] = {"provider": "openstreetmap_overpass", "mode": "local_files", "files": station_files}
            return stations_from_files(
                station_files,
                pool_locations,
                search_meters=float(config.get("stationSearchMeters", 1_500)),
            )
        stations, source, station_warnings = station_candidates(
            pool_locations,
            search_meters=float(config.get("stationSearchMeters", 1_500)),
            endpoint=args.overpass_endpoint,
            cache=cache,
        )
        warnings.extend(station_warnings)
        station_state["source"] = source
        return stations

    backend = make_backend(
        backend_name,
        locations=locations,
        walknet_files=args.walknet_file or network.get("files") or [],
        water_files=args.water_file or network.get("waterFiles") or [],
        overpass_endpoint=args.overpass_endpoint,
        valhalla_endpoint=args.valhalla_endpoint,
        valhalla_route_endpoint=args.valhalla_route_endpoint,
        cache=cache,
        speed=float(config.get("speedMetersPerSecond", 1.2)),
        progress=_emit_progress,
    )
    geometry: dict[str, Any] | None = {"schemaVersion": 1, "groups": {}} if args.geometry_output else None
    modules = sorted({*_parse_modules(args.modules), "day-route"} | ({"osm-network"} if backend_name == "osm" else set()))
    proposal, report = plan_days(
        package,
        config,
        backend=backend,
        station_provider=station_provider,
        base_proposal=base,
        modules=modules,
        geometry=geometry,
        progress=_emit_progress,
    )
    proposal["warnings"].extend(warnings)
    if station_state["source"]:
        proposal["dataSources"]["stations"] = station_state["source"]
    if backend_name == "osm":
        proposal["dataSources"]["walking"]["network"] = backend.loaded.source
    validate_proposal(package, proposal, final=False, stage="draft")
    output = save_proposal(proposal, args.output, force=args.force)
    geometry_path = _write_geometry(geometry, args.geometry_output, args.force)
    return {
        "status": "draft_proposal_created",
        "output": str(output),
        "backend": backend_name,
        "geometryOutput": geometry_path,
        "areas": report,
        **proposal_summary(proposal),
        "warnings": proposal.get("warnings", []),
        "nextGate": "Render the area preview, review every day and area, then stop at the area gate.",
    }


def _assist_stations(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    ensure_source_not_output(package.path, Path(args.output))
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="route-policy")
    if proposal["routePolicy"].get("stationsAssist") is not True:
        raise ProposalError("Station assistance was not approved at the route policy gate.")
    result = deepcopy(proposal)
    point_by_id = {point["id"]: point for point in package.plan["points"]}
    locations = _group_locations(result, point_by_id, {group["id"] for group in result["groups"]})
    cache = JsonCache(args.cache_dir)
    if args.station_file:
        stations = stations_from_files(args.station_file, locations, search_meters=args.max_access_meters)
        source = {"provider": "openstreetmap_overpass", "mode": "local_files", "files": args.station_file}
        warnings: list[str] = []
    else:
        stations, source, warnings = station_candidates(
            locations,
            search_meters=args.max_access_meters,
            endpoint=args.overpass_endpoint,
            cache=cache,
        )
    lines, line_warnings = fetch_station_lines(stations, endpoint=args.overpass_endpoint, cache=cache)
    summary = assist_stations(
        package,
        result,
        stations=stations,
        lines=lines,
        walk_threshold_meters=args.walk_threshold_meters,
        max_access_meters=args.max_access_meters,
        source=source,
    )
    if result["routePolicy"].get("useRoadNetwork") is not True:
        from miriago_planner.walking import StraightBackend

        refine_groups(
            package,
            result,
            group_ids={group_id for group_id in summary["changedGroupIds"]},
            backend=StraightBackend(),
        )
        for group in result["groups"]:
            group.pop("routeEvaluation", None)
            metrics = group.get("metrics") or {}
            for key in ("walkingPathMeters", "walkingDurationSeconds", "routedStopCount", "accessMeters"):
                metrics.pop(key, None)
            metrics["walkingEstimateMethod"] = "haversine_path_x_1.25"
            for item in (group.get("access") or {}).values():
                item["estimate"] = "haversine_x_1.3"
        result["dataSources"]["walking"] = {
            "provider": "local",
            "method": "haversine_path_x_1.25",
            "status": "rough_estimate",
        }
    validate_proposal(package, result, final=False, stage="route-policy")
    output = save_proposal(result, args.output, force=args.force)
    return {
        "status": "stations_assisted",
        "output": str(output),
        "stationsFound": len(stations),
        **summary,
        "warnings": warnings + line_warnings + summary["warnings"],
        "next": "Run refine for changedGroupIds (or --all-groups) before route review."
        if result["routePolicy"].get("useRoadNetwork")
        else "Review the station ends and transitions at the route gate.",
    }


def _draft_notes_spec(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="route-policy")
    spec = draft_notes_spec(
        proposal,
        checked_at=args.checked_at,
        timezone_name=args.timezone,
        title=args.title,
    )
    target = Path(args.output).expanduser().resolve()
    if target.exists() and not args.force:
        raise ProposalError(f"Output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "journey_notes_skeleton_written",
        "output": str(target),
        "segments": len(spec["segments"]),
        "next": "Fill durations, fares, and official sources (or user-confirmed details) before prepare-notes.",
    }


def _serve_review(args: argparse.Namespace) -> dict[str, Any]:
    serve_review(args.preview, db=args.db, package=args.package, port=args.port)
    return {"status": "review_server_stopped"}


def _apply_review(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    ensure_source_not_output(package.path, Path(args.output))
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="draft")
    decisions = load_decisions(args.db or args.decisions)
    result = deepcopy(proposal)
    assigned = {
        point_id
        for group in result["groups"]
        for point_id in group.get("pointIds", [])
    }
    curated = {
        item.get("pointId")
        for item in result.get("excludedPoints", [])
        if isinstance(item, dict) and item.get("kind") == "curation"
    }
    restore = sorted(point_id for point_id in curated if decisions.get(point_id) != "delete")
    restored = restore_curated_points(package, result, restore)
    remove = sorted(
        point_id
        for point_id, decision in decisions.items()
        if decision == "delete" and point_id in assigned and point_id not in curated
    )
    exclude_points_for_curation(package, result, [(point_id, args.reason) for point_id in remove])
    known = {point["id"] for point in package.plan["points"]}
    validate_proposal(package, result, final=False, stage="draft")
    output = save_proposal(result, args.output, force=args.force)
    return {
        "status": "curation_review_required",
        "output": str(output),
        "removed": len(remove),
        "restored": len(restored),
        "kept": sum(1 for decision in decisions.values() if decision == "keep"),
        "unknownDecisionIds": sorted(set(decisions) - known)[:20],
        **proposal_summary(result),
        "nextGate": "Present the removals per area and wait for explicit approval, then run confirm-curation.",
    }


def _confirm_curation(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    ensure_source_not_output(package.path, Path(args.output))
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="draft")
    result = deepcopy(proposal)
    result["review"]["curationConfirmed"] = True
    result["review"]["curationConfirmedAt"] = _now()
    validate_proposal(package, result, final=False, stage="draft")
    output = save_proposal(result, args.output, force=args.force)
    return {"status": "curation_confirmed", "output": str(output), **proposal_summary(result)}


def _dedupe_candidates(args: argparse.Namespace) -> dict[str, Any]:
    draft_path = Path(args.draft).expanduser().resolve()
    draft = load_import_proposal(draft_path)
    output_dir = _fresh_dir(args.output_dir, args.force)
    result = write_dedupe_batch(
        draft_path,
        draft_points(draft),
        output_dir,
        meters=args.meters,
        thumbnails=_thumbnails(args),
    )
    return {
        "status": "dedupe_review_required",
        **result,
        "next": "Compare each group (subagents or the picker page dedupe-review.html), write decisions.json, then apply-dedupe.",
    }


def _apply_dedupe(args: argparse.Namespace) -> dict[str, Any]:
    draft = load_import_proposal(args.draft)
    candidates = _read_json_file(args.candidates)
    if candidates.get("source", {}).get("sha256") != hashlib.sha256(Path(args.draft).expanduser().read_bytes()).hexdigest():
        raise CurationError("Dedupe candidates were built from a different import draft.")
    result, log = apply_dedupe(draft, candidates, _read_json_file(args.decisions))
    validate_import_proposal(None if draft.get("base") is None else load_package(draft["base"]["path"]), result, final=False)
    output = save_import_proposal(result, args.output, force=args.force)
    return {
        "status": "import_review_required",
        "output": str(output),
        "removed": len(log["removed"]),
        "annotated": len(log["annotated"]),
        **import_proposal_summary(result),
    }


def _verify_batches(args: argparse.Namespace) -> dict[str, Any]:
    draft_path = Path(args.draft).expanduser().resolve()
    draft = load_import_proposal(draft_path)
    output_dir = _fresh_dir(args.output_dir, args.force)
    result = write_verify_batches(
        draft_path,
        draft_points(draft),
        output_dir,
        meters=args.meters,
        batch_points=args.batch_points,
        thumbnails=_thumbnails(args),
    )
    return {
        "status": "verification_batches_written",
        **result,
        "next": "Check each batch (subagents with their own built-in browser), write <batch>/verify.json, then apply-verify.",
    }


def _apply_verify(args: argparse.Namespace) -> dict[str, Any]:
    draft_path = Path(args.draft).expanduser().resolve()
    draft = load_import_proposal(draft_path)
    pairs = []
    missing = []
    for folder in sorted(Path(args.batches_dir).expanduser().glob("V*")):
        batch_input = _read_json_file(folder / "input.json")
        if batch_input.get("source", {}).get("sha256") != hashlib.sha256(draft_path.read_bytes()).hexdigest():
            raise CurationError(f"{folder.name} was built from a different import draft.")
        if not (folder / "verify.json").exists():
            missing.append(folder.name)
            continue
        pairs.append((batch_input, _read_json_file(folder / "verify.json")))
    if missing:
        raise CurationError(f"Batches without verify.json: {missing}")
    result, log = apply_verify(draft, pairs)
    output = save_import_proposal(result, args.output, force=args.force)
    specials_path = None
    if args.special_groups_output and log["unverifiable"]:
        target = Path(args.special_groups_output).expanduser().resolve()
        if target.exists() and not args.force:
            raise CurationError(f"Output already exists: {target}")
        target.write_text(
            json.dumps(unverified_special_group(log["unverifiable"]), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        specials_path = str(target)
    return {
        "status": "import_review_required",
        "output": str(output),
        **{key: len(value) for key, value in log.items()},
        "specialGroupsOutput": specials_path,
        **import_proposal_summary(result),
    }


def _classify_batches(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="draft")
    output_dir = _fresh_dir(args.output_dir, args.force)
    args.package = args.package or args.input
    result = write_classify_batches(
        package,
        proposal,
        output_dir,
        meters=args.meters,
        batch_points=args.batch_points,
        thumbnails=_thumbnails(args, package=package),
    )
    return {
        "status": "classification_batches_written",
        **result,
        "next": "Classify each batch (prefer shootable shots and famous scenes), write <batch>/classify.json, then merge-classify.",
    }


def _merge_classify(args: argparse.Namespace) -> dict[str, Any]:
    package = load_package(args.input)
    ensure_source_not_output(package.path, Path(args.output))
    proposal = load_proposal(args.proposal)
    validate_proposal(package, proposal, final=False, stage="draft")
    results = []
    missing = []
    for folder in sorted(Path(args.batches_dir).expanduser().glob("C*")):
        if (folder / "classify.json").exists():
            results.append(_read_json_file(folder / "classify.json"))
        else:
            missing.append(folder.name)
    if missing:
        raise CurationError(f"Batches without classify.json: {missing}")
    merged, counts = merge_classify(proposal, results, {point["id"] for point in package.plan["points"]})
    if "classify" not in merged.get("modules", []):
        merged["modules"] = sorted({*merged.get("modules", []), "classify"})
    output = save_proposal(merged, args.output, force=args.force)
    return {"status": "review_hints_merged", "output": str(output), "counts": counts}


def _special_groups(package: Any, path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    value = _read_json_file(path)
    if not isinstance(value, list):
        raise ProposalError("--special-groups must be a JSON list.")
    point_by_id = {point["id"]: point for point in package.plan["points"]}
    remaining = [point["id"] for point in package.plan["points"] if has_valid_coordinate(point)]
    groups = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, dict)
            or item.get("kind") not in {"restricted", "unverified"}
            or not str(item.get("name") or "").strip()
        ):
            raise ProposalError(f"special group {index} needs kind restricted|unverified and a name.")
        selector = item.get("select") or {"pointIds": item.get("pointIds", [])}
        ids = select_points(selector, remaining, point_by_id, {})
        if not ids:
            continue
        remaining = [point_id for point_id in remaining if point_id not in set(ids)]
        groups.append({"kind": item["kind"], "name": item["name"], "pointIds": ids, "note": item.get("note")})
    return groups


def _group_locations(
    proposal: dict[str, Any],
    point_by_id: dict[str, Any],
    group_ids: set[str],
) -> list[tuple[float, float]]:
    locations = [
        (float(point_by_id[point_id]["latitude"]), float(point_by_id[point_id]["longitude"]))
        for group in proposal["groups"]
        if group["id"] in group_ids
        for point_id in group.get("pointIds", [])
    ]
    for group in proposal["groups"]:
        ends = group.get("routeEnds") or {}
        for side in ("start", "end"):
            end = ends.get(side) if isinstance(ends, dict) else None
            if isinstance(end, dict) and "latitude" in end:
                locations.append((float(end["latitude"]), float(end["longitude"])))
    for rule in (proposal.get("constraints") or {}).get("via", []) or []:
        locations.append((float(rule["latitude"]), float(rule["longitude"])))
    return locations


def _thumbnails(args: argparse.Namespace, package: Any = None) -> ThumbnailSource:
    source_package = package or (load_package(args.package) if getattr(args, "package", None) else None)
    return ThumbnailSource(
        package=source_package,
        cache_dir=Path(args.cache_dir) / "thumbnails" if args.cache_dir else None,
        download=not args.no_download,
    )


def _fresh_dir(path: str, force: bool) -> Path:
    target = Path(path).expanduser().resolve()
    if target.exists() and any(target.iterdir()) and not force:
        raise CurationError(f"Output directory is not empty: {target} (use --force to reuse it)")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _read_json_file(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc


def _write_geometry(geometry: dict[str, Any] | None, path: str | None, force: bool) -> str | None:
    if geometry is None or not path:
        return None
    target = Path(path).expanduser().resolve()
    if target.exists() and not force:
        raise ProposalError(f"Geometry output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(geometry, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return str(target)


def _emit_progress(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)


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
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _emit_image_download_progress(event: dict[str, Any]) -> None:
    print(
        json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def _emit_route_preview_progress(event: dict[str, Any]) -> None:
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
        CurationError,
        ImportProposalError,
        JourneyNotesError,
        ReviewStoreError,
        NetworkError,
        PackageError,
        ProposalError,
        PublicServiceError,
        ReferenceImageError,
        RoutePreviewError,
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
