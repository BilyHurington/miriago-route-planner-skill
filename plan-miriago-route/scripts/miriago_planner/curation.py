"""Optional point curation: duplicate scenes, coordinate checks, and AI hints.

The scripts here prepare batches and apply structured results; the visual
judgement itself (comparing screenshots, checking satellite imagery, choosing
famous scenes) is done by the agent, its subagents, or the user.

* ``dedupe``   works on an import draft: dropped scenes become
  ``selected: false`` and the kept scene's note lists where else it appears.
* ``verify``   works on an import draft: moved coordinates and notes are
  written back; unverifiable points can seed an ``unverified`` special group.
* ``classify`` works on a package and proposal: results become
  ``proposal.reviewHints`` and never change membership on their own.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clustering import GeoPoint, bundle_points
from .network_io import NetworkError, fetch_bytes
from .public_services import default_cache_dir
from .reference_images import (
    ANITABI_IMAGE_HOSTS,
    _anitabi_candidates,
    _anitabi_thumbnail_url,
    _is_supported_image,
)

HINT_CLASSES = frozenset({"keep", "optional", "drop"})
VERIFY_STATUSES = frozenset({"ok", "offset", "wrong", "unverifiable"})
TILE = (400, 225)
SHEET_COLUMNS = 4
SHEET_TILES = 20


class CurationError(ValueError):
    """Raised when curation input or results are inconsistent."""


@dataclass(frozen=True)
class CurationPoint:
    point_id: str
    name: str
    work: str
    episode: str
    latitude: float
    longitude: float
    note: str
    data: Mapping[str, Any]


def draft_points(draft: Mapping[str, Any], *, selected_only: bool = True) -> list[CurationPoint]:
    """Return the import draft's points with resolved work titles."""
    titles: dict[str, str] = {}
    for item in draft.get("availableWorks", []) or []:
        data = item.get("data") if isinstance(item, Mapping) else None
        if isinstance(data, Mapping) and isinstance(data.get("id"), str):
            titles[data["id"]] = str(data.get("title") or data.get("name") or data["id"])
    mapping = {
        item.get("sourceWorkRef"): item.get("selectedWorkId")
        for item in draft.get("workMappings", []) or []
        if isinstance(item, Mapping)
    }
    result = []
    for item in draft.get("points", []) or []:
        if not isinstance(item, Mapping) or (selected_only and item.get("selected") is not True):
            continue
        data = item.get("data")
        if not isinstance(data, Mapping) or not _has_coordinate(data):
            continue
        work_id = mapping.get(item.get("sourceWorkRef")) or data.get("workId")
        result.append(_curation_point(data, titles.get(str(work_id), str(work_id or ""))))
    return result


def package_points(package: Any, point_ids: Iterable[str] | None = None) -> list[CurationPoint]:
    titles = {
        work.get("id"): str(work.get("title") or work.get("name") or work.get("id"))
        for work in package.plan.get("works", [])
        if isinstance(work, Mapping)
    }
    wanted = set(point_ids) if point_ids is not None else None
    return [
        _curation_point(point, titles.get(point.get("workId"), str(point.get("workId") or "")))
        for point in package.plan["points"]
        if _has_coordinate(point) and (wanted is None or point["id"] in wanted)
    ]


