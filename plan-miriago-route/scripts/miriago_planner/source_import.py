"""Review-gated source import proposals and MiriaGo plan integration."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .package_io import LoadedPackage, coordinate_problem

IMPORT_PROPOSAL_SCHEMA_VERSION = 1


class ImportProposalError(ValueError):
    """Raised when an import proposal has not passed its review gate."""


def build_import_proposal(
    sources: Sequence[Mapping[str, Any]],
    *,
    base: LoadedPackage | None = None,
    plan_name: str | None = None,
    area: str | None = None,
) -> dict[str, Any]:
    if not sources:
        raise ImportProposalError("At least one import source is required.")
    if base is None and (not _text(plan_name) or not _text(area)):
        raise ImportProposalError(
            "A new package requires both --plan-name and --area."
        )
    imported_works: dict[str, dict[str, Any]] = {}
    source_works: list[tuple[str, dict[str, Any]]] = []
    point_values: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_summaries: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()

    for source in sources:
        provider = source.get("provider")
        source_id = source.get("sourceId")
        if not isinstance(provider, str) or not isinstance(source_id, str):
            raise ImportProposalError("Every source requires provider and sourceId.")
        if source_id in seen_source_ids:
            warnings.append(f"Repeated import source skipped: {source_id}")
            continue
        seen_source_ids.add(source_id)
        source_summaries.append(
            {
                "provider": provider,
                "sourceId": source_id,
                "label": source.get("label"),
                "metadata": deepcopy(source.get("metadata") or {}),
            }
        )
        warnings.extend(
            value
            for value in source.get("warnings", [])
            if isinstance(value, str)
        )
        for item in source.get("works", []):
            if not isinstance(item, Mapping):
                continue
            work_ref = item.get("sourceWorkRef")
            work = item.get("data")
            if not isinstance(work_ref, str) or not isinstance(work, Mapping):
                continue
            normalized = deepcopy(dict(work))
            work_id = normalized.get("id")
            if not isinstance(work_id, str) or not work_id.strip():
                raise ImportProposalError(f"Imported work {work_ref} has no valid ID.")
            existing = imported_works.get(work_id)
            if existing is not None and existing != normalized:
                raise ImportProposalError(f"Imported work ID collision: {work_id}")
            imported_works[work_id] = normalized
            source_works.append((work_ref, normalized))
        for item in source.get("points", []):
            if isinstance(item, Mapping):
                point_values.append(deepcopy(dict(item)))

    base_works = list(base.plan.get("works", [])) if base else []
    mappings = _build_work_mappings(source_works, base_works, imported_works)
    mapping_by_ref = {item["sourceWorkRef"]: item for item in mappings}
    duplicates: list[dict[str, Any]] = []
    selected_points: list[dict[str, Any]] = []
    seen_point_ids: dict[str, str] = {}
    base_points = list(base.plan.get("points", [])) if base else []
    base_ids = {
        point.get("id") for point in base_points if isinstance(point.get("id"), str)
    }
    base_identities = {
        (point.get("source"), point.get("sourceId"), point.get("workId")): point.get("id")
        for point in base_points
        if point.get("sourceId") is not None
    }

    for item in point_values:
        work_ref = item.get("sourceWorkRef")
        data = item.get("data")
        if not isinstance(work_ref, str) or not isinstance(data, dict):
            raise ImportProposalError("Imported point requires sourceWorkRef and data.")
        if work_ref not in mapping_by_ref:
            raise ImportProposalError(f"Point refers to an unknown source work: {work_ref}")
        point_id = data.get("id")
        if not isinstance(point_id, str) or not point_id.strip():
            raise ImportProposalError("Imported point has no valid ID.")
        target_work = mapping_by_ref[work_ref]["selectedWorkId"]
        duplicate_of: str | None = None
        reason: str | None = None
        if point_id in base_ids:
            duplicate_of, reason = point_id, "same_point_id_in_base"
        elif point_id in seen_point_ids:
            duplicate_of, reason = seen_point_ids[point_id], "same_point_id_in_import"
        else:
            identity = (data.get("source"), data.get("sourceId"), target_work)
            if data.get("sourceId") is not None and identity in base_identities:
                duplicate_of = str(base_identities[identity])
                reason = "same_source_identity_in_base"
        selected = bool(item.get("selected", True)) and duplicate_of is None
        normalized_item = {
            "sourceWorkRef": work_ref,
            "selected": selected,
            "duplicateOf": duplicate_of,
            "data": data,
        }
        selected_points.append(normalized_item)
        if duplicate_of:
            duplicates.append(
                {
                    "pointId": point_id,
                    "duplicateOf": duplicate_of,
                    "reason": reason,
                }
            )
        else:
            seen_point_ids[point_id] = point_id

    near_duplicate_warnings = _near_duplicate_warnings(
        selected_points,
        mappings,
        base_points,
    )
    warnings.extend(near_duplicate_warnings)
    selected_count = sum(item["selected"] for item in selected_points)
    proposal = {
        "schemaVersion": IMPORT_PROPOSAL_SCHEMA_VERSION,
        "kind": "miriago_source_import",
        "createdAt": _now(),
        "base": (
            {
                "path": str(base.path),
                "sha256": base.source_sha256,
                "planId": base.plan["id"],
                "planName": base.plan.get("name"),
            }
            if base
            else None
        ),
        "newPlan": (
            None
            if base
            else {"name": _text(plan_name), "area": _text(area), "memo": ""}
        ),
        "sources": source_summaries,
        "availableWorks": [
            *[
                {"origin": "base", "data": deepcopy(work)}
                for work in base_works
                if isinstance(work, dict)
            ],
            *[
                {"origin": "import", "data": deepcopy(work)}
                for work in imported_works.values()
            ],
        ],
        "importedWorks": list(imported_works.values()),
        "workMappings": mappings,
        "points": selected_points,
        "duplicates": duplicates,
        "warnings": list(dict.fromkeys(warnings)),
        "summary": {
            "sources": len(source_summaries),
            "importedWorks": len(imported_works),
            "candidatePoints": len(selected_points),
            "selectedPoints": selected_count,
            "exactDuplicatesSkipped": len(duplicates),
        },
        "review": {
            "confirmed": False,
            "confirmedAt": None,
            "requiredChecks": [
                "source identities and counts",
                "workMappings selectedWorkId values",
                "selected point scope",
                "duplicates and warnings",
            ],
        },
    }
    validate_import_proposal(base, proposal, final=False)
    return proposal


def validate_import_proposal(
    base: LoadedPackage | None,
    proposal: Mapping[str, Any],
    *,
    final: bool,
) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []
    if proposal.get("schemaVersion") != IMPORT_PROPOSAL_SCHEMA_VERSION:
        errors.append(
            f"import proposal schemaVersion must be {IMPORT_PROPOSAL_SCHEMA_VERSION}."
        )
    if proposal.get("kind") != "miriago_source_import":
        errors.append("proposal.kind must be 'miriago_source_import'.")
    base_info = proposal.get("base")
    if base_info is None:
        if base is not None:
            errors.append("Proposal creates a new plan but an input package was supplied.")
        new_plan = proposal.get("newPlan")
        if not isinstance(new_plan, Mapping):
            errors.append("A new-plan proposal requires newPlan metadata.")
        elif not _text(new_plan.get("name")) or not _text(new_plan.get("area")):
            errors.append("newPlan.name and newPlan.area must be non-empty.")
    elif not isinstance(base_info, Mapping):
        errors.append("proposal.base must be an object or null.")
    elif base is None:
        errors.append("This import proposal requires its original --input package.")
    else:
        if base_info.get("sha256") != base.source_sha256:
            errors.append("Import proposal source hash does not match the input package.")
        if base_info.get("planId") != base.plan.get("id"):
            errors.append("Import proposal planId does not match the input package.")

    imported_works = proposal.get("importedWorks")
    if not isinstance(imported_works, list) or any(
        not isinstance(work, dict) for work in imported_works
    ):
        errors.append("proposal.importedWorks must be a list of objects.")
        imported_works = []
    imported_by_id = _unique_objects_by_id(imported_works, "imported work", errors)
    base_by_id = {
        work["id"]: work
        for work in (base.plan.get("works", []) if base else [])
        if isinstance(work, dict) and isinstance(work.get("id"), str)
    }
    for work_id in set(imported_by_id) & set(base_by_id):
        imported = imported_by_id[work_id]
        existing = base_by_id[work_id]
        same_bangumi = (
            imported.get("bangumiId") is not None
            and imported.get("bangumiId") == existing.get("bangumiId")
        )
        if imported != existing and not same_bangumi:
            errors.append(f"Imported work ID collides with a different base work: {work_id}")
    known_work_ids = set(imported_by_id) | set(base_by_id)

    mappings = proposal.get("workMappings")
    if not isinstance(mappings, list) or any(
        not isinstance(item, dict) for item in mappings
    ):
        errors.append("proposal.workMappings must be a list of objects.")
        mappings = []
    mapping_by_ref: dict[str, str] = {}
    for index, mapping in enumerate(mappings):
        source_ref = mapping.get("sourceWorkRef")
        selected = mapping.get("selectedWorkId")
        if not isinstance(source_ref, str) or not source_ref:
            errors.append(f"workMappings[{index}].sourceWorkRef is invalid.")
            continue
        if source_ref in mapping_by_ref:
            errors.append(f"Duplicate sourceWorkRef mapping: {source_ref}")
        if not isinstance(selected, str) or selected not in known_work_ids:
            errors.append(
                f"workMappings[{index}].selectedWorkId is not an available work: {selected!r}"
            )
            continue
        mapping_by_ref[source_ref] = selected

    raw_points = proposal.get("points")
    if not isinstance(raw_points, list) or any(
        not isinstance(item, dict) for item in raw_points
    ):
        errors.append("proposal.points must be a list of objects.")
        raw_points = []
    base_point_ids = {
        point.get("id")
        for point in (base.plan.get("points", []) if base else [])
        if isinstance(point, dict) and isinstance(point.get("id"), str)
    }
    selected_ids: set[str] = set()
    base_identities = {
        (point.get("source"), point.get("sourceId"), point.get("workId"))
        for point in (base.plan.get("points", []) if base else [])
        if isinstance(point, dict) and point.get("sourceId") is not None
    }
    selected_identities: set[tuple[Any, Any, Any]] = set()
    for index, item in enumerate(raw_points):
        selected = item.get("selected")
        work_ref = item.get("sourceWorkRef")
        data = item.get("data")
        if not isinstance(selected, bool):
            errors.append(f"points[{index}].selected must be a boolean.")
        if not isinstance(work_ref, str) or work_ref not in mapping_by_ref:
            errors.append(f"points[{index}] has no valid sourceWorkRef mapping.")
        if not isinstance(data, dict):
            errors.append(f"points[{index}].data must be an object.")
            continue
        point_id = data.get("id")
        if not isinstance(point_id, str) or not point_id.strip():
            errors.append(f"points[{index}].data.id is invalid.")
            continue
        issue = coordinate_problem(data)
        if issue:
            errors.append(f"points[{index}] {point_id!r}: {issue}")
        if selected:
            if point_id in selected_ids:
                errors.append(f"Selected import point ID is duplicated: {point_id}")
            if point_id in base_point_ids:
                errors.append(f"Selected import point already exists in base: {point_id}")
            selected_ids.add(point_id)
            target_work = mapping_by_ref.get(work_ref)
            if data.get("sourceId") is not None and isinstance(target_work, str):
                identity = (data.get("source"), data.get("sourceId"), target_work)
                if identity in base_identities:
                    errors.append(
                        f"Selected import point duplicates a base source identity: {point_id}"
                    )
                if identity in selected_identities:
                    errors.append(
                        f"Selected import source identity is duplicated: {point_id}"
                    )
                selected_identities.add(identity)
    if not selected_ids:
        if final:
            errors.append("Import proposal selects no new points.")
        else:
            warnings.append("Draft import proposal currently selects no new points.")

    review = proposal.get("review")
    if not isinstance(review, Mapping):
        errors.append("proposal.review must be an object.")
    elif final and review.get("confirmed") is not True:
        errors.append(
            "Import review gate is closed: set review.confirmed only after explicit user confirmation."
        )
    if not final:
        warnings.append("Draft validation does not pass the explicit import review gate.")
    if errors:
        raise ImportProposalError("\n".join(errors))
    return warnings


def apply_import_proposal(
    base: LoadedPackage | None,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    validate_import_proposal(base, proposal, final=True)
    if base is None:
        new_plan = proposal["newPlan"]
        created_at = _now()
        plan_name = _text(new_plan["name"])
        plan_id = f"imported-{_digest(f'{plan_name}\0{created_at}')[:20]}"
        result: dict[str, Any] = {
            "schemaVersion": 2,
            "exportMode": "plan_only",
            "plan": {
                "id": plan_id,
                "name": plan_name,
                "area": _text(new_plan["area"]),
                "memo": _text(new_plan.get("memo")),
                "createdAt": created_at,
                "updatedAt": created_at,
                "currentPointId": None,
                "currentGroupId": None,
                "completedPointIds": [],
                "works": [],
                "groups": [],
                "points": [],
            },
        }
    else:
        result = deepcopy(base.plan_root)

    plan = result["plan"]
    existing_works = {
        work["id"]: work
        for work in plan["works"]
        if isinstance(work, dict) and isinstance(work.get("id"), str)
    }
    imported_works = {
        work["id"]: work
        for work in proposal["importedWorks"]
        if isinstance(work, dict) and isinstance(work.get("id"), str)
    }
    mapping_by_ref = {
        item["sourceWorkRef"]: item["selectedWorkId"]
        for item in proposal["workMappings"]
    }
    selected_items = [item for item in proposal["points"] if item["selected"]]
    referenced_work_ids = {
        mapping_by_ref[item["sourceWorkRef"]] for item in selected_items
    }
    for work_id in sorted(referenced_work_ids):
        if work_id in existing_works:
            continue
        work = imported_works.get(work_id)
        if work is None:
            raise ImportProposalError(f"Mapped imported work is missing: {work_id}")
        plan["works"].append(deepcopy(work))
        existing_works[work_id] = work

    existing_point_ids = {
        point["id"]
        for point in plan["points"]
        if isinstance(point, dict) and isinstance(point.get("id"), str)
    }
    for item in selected_items:
        point = deepcopy(item["data"])
        if point["id"] in existing_point_ids:
            raise ImportProposalError(f"Point ID already exists: {point['id']}")
        point["workId"] = mapping_by_ref[item["sourceWorkRef"]]
        point["groupId"] = None
        point["groupOrderIndex"] = None
        plan["points"].append(point)
        existing_point_ids.add(point["id"])
    plan["updatedAt"] = _now()
    return result


def load_import_proposal(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(
            source.read_text(encoding="utf-8-sig"),
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ImportProposalError(f"Cannot read import proposal {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ImportProposalError("Import proposal must be a JSON object.")
    return value


def save_import_proposal(
    proposal: Mapping[str, Any],
    path: str | Path,
    *,
    force: bool = False,
) -> Path:
    target = Path(path).expanduser().resolve()
    if target.exists() and not force:
        raise ImportProposalError(f"Import proposal output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def import_proposal_summary(proposal: Mapping[str, Any]) -> dict[str, Any]:
    summary = proposal.get("summary")
    result = dict(summary) if isinstance(summary, Mapping) else {}
    review = proposal.get("review")
    result["reviewConfirmed"] = (
        review.get("confirmed") is True if isinstance(review, Mapping) else False
    )
    result["warnings"] = list(proposal.get("warnings") or [])
    return result


def _build_work_mappings(
    source_works: Sequence[tuple[str, dict[str, Any]]],
    base_works: Sequence[Mapping[str, Any]],
    imported_works: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    available = [*base_works, *imported_works.values()]
    result: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for work_ref, work in source_works:
        if work_ref in seen_refs:
            continue
        seen_refs.add(work_ref)
        selected = work["id"]
        reason = "new_imported_work"
        bangumi_id = work.get("bangumiId")
        if bangumi_id is not None:
            matches = [item for item in base_works if item.get("bangumiId") == bangumi_id]
            if len(matches) == 1:
                selected, reason = matches[0]["id"], "same_bangumi_id_in_base"
        elif work_ref.startswith("mymaps:"):
            names = {_normalize_name(work.get("title")), _normalize_name(work.get("subtitle"))}
            names.discard("")
            matches = [
                item
                for item in available
                if item.get("id") != work.get("id")
                and names
                & {
                    _normalize_name(item.get("title")),
                    _normalize_name(item.get("subtitle")),
                }
            ]
            unique_ids = {item.get("id") for item in matches}
            if len(unique_ids) == 1:
                selected = str(matches[0]["id"])
                reason = "exact_normalized_title"
        result.append(
            {
                "sourceWorkRef": work_ref,
                "sourceLabel": work.get("title"),
                "suggestedWorkId": selected,
                "selectedWorkId": selected,
                "matchReason": reason,
                "requiresReview": True,
            }
        )
    return result


def _near_duplicate_warnings(
    points: Sequence[Mapping[str, Any]],
    mappings: Sequence[Mapping[str, Any]],
    base_points: Sequence[Mapping[str, Any]],
) -> list[str]:
    mapping_by_ref = {
        item.get("sourceWorkRef"): item.get("selectedWorkId") for item in mappings
    }
    candidates: list[tuple[str, str, float, float, str | None, str]] = []
    for point in base_points:
        if _point_coordinate(point) is None:
            continue
        latitude, longitude = _point_coordinate(point) or (0.0, 0.0)
        candidates.append(
            (
                str(point.get("id")),
                _normalize_name(point.get("name")),
                latitude,
                longitude,
                point.get("workId") if isinstance(point.get("workId"), str) else None,
                "base",
            )
        )
    warnings: list[str] = []
    for item in points:
        if item.get("selected") is not True:
            continue
        point = item.get("data")
        if not isinstance(point, Mapping) or _point_coordinate(point) is None:
            continue
        latitude, longitude = _point_coordinate(point) or (0.0, 0.0)
        normalized_name = _normalize_name(point.get("name"))
        target_work = mapping_by_ref.get(item.get("sourceWorkRef"))
        source_group = _source_group(item.get("sourceWorkRef"))
        for (
            other_id,
            other_name,
            other_lat,
            other_lon,
            other_work,
            other_source_group,
        ) in candidates:
            if (
                source_group != other_source_group
                and normalized_name
                and normalized_name == other_name
                and target_work == other_work
                and _haversine(latitude, longitude, other_lat, other_lon) <= 20
            ):
                warnings.append(
                    f"Possible cross-source duplicate within 20 m: "
                    f"{point.get('id')} and {other_id}."
                )
        candidates.append(
            (
                str(point.get("id")),
                normalized_name,
                latitude,
                longitude,
                target_work if isinstance(target_work, str) else None,
                source_group,
            )
        )
    return list(dict.fromkeys(warnings))


def _unique_objects_by_id(
    values: Sequence[dict[str, Any]],
    context: str,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            errors.append(f"Every {context} requires a non-empty id.")
        elif item_id in result:
            errors.append(f"Duplicate {context} ID: {item_id}")
        else:
            result[item_id] = item
    return result


def _point_coordinate(value: Mapping[str, Any]) -> tuple[float, float] | None:
    latitude, longitude = value.get("latitude"), value.get("longitude")
    if (
        isinstance(latitude, bool)
        or isinstance(longitude, bool)
        or not isinstance(latitude, (int, float))
        or not isinstance(longitude, (int, float))
    ):
        return None
    return float(latitude), float(longitude)


def _haversine(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    lat_a, lat_b = math.radians(latitude_a), math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(value)))


def _normalize_name(value: Any) -> str:
    text = value.casefold() if isinstance(value, str) else ""
    return "".join(character for character in text if character.isalnum())


def _source_group(value: Any) -> str:
    source_ref = value if isinstance(value, str) else ""
    return source_ref.split(":layer:", 1)[0]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
