"""Offline OpenStreetMap pedestrian network built from Overpass JSON.

The network is downloaded once per rounded bounding box through the public
Overpass API (or read from local Overpass JSON files), cached, and searched
with a dependency-free Dijkstra implementation.
"""

from __future__ import annotations

import heapq
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from .clustering import haversine_meters
from .public_services import (
    DEFAULT_OVERPASS_ENDPOINT,
    JsonCache,
    PublicServiceError,
    _cache_key,
    _post_form_json,
)
from .routing import segments_intersect

WALKABLE_HIGHWAYS = (
    "footway",
    "path",
    "pedestrian",
    "steps",
    "living_street",
    "residential",
    "service",
    "unclassified",
    "road",
    "track",
    "cycleway",
    "corridor",
    "platform",
    "tertiary",
    "tertiary_link",
    "secondary",
    "secondary_link",
    "primary",
    "primary_link",
    "trunk",
    "trunk_link",
)
STEPS_COST_FACTOR = 1.4
WALKNET_CACHE_DAYS = 30
MAIN_COMPONENT_NODES = 5_000
BBOX_GRID_DEGREES = 0.01
CLUSTER_GAP_METERS = 3_000
GRID_DEGREES = 0.002


def walknet_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    area = f"({south:.4f},{west:.4f},{north:.4f},{east:.4f})"
    highways = "|".join(WALKABLE_HIGHWAYS)
    return (
        "[out:json][timeout:120];("
        f'way["highway"~"^({highways})$"]["foot"!~"^(no|private)$"]'
        f'["access"!~"^(no|private)$"]{area};'
        f'way["railway"="platform"]{area};'
        f'way["public_transport"="platform"]["highway"!~"."]{area};'
        ")->.w;.w out body qt;node(w.w);out skel qt;"
    )


def water_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    area = f"({south:.4f},{west:.4f},{north:.4f},{east:.4f})"
    return (
        "[out:json][timeout:90];("
        f'way["waterway"~"^(river|canal)$"]{area};'
        f'way["natural"="water"]{area};'
        f'relation["natural"="water"]{area};'
        ");out geom qt;"
    )


