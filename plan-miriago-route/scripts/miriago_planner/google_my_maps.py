"""Download and normalize public Google My Maps KML data."""

from __future__ import annotations

import hashlib
import io
import math
import re
import zipfile
from html import unescape
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen
from xml.etree import ElementTree as ET

from .bangumi_anitabi import SourceImportError
from .network_io import fetch_bytes

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
KML_NS = {"k": KML_NAMESPACE}
MAX_KML_BYTES = 64 * 1024 * 1024
MAP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,}$")


def collect_google_my_maps_source(
    source: str,
    *,
    requester: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    source_value = source.strip()
    if not source_value:
        raise SourceImportError("Google My Maps source must not be empty.")
    local_path = Path(source_value).expanduser()
    source_url: str | None = None
    if local_path.is_file():
        data = local_path.read_bytes()
        if len(data) > MAX_KML_BYTES:
            raise SourceImportError("Local KML/KMZ exceeds the 64 MiB limit.")
        map_key = f"file-{hashlib.sha256(data).hexdigest()[:20]}"
        source_label = str(local_path.resolve())
    else:
        map_id = extract_google_my_maps_id(source_value)
        source_url = f"https://www.google.com/maps/d/viewer?{urlencode({'mid': map_id})}"
        download_url = (
            f"https://www.google.com/maps/d/kml?"
            f"{urlencode({'mid': map_id, 'forcekml': '1'})}"
        )
        data = fetch_bytes(
            download_url,
            max_bytes=MAX_KML_BYTES,
            requester=requester,
        )
        map_key = map_id
        source_label = source_url

    kml = _kml_bytes(data)
    title, features, parse_warnings = parse_kml(kml)
    if not features:
        raise SourceImportError("Google My Maps source contains no usable placemarks.")
    works_by_ref: dict[str, dict[str, Any]] = {}
    points: list[dict[str, Any]] = []
    warnings = list(parse_warnings)
    geometry_counts: dict[str, int] = {}
    image_count = 0

    for feature in features:
        layer = feature["layer"] or title
        layer_ref = _layer_ref(map_key, layer)
        work_id = f"mymaps-work-{_digest(layer_ref)[:16]}"
        works_by_ref.setdefault(
            layer_ref,
            {
                "sourceWorkRef": layer_ref,
                "data": {
                    "id": work_id,
                    "bangumiId": None,
                    "bangumiSubjectType": None,
                    "coverImageUrl": None,
                    "title": layer.rsplit(" / ", 1)[-1] or title,
                    "subtitle": title,
                    "city": "Google My Maps 导入",
                    "source": "manual",
                },
            },
        )
        geometry = feature["geometry"]
        geometry_counts[geometry] = geometry_counts.get(geometry, 0) + 1
        urls = feature["imageUrls"] or [None]
        image_count += len(feature["imageUrls"])
        for image_index, image_url in enumerate(urls, 1):
            source_id = (
                f"gmm:{map_key}:{feature['stableId']}:"
                f"{feature['geometryIndex']}:{image_index if image_url else 0}"
            )
            note_parts = [f"Google My Maps 图层：{layer}"]
            if feature["description"]:
                note_parts.append(feature["description"])
            if geometry != "Point":
                note_parts.append(
                    f"原始几何为 {geometry}；MiriaGo 使用路径长度中点作为代表坐标。"
                )
                note_parts.append(f"原始坐标：{feature['rawCoordinates']}")
            if len(urls) > 1:
                note_parts.append(
                    f"此点位由原点位的第 {image_index}/{len(urls)} 张图片拆分而来。"
                )
            point_name = feature["name"]
            if len(urls) > 1:
                point_name = f"{point_name} ({image_index})"
            latitude, longitude = feature["position"]
            points.append(
                {
                    "sourceWorkRef": layer_ref,
                    "selected": True,
                    "data": {
                        "id": f"gmm-{_digest(source_id)[:24]}",
                        "name": point_name,
                        "subtitle": layer,
                        "latitude": latitude,
                        "longitude": longitude,
                        "episodeLabel": "",
                        "referenceLabel": (
                            f"Google My Maps 图片 {image_index}/{len(urls)}"
                            if image_url
                            else "Google My Maps"
                        ),
                        "source": "manual",
                        "sourceId": source_id,
                        "referenceImageUrl": image_url,
                        "referenceThumbnailPath": None,
                        "referenceFullImagePath": None,
                        "referenceThumbnailAsset": None,
                        "referenceFullReferenceAsset": None,
                        "userReferenceAsset": None,
                        "sourceUrl": source_url,
                        "note": "\n\n".join(note_parts),
                        "groupId": None,
                        "groupOrderIndex": None,
                    },
                }
            )

    non_point_count = sum(
        count for kind, count in geometry_counts.items() if kind != "Point"
    )
    if non_point_count:
        warnings.append(
            f"{non_point_count} 个非点 KML 几何已转换为路径长度中点。"
        )
    return {
        "provider": "google_my_maps",
        "sourceId": f"google-my-maps:{map_key}",
        "label": title,
        "metadata": {
            "source": source_label,
            "mapKey": map_key,
            "layers": len(works_by_ref),
            "placemarkGeometries": len(features),
            "expandedPoints": len(points),
            "remoteImages": image_count,
            "geometryCounts": geometry_counts,
        },
        "works": list(works_by_ref.values()),
        "points": points,
        "warnings": warnings,
    }


def extract_google_my_maps_id(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not _is_google_host(host):
        raise SourceImportError(
            "Google My Maps input must be a public google.* /maps/d/ URL or a local KML/KMZ file."
        )
    if "/maps/d/" not in parsed.path:
        raise SourceImportError("URL is not a Google My Maps /maps/d/ link.")
    values = parse_qs(parsed.query).get("mid", [])
    map_id = values[0].strip() if values else ""
    if not MAP_ID_PATTERN.fullmatch(map_id):
        raise SourceImportError("Google My Maps URL has no valid mid parameter.")
    return map_id


def parse_kml(data: bytes) -> tuple[str, list[dict[str, Any]], list[str]]:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise SourceImportError("KML document type and entity declarations are not allowed.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SourceImportError(f"Invalid KML XML: {exc}") from exc
    document = root if _local_name(root.tag) == "Document" else root.find("k:Document", KML_NS)
    if document is None:
        raise SourceImportError("KML has no Document element.")
    title = _node_text(document, "k:name", "Google My Maps 导入")
    features: list[dict[str, Any]] = []
    warnings: list[str] = []

    def add_placemark(placemark: ET.Element, layers: list[str]) -> None:
        layer = " / ".join(layers)
        name = _node_text(placemark, "k:name", "未命名点位")
        description_html = _node_text(placemark, "k:description", "")
        fields = _extended_fields(placemark)
        geometries = list(_geometry_values(placemark))
        if not geometries:
            warnings.append(f"跳过无可用坐标的点位：{name}")
            return
        stable_material = "\0".join(
            [layer, name, description_html, *(item[2] for item in geometries)]
        )
        stable_id = placemark.attrib.get("id") or _digest(stable_material)[:20]
        urls = _image_urls(description_html, fields)
        for geometry_index, (kind, position, raw) in enumerate(geometries, 1):
            feature_name = name
            if len(geometries) > 1:
                feature_name = f"{name} ({geometry_index})"
                warnings.append(
                    f"点位 {name!r} 包含多个几何，已拆分为 {len(geometries)} 个点位。"
                )
            features.append(
                {
                    "stableId": stable_id,
                    "geometryIndex": geometry_index,
                    "layer": layer,
                    "name": feature_name,
                    "description": _html_to_text(description_html),
                    "imageUrls": urls,
                    "geometry": kind,
                    "rawCoordinates": raw,
                    "position": position,
                }
            )

    def walk(folder: ET.Element, parents: list[str]) -> None:
        folder_name = _node_text(folder, "k:name", "")
        layers = [*parents, *([folder_name] if folder_name else [])]
        for placemark in folder.findall("k:Placemark", KML_NS):
            add_placemark(placemark, layers)
        for child in folder.findall("k:Folder", KML_NS):
            walk(child, layers)

    for placemark in document.findall("k:Placemark", KML_NS):
        add_placemark(placemark, [])
    for folder in document.findall("k:Folder", KML_NS):
        walk(folder, [])
    return title, features, list(dict.fromkeys(warnings))


def _geometry_values(
    placemark: ET.Element,
) -> Iterator[tuple[str, tuple[float, float], str]]:
    for node in placemark.iter():
        kind = _local_name(node.tag)
        if kind not in {"Point", "LineString", "Polygon"}:
            continue
        coordinate_node = node.find(".//k:coordinates", KML_NS)
        raw = (
            coordinate_node.text.strip()
            if coordinate_node is not None and coordinate_node.text
            else ""
        )
        coordinates = _parse_coordinates(raw)
        if not coordinates:
            continue
        position = coordinates[0] if kind == "Point" else _path_midpoint(coordinates)
        yield kind, position, raw


def _extended_fields(placemark: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for data in placemark.findall("k:ExtendedData/k:Data", KML_NS):
        name = data.attrib.get("name", "")
        if name:
            result[name] = _node_text(data, "k:value", "")
    for data in placemark.findall(".//k:SimpleData", KML_NS):
        name = data.attrib.get("name", "")
        if name and data.text:
            result[name] = data.text.strip()
    return result


def _image_urls(description: str, fields: dict[str, str]) -> list[str]:
    values = fields.get("gx_media_links", "").split()
    values.extend(
        re.findall(r'<img\b[^>]*?\bsrc=["\']([^"\']+)', description, re.I)
    )
    return list(
        dict.fromkeys(
            unescape(value) for value in values if value.startswith(("http://", "https://"))
        )
    )


def _kml_bytes(data: bytes) -> bytes:
    if not data.startswith(b"PK"):
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 10_000:
                raise SourceImportError("KMZ contains too many ZIP entries.")
            candidates = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".kml")
            ]
            if not candidates:
                raise SourceImportError("KMZ contains no KML file.")
            preferred = next(
                (item for item in candidates if item.filename.lower() == "doc.kml"),
                candidates[0],
            )
            if preferred.file_size > MAX_KML_BYTES:
                raise SourceImportError("KML inside KMZ exceeds the 64 MiB limit.")
            result = archive.read(preferred)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise SourceImportError(f"Invalid KMZ archive: {exc}") from exc
    if len(result) > MAX_KML_BYTES:
        raise SourceImportError("Expanded KML exceeds the 64 MiB limit.")
    return result


def _parse_coordinates(raw: str) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for token in raw.split():
        values = token.split(",")
        if len(values) < 2:
            continue
        try:
            longitude, latitude = float(values[0]), float(values[1])
        except ValueError:
            continue
        if (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            result.append((latitude, longitude))
    return result


def _path_midpoint(points: list[tuple[float, float]]) -> tuple[float, float]:
    if len(points) == 1:
        return points[0]
    lengths = [_haversine(left, right) for left, right in zip(points, points[1:])]
    target = sum(lengths) / 2
    walked = 0.0
    for left, right, length in zip(points, points[1:], lengths):
        if walked + length >= target:
            ratio = 0.0 if length == 0 else (target - walked) / length
            return (
                left[0] + (right[0] - left[0]) * ratio,
                left[1] + (right[1] - left[1]) * ratio,
            )
        walked += length
    return points[-1]


def _haversine(left: tuple[float, float], right: tuple[float, float]) -> float:
    lat_left, lon_left = map(math.radians, left)
    lat_right, lon_right = map(math.radians, right)
    delta_lat = lat_right - lat_left
    delta_lon = lon_right - lon_left
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_left) * math.cos(lat_right) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(value)))


def _html_to_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(?:p|div|li)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\n{3,}", "\n\n", unescape(value)).strip()


def _node_text(node: ET.Element, path: str, default: str) -> str:
    child = node.find(path, KML_NS)
    return child.text.strip() if child is not None and child.text else default


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _layer_ref(map_key: str, layer: str) -> str:
    return f"mymaps:{map_key}:layer:{_digest(layer)[:16]}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_google_host(host: str) -> bool:
    return bool(re.fullmatch(r"(?:[a-z0-9-]+\.)*google\.[a-z.]{2,12}", host))
