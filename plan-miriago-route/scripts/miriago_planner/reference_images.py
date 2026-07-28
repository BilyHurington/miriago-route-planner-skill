"""Bundle required MiriaGo reference-image assets before package export."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import hashlib
import io
import ipaddress
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .network_io import NetworkError, fetch_bytes

ANITABI_IMAGE_HOSTS = frozenset({"image.anitabi.cn", "img-tc.anitabi.cn"})
MAX_IMAGE_BYTES = 64 * 1024 * 1024
THUMBNAIL_SIZE = (320, 320)
MAX_IMAGE_PIXELS = 50_000_000
DEFAULT_IMAGE_DOWNLOAD_WORKERS = 8
MAX_IMAGE_DOWNLOAD_WORKERS = 32


class ReferenceImageError(ValueError):
    """Raised when a required reference image cannot be bundled."""


@dataclass(frozen=True)
class ReferenceImageBundle:
    plan_root: dict[str, Any]
    entries: dict[str, bytes]
    summary: dict[str, Any]


@dataclass(frozen=True)
class _DownloadJob:
    candidates: tuple[str, ...]
    point_id: str
    kind: str


@dataclass(frozen=True)
class _PointImagePlan:
    point: dict[str, Any]
    point_id: str
    source: str
    existing_thumbnail: tuple[str, bytes] | None
    existing_full_reference: tuple[str, bytes] | None
    existing_user_reference: tuple[str, bytes] | None
    thumbnail_download: tuple[str, ...] | None = None
    full_download: tuple[str, ...] | None = None


def bundle_reference_images(
    plan_root: Mapping[str, Any],
    *,
    existing_entries: Mapping[str, bytes] | None = None,
    include_full_reference_images: bool,
    fetcher: Callable[..., bytes] = fetch_bytes,
    max_workers: int = DEFAULT_IMAGE_DOWNLOAD_WORKERS,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> ReferenceImageBundle:
    """Return plan data and new ZIP entries satisfying the export image policy."""
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or not 1 <= max_workers <= MAX_IMAGE_DOWNLOAD_WORKERS
    ):
        raise ReferenceImageError(
            "Image download workers must be an integer between "
            f"1 and {MAX_IMAGE_DOWNLOAD_WORKERS}."
        )
    root = deepcopy(dict(plan_root))
    plan = root.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("points"), list):
        raise ReferenceImageError("plan_root is missing plan.points.")
    existing = existing_entries or {}
    added: dict[str, bytes] = {}
    summary: dict[str, Any] = {
        "policy": "include" if include_full_reference_images else "omit",
        "referenceImagePoints": 0,
        "pointsWithoutReferenceImage": 0,
        "thumbnailsAdded": 0,
        "anitabiFullImagesAdded": 0,
        "externalFullImagesAdded": 0,
        "downloadedBytes": 0,
    }
    point_plans: list[_PointImagePlan] = []
    jobs: dict[tuple[str, ...], _DownloadJob] = {}

    for raw_point in plan["points"]:
        if not isinstance(raw_point, dict):
            continue
        point_id = _required_point_id(raw_point)
        source = str(raw_point.get("source") or "").strip().lower()
        image_url = _image_url(raw_point.get("referenceImageUrl"), point_id)
        existing_thumbnail = _existing_asset(
            raw_point.get("referenceThumbnailAsset"), existing, added
        )
        existing_full_reference = _existing_asset(
            raw_point.get("referenceFullReferenceAsset"), existing, added
        )
        existing_user_reference = _existing_asset(
            raw_point.get("userReferenceAsset"), existing, added
        )
        has_reference = bool(
            image_url
            or existing_thumbnail
            or existing_full_reference
            or existing_user_reference
        )
        if not has_reference:
            summary["pointsWithoutReferenceImage"] += 1
            continue
        summary["referenceImagePoints"] += 1

        if source == "anitabi":
            thumbnail_download = None
            if existing_thumbnail is None:
                if image_url is None:
                    raise ReferenceImageError(
                        f"Point {point_id!r} has no usable thumbnail asset or image URL."
                    )
                thumbnail_download = _anitabi_candidates(
                    _anitabi_thumbnail_url(image_url)
                )
                jobs.setdefault(
                    thumbnail_download,
                    _DownloadJob(thumbnail_download, point_id, "thumbnail"),
                )
            full_download = None
            if include_full_reference_images and existing_full_reference is None:
                if image_url is None:
                    raise ReferenceImageError(
                        f"Point {point_id!r} has no image URL for its requested full reference."
                    )
                full_download = _anitabi_candidates(_anitabi_full_url(image_url))
                jobs.setdefault(
                    full_download,
                    _DownloadJob(full_download, point_id, "full reference"),
                )
            point_plans.append(
                _PointImagePlan(
                    point=raw_point,
                    point_id=point_id,
                    source=source,
                    existing_thumbnail=existing_thumbnail,
                    existing_full_reference=existing_full_reference,
                    existing_user_reference=existing_user_reference,
                    thumbnail_download=thumbnail_download,
                    full_download=full_download,
                )
            )
            continue

        full_asset = existing_user_reference or existing_full_reference
        full_download = None
        if full_asset is None:
            if image_url is None:
                raise ReferenceImageError(
                    f"External point {point_id!r} has no usable full image asset or URL."
                )
            full_download = (image_url,)
            jobs.setdefault(
                full_download,
                _DownloadJob(full_download, point_id, "external full reference"),
            )
        point_plans.append(
            _PointImagePlan(
                point=raw_point,
                point_id=point_id,
                source=source,
                existing_thumbnail=existing_thumbnail,
                existing_full_reference=existing_full_reference,
                existing_user_reference=existing_user_reference,
                full_download=full_download,
            )
        )

    downloads = _run_download_jobs(
        tuple(jobs.values()),
        fetcher=fetcher,
        max_workers=max_workers,
        progress=progress,
    )

    for point_plan in point_plans:
        raw_point = point_plan.point
        point_id = point_plan.point_id
        if point_plan.source == "anitabi":
            if point_plan.thumbnail_download is not None:
                thumbnail = downloads[point_plan.thumbnail_download]
                path = _asset_path("thumbnails", point_id, thumbnail)
                added[path] = thumbnail
                raw_point["referenceThumbnailAsset"] = path
                summary["thumbnailsAdded"] += 1
                summary["downloadedBytes"] += len(thumbnail)
            if point_plan.full_download is not None:
                full_image = downloads[point_plan.full_download]
                path = _asset_path("full_references", point_id, full_image)
                added[path] = full_image
                raw_point["referenceFullReferenceAsset"] = path
                summary["anitabiFullImagesAdded"] += 1
                summary["downloadedBytes"] += len(full_image)
            continue

        full_asset = (
            point_plan.existing_user_reference
            or point_plan.existing_full_reference
        )
        if point_plan.full_download is not None:
            full_image = downloads[point_plan.full_download]
            full_path = _asset_path("user_references", point_id, full_image)
            added[full_path] = full_image
            raw_point["userReferenceAsset"] = full_path
            raw_point["referenceFullReferenceAsset"] = None
            full_asset = (full_path, full_image)
            summary["externalFullImagesAdded"] += 1
            summary["downloadedBytes"] += len(full_image)
        elif point_plan.existing_user_reference is None and full_asset is not None:
            full_path = _asset_path("user_references", point_id, full_asset[1])
            if full_path not in existing:
                added[full_path] = full_asset[1]
            raw_point["userReferenceAsset"] = full_path
            raw_point["referenceFullReferenceAsset"] = None
            full_asset = (full_path, full_asset[1])
            summary["externalFullImagesAdded"] += 1

        if point_plan.existing_thumbnail is None:
            if full_asset is None:
                raise ReferenceImageError(
                    f"External point {point_id!r} has no usable full image."
                )
            thumbnail = _make_thumbnail(full_asset[1], point_id=point_id)
            path = _asset_path("thumbnails", point_id, thumbnail)
            added[path] = thumbnail
            raw_point["referenceThumbnailAsset"] = path
            summary["thumbnailsAdded"] += 1

    return ReferenceImageBundle(plan_root=root, entries=added, summary=summary)


def _run_download_jobs(
    jobs: tuple[_DownloadJob, ...],
    *,
    fetcher: Callable[..., bytes],
    max_workers: int,
    progress: Callable[[dict[str, Any]], None] | None,
) -> dict[tuple[str, ...], bytes]:
    total = len(jobs)
    worker_count = min(max_workers, total) if total else 0
    _report_progress(
        progress,
        {
            "event": "image_download_start",
            "total": total,
            "workers": worker_count,
        },
    )
    if not jobs:
        _report_progress(
            progress,
            {
                "event": "image_download_complete",
                "completed": 0,
                "total": 0,
                "downloadedBytes": 0,
                "failed": 0,
            },
        )
        return {}

    downloads: dict[tuple[str, ...], bytes] = {}
    failures: dict[tuple[str, ...], Exception] = {}
    downloaded_bytes = 0
    completed = 0
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="miriago-image",
    ) as executor:
        futures: dict[Future[bytes], _DownloadJob] = {
            executor.submit(
                _download,
                job.candidates,
                point_id=job.point_id,
                kind=job.kind,
                fetcher=fetcher,
            ): job
            for job in jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            completed += 1
            try:
                data = future.result()
            except Exception as exc:
                failures[job.candidates] = exc
                event: dict[str, Any] = {
                    "event": "image_download_progress",
                    "completed": completed,
                    "total": total,
                    "status": "failed",
                    "pointId": job.point_id,
                    "kind": job.kind,
                    "error": str(exc),
                }
            else:
                downloads[job.candidates] = data
                downloaded_bytes += len(data)
                event = {
                    "event": "image_download_progress",
                    "completed": completed,
                    "total": total,
                    "status": "downloaded",
                    "pointId": job.point_id,
                    "kind": job.kind,
                    "bytes": len(data),
                }
            _report_progress(progress, event)

    _report_progress(
        progress,
        {
            "event": "image_download_complete",
            "completed": completed,
            "total": total,
            "downloadedBytes": downloaded_bytes,
            "failed": len(failures),
        },
    )
    for job in jobs:
        failure = failures.get(job.candidates)
        if failure is not None:
            raise failure
    return downloads


def _report_progress(
    callback: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
) -> None:
    if callback is not None:
        callback(event)


def _existing_asset(
    value: Any,
    existing: Mapping[str, bytes],
    added: Mapping[str, bytes],
) -> tuple[str, bytes] | None:
    if not isinstance(value, str) or not value.startswith("assets/"):
        return None
    data = added.get(value, existing.get(value))
    if data is None or not _is_supported_image(data):
        return None
    return value, data


def _download(
    candidates: tuple[str, ...],
    *,
    point_id: str,
    kind: str,
    fetcher: Callable[..., bytes],
) -> bytes:
    failures: list[str] = []
    for url in candidates:
        try:
            data = fetcher(url, max_bytes=MAX_IMAGE_BYTES)
        except (NetworkError, OSError, ValueError) as exc:
            failures.append(str(exc))
            continue
        if not _is_supported_image(data):
            failures.append(f"unsupported image bytes from {url}")
            continue
        return data
    detail = "; ".join(failures) or "no download candidate succeeded"
    raise ReferenceImageError(
        f"Cannot bundle required {kind} for point {point_id!r}: {detail}"
    )


def _make_thumbnail(data: bytes, *, point_id: str) -> bytes:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise ReferenceImageError(
            "Pillow is required to generate thumbnails for non-Anitabi images."
        ) from exc
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ReferenceImageError(
                    f"External point {point_id!r} image exceeds "
                    f"the {MAX_IMAGE_PIXELS}-pixel safety limit."
                )
            image = ImageOps.exif_transpose(source)
            image.thumbnail(THUMBNAIL_SIZE)
            if image.mode != "RGB":
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
            return output.getvalue()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ReferenceImageError(
            f"Cannot generate thumbnail for external point {point_id!r}: {exc}"
        ) from exc


def _image_url(value: Any, point_id: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ReferenceImageError(f"Point {point_id!r} has a non-string image URL.")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReferenceImageError(f"Point {point_id!r} has an invalid image URL.")
    if parsed.username or parsed.password:
        raise ReferenceImageError(
            f"Point {point_id!r} image URL must not contain credentials."
        )
    host = parsed.hostname.casefold()
    if host == "localhost":
        raise ReferenceImageError(f"Point {point_id!r} image URL is not public.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ReferenceImageError(f"Point {point_id!r} image URL is not public.")
    return value.strip()


def _anitabi_full_url(value: str) -> str:
    parsed = urlsplit(value)
    query = [(key, item) for key, item in parse_qsl(parsed.query) if key != "plan"]
    return urlunsplit(parsed._replace(query=urlencode(query), fragment=""))


def _anitabi_thumbnail_url(value: str) -> str:
    parsed = urlsplit(_anitabi_full_url(value))
    if (parsed.hostname or "").casefold() not in ANITABI_IMAGE_HOSTS:
        return urlunsplit(parsed)
    query = [*parse_qsl(parsed.query), ("plan", "h160")]
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _anitabi_candidates(value: str) -> tuple[str, ...]:
    parsed = urlsplit(value)
    if (parsed.hostname or "").casefold() not in ANITABI_IMAGE_HOSTS:
        return (value,)
    official = urlunsplit(parsed._replace(netloc="image.anitabi.cn"))
    mirror = urlunsplit(parsed._replace(netloc="img-tc.anitabi.cn"))
    return tuple(dict.fromkeys((official, mirror)))


def _asset_path(directory: str, point_id: str, data: bytes) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", point_id).strip("_") or "asset"
    safe = re.sub(r"_+", "_", safe)
    if safe != point_id or len(safe) > 120:
        digest = hashlib.sha256(point_id.encode("utf-8")).hexdigest()[:10]
        safe = f"{safe[:100]}-{digest}"
    return f"assets/{directory}/{safe}.{_image_extension(data)}"


def _required_point_id(point: Mapping[str, Any]) -> str:
    value = point.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ReferenceImageError("Every point requires an ID before image bundling.")
    return value


def _image_extension(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise ReferenceImageError("Unsupported image format.")


def _is_supported_image(data: bytes) -> bool:
    try:
        _image_extension(data)
    except ReferenceImageError:
        return False
    return True