@dataclass
class WalkNetwork:
    """Undirected pedestrian graph with metric lengths and walking costs."""

    latitudes: list[float] = field(default_factory=list)
    longitudes: list[float] = field(default_factory=list)
    adjacency: list[list[tuple[int, float, float]]] = field(default_factory=list)
    component_size: list[int] = field(default_factory=list)
    _index: dict[int, int] = field(default_factory=dict)
    _grid: dict[tuple[int, int], list[int]] = field(default_factory=dict)
    _main_threshold: int = 0

    def add_overpass(self, payload: Mapping[str, Any]) -> None:
        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise PublicServiceError("Overpass walking network has no elements list.")
        coordinates: dict[int, tuple[float, float]] = {}
        for element in elements:
            if (
                isinstance(element, Mapping)
                and element.get("type") == "node"
                and isinstance(element.get("lat"), (int, float))
                and isinstance(element.get("lon"), (int, float))
            ):
                coordinates[int(element["id"])] = (
                    float(element["lat"]),
                    float(element["lon"]),
                )
        for element in elements:
            if not isinstance(element, Mapping) or element.get("type") != "way":
                continue
            tags = element.get("tags") if isinstance(element.get("tags"), Mapping) else {}
            factor = STEPS_COST_FACTOR if tags.get("highway") == "steps" else 1.0
            refs = [ref for ref in element.get("nodes", []) if ref in coordinates]
            for left, right in zip(refs, refs[1:]):
                if left == right:
                    continue
                a = self._node(left, coordinates[left])
                b = self._node(right, coordinates[right])
                meters = haversine_meters(
                    self.latitudes[a], self.longitudes[a], self.latitudes[b], self.longitudes[b]
                )
                self._connect(a, b, meters * factor, meters)

    def finalize(self) -> None:
        size = len(self.latitudes)
        self.component_size = [0] * size
        seen = [False] * size
        for start in range(size):
            if seen[start]:
                continue
            stack = [start]
            seen[start] = True
            members = []
            while stack:
                node = stack.pop()
                members.append(node)
                for neighbor, _, _ in self.adjacency[node]:
                    if not seen[neighbor]:
                        seen[neighbor] = True
                        stack.append(neighbor)
            for node in members:
                self.component_size[node] = len(members)
        largest = max(self.component_size, default=0)
        self._main_threshold = min(MAIN_COMPONENT_NODES, max(50, int(largest * 0.2)))
        self._grid = {}
        for node in range(size):
            self._grid.setdefault(_grid_key(self.latitudes[node], self.longitudes[node]), []).append(node)

    @property
    def node_count(self) -> int:
        return len(self.latitudes)

    def snap(
        self,
        latitude: float,
        longitude: float,
        *,
        max_radius_meters: float = 800,
    ) -> tuple[int, float] | None:
        """Return the nearest node of a main component and its offset."""
        if not self.latitudes:
            return None
        center = _grid_key(latitude, longitude)
        best_main: tuple[float, int] | None = None
        best_any: tuple[float, int] | None = None
        cell_meters = GRID_DEGREES * 111_320 * max(0.2, math.cos(math.radians(latitude)))
        max_ring = int(max_radius_meters / cell_meters) + 1
        for ring in range(max_ring + 1):
            for key in _ring(center, ring):
                for node in self._grid.get(key, ()):
                    distance = haversine_meters(
                        latitude, longitude, self.latitudes[node], self.longitudes[node]
                    )
                    candidate = (distance, node)
                    if best_any is None or candidate < best_any:
                        best_any = candidate
                    if self.component_size[node] >= self._main_threshold and (
                        best_main is None or candidate < best_main
                    ):
                        best_main = candidate
            if best_main is not None and best_main[0] <= ring * cell_meters:
                break
        chosen = best_main or best_any
        if chosen is None or chosen[0] > max_radius_meters:
            return None
        return chosen[1], chosen[0]

    def shortest_from(
        self,
        source: int,
        targets: Iterable[int],
    ) -> dict[int, tuple[float, float]]:
        """Return ``{target: (cost, meters)}`` for every reachable target."""
        remaining = set(targets)
        found: dict[int, tuple[float, float]] = {}
        if source in remaining:
            found[source] = (0.0, 0.0)
            remaining.discard(source)
        best = {source: 0.0}
        meters = {source: 0.0}
        heap = [(0.0, source)]
        while heap and remaining:
            cost, node = heapq.heappop(heap)
            if cost > best.get(node, math.inf):
                continue
            if node in remaining:
                found[node] = (cost, meters[node])
                remaining.discard(node)
            for neighbor, edge_cost, edge_meters in self.adjacency[node]:
                value = cost + edge_cost
                if value < best.get(neighbor, math.inf):
                    best[neighbor] = value
                    meters[neighbor] = meters[node] + edge_meters
                    heapq.heappush(heap, (value, neighbor))
        return found

    def shortest_path(self, source: int, target: int) -> tuple[float, list[tuple[float, float]]] | None:
        """Return metric length and ``(lat, lon)`` coordinates, or ``None``."""
        if source == target:
            return 0.0, [(self.latitudes[source], self.longitudes[source])]
        best = {source: 0.0}
        meters = {source: 0.0}
        previous: dict[int, int] = {}
        heap = [(0.0, source)]
        while heap:
            cost, node = heapq.heappop(heap)
            if node == target:
                break
            if cost > best.get(node, math.inf):
                continue
            for neighbor, edge_cost, edge_meters in self.adjacency[node]:
                value = cost + edge_cost
                if value < best.get(neighbor, math.inf):
                    best[neighbor] = value
                    meters[neighbor] = meters[node] + edge_meters
                    previous[neighbor] = node
                    heapq.heappush(heap, (value, neighbor))
        if target not in best:
            return None
        nodes = [target]
        while nodes[-1] != source:
            nodes.append(previous[nodes[-1]])
        nodes.reverse()
        return meters[target], [(self.latitudes[node], self.longitudes[node]) for node in nodes]

    def _node(self, osm_id: int, coordinate: tuple[float, float]) -> int:
        index = self._index.get(osm_id)
        if index is None:
            index = len(self.latitudes)
            self._index[osm_id] = index
            self.latitudes.append(coordinate[0])
            self.longitudes.append(coordinate[1])
            self.adjacency.append([])
        return index

    def _connect(self, a: int, b: int, cost: float, meters: float) -> None:
        for edges, other in ((self.adjacency[a], b), (self.adjacency[b], a)):
            for position, (neighbor, existing_cost, _) in enumerate(edges):
                if neighbor == other:
                    if cost < existing_cost:
                        edges[position] = (other, cost, meters)
                    break
            else:
                edges.append((other, cost, meters))


