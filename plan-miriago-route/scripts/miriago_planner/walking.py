"""Walking-distance backends and stop sequencing shared by planning commands.

Three interchangeable backends produce a metre matrix between sites:

- ``straight``: haversine × 1.3, a labelled estimate that is never treated as
  road-network evidence;
- ``osm``: an offline OpenStreetMap pedestrian network (see :mod:`walknet`);
- ``valhalla``: the public FOSSGIS Valhalla matrix, tiled to its limits.

On top of any backend, :func:`sequence_stops` orders stops as an open path
with optional fixed, external, or automatically chosen (station) endpoints and
via points, and reports every leg so callers can audit the total.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .clustering import haversine_meters
from .public_services import (
    DEFAULT_VALHALLA_ENDPOINT,
    DEFAULT_VALHALLA_ROUTE_ENDPOINT,
    JsonCache,
    PublicServiceError,
    fetch_walking_matrix,
    fetch_walking_route,
)
from .routing import UNREACHABLE, solve_open_path
from .walknet import LoadedWalkNetwork, WaterIndex

DEFAULT_SPEED_MPS = 1.2
LOCAL_CORRECTION_METERS = 150
SNAP_ALLOWANCE_METERS = 15
STRAIGHT_FACTOR = 1.3
UNREACHABLE_FACTOR = 1.5
BACKENDS = ("straight", "osm", "valhalla")


@dataclass(frozen=True)
class Site:
    latitude: float
    longitude: float

    @property
    def coordinate(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


@dataclass
class RawMatrix:
    meters: list[list[float]]
    seconds: list[list[float]] | None
    warnings: list[str] = field(default_factory=list)


class StraightBackend:
    name = "straight"
    provider = "haversine_estimate"
    road_network = False
    water: WaterIndex | None = None

    def __init__(self, *, factor: float = STRAIGHT_FACTOR, speed: float = DEFAULT_SPEED_MPS):
        self.factor = factor
        self.speed = speed

    def raw_matrix(self, sites: Sequence[Site]) -> RawMatrix:
        return RawMatrix(
            meters=[
                [haversine_meters(*a.coordinate, *b.coordinate) * self.factor for b in sites]
                for a in sites
            ],
            seconds=None,
        )

    def polyline(
        self,
        sites: Sequence[Site],
        straight_pairs: set[tuple[int, int]] | None = None,
    ) -> list[list[float]]:
        return [[site.longitude, site.latitude] for site in sites]


class OsmBackend:
    name = "osm"
    provider = "openstreetmap_walknet"
    road_network = True

    def __init__(self, loaded: LoadedWalkNetwork, *, speed: float = DEFAULT_SPEED_MPS):
        self.loaded = loaded
        self.network = loaded.network
        self.water: WaterIndex | None = loaded.water
        self.speed = speed
        self._snaps: dict[tuple[float, float], tuple[int, float] | None] = {}

    def snap(self, site: Site) -> tuple[int, float] | None:
        key = (round(site.latitude, 7), round(site.longitude, 7))
        if key not in self._snaps:
            self._snaps[key] = self.network.snap(site.latitude, site.longitude)
        return self._snaps[key]

    def raw_matrix(self, sites: Sequence[Site]) -> RawMatrix:
        snaps = [self.snap(site) for site in sites]
        nodes = sorted({snap[0] for snap in snaps if snap is not None})
        distances: dict[int, dict[int, tuple[float, float]]] = {}
        for node in nodes:
            distances[node] = self.network.shortest_from(node, nodes)
        size = len(sites)
        meters = [[0.0] * size for _ in range(size)]
        warnings: list[str] = []
        unreachable = 0
        for left in range(size):
            for right in range(size):
                if left == right:
                    continue
                a, b = snaps[left], snaps[right]
                found = distances.get(a[0], {}).get(b[0]) if a and b else None
                if found is None:
                    unreachable += 1
                    meters[left][right] = (
                        haversine_meters(*sites[left].coordinate, *sites[right].coordinate)
                        * UNREACHABLE_FACTOR
                    )
                    continue
                value = found[1]
                value += max(0.0, a[1] - SNAP_ALLOWANCE_METERS)
                value += max(0.0, b[1] - SNAP_ALLOWANCE_METERS)
                meters[left][right] = value
        if unreachable:
            warnings.append(
                f"{unreachable // 2 or unreachable} site pairs are disconnected in the "
                "walking network; haversine × 1.5 was used for them."
            )
        for left in range(size):
            for right in range(left + 1, size):
                value = min(meters[left][right], meters[right][left])
                meters[left][right] = meters[right][left] = value
        return RawMatrix(meters=meters, seconds=None, warnings=warnings)

    def polyline(
        self,
        sites: Sequence[Site],
        straight_pairs: set[tuple[int, int]] | None = None,
    ) -> list[list[float]]:
        coordinates: list[list[float]] = []
        for index, (a, b) in enumerate(zip(sites, sites[1:])):
            leg = [a.coordinate, b.coordinate]
            if not straight_pairs or (index, index + 1) not in straight_pairs:
                snap_a, snap_b = self.snap(a), self.snap(b)
                if snap_a and snap_b:
                    found = self.network.shortest_path(snap_a[0], snap_b[0])
                    if found is not None:
                        leg = [a.coordinate, *found[1], b.coordinate]
            _extend(coordinates, [[lon, lat] for lat, lon in leg])
        return coordinates


class ValhallaBackend:
    name = "valhalla"
    provider = "fossgis_valhalla_pedestrian"
    road_network = True
    water: WaterIndex | None = None

    def __init__(
        self,
        *,
        matrix_endpoint: str = DEFAULT_VALHALLA_ENDPOINT,
        route_endpoint: str = DEFAULT_VALHALLA_ROUTE_ENDPOINT,
        cache: JsonCache | None = None,
        speed: float = DEFAULT_SPEED_MPS,
        request_delay_seconds: float = 0.25,
        requester: Callable[..., Any] | None = None,
    ):
        self.matrix_endpoint = matrix_endpoint
        self.route_endpoint = route_endpoint
        self.cache = cache or JsonCache()
        self.speed = speed
        self.request_delay_seconds = request_delay_seconds
        self.requester = requester

    def raw_matrix(self, sites: Sequence[Site]) -> RawMatrix:
        extra = {"requester": self.requester} if self.requester else {}
        result = fetch_walking_matrix(
            [{"latitude": site.latitude, "longitude": site.longitude} for site in sites],
            endpoint=self.matrix_endpoint,
            cache=self.cache,
            request_delay_seconds=self.request_delay_seconds,
            **extra,
        )
        meters = [list(row) for row in result.distances_meters]
        seconds = [list(row) for row in result.durations_seconds]
        warnings: list[str] = []
        for left in range(len(sites)):
            for right in range(len(sites)):
                if left != right and not math.isfinite(meters[left][right]):
                    meters[left][right] = (
                        haversine_meters(*sites[left].coordinate, *sites[right].coordinate)
                        * UNREACHABLE_FACTOR
                    )
                    seconds[left][right] = meters[left][right] / self.speed
                    warnings.append("Valhalla reported a disconnected pair.")
        return RawMatrix(meters=meters, seconds=seconds, warnings=sorted(set(warnings)))

    def polyline(
        self,
        sites: Sequence[Site],
        straight_pairs: set[tuple[int, int]] | None = None,
    ) -> list[list[float]]:
        extra = {"requester": self.requester} if self.requester else {}
        route = fetch_walking_route(
            [{"latitude": site.latitude, "longitude": site.longitude} for site in sites],
            endpoint=self.route_endpoint,
            cache=self.cache,
            request_delay_seconds=self.request_delay_seconds,
            **extra,
        )
        return [list(item) for item in route.geojson_coordinates]


@dataclass
class CostMatrix:
    meters: list[list[float]]
    seconds: list[list[float]]
    shortcut_pairs: set[tuple[int, int]]
    warnings: list[str]


def cost_matrix(
    backend: Any,
    sites: Sequence[Site],
    *,
    via: Mapping[int, int] | None = None,
    local_correction_meters: float = LOCAL_CORRECTION_METERS,
) -> CostMatrix:
    """Return corrected metres and seconds between sites.

    * Short legs: OpenStreetMap often lacks paths inside stations, shrines, and
      parks, so a leg shorter than ``local_correction_meters`` in a straight
      line is capped at ``1.5 × straight + 30`` unless it crosses water.
    * Via points: ``via[stop] = via_site`` forces every leg to or from ``stop``
      through ``via_site`` (for example a specific trailhead).
    """
    raw = backend.raw_matrix(sites)
    size = len(sites)
    meters = [list(row) for row in raw.meters]
    shortcut_pairs: set[tuple[int, int]] = set()
    water = getattr(backend, "water", None)
    if backend.road_network and water is not None and local_correction_meters > 0:
        for left in range(size):
            for right in range(left + 1, size):
                straight = haversine_meters(*sites[left].coordinate, *sites[right].coordinate)
                cap = straight * 1.5 + 30
                if (
                    straight < local_correction_meters
                    and meters[left][right] > cap
                    and not water.crosses(sites[left].coordinate, sites[right].coordinate)
                ):
                    meters[left][right] = meters[right][left] = cap
                    shortcut_pairs.update({(left, right), (right, left)})
    for stop, via_site in (via or {}).items():
        for other in range(size):
            if other in (stop, via_site):
                continue
            value = meters[other][via_site] + meters[via_site][stop]
            meters[other][stop] = meters[stop][other] = value
            shortcut_pairs.discard((other, stop))
            shortcut_pairs.discard((stop, other))
    speed = getattr(backend, "speed", DEFAULT_SPEED_MPS)
    seconds = [[value / speed for value in row] for row in meters]
    if raw.seconds is not None and not via and not shortcut_pairs:
        seconds = [list(row) for row in raw.seconds]
    return CostMatrix(
        meters=meters,
        seconds=seconds,
        shortcut_pairs=shortcut_pairs,
        warnings=list(raw.warnings),
    )


@dataclass(frozen=True)
class EndSpec:
    """How an open path starts or ends.

    ``free``: no constraint. ``site``: walk from/to an external location (a
    station or the previous area's last stop). ``stop``: the given stop index
    must be first/last. ``auto``: the cheapest of ``candidates`` (stations).
    """

    kind: str = "free"
    site: Site | None = None
    label: str | None = None
    stop: int | None = None
    candidates: tuple[tuple[str, Site], ...] = ()


@dataclass
class SequenceResult:
    order: list[int]
    inner_meters: list[float]
    start_meters: float
    end_meters: float
    start_label: str | None
    end_label: str | None
    start_site: Site | None
    end_site: Site | None
    stop_matrix: list[list[float]]
    nearest_candidate: list[tuple[float, str | None]]
    candidate_sites: dict[str, Site]
    sites: list[Site]
    via: dict[int, int]
    shortcut_pairs: set[tuple[int, int]]
    warnings: list[str]
    seconds_per_meter: float

    @property
    def total_meters(self) -> float:
        return self.start_meters + sum(self.inner_meters) + self.end_meters

    def ordered_sites(self) -> tuple[list[Site], set[tuple[int, int]]]:
        """Visit-order sites including endpoints and via points for geometry."""
        indices: list[int] = []
        if self.start_site is not None:
            indices.append(self._site_index(self.start_site))
        indices.extend(self.order)
        if self.end_site is not None:
            indices.append(self._site_index(self.end_site))
        expanded: list[int] = []
        for position, index in enumerate(indices):
            if position > 0:
                previous = indices[position - 1]
                via_site = self.via.get(index, self.via.get(previous))
                if via_site is not None and via_site not in (index, previous):
                    expanded.append(via_site)
            expanded.append(index)
        straight = {
            (position, position + 1)
            for position, (a, b) in enumerate(zip(expanded, expanded[1:]))
            if (a, b) in self.shortcut_pairs
        }
        return [self.sites[index] for index in expanded], straight

    def _site_index(self, site: Site) -> int:
        return self.sites.index(site)


def sequence_stops(
    backend: Any,
    stops: Sequence[Site],
    *,
    start: EndSpec | None = None,
    end: EndSpec | None = None,
    via: Mapping[int, Site] | None = None,
    restarts: int = 48,
    local_correction_meters: float = LOCAL_CORRECTION_METERS,
    candidate_limit: int = 40,
    station_candidates: Sequence[tuple[str, Site]] = (),
) -> SequenceResult:
    start = start or EndSpec()
    end = end or EndSpec()
    count = len(stops)
    if count == 0:
        raise ValueError("At least one stop is required.")
    sites: list[Site] = list(stops)

    def add(site: Site) -> int:
        if site in sites[count:]:
            return sites.index(site, count)
        sites.append(site)
        return len(sites) - 1

    start_index = add(start.site) if start.kind == "site" and start.site else None
    end_index = add(end.site) if end.kind == "site" and end.site else None
    candidates: list[tuple[str, int]] = []
    pool = {label: site for label, site in station_candidates}
    pool.update(
        {label: site for spec in (start, end) if spec.kind == "auto" for label, site in spec.candidates}
    )
    if pool:
        ranked = sorted(
            pool.items(),
            key=lambda item: min(haversine_meters(*item[1].coordinate, *stop.coordinate) for stop in stops),
        )[:candidate_limit]
        candidates = [(label, add(site)) for label, site in ranked]
    via_map = {stop: add(site) for stop, site in (via or {}).items() if 0 <= stop < count}
    matrix = cost_matrix(
        backend,
        sites,
        via=via_map,
        local_correction_meters=local_correction_meters,
    )
    meters = matrix.meters
    nearest = [
        min(((meters[index][site], label) for label, site in candidates), default=(0.0, None))
        for index in range(count)
    ]

    def costs(spec: EndSpec, fixed_index: int | None) -> list[float] | None:
        if spec.kind == "site" and fixed_index is not None:
            return [meters[fixed_index][index] for index in range(count)]
        if spec.kind == "stop" and spec.stop is not None:
            return [0.0 if index == spec.stop else UNREACHABLE for index in range(count)]
        if spec.kind == "auto" and candidates:
            return [nearest[index][0] for index in range(count)]
        return None

    stop_matrix = [row[:count] for row in meters[:count]]
    order = solve_open_path(
        stop_matrix,
        start_costs=costs(start, start_index),
        end_costs=costs(end, end_index),
        restarts=restarts,
    )
    first, last = order[0], order[-1]

    def resolve(spec: EndSpec, fixed_index: int | None, stop: int) -> tuple[float, str | None, Site | None]:
        if spec.kind == "site" and fixed_index is not None:
            return meters[fixed_index][stop], spec.label, sites[fixed_index]
        if spec.kind == "auto" and candidates:
            value, label = nearest[stop]
            site = next(sites[index] for name, index in candidates if name == label)
            return value, label, site
        return 0.0, None, None

    start_meters, start_label, start_site = resolve(start, start_index, first)
    end_meters, end_label, end_site = resolve(end, end_index, last)
    total_meters = sum(sum(row) for row in meters) or 1.0
    total_seconds = sum(sum(row) for row in matrix.seconds) or total_meters
    return SequenceResult(
        order=order,
        inner_meters=[meters[a][b] for a, b in zip(order, order[1:])],
        start_meters=start_meters,
        end_meters=end_meters,
        start_label=start_label,
        end_label=end_label,
        start_site=start_site,
        end_site=end_site,
        stop_matrix=stop_matrix,
        nearest_candidate=nearest,
        candidate_sites={label: sites[index] for label, index in candidates},
        sites=sites,
        via=via_map,
        shortcut_pairs=matrix.shortcut_pairs,
        warnings=matrix.warnings,
        seconds_per_meter=total_seconds / total_meters,
    )


def route_polyline(backend: Any, result: SequenceResult) -> list[list[float]]:
    """Return GeoJSON-order coordinates for the whole sequenced route."""
    sites, straight_pairs = result.ordered_sites()
    if len(sites) < 2:
        return [[site.longitude, site.latitude] for site in sites]
    try:
        return backend.polyline(sites, straight_pairs)
    except PublicServiceError:
        return [[site.longitude, site.latitude] for site in sites]


def make_backend(
    name: str,
    *,
    locations: Sequence[tuple[float, float]] = (),
    walknet_files: Sequence[str] = (),
    water_files: Sequence[str] = (),
    overpass_endpoint: str | None = None,
    valhalla_endpoint: str = DEFAULT_VALHALLA_ENDPOINT,
    valhalla_route_endpoint: str = DEFAULT_VALHALLA_ROUTE_ENDPOINT,
    cache: JsonCache | None = None,
    speed: float = DEFAULT_SPEED_MPS,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    if name == "straight":
        return StraightBackend(speed=speed)
    if name == "valhalla":
        return ValhallaBackend(
            matrix_endpoint=valhalla_endpoint,
            route_endpoint=valhalla_route_endpoint,
            cache=cache,
            speed=speed,
        )
    if name == "osm":
        from .walknet import load_walk_network

        extra: dict[str, Any] = {}
        if overpass_endpoint:
            extra["endpoint"] = overpass_endpoint
        loaded = load_walk_network(
            locations,
            files=walknet_files,
            water_files=water_files,
            cache=cache,
            progress=progress,
            **extra,
        )
        return OsmBackend(loaded, speed=speed)
    raise ValueError(f"Unknown walking backend: {name}")


def _extend(target: list[list[float]], values: list[list[float]]) -> None:
    for value in values:
        if not target or target[-1] != value:
            target.append(value)