class ThumbnailSource:
    """Local-first reference thumbnails for contact sheets and review pages.

    Package thumbnails are used when available. Otherwise one small thumbnail
    per point is downloaded once and cached; ``download=False`` skips network.
    """

    def __init__(
        self,
        *,
        package: Any = None,
        cache_dir: str | Path | None = None,
        download: bool = True,
        workers: int = 8,
        fetcher: Callable[..., bytes] = fetch_bytes,
    ) -> None:
        self.package = package
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else default_cache_dir() / "thumbnails"
        self.download = download
        self.workers = workers
        self.fetcher = fetcher

    def get(self, point: Mapping[str, Any]) -> bytes | None:
        asset = point.get("referenceThumbnailAsset")
        if self.package is not None and isinstance(asset, str) and asset in self.package.entries:
            return self.package.entries[asset]
        url = point.get("referenceImageUrl")
        if not self.download or not isinstance(url, str) or not url.startswith("http"):
            return None
        target = self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}.jpg"
        if target.exists():
            return target.read_bytes()
        host = url.split("/")[2].casefold() if "//" in url else ""
        candidates = (
            _anitabi_candidates(_anitabi_thumbnail_url(url)) if host in ANITABI_IMAGE_HOSTS else (url,)
        )
        for candidate in candidates:
            try:
                data = self.fetcher(candidate, max_bytes=16 * 1024 * 1024)
            except (NetworkError, OSError, ValueError):
                continue
            if not _is_supported_image(data):
                continue
            data = _shrink(data)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return data
        return None

    def many(self, points: Sequence[Mapping[str, Any]]) -> dict[str, bytes]:
        with ThreadPoolExecutor(max(1, self.workers)) as pool:
            values = list(pool.map(self.get, points))
        return {str(point["id"]): data for point, data in zip(points, values) if data}


def dedupe_candidates(
    points: Sequence[CurationPoint],
    *,
    meters: float = 30,
) -> list[dict[str, Any]]:
    """Group points within ``meters`` (complete linkage) as same-shot candidates."""
    by_id = {point.point_id: point for point in points}
    bundled = bundle_points(
        [GeoPoint(point.point_id, point.latitude, point.longitude, (point.point_id,)) for point in points],
        meters,
    )
    groups = []
    for stop in bundled:
        if len(stop.point_ids) < 2:
            continue
        groups.append(
            {
                "groupId": f"D{len(groups) + 1:03d}",
                "latitude": round(stop.latitude, 6),
                "longitude": round(stop.longitude, 6),
                "members": [_member(by_id[point_id]) for point_id in stop.point_ids],
            }
        )
    return groups


def write_dedupe_batch(
    source_path: Path,
    points: Sequence[CurationPoint],
    output_dir: Path,
    *,
    meters: float,
    thumbnails: ThumbnailSource | None,
) -> dict[str, Any]:
    groups = dedupe_candidates(points, meters=meters)
    output_dir.mkdir(parents=True, exist_ok=True)
    member_ids = {member["pointId"] for group in groups for member in group["members"]}
    images = _write_thumbs(
        [point.data for point in points if point.point_id in member_ids],
        output_dir / "thumbs",
        thumbnails,
    )
    for group in groups:
        for member in group["members"]:
            member["thumbnail"] = images.get(member["pointId"])
        group["sheets"] = _contact_sheets(
            [(index + 1, output_dir / member["thumbnail"]) for index, member in enumerate(group["members"]) if member["thumbnail"]],
            output_dir / "sheets" / group["groupId"],
        )
        group["sheets"] = [str(path.relative_to(output_dir)) for path in group["sheets"]]
    candidates = {
        "schemaVersion": 1,
        "kind": "miriago_dedupe_candidates",
        "source": {"path": str(source_path), "sha256": _sha256(source_path)},
        "meters": meters,
        "groups": groups,
    }
    _write_json(output_dir / "candidates.json", candidates)
    _write_json(
        output_dir / "decisions.template.json",
        {
            "schemaVersion": 1,
            "groups": {
                group["groupId"]: {"keep": [m["pointId"] for m in group["members"]], "drop": [], "note": ""}
                for group in groups
            },
        },
    )
    (output_dir / "dedupe-review.html").write_text(_dedupe_html(groups), encoding="utf-8")
    return {
        "groups": len(groups),
        "points": sum(len(group["members"]) for group in groups),
        "thumbnails": len(images),
        "output": str(output_dir),
    }