@dataclass
class WaterIndex:
    """Water centrelines and shores used to keep shortcuts off rivers."""

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = field(default_factory=list)
    _grid: dict[tuple[int, int], list[int]] = field(default_factory=dict)

    def add_overpass(self, payload: Mapping[str, Any]) -> None:
        for element in payload.get("elements", []):
            if not isinstance(element, Mapping):
                continue
            geometries = []
            if isinstance(element.get("geometry"), list):
                geometries.append(element["geometry"])
            for member in element.get("members", []) or []:
                if isinstance(member, Mapping) and isinstance(member.get("geometry"), list):
                    geometries.append(member["geometry"])
            for geometry in geometries:
                points = [
                    (float(item["lat"]), float(item["lon"]))
                    for item in geometry
                    if isinstance(item, Mapping) and "lat" in item and "lon" in item
                ]
                for a, b in zip(points, points[1:]):
                    index = len(self.segments)
                    self.segments.append((a, b))
                    steps = max(1, int(haversine_meters(*a, *b) / 100))
                    keys = {
                        _grid_key(
                            a[0] + (b[0] - a[0]) * step / steps,
                            a[1] + (b[1] - a[1]) * step / steps,
                        )
                        for step in range(steps + 1)
                    }
                    for key in keys:
                        self._grid.setdefault(key, []).append(index)

    def crosses(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        if not self.segments:
            return False
        keys: set[tuple[int, int]] = set()
        steps = max(1, int(haversine_meters(*a, *b) / 50))
        for step in range(steps + 1):
            latitude = a[0] + (b[0] - a[0]) * step / steps
            longitude = a[1] + (b[1] - a[1]) * step / steps
            center = _grid_key(latitude, longitude)
            keys.update(_ring(center, 0))
            keys.update(_ring(center, 1))
        checked: set[int] = set()
        for key in keys:
            for index in self._grid.get(key, ()):
                if index in checked:
                    continue
                checked.add(index)
                c, d = self.segments[index]
                if segments_intersect(a, b, c, d):
                    return True
        return False


@dataclass(frozen=True)
class LoadedWalkNetwork:
    network: WalkNetwork
    water: WaterIndex
    source: dict[str, Any]


def load_walk_network(
    locations: Sequence[tuple[float, float]],
    *,
    files: Sequence[str | Path] = (),
    water_files: Sequence[str | Path] = (),
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    cache: JsonCache | None = None,
    margin_meters: float = 800,
    fetch_water: bool = True,
    requester: Callable[..., Any] = urlopen,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> LoadedWalkNetwork:
    """Build a walking network covering every location.

    Local Overpass JSON files are used as-is. Otherwise the locations are split
    into clusters, each cluster's bounding box is rounded outward to a 0.01°
    grid (so later runs reuse the cache), and the network is downloaded.
    """
    network = WalkNetwork()
    water = WaterIndex()
    source: dict[str, Any] = {
        "provider": "openstreetmap_overpass_walknet",
        "license": "ODbL",
        "attribution": "© OpenStreetMap contributors",
    }
    if files:
        for path in files:
            network.add_overpass(_read_json(path))
        for path in water_files:
            water.add_overpass(_read_json(path))
        source.update({"mode": "local_files", "files": [str(Path(path)) for path in files]})
    else:
        cache_value = cache or JsonCache()
        boxes = covering_boxes(locations, margin_meters=margin_meters)
        hits = 0
        for index, box in enumerate(boxes):
            payload, hit = _overpass(walknet_query(box), endpoint, cache_value, requester)
            hits += int(hit)
            network.add_overpass(payload)
            if fetch_water:
                water_payload, water_hit = _overpass(water_query(box), endpoint, cache_value, requester)
                hits += int(water_hit)
                water.add_overpass(water_payload)
            if progress is not None:
                progress(
                    {
                        "event": "walknet_progress",
                        "completed": index + 1,
                        "total": len(boxes),
                        "cacheHit": hit,
                    }
                )
        source.update(
            {
                "mode": "overpass",
                "endpoint": endpoint,
                "boxes": [list(box) for box in boxes],
                "cacheHits": hits,
            }
        )
    network.finalize()
    if network.node_count == 0:
        raise PublicServiceError("The walking network around the points is empty.")
    source["nodes"] = network.node_count
    source["waterSegments"] = len(water.segments)
    return LoadedWalkNetwork(network=network, water=water, source=source)


def covering_boxes(
    locations: Sequence[tuple[float, float]],
    *,
    margin_meters: float = 800,
) -> list[tuple[float, float, float, float]]:
    """Group nearby locations and return one rounded box per group."""
    remaining = list(dict.fromkeys((round(lat, 5), round(lon, 5)) for lat, lon in locations))
    clusters: list[list[tuple[float, float]]] = []
    while remaining:
        cluster = [remaining.pop()]
        changed = True
        while changed:
            changed = False
            for candidate in list(remaining):
                if any(
                    haversine_meters(*candidate, *member) <= CLUSTER_GAP_METERS
                    for member in cluster
                ):
                    cluster.append(candidate)
                    remaining.remove(candidate)
                    changed = True
        clusters.append(cluster)
    boxes = []
    for cluster in clusters:
        latitude = sum(point[0] for point in cluster) / len(cluster)
        dlat = margin_meters / 111_320
        dlon = margin_meters / (111_320 * max(0.2, math.cos(math.radians(latitude))))
        south = min(point[0] for point in cluster) - dlat
        north = max(point[0] for point in cluster) + dlat
        west = min(point[1] for point in cluster) - dlon
        east = max(point[1] for point in cluster) + dlon
        boxes.append(
            (
                math.floor(south / BBOX_GRID_DEGREES) * BBOX_GRID_DEGREES,
                math.floor(west / BBOX_GRID_DEGREES) * BBOX_GRID_DEGREES,
                math.ceil(north / BBOX_GRID_DEGREES) * BBOX_GRID_DEGREES,
                math.ceil(east / BBOX_GRID_DEGREES) * BBOX_GRID_DEGREES,
            )
        )
    return sorted(set(boxes))


def _overpass(
    query: str,
    endpoint: str,
    cache: JsonCache,
    requester: Callable[..., Any],
) -> tuple[Mapping[str, Any], bool]:
    key = _cache_key(endpoint, query)
    cached = cache.get("overpass-walknet", key, WALKNET_CACHE_DAYS)
    if cached is not None:
        return cached, True
    response = _post_form_json(
        endpoint,
        {"data": query},
        timeout_seconds=180,
        requester=requester,
    )
    if not isinstance(response, Mapping):
        raise PublicServiceError("Overpass returned a non-object response.")
    cache.put("overpass-walknet", key, response)
    return response, False


def _read_json(path: str | Path) -> Mapping[str, Any]:
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicServiceError(f"Cannot read Overpass JSON {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise PublicServiceError(f"Overpass JSON {path} must be an object.")
    return value


def _grid_key(latitude: float, longitude: float) -> tuple[int, int]:
    return math.floor(latitude / GRID_DEGREES), math.floor(longitude / GRID_DEGREES)


def _ring(center: tuple[int, int], radius: int) -> list[tuple[int, int]]:
    if radius == 0:
        return [center]
    x, y = center
    cells = []
    for dx in range(-radius, radius + 1):
        cells.append((x + dx, y - radius))
        cells.append((x + dx, y + radius))
    for dy in range(-radius + 1, radius):
        cells.append((x - radius, y + dy))
        cells.append((x + radius, y + dy))
    return cells
