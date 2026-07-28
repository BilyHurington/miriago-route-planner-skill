"""Safe MiriaGo v2 package input, validation, and lossless rebuilding."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import zipfile
from copy import copy, deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

MIRIAGO_FORMAT = "miriago_export_package"
MIRIAGO_SCHEMA_VERSION = 2
DERIVED_ENTRIES = frozenset({"manifest.json", "plan.json", "points.csv", "records.csv"})
MAX_ENTRY_COUNT = 10_000
MAX_ENTRY_BYTES = 1_000_000_000
MAX_TOTAL_BYTES = 2_000_000_000


class PackageError(ValueError):
    """Raised when a package cannot be processed safely."""


@dataclass(frozen=True)
class LoadedPackage:
    path: Path
    source_sha256: str
    manifest: dict[str, Any]
    plan_root: dict[str, Any]
    entries: dict[str, bytes]
    entry_infos: dict[str, zipfile.ZipInfo]
    entry_order: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def plan(self) -> dict[str, Any]:
        return self.plan_root["plan"]


def load_package(path: str | Path) -> LoadedPackage:
    source_path = Path(path).expanduser().resolve()
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise PackageError(f"Cannot read package: {source_path}: {exc}") from exc
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    entries: dict[str, bytes] = {}
    infos: dict[str, zipfile.ZipInfo] = {}
    order: list[str] = []
    total_size = 0

    try:
        with zipfile.ZipFile(io.BytesIO(source_bytes), "r") as archive:
            archive_infos = archive.infolist()
            if len(archive_infos) > MAX_ENTRY_COUNT:
                raise PackageError(
                    f"Package contains too many ZIP entries ({len(archive_infos)})."
                )
            for info in archive_infos:
                name = _safe_entry_name(info.filename)
                if name in entries:
                    raise PackageError(f"Package contains duplicate ZIP entry: {name}")
                if info.flag_bits & 0x1:
                    raise PackageError(f"Encrypted ZIP entry is not supported: {name}")
                if info.file_size > MAX_ENTRY_BYTES:
                    raise PackageError(f"ZIP entry is too large: {name}")
                total_size += info.file_size
                if total_size > MAX_TOTAL_BYTES:
                    raise PackageError(
                        "Package uncompressed size exceeds the safety limit."
                    )
                try:
                    data = archive.read(info)
                except (
                    OSError,
                    RuntimeError,
                    NotImplementedError,
                    zipfile.BadZipFile,
                ) as exc:
                    raise PackageError(f"Cannot read ZIP entry {name}: {exc}") from exc
                if len(data) != info.file_size:
                    raise PackageError(f"ZIP entry size mismatch: {name}")
                entries[name] = data
                infos[name] = copy(info)
                order.append(name)
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise PackageError(f"Invalid MiriaGo ZIP package: {exc}") from exc

    manifest = _json_object(entries, "manifest.json")
    plan_root = _json_object(entries, "plan.json")
    warnings = _validate_root(manifest, plan_root)
    return LoadedPackage(
        path=source_path,
        source_sha256=source_hash,
        manifest=manifest,
        plan_root=plan_root,
        entries=entries,
        entry_infos=infos,
        entry_order=tuple(order),
        warnings=tuple(warnings),
    )


def _safe_entry_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise PackageError(f"Unsafe ZIP entry path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise PackageError(f"Unsafe ZIP entry path: {name!r}")
    normalized = path.as_posix()
    if normalized in {"", "."} or normalized.startswith("/"):
        raise PackageError(f"Unsafe ZIP entry path: {name!r}")
    canonical = f"{normalized}/" if name.endswith("/") else normalized
    if canonical != name:
        raise PackageError(f"Non-canonical ZIP entry path: {name!r}")
    return name


def _json_object(entries: Mapping[str, bytes], name: str) -> dict[str, Any]:
    if name not in entries:
        raise PackageError(f"Package is missing required entry: {name}")
    try:
        decoded = json.loads(
            entries[name].decode("utf-8-sig"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PackageError(f"Invalid JSON in {name}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise PackageError(f"{name} must contain a JSON object.")
    return decoded


def _validate_root(
    manifest: Mapping[str, Any],
    plan_root: Mapping[str, Any],
) -> list[str]:
    if manifest.get("format") != MIRIAGO_FORMAT:
        raise PackageError(f"Unsupported package format: {manifest.get('format')!r}")
    if manifest.get("schemaVersion") != MIRIAGO_SCHEMA_VERSION:
        raise PackageError(
            f"Only MiriaGo schema v2 is supported; got "
            f"{manifest.get('schemaVersion')!r}."
        )
    if plan_root.get("schemaVersion") != MIRIAGO_SCHEMA_VERSION:
        raise PackageError(
            f"plan.json must use schemaVersion {MIRIAGO_SCHEMA_VERSION}."
        )
    plan = plan_root.get("plan")
    if not isinstance(plan, dict):
        raise PackageError("plan.json is missing the plan object.")
    plan_id = _required_string(plan, "id", "plan")
    manifest_plan_id = manifest.get("planId")
    if isinstance(manifest_plan_id, str) and manifest_plan_id != plan_id:
        raise PackageError("manifest.json and plan.json have different plan IDs.")
    works = _required_object_list(plan, "works")
    groups = _required_object_list(plan, "groups")
    points = _required_object_list(plan, "points")
    work_ids = _unique_ids(works, "work")
    group_ids = _unique_ids(groups, "group")
    point_ids = _unique_ids(points, "point")
    warnings: list[str] = []
    for point in points:
        point_id = str(point["id"])
        work_id = point.get("workId")
        if not isinstance(work_id, str) or work_id not in work_ids:
            raise PackageError(
                f"Point {point_id!r} refers to an unknown workId: {work_id!r}"
            )
        group_id = point.get("groupId")
        if group_id is not None and (
            not isinstance(group_id, str) or group_id not in group_ids
        ):
            warnings.append(
                f"Point {point_id!r} has an unknown existing groupId; "
                "the planner will ignore existing grouping."
            )
        coordinate_issue = coordinate_problem(point)
        if coordinate_issue:
            warnings.append(f"Point {point_id!r}: {coordinate_issue}")
    completed = plan.get("completedPointIds", [])
    if not isinstance(completed, list):
        raise PackageError("plan.completedPointIds must be a list.")
    for point_id in completed:
        if not isinstance(point_id, str) or point_id not in point_ids:
            warnings.append(
                f"completedPointIds contains an unknown point ID: {point_id!r}"
            )
    records = plan_root.get("visitRecords", [])
    if records is not None:
        if not isinstance(records, list) or any(
            not isinstance(item, dict) for item in records
        ):
            raise PackageError("plan.json visitRecords must be a list of objects.")
        _unique_ids(records, "visit record")
        for record in records:
            point_id = record.get("pointId")
            if not isinstance(point_id, str) or point_id not in point_ids:
                warnings.append(
                    f"Visit record {record.get('id')!r} refers to an unknown point."
                )
    return warnings


def _required_object_list(
    value: Mapping[str, Any],
    field: str,
) -> list[dict[str, Any]]:
    items = value.get(field)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise PackageError(f"plan.{field} must be a list of objects.")
    return items


def _required_string(
    value: Mapping[str, Any],
    field: str,
    context: str,
) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise PackageError(f"{context}.{field} must be a non-empty string.")
    return result


def _unique_ids(
    values: list[dict[str, Any]],
    context: str,
) -> set[str]:
    result: set[str] = set()
    for item in values:
        item_id = _required_string(item, "id", context)
        if item_id in result:
            raise PackageError(f"Duplicate {context} ID: {item_id}")
        result.add(item_id)
    return result


def coordinate_problem(point: Mapping[str, Any]) -> str | None:
    latitude = point.get("latitude")
    longitude = point.get("longitude")
    if (
        isinstance(latitude, bool)
        or isinstance(longitude, bool)
        or not isinstance(latitude, (int, float))
        or not isinstance(longitude, (int, float))
    ):
        return "latitude and longitude must be numbers"
    if not math.isfinite(float(latitude)) or not math.isfinite(float(longitude)):
        return "latitude and longitude must be finite"
    if not (-90 <= float(latitude) <= 90):
        return "latitude is outside [-90, 90]"
    if not (-180 <= float(longitude) <= 180):
        return "longitude is outside [-180, 180]"
    # MiriaGo uses (-90, 0) as the sentinel for a point pending geocoding.
    if float(latitude) == -90 and float(longitude) == 0:
        return "coordinate is MiriaGo's pending-location sentinel (-90, 0)"
    return None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")


def has_valid_coordinate(point: Mapping[str, Any]) -> bool:
    return coordinate_problem(point) is None


def package_summary(package: LoadedPackage) -> dict[str, Any]:
    plan = package.plan
    points = _required_object_list(plan, "points")
    valid = [point for point in points if has_valid_coordinate(point)]
    raw_asset_counts = package.manifest.get("assetCounts")
    asset_counts = dict(raw_asset_counts) if isinstance(raw_asset_counts, dict) else {}
    return {
        "path": str(package.path),
        "sha256": package.source_sha256,
        "format": package.manifest.get("format"),
        "schemaVersion": package.manifest.get("schemaVersion"),
        "planId": plan.get("id"),
        "planName": plan.get("name"),
        "counts": {
            "works": len(_required_object_list(plan, "works")),
            "groups": len(_required_object_list(plan, "groups")),
            "points": len(points),
            "plannablePoints": len(valid),
            "unplannablePoints": len(points) - len(valid),
            "visitRecords": len(package.plan_root.get("visitRecords") or []),
            "zipEntries": len(package.entries),
        },
        "assetCounts": asset_counts,
        "warnings": list(package.warnings),
    }


def rebuild_package(
    package: LoadedPackage,
    plan_root: Mapping[str, Any],
    output_path: str | Path,
    *,
    planner_version: str,
    operation: str = "planned",
    operation_metadata: Mapping[str, Any] | None = None,
    additional_entries: Mapping[str, bytes] | None = None,
    force: bool = False,
) -> Path:
    output = Path(output_path).expanduser().resolve()
    if output == package.path:
        raise PackageError("Refusing to overwrite the input MiriaGo package.")
    if output.exists() and not force:
        raise PackageError(
            f"Output already exists: {output}. Use --force only if replacement is intended."
        )
    if not output.parent.exists():
        output.parent.mkdir(parents=True, exist_ok=True)

    extra_entries = _validate_additional_entries(additional_entries, package.entries)
    new_plan_root = deepcopy(dict(plan_root))
    new_manifest = deepcopy(package.manifest)
    validation_warnings = _validate_root(new_manifest, new_plan_root)
    plan = new_plan_root["plan"]
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    package_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    plan["updatedAt"] = now
    new_manifest["exportedAt"] = now
    if operation not in {"planned", "imported"}:
        raise PackageError(f"Unknown package rebuild operation: {operation}")
    new_manifest["packageId"] = (
        f"miriago-{operation}-{package_timestamp}-{package.source_sha256[:8]}"
    )
    new_manifest["planId"] = plan["id"]
    new_manifest["planName"] = plan.get("name")
    counts = new_manifest.setdefault("counts", {})
    if not isinstance(counts, dict):
        counts = {}
        new_manifest["counts"] = counts
    counts.update(
        {
            "works": len(plan["works"]),
            "groups": len(plan["groups"]),
            "points": len(plan["points"]),
            "visitRecords": len(new_plan_root.get("visitRecords") or []),
        }
    )
    metadata_field = "plannedBy" if operation == "planned" else "importedBy"
    timestamp_field = "plannedAt" if operation == "planned" else "importedAt"
    new_manifest[metadata_field] = {
        "skill": "plan-miriago-route",
        "version": planner_version,
        "sourceSha256": package.source_sha256,
        timestamp_field: now,
        **dict(operation_metadata or {}),
    }
    if validation_warnings:
        manifest_warnings = new_manifest.get("warnings")
        if not isinstance(manifest_warnings, list):
            manifest_warnings = []
        new_manifest["warnings"] = [
            *manifest_warnings,
            *[
                warning
                for warning in validation_warnings
                if warning not in manifest_warnings
            ],
        ]
    _sync_asset_manifest(new_manifest, {*package.entries, *extra_entries})

    replacements = {
        "manifest.json": _json_bytes(new_manifest),
        "plan.json": _json_bytes(new_plan_root),
        "points.csv": _points_csv(plan, new_plan_root.get("visitRecords") or []),
    }
    included_content = new_manifest.get("includedContent")
    records_included = (
        "records.csv" in package.entries
        or bool(new_plan_root.get("visitRecords"))
        or (
            isinstance(included_content, dict)
            and included_content.get("visitRecords") is True
        )
    )
    if records_included:
        replacements["records.csv"] = _records_csv(
            plan, new_plan_root.get("visitRecords") or []
        )

    temp = output.with_name(f".{output.name}.miriago-planner.tmp")
    if temp.exists():
        temp.unlink()
    try:
        with zipfile.ZipFile(
            temp,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            written: set[str] = set()
            for name in package.entry_order:
                if name == "records.csv" and not records_included:
                    continue
                data = replacements.get(name, package.entries[name])
                info = _write_info(package.entry_infos[name], name)
                archive.writestr(info, data)
                written.add(name)
            for name in ("manifest.json", "plan.json", "points.csv", "records.csv"):
                if name in replacements and name not in written:
                    archive.writestr(_new_info(name), replacements[name])
            for name, data in sorted(extra_entries.items()):
                archive.writestr(_new_info(name), data)
        if output.exists():
            output.unlink()
        temp.replace(output)
    except Exception:
        if temp.exists():
            temp.unlink()
        raise

    rebuilt = load_package(output)
    _verify_preserved_entries(package, rebuilt)
    return output


def create_package(
    plan_root: Mapping[str, Any],
    output_path: str | Path,
    *,
    planner_version: str,
    import_metadata: Mapping[str, Any] | None = None,
    additional_entries: Mapping[str, bytes] | None = None,
    force: bool = False,
) -> Path:
    """Create a minimal importable MiriaGo v2 package from normalized plan data."""
    output = Path(output_path).expanduser().resolve()
    if output.exists() and not force:
        raise PackageError(
            f"Output already exists: {output}. Use --force only if replacement is intended."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    extra_entries = _validate_additional_entries(additional_entries, {})
    root = deepcopy(dict(plan_root))
    plan = root.get("plan")
    if not isinstance(plan, dict):
        raise PackageError("plan_root is missing the plan object.")
    root["schemaVersion"] = MIRIAGO_SCHEMA_VERSION
    root.setdefault("exportMode", "plan_only")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    plan["updatedAt"] = now
    plan.setdefault("createdAt", now)
    records = root.get("visitRecords") or []
    package_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    manifest: dict[str, Any] = {
        "format": MIRIAGO_FORMAT,
        "container": "zip",
        "schemaVersion": MIRIAGO_SCHEMA_VERSION,
        "appName": "MiriaGo",
        "appVersion": f"external-skill/{planner_version}",
        "exportedAt": now,
        "exportMode": root["exportMode"],
        "packageId": f"miriago-imported-{package_timestamp}",
        "planId": plan.get("id"),
        "planName": plan.get("name"),
        "includedContent": {
            "plan": True,
            "works": True,
            "groups": True,
            "points": True,
            "pointCompletion": True,
            "thumbnails": False,
            "userReferenceImages": False,
            "fullReferenceCache": False,
            "visitRecords": bool(records),
            "visitPhotos": False,
            "gradedPhotos": False,
            "colorGradingParams": False,
        },
        "counts": {
            "works": len(plan.get("works") or []),
            "groups": len(plan.get("groups") or []),
            "points": len(plan.get("points") or []),
            "visitRecords": len(records),
        },
        "assetCounts": {
            "thumbnails": 0,
            "userReferenceImages": 0,
            "fullReferences": 0,
            "visitPhotos": 0,
            "gradedPhotos": 0,
        },
        "warnings": [],
        "importedBy": {
            "skill": "plan-miriago-route",
            "version": planner_version,
            "importedAt": now,
            **dict(import_metadata or {}),
        },
    }
    validation_warnings = _validate_root(manifest, root)
    manifest["warnings"] = validation_warnings
    _sync_asset_manifest(manifest, set(extra_entries))
    replacements = {
        "manifest.json": _json_bytes(manifest),
        "plan.json": _json_bytes(root),
        "points.csv": _points_csv(plan, records),
    }
    if records:
        replacements["records.csv"] = _records_csv(plan, records)

    temp = output.with_name(f".{output.name}.miriago-import.tmp")
    if temp.exists():
        temp.unlink()
    try:
        with zipfile.ZipFile(
            temp,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for name, data in replacements.items():
                archive.writestr(_new_info(name), data)
            for name, data in sorted(extra_entries.items()):
                archive.writestr(_new_info(name), data)
        if output.exists():
            output.unlink()
        temp.replace(output)
    except Exception:
        if temp.exists():
            temp.unlink()
        raise
    load_package(output)
    return output


def _validate_additional_entries(
    values: Mapping[str, bytes] | None,
    existing: Mapping[str, bytes],
) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for raw_name, data in (values or {}).items():
        name = _safe_entry_name(raw_name)
        if name.endswith("/"):
            raise PackageError(f"Additional ZIP entry must be a file: {name}")
        if name in DERIVED_ENTRIES:
            raise PackageError(f"Cannot add a derived ZIP entry as an asset: {name}")
        if not isinstance(data, bytes) or not data:
            raise PackageError(f"Additional ZIP entry must contain bytes: {name}")
        if len(data) > MAX_ENTRY_BYTES:
            raise PackageError(f"Additional ZIP entry is too large: {name}")
        if name in existing:
            if existing[name] != data:
                raise PackageError(f"Refusing to replace preserved ZIP entry: {name}")
            continue
        result[name] = data
    return result


def _sync_asset_manifest(manifest: dict[str, Any], entry_names: set[str]) -> None:
    prefixes = {
        "thumbnails": "assets/thumbnails/",
        "userReferenceImages": "assets/user_references/",
        "fullReferences": "assets/full_references/",
        "visitPhotos": "assets/visit_photos/",
        "gradedPhotos": "assets/graded_photos/",
    }
    calculated_counts = {
        key: sum(name.startswith(prefix) for name in entry_names)
        for key, prefix in prefixes.items()
    }
    raw_asset_counts = manifest.get("assetCounts")
    asset_counts = dict(raw_asset_counts) if isinstance(raw_asset_counts, dict) else {}
    asset_counts.update(calculated_counts)
    manifest["assetCounts"] = asset_counts
    included = manifest.get("includedContent")
    if not isinstance(included, dict):
        included = {}
        manifest["includedContent"] = included
    included["thumbnails"] = asset_counts["thumbnails"] > 0
    included["userReferenceImages"] = asset_counts["userReferenceImages"] > 0
    included["fullReferenceCache"] = asset_counts["fullReferences"] > 0
    included["visitPhotos"] = asset_counts["visitPhotos"] > 0
    included["gradedPhotos"] = asset_counts["gradedPhotos"] > 0


def _write_info(original: zipfile.ZipInfo, name: str) -> zipfile.ZipInfo:
    info = copy(original)
    info.filename = name
    if name in DERIVED_ENTRIES and not name.endswith("/"):
        info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _new_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.date_time = datetime.now(timezone.utc).timetuple()[:6]
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _verify_preserved_entries(
    source: LoadedPackage,
    output: LoadedPackage,
) -> None:
    for name, data in source.entries.items():
        if name in DERIVED_ENTRIES:
            continue
        if output.entries.get(name) != data:
            raise PackageError(
                f"Output did not preserve ZIP entry byte-for-byte: {name}"
            )


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")


def _points_csv(
    plan: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> bytes:
    works = {
        item["id"]: item
        for item in plan.get("works", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    groups = {
        item["id"]: item
        for item in plan.get("groups", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    completed = set(plan.get("completedPointIds") or [])
    record_counts: dict[str, int] = {}
    for record in records:
        point_id = record.get("pointId")
        if isinstance(point_id, str):
            record_counts[point_id] = record_counts.get(point_id, 0) + 1
    rows: list[list[Any]] = [
        [
            "作品",
            "片区",
            "点位名",
            "副标题",
            "纬度",
            "经度",
            "集数/场景",
            "来源",
            "来源ID",
            "参考图URL",
            "已完成",
            "记录数",
        ]
    ]
    for point in plan.get("points", []):
        work = works.get(point.get("workId"), {})
        group = groups.get(point.get("groupId"))
        valid = has_valid_coordinate(point)
        rows.append(
            [
                work.get("title", ""),
                group.get("name", "未知片区") if group else "未分组",
                point.get("name", ""),
                point.get("subtitle", ""),
                point.get("latitude") if valid else "",
                point.get("longitude") if valid else "",
                _display_episode_label(point.get("episodeLabel", "")),
                point.get("source", ""),
                point.get("sourceId", ""),
                ""
                if _is_local_uploaded_reference(point)
                else point.get("referenceImageUrl", ""),
                "是" if point.get("id") in completed else "否",
                record_counts.get(point.get("id"), 0),
            ]
        )
    return _csv_bytes(rows)


def _records_csv(
    plan: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> bytes:
    points = {
        item["id"]: item
        for item in plan.get("points", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    works = {
        item["id"]: item
        for item in plan.get("works", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    groups = {
        item["id"]: item
        for item in plan.get("groups", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    rows: list[list[Any]] = [
        [
            "作品",
            "片区",
            "点位名",
            "记录ID",
            "拍摄时间",
            "参考模式",
            "是否调色",
            "照片文件名",
            "调色照片文件名",
        ]
    ]
    for record in records:
        point = points.get(record.get("pointId"), {})
        work = works.get(point.get("workId"), {})
        group = groups.get(point.get("groupId"))
        photo_path = str(record.get("photoPath") or "")
        graded_path = str(record.get("gradedPhotoPath") or "")
        rows.append(
            [
                work.get("title")
                or record.get("workTitle")
                or record.get("workId", ""),
                group.get("name", "未知片区") if group else "未分组",
                point.get("name")
                or record.get("pointName")
                or record.get("pointId", ""),
                record.get("id", ""),
                record.get("capturedAt", ""),
                record.get("referenceMode", ""),
                "是"
                if graded_path and record.get("colorGradingParamsJson") is not None
                else "否",
                _basename(photo_path),
                _basename(graded_path),
            ]
        )
    return _csv_bytes(rows)


def _csv_bytes(rows: list[list[Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows([["" if cell is None else cell for cell in row] for row in rows])
    return output.getvalue().encode("utf-8")


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _display_episode_label(value: Any) -> str:
    # Match MiriaGo's display conversion of raw "... / 125s" labels.
    import re

    def replace(match: re.Match[str]) -> str:
        seconds = int(match.group(1))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds_value = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds_value:02d}"
        return f"{minutes}:{seconds_value:02d}"

    return re.sub(r"\b(\d+)s\b", replace, str(value or ""))


def _is_local_uploaded_reference(point: Mapping[str, Any]) -> bool:
    source = str(point.get("source") or "").lower()
    thumbnail = str(point.get("referenceThumbnailPath") or "").strip()
    full = str(point.get("referenceFullImagePath") or "").strip()
    if source == "manual" and (thumbnail or full):
        return True
    for value in (thumbnail, full):
        normalized = value.replace("\\", "/").lower().lstrip("/")
        if normalized.startswith("assets/user_reference_images/"):
            return True
        if normalized.startswith("assets/user_references/"):
            return True
        if "/user_reference_images/" in f"/{normalized}":
            return True
        if "/user_references/" in f"/{normalized}":
            return True
    return False
