"""Optional, keyless public-service integrations with bounded local caching."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .clustering import haversine_meters, order_open_path

DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
DEFAULT_VALHALLA_ENDPOINT = "https://valhalla1.openstreetmap.de/sources_to_targets"
USER_AGENT = "plan-miriago-route/1.6"
VALHALLA_CLIENT_ID = "plan-miriago-route"
DEFAULT_CACHE_DAYS = 7
MAX_STATION_BOXES_PER_QUERY = 30
MAX_WALKING_MATRIX_POINTS = 25


class PublicServiceError(RuntimeError):
    """Raised when an optional public data service cannot satisfy a request."""


@dataclass(frozen=True)
class StationResult:
    stations: list[dict[str, Any]]
    source: dict[str, Any]
    warnings: list[str]


@dataclass(frozen=True)
class WalkingMatrix:
    distances_meters: list[list[float]]
    durations_seconds: list[list[float]]
    endpoint: str
    cache_hit: bool


class JsonCache:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = (
            Path(directory).expanduser().resolve()
            if directory is not None
            else default_cache_dir()
        )

    def get(self, namespace: str, key: str, max_age_days: int) -> Any | None:
        path = self._path(namespace, key)
        try:
            stat = path.stat()
            if datetime.now(timezone.utc) - datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ) > timedelta(days=max_age_days):
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def put(self, namespace: str, key: str, value: Any) -> None:
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _path(self, namespace: str, key: str) -> Path:
        safe_namespace = "".join(
            char if char.isalnum() or char in "-_" else "_" for char in namespace
        )
        return self.directory / safe_namespace / f"{key}.json"


def default_cache_dir() -> Path:
    configured = os.environ.get("MIRIAGO_PLANNER_CACHE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser().resolve() / "plan-miriago-route"
    return Path.home() / ".cache" / "plan-miriago-route"


def station_boxes_from_proposal(
    proposal: Mapping[str, Any],
    *,
    padding_meters: float = 3_000,
) -> list[tuple[float, float, float, float]]:
    boxes: list[tuple[float, float, float, float]] = []
    groups = proposal.get("groups", [])
    if not isinstance(groups, list):
        return boxes
    for group in groups:
        if not isinstance(group, dict):
            continue
        metrics = group.get("metrics")
        bbox = metrics.get("bbox") if isinstance(metrics, dict) else None
        medoid = group.get("medoid")
        if not isinstance(bbox, dict) or not isinstance(medoid, dict):
            continue
        center_latitude = float(medoid["latitude"])
        latitude_padding = padding_meters / 111_320
        longitude_scale = max(
            111_320 * math.cos(math.radians(center_latitude)),
            1_000,
        )
        longitude_padding = padding_meters / longitude_scale
        south = max(-90.0, float(bbox["south"]) - latitude_padding)
        north = min(90.0, float(bbox["north"]) + latitude_padding)
        for west, east in _expanded_longitude_intervals(
            float(bbox["west"]),
            float(bbox["east"]),
            longitude_padding,
        ):
            boxes.append((south, west, north, east))
    return _merge_boxes(boxes)


def _expanded_longitude_intervals(
    west: float,
    east: float,
    padding: float,
) -> list[tuple[float, float]]:
    bases = [(west, east)] if west <= east else [(west, 180.0), (-180.0, east)]
    result: list[tuple[float, float]] = []
    for left, right in bases:
        left -= padding
        right += padding
        if left < -180:
            result.extend([(-180.0, right), (left + 360, 180.0)])
        elif right > 180:
            result.extend([(left, 180.0), (-180.0, right - 360)])
        else:
            result.append((left, right))
    return result


def _merge_boxes(
    boxes: Sequence[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    result: list[tuple[float, float, float, float]] = []
    for box in sorted(boxes):
        merged = box
        remaining: list[tuple[float, float, float, float]] = []
        for existing in result:
            if _boxes_overlap(merged, existing):
                merged = (
                    min(merged[0], existing[0]),
                    min(merged[1], existing[1]),
                    max(merged[2], existing[2]),
                    max(merged[3], existing[3]),
                )
            else:
                remaining.append(existing)
        result = [*remaining, merged]
    return sorted(result)


def _boxes_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return not (
        left[2] < right[0]
        or right[2] < left[0]
        or left[3] < right[1]
        or right[3] < left[1]
    )


def fetch_stations(
    boxes: Sequence[tuple[float, float, float, float]],
    *,
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    cache: JsonCache | None = None,
    timeout_seconds: float = 30,
    cache_days: int = DEFAULT_CACHE_DAYS,
    requester: Callable[..., Any] = urlopen,
) -> StationResult:
    if not boxes:
        return StationResult(
            stations=[],
            source={"provider": "openstreetmap_overpass", "status": "empty_scope"},
            warnings=[],
        )
    cache_value = cache or JsonCache()
    all_elements: list[dict[str, Any]] = []
    warnings: list[str] = []
    cache_hits = 0
    chunks = [
        boxes[index : index + MAX_STATION_BOXES_PER_QUERY]
        for index in range(0, len(boxes), MAX_STATION_BOXES_PER_QUERY)
    ]
    for chunk_index, chunk in enumerate(chunks):
        query = build_station_query(chunk)
        cache_key = _cache_key(endpoint, query)
        response = cache_value.get("overpass", cache_key, cache_days)
        if response is not None:
            cache_hits += 1
        else:
            try:
                response = _post_form_json(
                    endpoint,
                    {"data": query},
                    timeout_seconds=timeout_seconds,
                    requester=requester,
                )
                cache_value.put("overpass", cache_key, response)
            except PublicServiceError as exc:
                warnings.append(
                    f"Overpass station batch {chunk_index + 1}/{len(chunks)} "
                    f"failed: {exc}"
                )
                continue
            if chunk_index + 1 < len(chunks):
                # Public Overpass policy asks clients to submit serial queries.
                time.sleep(0.25)
        elements = response.get("elements") if isinstance(response, dict) else None
        if not isinstance(elements, list):
            warnings.append(
                f"Overpass station batch {chunk_index + 1} had no elements list."
            )
            continue
        all_elements.extend(item for item in elements if isinstance(item, dict))
    stations = parse_overpass_stations({"elements": all_elements})
    status = "ok" if not warnings else ("partial" if stations else "unavailable")
    return StationResult(
        stations=stations,
        source={
            "provider": "openstreetmap_overpass",
            "endpoint": endpoint,
            "status": status,
            "queries": len(chunks),
            "cacheHits": cache_hits,
            "license": "ODbL",
            "attribution": "© OpenStreetMap contributors",
        },
        warnings=warnings,
    )


def build_station_query(
    boxes: Sequence[tuple[float, float, float, float]],
) -> str:
    clauses: list[str] = []
    for south, west, north, east in boxes:
        bbox = f"({south:.7f},{west:.7f},{north:.7f},{east:.7f})"
        clauses.extend(
            [
                f'nwr["railway"~"^(station|halt|tram_stop)$"]{bbox};',
                (
                    'nwr["public_transport"="station"]'
                    '["station"~"^(train|subway|light_rail|monorail|tram)$"]'
                    f"{bbox};"
                ),
                f'node["railway"="subway_entrance"]{bbox};',
            ]
        )
    return "[out:json][timeout:25];(" + "".join(clauses) + ");out center tags;"


def parse_overpass_stations(
    response: Mapping[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    elements = response.get("elements", [])
    if not isinstance(elements, list):
        raise PublicServiceError("Overpass response elements must be a list.")
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags")
        if not isinstance(tags, dict):
            tags = {}
        center = element.get("center")
        latitude = element.get("lat")
        longitude = element.get("lon")
        if isinstance(center, dict):
            latitude = center.get("lat", latitude)
            longitude = center.get("lon", longitude)
        if (
            isinstance(latitude, bool)
            or isinstance(longitude, bool)
            or not isinstance(latitude, (int, float))
            or not isinstance(longitude, (int, float))
        ):
            continue
        name = _station_name(tags)
        if not name:
            continue
        element_type = str(element.get("type") or "unknown")
        element_id = element.get("id")
        candidates.append(
            {
                "id": f"osm:{element_type}:{element_id}",
                "name": name,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "kind": _station_kind(tags),
                "operator": _optional_tag(tags, "operator"),
                "network": _optional_tag(tags, "network"),
                "ref": _optional_tag(tags, "ref"),
                "osmType": element_type,
                "osmId": element_id,
            }
        )
    return _deduplicate_stations(candidates)


def _station_name(tags: Mapping[str, Any]) -> str | None:
    # A bare entrance/platform ref such as "2" or "M17" is not a useful
    # human-facing anchor name, so unnamed elements are intentionally skipped.
    for key in ("name", "name:zh", "name:ja", "name:en"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if re.fullmatch(r"[A-Za-z]?\d+(?:-\d+)?[A-Za-z]?", candidate):
                continue
            return candidate
    return None


def _station_kind(tags: Mapping[str, Any]) -> str:
    if tags.get("railway") == "subway_entrance":
        return "subway_entrance"
    station = tags.get("station")
    if isinstance(station, str) and station:
        return station
    railway = tags.get("railway")
    return str(railway or "station")


def _optional_tag(tags: Mapping[str, Any], key: str) -> str | None:
    value = tags.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _deduplicate_stations(
    candidates: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda value: (
            _normalize_station_name(str(value["name"])),
            str(value["id"]),
        ),
    ):
        normalized = _normalize_station_name(str(candidate["name"]))
        duplicate = next(
            (
                existing
                for existing in result
                if _normalize_station_name(str(existing["name"])) == normalized
                and haversine_meters(
                    float(existing["latitude"]),
                    float(existing["longitude"]),
                    float(candidate["latitude"]),
                    float(candidate["longitude"]),
                )
                <= 300
            ),
            None,
        )
        if duplicate is None:
            result.append(candidate)
        elif (
            duplicate.get("kind") == "subway_entrance"
            and candidate.get("kind") != "subway_entrance"
        ):
            result[result.index(duplicate)] = candidate
    return sorted(result, key=lambda value: str(value["id"]))


def _normalize_station_name(value: str) -> str:
    lowered = value.casefold()
    return "".join(
        char for char in lowered if char.isalnum() and char not in {"駅", "站"}
    )


def fetch_walking_matrix(
    locations: Sequence[Mapping[str, Any]],
    *,
    endpoint: str = DEFAULT_VALHALLA_ENDPOINT,
    cache: JsonCache | None = None,
    timeout_seconds: float = 45,
    cache_days: int = DEFAULT_CACHE_DAYS,
    requester: Callable[..., Any] = urlopen,
) -> WalkingMatrix:
    if not 2 <= len(locations) <= MAX_WALKING_MATRIX_POINTS:
        raise PublicServiceError(
            f"Walking matrix requires 2–{MAX_WALKING_MATRIX_POINTS} locations."
        )
    normalized = [
        {
            "lat": float(location["latitude"]),
            "lon": float(location["longitude"]),
        }
        for location in locations
    ]
    payload = {
        "sources": normalized,
        "targets": normalized,
        "costing": "pedestrian",
        "units": "kilometers",
        "verbose": False,
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    cache_value = cache or JsonCache()
    cache_key = _cache_key(endpoint, serialized)
    response = cache_value.get("valhalla", cache_key, cache_days)
    cache_hit = response is not None
    if response is None:
        response = _post_json(
            endpoint,
            payload,
            timeout_seconds=timeout_seconds,
            requester=requester,
            headers={"X-Client-Id": VALHALLA_CLIENT_ID},
        )
        cache_value.put("valhalla", cache_key, response)
    distances, durations = parse_valhalla_matrix(response, len(locations))
    return WalkingMatrix(
        distances_meters=distances,
        durations_seconds=durations,
        endpoint=endpoint,
        cache_hit=cache_hit,
    )


def parse_valhalla_matrix(
    response: Any,
    size: int,
) -> tuple[list[list[float]], list[list[float]]]:
    if not isinstance(response, dict):
        raise PublicServiceError("Valhalla response must be an object.")
    body = response.get("sources_to_targets")
    if isinstance(body, dict):
        raw_distances = body.get("distances")
        raw_durations = body.get("durations")
    elif isinstance(body, list):
        raw_distances = [[None for _ in range(size)] for _ in range(size)]
        raw_durations = [[None for _ in range(size)] for _ in range(size)]
        for item in body:
            if not isinstance(item, dict):
                continue
            source = item.get("from_index")
            target = item.get("to_index")
            if isinstance(source, int) and isinstance(target, int):
                raw_distances[source][target] = item.get("distance")
                raw_durations[source][target] = item.get("time")
    else:
        raise PublicServiceError("Valhalla response has no matrix result.")
    distances = _numeric_matrix(raw_distances, size, scale=1_000)
    durations = _numeric_matrix(raw_durations, size, scale=1)
    return distances, durations


def _numeric_matrix(value: Any, size: int, *, scale: float) -> list[list[float]]:
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(not isinstance(row, list) or len(row) != size for row in value)
    ):
        raise PublicServiceError("Valhalla matrix has unexpected dimensions.")
    result: list[list[float]] = []
    for row_index, row in enumerate(value):
        converted: list[float] = []
        for column_index, cell in enumerate(row):
            if cell is None:
                converted.append(0.0 if row_index == column_index else math.inf)
            elif isinstance(cell, bool) or not isinstance(cell, (int, float)):
                raise PublicServiceError("Valhalla matrix contains a non-number.")
            else:
                converted.append(float(cell) * scale)
        result.append(converted)
    return result


def optimize_walking_order(
    point_ids: Sequence[str],
    matrix: WalkingMatrix,
) -> tuple[list[str], float, float]:
    if len(point_ids) != len(matrix.distances_meters):
        raise PublicServiceError("Point IDs and walking matrix dimensions differ.")
    order = order_open_path(
        list(range(len(point_ids))),
        matrix.distances_meters,
    )
    distance = _ordered_total(matrix.distances_meters, order)
    duration = _ordered_total(matrix.durations_seconds, order)
    if not math.isfinite(distance) or not math.isfinite(duration):
        raise PublicServiceError(
            "Some points are disconnected in the pedestrian routing graph."
        )
    return [point_ids[index] for index in order], distance, duration


def _ordered_total(matrix: Sequence[Sequence[float]], order: Sequence[int]) -> float:
    return sum(matrix[left][right] for left, right in zip(order, order[1:]))


def _post_form_json(
    endpoint: str,
    values: Mapping[str, str],
    *,
    timeout_seconds: float,
    requester: Callable[..., Any],
) -> Any:
    data = urlencode(values).encode("utf-8")
    request = Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept": "application/json",
        },
    )
    return _read_json_response(request, timeout_seconds, requester)


def _post_json(
    endpoint: str,
    value: Mapping[str, Any],
    *,
    timeout_seconds: float,
    requester: Callable[..., Any],
    headers: Mapping[str, str] | None = None,
) -> Any:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        **dict(headers or {}),
    }
    request = Request(
        endpoint,
        data=json.dumps(value, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers=request_headers,
    )
    return _read_json_response(request, timeout_seconds, requester)


def _read_json_response(
    request: Request,
    timeout_seconds: float,
    requester: Callable[..., Any],
) -> Any:
    try:
        with requester(request, timeout=timeout_seconds) as response:
            data = response.read()
    except HTTPError as exc:
        raise PublicServiceError(f"HTTP {exc.code} from {request.full_url}") from exc
    except (URLError, OSError, TimeoutError) as exc:
        # The system Python on older macOS releases is linked to LibreSSL 2.8,
        # which cannot negotiate TLS with some current public services even
        # though the OS curl can. Keep the standard-library path primary and
        # use curl only as a transport compatibility fallback.
        if requester is urlopen and shutil.which("curl"):
            data = _curl_response(request, timeout_seconds)
        else:
            raise PublicServiceError(
                f"Request failed for {request.full_url}: {exc}"
            ) from exc
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicServiceError(
            f"Service returned invalid JSON from {request.full_url}"
        ) from exc


def _curl_response(request: Request, timeout_seconds: float) -> bytes:
    command = [
        shutil.which("curl") or "curl",
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--max-time",
        str(max(1, math.ceil(timeout_seconds))),
        "--request",
        request.get_method(),
    ]
    for key, value in request.header_items():
        command.extend(["--header", f"{key}: {value}"])
    if request.data is not None:
        command.extend(["--data-binary", "@-"])
    command.append(request.full_url)
    try:
        completed = subprocess.run(
            command,
            input=request.data,
            capture_output=True,
            check=False,
            timeout=timeout_seconds + 2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicServiceError(
            f"curl fallback failed for {request.full_url}: {exc}"
        ) from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PublicServiceError(
            f"curl fallback failed for {request.full_url}: "
            f"{message or f'exit {completed.returncode}'}"
        )
    return completed.stdout


def _cache_key(*values: str) -> str:
    content = "\0".join(values).encode("utf-8")
    return hashlib.sha256(content).hexdigest()