def apply_dedupe(draft: Mapping[str, Any], candidates: Mapping[str, Any], decisions: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    result = deepcopy(dict(draft))
    items = {item["data"]["id"]: item for item in result.get("points", []) if isinstance(item, Mapping)}
    groups = {group["groupId"]: group for group in candidates.get("groups", [])}
    removed: list[str] = []
    noted: list[str] = []
    for group_id, decision in (decisions.get("groups") or {}).items():
        group = groups.get(group_id)
        if group is None:
            raise CurationError(f"Unknown dedupe group: {group_id}")
        members = {member["pointId"]: member for member in group["members"]}
        keep = list(decision.get("keep") or [])
        drop = list(decision.get("drop") or [])
        unknown = sorted(set(keep + drop) - set(members))
        if unknown:
            raise CurationError(f"{group_id}: IDs not in the group: {unknown}")
        if set(keep) & set(drop):
            raise CurationError(f"{group_id}: a point cannot be both kept and dropped.")
        if drop and not keep:
            raise CurationError(f"{group_id}: keep at least one point when dropping others.")
        if not drop:
            continue
        for point_id in drop:
            items[point_id]["selected"] = False
            removed.append(point_id)
        seen = "；".join(
            "{} {} {}".format(members[point_id]["work"], members[point_id]["episode"], members[point_id]["name"]).strip()
            for point_id in drop
        )
        extra = str(decision.get("note") or "").strip()
        for point_id in keep:
            _append_note(items[point_id]["data"], f"【同机位画面另见】{seen}" + (f"（{extra}）" if extra else ""))
            noted.append(point_id)
    _reopen_import(result)
    return result, {"removed": removed, "annotated": noted}


def write_verify_batches(
    source_path: Path,
    points: Sequence[CurationPoint],
    output_dir: Path,
    *,
    meters: float,
    batch_points: int,
    thumbnails: ThumbnailSource | None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    by_id = {point.point_id: point for point in points}
    images = _write_thumbs([point.data for point in points], output_dir / "thumbs", thumbnails)
    stops = bundle_points(
        [GeoPoint(point.point_id, point.latitude, point.longitude, (point.point_id,)) for point in points],
        meters,
    )
    stops.sort(key=lambda stop: (round(stop.latitude, 2), stop.longitude))
    batches: list[list[Any]] = [[]]
    for stop in stops:
        if batches[-1] and sum(len(item.point_ids) for item in batches[-1]) + len(stop.point_ids) > batch_points:
            batches.append([])
        batches[-1].append(stop)
    written = []
    for index, batch in enumerate(batch for batch in batches if batch):
        folder = output_dir / f"V{index + 1:02d}"
        entries = []
        for stop_index, stop in enumerate(batch, 1):
            members = [_member(by_id[point_id]) for point_id in stop.point_ids]
            for number, member in enumerate(members, 1):
                member["n"] = number
                member["thumbnail"] = images.get(member["pointId"])
            sheets = _contact_sheets(
                [(member["n"], output_dir / member["thumbnail"]) for member in members if member["thumbnail"]],
                folder / "sheets" / f"stop_{stop_index:03d}",
            )
            entries.append(
                {
                    "stopId": stop_index,
                    "latitude": round(stop.latitude, 6),
                    "longitude": round(stop.longitude, 6),
                    "links": map_links(stop.latitude, stop.longitude),
                    "sheets": [str(path.relative_to(output_dir)) for path in sheets],
                    "points": members,
                }
            )
        _write_json(
            folder / "input.json",
            {
                "schemaVersion": 1,
                "kind": "miriago_verify_batch",
                "batch": folder.name,
                "source": {"path": str(source_path), "sha256": _sha256(source_path)},
                "resultFile": "verify.json",
                "stops": entries,
            },
        )
        written.append(folder.name)
    return {"batches": written, "stops": len(stops), "points": len(points), "output": str(output_dir)}


def apply_verify(draft: Mapping[str, Any], results: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply ``(input.json, verify.json)`` pairs to a new import draft."""
    result = deepcopy(dict(draft))
    items = {item["data"]["id"]: item for item in result.get("points", []) if isinstance(item, Mapping)}
    log: dict[str, list[str]] = {"moved": [], "renamed": [], "noted": [], "unverifiable": []}

    def move(point_id: str, latitude: Any, longitude: Any, detail: str) -> None:
        data = items[point_id]["data"]
        old = (float(data["latitude"]), float(data["longitude"]))
        new = (float(latitude), float(longitude))
        if not (-90 <= new[0] <= 90 and -180 <= new[1] <= 180):
            raise CurationError(f"{point_id}: suggested coordinate is invalid.")
        if abs(old[0] - new[0]) < 1e-6 and abs(old[1] - new[1]) < 1e-6:
            return
        data["latitude"], data["longitude"] = new
        _append_note(data, f"【坐标核对·已修正】原坐标 {old[0]:.6f},{old[1]:.6f}。{detail}".rstrip("。") + "。")
        log["moved"].append(point_id)

    for batch_input, verdict in results:
        stops = {stop["stopId"]: stop for stop in batch_input.get("stops", [])}
        point_fixes = {item["pointId"]: item for item in verdict.get("points", []) or []}
        for stop_result in verdict.get("stops", []) or []:
            stop = stops.get(stop_result.get("stopId"))
            if stop is None:
                raise CurationError(f"Unknown stopId in verify result: {stop_result.get('stopId')}")
            status = stop_result.get("status")
            if status not in VERIFY_STATUSES:
                raise CurationError(f"Stop {stop['stopId']}: status must be one of {sorted(VERIFY_STATUSES)}.")
            detail = str(stop_result.get("detail") or "").strip()
            for member in stop["points"]:
                point_id = member["pointId"]
                if point_id not in items or point_id in point_fixes:
                    continue
                if status == "offset" and stop_result.get("suggestedLat") is not None:
                    move(point_id, stop_result["suggestedLat"], stop_result["suggestedLng"], detail)
                elif status in {"wrong", "unverifiable"}:
                    label = "位置有误" if status == "wrong" else "无法确认"
                    _append_note(items[point_id]["data"], f"【坐标核对·{label}】{detail}")
                    log["noted"].append(point_id)
                    if status == "unverifiable":
                        log["unverifiable"].append(point_id)
        for point_id, fix in point_fixes.items():
            if point_id not in items:
                raise CurationError(f"Unknown pointId in verify result: {point_id}")
            detail = str(fix.get("detail") or "").strip()
            if fix.get("suggestedLat") is not None:
                move(point_id, fix["suggestedLat"], fix["suggestedLng"], detail)
            elif detail:
                _append_note(items[point_id]["data"], f"【坐标核对】{detail}")
                log["noted"].append(point_id)
            if fix.get("status") == "unverifiable" and point_id not in log["unverifiable"]:
                log["unverifiable"].append(point_id)
            name = fix.get("suggestedName")
            if isinstance(name, str) and name.strip() and items[point_id]["data"].get("name") != name.strip():
                old = items[point_id]["data"].get("name")
                items[point_id]["data"]["name"] = name.strip()
                _append_note(items[point_id]["data"], f"【名称已修正】原名「{old}」")
                log["renamed"].append(point_id)
    _reopen_import(result)
    return result, log


def write_classify_batches(
    package: Any,
    proposal: Mapping[str, Any],
    output_dir: Path,
    *,
    meters: float,
    batch_points: int,
    thumbnails: ThumbnailSource | None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    titles = {point.point_id: point for point in package_points(package)}
    images = _write_thumbs(
        [point.data for point in titles.values()], output_dir / "thumbs", thumbnails
    )
    batches: list[list[Mapping[str, Any]]] = [[]]
    for group in proposal.get("groups", []):
        if not isinstance(group, Mapping):
            continue
        size = len(group.get("pointIds", []))
        if batches[-1] and sum(len(item.get("pointIds", [])) for item in batches[-1]) + size > batch_points:
            batches.append([])
        batches[-1].append(group)
    written = []
    for index, batch in enumerate(batch for batch in batches if batch):
        folder = output_dir / f"C{index + 1:02d}"
        stops_out = []
        stop_number = 0
        for group in batch:
            members = [titles[point_id] for point_id in group.get("pointIds", []) if point_id in titles]
            for stop in bundle_points(
                [GeoPoint(point.point_id, point.latitude, point.longitude, (point.point_id,)) for point in members],
                meters,
            ):
                stop_number += 1
                points = [_member(titles[point_id]) for point_id in stop.point_ids]
                for number, member in enumerate(points, 1):
                    member["n"] = number
                    member["thumbnail"] = images.get(member["pointId"])
                sheets = _contact_sheets(
                    [(member["n"], output_dir / member["thumbnail"]) for member in points if member["thumbnail"]],
                    folder / "sheets" / f"stop_{stop_number:03d}",
                )
                stops_out.append(
                    {
                        "stopId": stop_number,
                        "group": group.get("name"),
                        "latitude": round(stop.latitude, 6),
                        "longitude": round(stop.longitude, 6),
                        "sheets": [str(path.relative_to(output_dir)) for path in sheets],
                        "points": points,
                    }
                )
        _write_json(
            folder / "input.json",
            {
                "schemaVersion": 1,
                "kind": "miriago_classify_batch",
                "batch": folder.name,
                "resultFile": "classify.json",
                "stops": stops_out,
            },
        )
        written.append(folder.name)
    return {"batches": written, "output": str(output_dir)}


def merge_classify(proposal: Mapping[str, Any], results: Sequence[Mapping[str, Any]], known_ids: set[str]) -> tuple[dict[str, Any], dict[str, int]]:
    merged = deepcopy(dict(proposal))
    hints = dict(merged.get("reviewHints") or {})
    for result in results:
        for point_id, value in (result.get("points") or {}).items():
            if point_id not in known_ids:
                raise CurationError(f"Unknown pointId in classify result: {point_id}")
            if not isinstance(value, Mapping) or value.get("class") not in HINT_CLASSES:
                raise CurationError(f"{point_id}: class must be one of {sorted(HINT_CLASSES)}.")
            similar = [item for item in value.get("similarTo") or [] if item in known_ids]
            hints[point_id] = {
                "class": value["class"],
                "famous": bool(value.get("famous")),
                "reason": str(value.get("reason") or "").strip(),
                "similarTo": similar,
            }
    merged["reviewHints"] = hints
    counts = {name: sum(1 for hint in hints.values() if hint["class"] == name) for name in sorted(HINT_CLASSES)}
    counts["famous"] = sum(1 for hint in hints.values() if hint["famous"])
    return merged, counts


def map_links(latitude: float, longitude: float) -> dict[str, str]:
    return {
        "googleEarth": f"https://earth.google.com/web/@{latitude:.6f},{longitude:.6f},40a,300d,35y,0h,60t,0r",
        "googleMapsSatellite": (
            "https://www.google.com/maps/@?api=1&map_action=map"
            f"&center={latitude:.6f},{longitude:.6f}&zoom=19&basemap=satellite"
        ),
        "streetView": (
            "https://www.google.com/maps/@?api=1&map_action=pano"
            f"&viewpoint={latitude:.6f},{longitude:.6f}"
        ),
    }


def unverified_special_group(point_ids: Sequence[str], name: str = "待考证-位置未确定") -> list[dict[str, Any]]:
    return [{"kind": "unverified", "name": name, "select": {"pointIds": list(point_ids)}}] if point_ids else []


def _curation_point(data: Mapping[str, Any], work: str) -> CurationPoint:
    return CurationPoint(
        point_id=str(data["id"]),
        name=str(data.get("name") or data["id"]),
        work=work,
        episode=str(data.get("episodeLabel") or ""),
        latitude=float(data["latitude"]),
        longitude=float(data["longitude"]),
        note=str(data.get("note") or ""),
        data=data,
    )


def _member(point: CurationPoint) -> dict[str, Any]:
    return {
        "pointId": point.point_id,
        "name": point.name,
        "work": point.work,
        "episode": point.episode,
        "latitude": point.latitude,
        "longitude": point.longitude,
        "note": point.note,
    }


def _has_coordinate(data: Mapping[str, Any]) -> bool:
    latitude, longitude = data.get("latitude"), data.get("longitude")
    return (
        isinstance(latitude, (int, float))
        and isinstance(longitude, (int, float))
        and not isinstance(latitude, bool)
        and math.isfinite(float(latitude))
        and math.isfinite(float(longitude))
        and -90 <= float(latitude) <= 90
        and -180 <= float(longitude) <= 180
        and not (float(latitude) == -90 and float(longitude) == 0)
    )


def _write_thumbs(points: Sequence[Mapping[str, Any]], folder: Path, source: ThumbnailSource | None) -> dict[str, str]:
    if source is None:
        return {}
    data = source.many(points)
    folder.mkdir(parents=True, exist_ok=True)
    result = {}
    for point_id, content in data.items():
        name = f"{_safe(point_id)}.jpg"
        (folder / name).write_bytes(content)
        result[point_id] = f"{folder.name}/{name}"
    return result


def _contact_sheets(tiles: Sequence[tuple[int, Path]], stem: Path) -> list[Path]:
    """Numbered contact sheets (Pillow optional; returns [] without it)."""
    if not tiles:
        return []
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return []
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    stem.parent.mkdir(parents=True, exist_ok=True)
    sheets = []
    for start in range(0, len(tiles), SHEET_TILES):
        chunk = tiles[start : start + SHEET_TILES]
        columns = min(SHEET_COLUMNS, len(chunk))
        rows = math.ceil(len(chunk) / columns)
        sheet = Image.new("RGB", (columns * TILE[0], rows * TILE[1]), "black")
        draw = ImageDraw.Draw(sheet)
        for position, (number, path) in enumerate(chunk):
            x, y = (position % columns) * TILE[0] + 2, (position // columns) * TILE[1] + 2
            try:
                with Image.open(path) as tile:
                    image = tile.convert("RGB")
                    image.thumbnail((TILE[0] - 4, TILE[1] - 4))
                    sheet.paste(image, (x, y))
            except OSError:
                continue
            draw.rectangle([x, y, x + 44, y + 28], fill="yellow")
            draw.text((x + 4, y + 2), str(number), fill="black", font=font)
        target = stem.with_name(f"{stem.name}_{start // SHEET_TILES + 1}.jpg")
        sheet.save(target, quality=82)
        sheets.append(target)
    return sheets


def _shrink(data: bytes) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return data
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGB")
            image.thumbnail((480, 480))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
            return output.getvalue()
    except OSError:
        return data


def _append_note(data: dict[str, Any], text: str) -> None:
    current = str(data.get("note") or "")
    if text in current:
        return
    data["note"] = f"{current}\n{text}" if current else text


def _reopen_import(draft: dict[str, Any]) -> None:
    review = draft.setdefault("review", {})
    review["confirmed"] = False
    review["confirmedAt"] = None
    summary = draft.setdefault("summary", {})
    summary["selectedPoints"] = sum(1 for item in draft.get("points", []) if item.get("selected") is True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)


def _dedupe_html(groups: Sequence[Mapping[str, Any]]) -> str:
    """A self-contained picker page that exports dedupe decisions JSON."""
    payload = json.dumps(groups, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>同机位去重</title>
<style>
:root{{--bg:#f6f7f9;--fg:#17202a;--muted:#5e6b73;--card:#fff;--line:#d9e0e4;--keep:#15803d;--drop:#b91c1c}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f151a;--fg:#e8eef2;--muted:#9fb0ba;--card:#182128;--line:#2c3a43}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Noto Sans SC",sans-serif}}
header{{position:sticky;top:0;z-index:2;display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px 16px;background:var(--card);border-bottom:1px solid var(--line)}}
header h1{{margin:0;font-size:16px}} header span{{color:var(--muted);font-size:12px}}
button{{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}}
main{{padding:12px 16px;display:grid;gap:14px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}}
section h2{{margin:0 0 8px;font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}}
figure{{margin:0;border:2px solid var(--keep);border-radius:6px;overflow:hidden;cursor:pointer}}
figure.drop{{border-color:var(--drop);opacity:.55}}
figure img{{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#0001}}
figcaption{{padding:4px 6px;font-size:12px}} figcaption b{{display:block}} figcaption small{{color:var(--muted)}}
</style></head><body>
<header><h1>同机位去重</h1><span>点击图片在「保留 / 去掉」之间切换；每组至少保留一张。选择保存在本浏览器。</span>
<button id="export" type="button">导出 decisions.json</button><span id="count"></span></header>
<main id="list"></main>
<script id="data" type="application/json">{payload}</script>
<script>
const groups=JSON.parse(document.getElementById("data").textContent);
const KEY="miriago-dedupe:"+location.pathname;
let state={{}};try{{state=JSON.parse(localStorage.getItem(KEY)||"{{}}")}}catch(e){{}}
const esc=t=>String(t??"").replace(/[&<>"]/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c]));
function save(){{try{{localStorage.setItem(KEY,JSON.stringify(state))}}catch(e){{}};count()}}
function count(){{const n=Object.values(state).reduce((a,g)=>a+Object.values(g).filter(v=>v==="drop").length,0);document.getElementById("count").textContent=`已去掉 ${{n}} 张`}}
const list=document.getElementById("list");
groups.forEach(g=>{{const s=document.createElement("section");s.innerHTML=`<h2>${{g.groupId}} · ${{g.members.length}} 张</h2><div class="grid"></div>`;
 const grid=s.querySelector(".grid");state[g.groupId]=state[g.groupId]||{{}};
 g.members.forEach(m=>{{const f=document.createElement("figure");const sync=()=>f.classList.toggle("drop",state[g.groupId][m.pointId]==="drop");
  f.innerHTML=(m.thumbnail?`<img loading="lazy" src="${{esc(m.thumbnail)}}" alt="">`:"")+`<figcaption><b>${{esc(m.name)}}</b><small>${{esc([m.work,m.episode].filter(Boolean).join(" · "))}}</small></figcaption>`;
  f.addEventListener("click",()=>{{const cur=state[g.groupId];const drop=cur[m.pointId]!=="drop";
   if(drop&&g.members.filter(x=>cur[x.pointId]!=="drop").length<=1){{alert("每组至少保留一张");return}}
   cur[m.pointId]=drop?"drop":"keep";sync();save()}});sync();grid.append(f)}});list.append(s)}});
document.getElementById("export").addEventListener("click",()=>{{
 const out={{schemaVersion:1,groups:{{}}}};groups.forEach(g=>{{const cur=state[g.groupId]||{{}};out.groups[g.groupId]={{keep:g.members.filter(m=>cur[m.pointId]!=="drop").map(m=>m.pointId),drop:g.members.filter(m=>cur[m.pointId]==="drop").map(m=>m.pointId),note:""}}}});
 const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,1)],{{type:"application/json"}}));a.download="decisions.json";a.click()}});
count();
</script></body></html>
"""


__all__ = [
    "CurationError",
    "ThumbnailSource",
    "apply_dedupe",
    "apply_verify",
    "draft_points",
    "merge_classify",
    "package_points",
    "unverified_special_group",
    "write_classify_batches",
    "write_dedupe_batch",
    "write_verify_batches",
]
