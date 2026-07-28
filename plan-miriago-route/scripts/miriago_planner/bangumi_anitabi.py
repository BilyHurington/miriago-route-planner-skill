"""Bangumi discovery and Anitabi static-map import."""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .network_io import DEFAULT_USER_AGENT, NetworkError, fetch_json

BANGUMI_API_BASE = "https://api.bgm.tv"
ANITABI_STATIC_BASES = (
    "https://www.anitabi.cn/d",
    "https://anitabi.cn/d",
)
SUBJECT_TYPES = {
    "book": (1, "书籍"),
    "anime": (2, "动画"),
    "music": (3, "音乐"),
    "game": (4, "游戏"),
    "real": (6, "三次元"),
}
SUBJECT_TYPE_BY_CODE = {
    code: (name, label) for name, (code, label) in SUBJECT_TYPES.items()
}


class SourceImportError(ValueError):
    """Raised when a Bangumi or Anitabi source is incomplete or malformed."""


def search_bangumi(
    keyword: str,
    *,
    subject_types: Sequence[str] = ("anime", "game"),
    limit: int = 12,
    requester: Callable[..., Any] = urlopen,
) -> list[dict[str, Any]]:
    query = keyword.strip()
    if not query:
        raise SourceImportError("Bangumi search keyword must not be empty.")
    if not 1 <= limit <= 50:
        raise SourceImportError("Bangumi search limit must be between 1 and 50.")
    type_codes = _subject_type_codes(subject_types)
    request = Request(
        f"{BANGUMI_API_BASE}/v0/search/subjects?{urlencode({'limit': limit})}",
        data=_json_request_bytes(
            {
                "keyword": query,
                "sort": "match",
                **({"filter": {"type": type_codes}} if type_codes else {}),
            }
        ),
        headers={
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )
    value = fetch_json(request, requester=requester)
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise SourceImportError("Bangumi search response has no data list.")
    results: list[dict[str, Any]] = []
    for raw in value["data"]:
        if not isinstance(raw, dict):
            continue
        try:
            results.append(_bangumi_candidate(raw))
        except (KeyError, TypeError, ValueError):
            continue
    return results


def collect_anitabi_source(
    bangumi_id: int,
    *,
    requester: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    if bangumi_id <= 0:
        raise SourceImportError("Bangumi ID must be positive.")
    index = _fetch_anitabi_json("g.json", requester=requester)
    if not isinstance(index, list) or len(index) < 2:
        raise SourceImportError("Anitabi g.json has an unexpected structure.")
    raw_works = index[0]
    page_size = index[1]
    version = _optional_text(index[2] if len(index) > 2 else None)
    if not isinstance(raw_works, list) or not isinstance(page_size, int) or page_size < 1:
        raise SourceImportError("Anitabi index has invalid works or page size.")

    work_index = next(
        (
            position
            for position, work in enumerate(raw_works)
            if isinstance(work, list)
            and work
            and _integer(work[0]) == bangumi_id
        ),
        None,
    )
    if work_index is None:
        raise SourceImportError(
            f"Anitabi has no static-map work for Bangumi {bangumi_id}."
        )
    work_row = raw_works[work_index]
    if not isinstance(work_row, list) or len(work_row) < 13:
        raise SourceImportError(f"Anitabi work {bangumi_id} is malformed.")
    lite_points = _lite_points(work_row[12])
    page_index = work_index // page_size
    point_rows = _find_point_rows(
        bangumi_id,
        page_index,
        page_count=math.ceil(len(raw_works) / page_size),
        version=version,
        requester=requester,
    )
    if len(point_rows) != len(lite_points):
        raise SourceImportError(
            f"Anitabi returned {len(point_rows)} of {len(lite_points)} expected "
            f"points for Bangumi {bangumi_id}."
        )

    subject = _try_fetch_bangumi_subject(bangumi_id, requester=requester)
    work = _work_from_sources(bangumi_id, work_row, subject)
    points: list[dict[str, Any]] = []
    for row in point_rows:
        point = _point_from_compact_row(bangumi_id, row, lite_points)
        points.append(
            {
                "sourceWorkRef": f"anitabi:{bangumi_id}",
                "selected": True,
                "data": point,
            }
        )
    return {
        "provider": "anitabi",
        "sourceId": f"anitabi:{bangumi_id}",
        "label": work["title"],
        "metadata": {
            "bangumiId": bangumi_id,
            "staticVersion": version,
            "expectedPoints": len(lite_points),
            "loadedPoints": len(points),
        },
        "works": [
            {
                "sourceWorkRef": f"anitabi:{bangumi_id}",
                "data": work,
            }
        ],
        "points": points,
        "warnings": [],
    }


def _find_point_rows(
    bangumi_id: int,
    guessed_page: int,
    *,
    page_count: int,
    version: str | None,
    requester: Callable[..., Any],
) -> list[list[Any]]:
    pages = [guessed_page, *[value for value in range(page_count) if value != guessed_page]]
    for page_index in pages:
        page = _fetch_anitabi_json(
            f"g{page_index}.json",
            version=version,
            requester=requester,
        )
        if not isinstance(page, list):
            continue
        for entry in page:
            if (
                isinstance(entry, list)
                and len(entry) > 2
                and _integer(entry[0]) == bangumi_id
                and isinstance(entry[2], list)
            ):
                return [row for row in entry[2] if isinstance(row, list)]
    raise SourceImportError(f"Anitabi point page for Bangumi {bangumi_id} was not found.")


def _fetch_anitabi_json(
    file_name: str,
    *,
    version: str | None = None,
    requester: Callable[..., Any],
) -> Any:
    if not (
        file_name == "g.json"
        or (
            file_name.startswith("g")
            and file_name.endswith(".json")
            and file_name[1:-5].isdigit()
        )
    ):
        raise SourceImportError(f"Unsafe Anitabi static file name: {file_name!r}")
    query = f"?{urlencode({'v': version})}" if version else ""
    first_error: NetworkError | None = None
    for base in ANITABI_STATIC_BASES:
        try:
            return fetch_json(
                f"{base}/{file_name}{query}",
                max_bytes=16 * 1024 * 1024,
                requester=requester,
            )
        except NetworkError as exc:
            first_error = first_error or exc
    raise SourceImportError(
        f"Cannot load Anitabi {file_name}: {first_error or 'unknown network error'}"
    )


def _try_fetch_bangumi_subject(
    bangumi_id: int,
    *,
    requester: Callable[..., Any],
) -> Mapping[str, Any] | None:
    request = Request(
        f"{BANGUMI_API_BASE}/v0/subjects/{bangumi_id}",
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    try:
        value = fetch_json(request, requester=requester)
    except NetworkError:
        return None
    return value if isinstance(value, dict) else None


def _work_from_sources(
    bangumi_id: int,
    static: Sequence[Any],
    subject: Mapping[str, Any] | None,
) -> dict[str, Any]:
    static_title = _preferred_text(static[1], static[3]) or f"Bangumi #{bangumi_id}"
    static_subtitle = _compact_text(static[3]) or ""
    static_city = _compact_text(static[4]) or "未设置地区"
    if subject is None:
        return {
            "id": f"bangumi-{bangumi_id}",
            "bangumiId": bangumi_id,
            "bangumiSubjectType": None,
            "coverImageUrl": _anitabi_image_url(_compact_text(static[6])),
            "title": static_title,
            "subtitle": static_subtitle,
            "city": static_city,
            "source": "bangumi",
        }
    candidate = _bangumi_candidate(subject)
    meta = [candidate["subjectTypeLabel"], candidate["date"]]
    return {
        "id": f"bangumi-{bangumi_id}",
        "bangumiId": bangumi_id,
        "bangumiSubjectType": candidate["subjectType"],
        "coverImageUrl": candidate["coverImageUrl"],
        "title": candidate["title"],
        "subtitle": candidate["subtitle"],
        "city": " / ".join(value for value in meta if value) or static_city,
        "source": "bangumi",
    }


def _point_from_compact_row(
    bangumi_id: int,
    row: Sequence[Any],
    lite_points: Mapping[str, tuple[float, float]],
) -> dict[str, Any]:
    if len(row) < 13:
        raise SourceImportError(f"Anitabi point row is too short for {bangumi_id}.")
    point_id = _optional_text(row[0])
    if point_id is None or point_id not in lite_points:
        raise SourceImportError(
            f"Anitabi point has no matching coordinate in Bangumi {bangumi_id}."
        )
    name = _compact_text(row[2]) or _compact_text(row[1]) or "Anitabi 点位"
    subtitle = _compact_text(row[1]) or name
    latitude, longitude = lite_points[point_id]
    ep = row[8]
    second = row[9]
    episode = f"EP {'?' if ep is None else ep}"
    scene_time = _format_seconds(second)
    if scene_time is not None:
        episode = f"{episode} / {scene_time}"
    return {
        "id": f"anitabi-{bangumi_id}-{point_id}",
        "name": name,
        "subtitle": subtitle,
        "latitude": latitude,
        "longitude": longitude,
        "episodeLabel": episode,
        "referenceLabel": _compact_text(row[11]) or "Anitabi",
        "source": "anitabi",
        "sourceId": point_id,
        "referenceImageUrl": _anitabi_image_url(_compact_text(row[6])),
        "referenceThumbnailPath": None,
        "referenceFullImagePath": None,
        "referenceThumbnailAsset": None,
        "referenceFullReferenceAsset": None,
        "userReferenceAsset": None,
        "sourceUrl": _compact_text(row[12]),
        "note": _compact_text(row[10]),
        "groupId": None,
        "groupOrderIndex": None,
    }


def _lite_points(value: Any) -> dict[str, tuple[float, float]]:
    raw = value if isinstance(value, list) else []
    result: dict[str, tuple[float, float]] = {}
    for index in range(0, len(raw) - 3, 4):
        point_id = _optional_text(raw[index])
        latitude = _number(raw[index + 1])
        longitude = _number(raw[index + 2])
        if point_id and latitude is not None and longitude is not None:
            result[point_id] = (latitude, longitude)
    return result


def _bangumi_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    bangumi_id = _integer(value.get("id"))
    if bangumi_id is None or bangumi_id <= 0:
        raise ValueError("invalid Bangumi ID")
    code = _integer(value.get("type"))
    type_name, type_label = SUBJECT_TYPE_BY_CODE.get(code, (None, ""))
    original = _optional_text(value.get("name")) or f"Bangumi #{bangumi_id}"
    chinese = _optional_text(value.get("name_cn"))
    return {
        "bangumiId": bangumi_id,
        "subjectType": type_name,
        "subjectTypeCode": code,
        "subjectTypeLabel": type_label,
        "title": chinese or original,
        "subtitle": original if chinese else f"Bangumi #{bangumi_id}",
        "date": _optional_text(value.get("date")) or "",
        "coverImageUrl": _cover_url(value.get("images")),
    }


def _cover_url(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("small", "common", "medium", "grid", "large"):
        candidate = _optional_text(value.get(key))
        if not candidate:
            continue
        if candidate.startswith("//"):
            return f"https:{candidate}"
        parsed = urlsplit(candidate)
        if parsed.netloc:
            return urlunsplit(("https" if parsed.scheme == "http" else parsed.scheme, *parsed[1:]))
    return None


def _anitabi_image_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("/images/"):
        value = f"https://image.anitabi.cn{value[len('/images'):]}"
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if host == "img-tc.anitabi.cn":
        parsed = parsed._replace(netloc="image.anitabi.cn")
    query_values = [
        item for item in parsed.query.split("&") if item and not item.startswith("plan=")
    ]
    return urlunsplit(parsed._replace(query="&".join(query_values), fragment=""))


def _subject_type_codes(values: Sequence[str]) -> list[int]:
    result: list[int] = []
    for value in values:
        if value not in SUBJECT_TYPES:
            raise SourceImportError(f"Unknown Bangumi subject type: {value}")
        result.append(SUBJECT_TYPES[value][0])
    return list(dict.fromkeys(result))


def _format_seconds(value: Any) -> str | None:
    seconds = _integer(value)
    if seconds is None or seconds < 0:
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining:02d}"
    return f"{minutes}:{remaining:02d}"


def _json_request_bytes(value: Any) -> bytes:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _optional_text(value: Any) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def _compact_text(value: Any) -> str | None:
    if value in (None, 0, False):
        return None
    return _optional_text(value)


def _preferred_text(primary: Any, fallback: Any) -> str | None:
    return _compact_text(primary) or _compact_text(fallback)
