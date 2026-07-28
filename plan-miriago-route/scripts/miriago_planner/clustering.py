"""Dependency-free geographic clustering and route ordering."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Iterable, Sequence

EARTH_RADIUS_METERS = 6_371_008.8


@dataclass(frozen=True)
class GeoPoint:
    key: str
    latitude: float
    longitude: float
    point_ids: tuple[str, ...]

    @property
    def weight(self) -> int:
        return len(self.point_ids)


@dataclass(frozen=True)
class PlanningProfile:
    name: str
    bundle_distance_meters: float
    hard_link_meters: float
    max_radius_p90_meters: float
    max_diameter_meters: float
    max_points: int
    cut_mad_multiplier: float
    local_gap_ratio: float
    merge_distance_meters: float


PROFILES: dict[str, PlanningProfile] = {
    "compact": PlanningProfile(
        name="compact",
        bundle_distance_meters=12,
        hard_link_meters=800,
        max_radius_p90_meters=650,
        max_diameter_meters=1_500,
        max_points=15,
        cut_mad_multiplier=2.5,
        local_gap_ratio=1.8,
        merge_distance_meters=350,
    ),
    "balanced": PlanningProfile(
        name="balanced",
        bundle_distance_meters=15,
        hard_link_meters=1_400,
        max_radius_p90_meters=1_100,
        max_diameter_meters=2_600,
        max_points=25,
        cut_mad_multiplier=3.25,
        local_gap_ratio=2.2,
        merge_distance_meters=550,
    ),
    "broad": PlanningProfile(
        name="broad",
        bundle_distance_meters=20,
        hard_link_meters=2_600,
        max_radius_p90_meters=2_000,
        max_diameter_meters=4_800,
        max_points=35,
        cut_mad_multiplier=4.0,
        local_gap_ratio=2.8,
        merge_distance_meters=900,
    ),
}


def profile_with_overrides(
    name: str,
    *,
    max_points: int | None = None,
    max_radius_meters: float | None = None,
    hard_link_meters: float | None = None,
) -> PlanningProfile:
    try:
        source = PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown planning profile: {name}") from exc
    if max_points is not None and max_points < 1:
        raise ValueError("max_points must be positive")
    if max_radius_meters is not None and max_radius_meters <= 0:
        raise ValueError("max_radius_meters must be positive")
    if hard_link_meters is not None and hard_link_meters <= 0:
        raise ValueError("hard_link_meters must be positive")
    return PlanningProfile(
        name=source.name,
        bundle_distance_meters=source.bundle_distance_meters,
        hard_link_meters=hard_link_meters or source.hard_link_meters,
        max_radius_p90_meters=max_radius_meters or source.max_radius_p90_meters,
        max_diameter_meters=max(
            (max_radius_meters or source.max_radius_p90_meters) * 2.2,
            source.max_diameter_meters if max_radius_meters is None else 0,
        ),
        max_points=max_points or source.max_points,
        cut_mad_multiplier=source.cut_mad_multiplier,
        local_gap_ratio=source.local_gap_ratio,
        merge_distance_meters=source.merge_distance_meters,
    )


def haversine_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    sin_lat = math.sin(delta_lat / 2)
    sin_lon = math.sin(delta_lon / 2)
    value = sin_lat * sin_lat + math.cos(lat_a) * math.cos(lat_b) * sin_lon * sin_lon
    return 2 * EARTH_RADIUS_METERS * math.asin(min(1.0, math.sqrt(value)))


def distance_matrix(points: Sequence[GeoPoint]) -> list[list[float]]:
    size = len(points)
    matrix = [[0.0] * size for _ in range(size)]
    for left in range(size):
        for right in range(left + 1, size):
            value = haversine_meters(
                points[left].latitude,
                points[left].longitude,
                points[right].latitude,
                points[right].longitude,
            )
            matrix[left][right] = value
            matrix[right][left] = value
    return matrix


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        if root_left < root_right:
            self.parent[root_right] = root_left
        else:
            self.parent[root_left] = root_right


def bundle_points(
    points: Sequence[GeoPoint],
    threshold_meters: float,
) -> list[GeoPoint]:
    if not points:
        return []
    matrix = distance_matrix(points)
    union_find = _UnionFind(len(points))
    for left in range(len(points)):
        for right in range(left + 1, len(points)):
            if matrix[left][right] <= threshold_meters:
                union_find.union(left, right)

    members: dict[int, list[int]] = {}
    for index in range(len(points)):
        members.setdefault(union_find.find(index), []).append(index)

    bundled: list[GeoPoint] = []
    for indices in sorted(members.values(), key=lambda values: points[min(values)].key):
        source_points = [points[index] for index in indices]
        point_ids = tuple(
            point_id for point in source_points for point_id in point.point_ids
        )
        bundled.append(
            GeoPoint(
                key=min(point.key for point in source_points),
                latitude=sum(point.latitude for point in source_points)
                / len(source_points),
                longitude=_mean_longitude([point.longitude for point in source_points]),
                point_ids=point_ids,
            )
        )
    return bundled


def _mean_longitude(longitudes: Sequence[float]) -> float:
    sine = sum(math.sin(math.radians(value)) for value in longitudes)
    cosine = sum(math.cos(math.radians(value)) for value in longitudes)
    if sine == 0 and cosine == 0:
        return longitudes[0]
    result = math.degrees(math.atan2(sine, cosine))
    return ((result + 180) % 360) - 180


def cluster_points(
    points: Sequence[GeoPoint],
    profile: PlanningProfile,
) -> tuple[list[list[int]], dict[str, object]]:
    if not points:
        return [], {"mstEdges": [], "cutEdges": [], "localScaleMeters": []}
    if len(points) == 1:
        return [[0]], {
            "mstEdges": [],
            "cutEdges": [],
            "localScaleMeters": [0.0],
        }

    matrix = distance_matrix(points)
    local_scales = _local_scales(matrix)
    reachability = [
        [
            0.0
            if left == right
            else max(local_scales[left], local_scales[right], matrix[left][right])
            for right in range(len(points))
        ]
        for left in range(len(points))
    ]
    mst_edges = _prim_mst(reachability)
    edge_weights = [weight for _, _, weight in mst_edges]
    edge_median = median(edge_weights)
    deviations = [abs(value - edge_median) for value in edge_weights]
    edge_mad = median(deviations) if deviations else 0.0
    robust_spread = max(edge_mad, edge_median * 0.12, 1.0)
    statistical_cut = edge_median + profile.cut_mad_multiplier * robust_spread

    union_find = _UnionFind(len(points))
    cut_edges: list[dict[str, float | int | str]] = []
    for left, right, reachability_weight in mst_edges:
        direct = matrix[left][right]
        local_base = max(local_scales[left], local_scales[right], 1.0)
        reason: str | None = None
        if direct > profile.hard_link_meters:
            reason = "hard_link"
        elif (
            reachability_weight > statistical_cut
            and direct / local_base > profile.local_gap_ratio
        ):
            reason = "density_gap"
        if reason is None:
            union_find.union(left, right)
        else:
            cut_edges.append(
                {
                    "left": left,
                    "right": right,
                    "distanceMeters": round(direct, 3),
                    "reachabilityMeters": round(reachability_weight, 3),
                    "reason": reason,
                }
            )

    initial: dict[int, list[int]] = {}
    for index in range(len(points)):
        initial.setdefault(union_find.find(index), []).append(index)

    split_clusters: list[list[int]] = []
    for cluster in initial.values():
        split_clusters.extend(_split_to_constraints(cluster, points, matrix, profile))
    clusters = _merge_small_clusters(split_clusters, points, matrix, profile)
    clusters.sort(key=lambda cluster: _cluster_sort_key(cluster, points))

    return clusters, {
        "mstEdges": [
            {
                "left": left,
                "right": right,
                "reachabilityMeters": round(weight, 3),
                "distanceMeters": round(matrix[left][right], 3),
            }
            for left, right, weight in mst_edges
        ],
        "cutEdges": cut_edges,
        "localScaleMeters": [round(value, 3) for value in local_scales],
        "statisticalCutMeters": round(statistical_cut, 3),
    }


def _local_scales(matrix: Sequence[Sequence[float]]) -> list[float]:
    neighbor_index = min(3, len(matrix) - 1)
    scales: list[float] = []
    for row in matrix:
        distances = sorted(value for value in row if value > 0)
        scales.append(distances[neighbor_index - 1] if distances else 0.0)
    return scales


def _prim_mst(matrix: Sequence[Sequence[float]]) -> list[tuple[int, int, float]]:
    size = len(matrix)
    in_tree = [False] * size
    best = [math.inf] * size
    parent = [-1] * size
    best[0] = 0.0
    edges: list[tuple[int, int, float]] = []
    for _ in range(size):
        candidates = [index for index in range(size) if not in_tree[index]]
        current = min(candidates, key=lambda index: (best[index], index))
        in_tree[current] = True
        if parent[current] >= 0:
            edges.append((parent[current], current, best[current]))
        for target in range(size):
            weight = matrix[current][target]
            if (
                not in_tree[target]
                and current != target
                and (weight, current) < (best[target], parent[target])
            ):
                best[target] = weight
                parent[target] = current
    return edges


def _split_to_constraints(
    cluster: list[int],
    points: Sequence[GeoPoint],
    matrix: Sequence[Sequence[float]],
    profile: PlanningProfile,
) -> list[list[int]]:
    if _fits_constraints(cluster, points, matrix, profile):
        return [sorted(cluster)]
    if len(cluster) <= 1:
        return [cluster]

    seed_left, seed_right = _farthest_pair(cluster, matrix)
    left: list[int] = []
    right: list[int] = []
    for index in sorted(cluster):
        distance_left = matrix[index][seed_left]
        distance_right = matrix[index][seed_right]
        if (distance_left, 0) <= (distance_right, 1):
            left.append(index)
        else:
            right.append(index)
    if not left or not right:
        ordered = sorted(cluster, key=lambda index: points[index].key)
        midpoint = max(1, len(ordered) // 2)
        left, right = ordered[:midpoint], ordered[midpoint:]
    return _split_to_constraints(left, points, matrix, profile) + _split_to_constraints(
        right, points, matrix, profile
    )


def _fits_constraints(
    cluster: Sequence[int],
    points: Sequence[GeoPoint],
    matrix: Sequence[Sequence[float]],
    profile: PlanningProfile,
) -> bool:
    if sum(points[index].weight for index in cluster) > profile.max_points:
        return False
    if len(cluster) <= 1:
        return True
    medoid = medoid_index(cluster, matrix)
    radii = sorted(matrix[medoid][index] for index in cluster)
    p90 = percentile(radii, 90)
    diameter = max(matrix[left][right] for left in cluster for right in cluster)
    return (
        p90 <= profile.max_radius_p90_meters and diameter <= profile.max_diameter_meters
    )


def _merge_small_clusters(
    clusters: list[list[int]],
    points: Sequence[GeoPoint],
    matrix: Sequence[Sequence[float]],
    profile: PlanningProfile,
) -> list[list[int]]:
    result = [sorted(cluster) for cluster in clusters]
    changed = True
    while changed and len(result) > 1:
        changed = False
        candidates: list[tuple[float, int, int]] = []
        for left in range(len(result)):
            for right in range(left + 1, len(result)):
                closest = min(matrix[a][b] for a in result[left] for b in result[right])
                candidates.append((closest, left, right))
        for closest, left, right in sorted(candidates):
            if closest > profile.merge_distance_meters:
                break
            combined = sorted(result[left] + result[right])
            if _fits_constraints(combined, points, matrix, profile):
                result[left] = combined
                del result[right]
                changed = True
                break
    return result


def _farthest_pair(
    cluster: Sequence[int],
    matrix: Sequence[Sequence[float]],
) -> tuple[int, int]:
    return max(
        (
            (left, right)
            for position, left in enumerate(cluster)
            for right in cluster[position + 1 :]
        ),
        key=lambda pair: (matrix[pair[0]][pair[1]], -pair[0], -pair[1]),
    )


def medoid_index(
    cluster: Sequence[int],
    matrix: Sequence[Sequence[float]],
) -> int:
    return min(
        cluster,
        key=lambda candidate: (
            sum(matrix[candidate][other] for other in cluster),
            candidate,
        ),
    )


def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def cluster_metrics(
    cluster: Sequence[int],
    points: Sequence[GeoPoint],
    matrix: Sequence[Sequence[float]],
) -> dict[str, object]:
    medoid = medoid_index(cluster, matrix)
    radii = [matrix[medoid][index] for index in cluster]
    diameter = max(
        (matrix[left][right] for left in cluster for right in cluster),
        default=0.0,
    )
    latitudes = [points[index].latitude for index in cluster]
    longitudes = [points[index].longitude for index in cluster]
    west, east = _longitude_bounds(longitudes)
    return {
        "stopCount": len(cluster),
        "pointCount": sum(points[index].weight for index in cluster),
        "medoidStopKey": points[medoid].key,
        "radiusP90Meters": round(percentile(radii, 90), 1),
        "diameterMeters": round(diameter, 1),
        "bbox": {
            "south": min(latitudes),
            "west": west,
            "north": max(latitudes),
            "east": east,
        },
    }


def _longitude_bounds(longitudes: Sequence[float]) -> tuple[float, float]:
    """Return the shortest circular interval; west > east means dateline wrap."""
    if len(longitudes) == 1:
        return longitudes[0], longitudes[0]
    normalized = sorted((value + 180) % 360 for value in longitudes)
    gaps = [
        (
            (
                normalized[(index + 1) % len(normalized)]
                + (360 if index + 1 == len(normalized) else 0)
            )
            - normalized[index],
            index,
        )
        for index in range(len(normalized))
    ]
    _, gap_index = max(gaps)
    start = normalized[(gap_index + 1) % len(normalized)]
    end = normalized[gap_index]
    west = ((start - 180 + 180) % 360) - 180
    east = ((end - 180 + 180) % 360) - 180
    return west, east


def order_open_path(
    indices: Sequence[int],
    matrix: Sequence[Sequence[float]],
) -> list[int]:
    if len(indices) <= 1:
        return list(indices)
    if len(indices) == 2:
        left, right = indices
        if (matrix[right][left], right, left) < (
            matrix[left][right],
            left,
            right,
        ):
            return [right, left]
        return [left, right]
    start, end = _farthest_pair(indices, matrix)
    candidates = [
        _greedy_path(indices, matrix, start, end),
        _greedy_path(indices, matrix, end, start),
    ]
    optimized = [_two_opt_open(path, matrix) for path in candidates]
    return min(
        optimized,
        key=lambda path: (
            path_length(path, matrix),
            tuple(path),
        ),
    )


def _greedy_path(
    indices: Sequence[int],
    matrix: Sequence[Sequence[float]],
    start: int,
    end: int,
) -> list[int]:
    remaining = set(indices)
    remaining.remove(start)
    remaining.remove(end)
    path = [start]
    current = start
    while remaining:
        next_index = min(remaining, key=lambda value: (matrix[current][value], value))
        path.append(next_index)
        remaining.remove(next_index)
        current = next_index
    path.append(end)
    return path


def _two_opt_open(
    path: list[int],
    matrix: Sequence[Sequence[float]],
) -> list[int]:
    best = list(path)
    best_length = path_length(best, matrix)
    changed = True
    while changed:
        changed = False
        for left in range(0, len(best) - 2):
            for right in range(left + 2, len(best)):
                candidate = (
                    best[: left + 1]
                    + list(reversed(best[left + 1 : right + 1]))
                    + best[right + 1 :]
                )
                candidate_length = path_length(candidate, matrix)
                if candidate_length + 1e-9 < best_length:
                    best = candidate
                    best_length = candidate_length
                    changed = True
                    break
            if changed:
                break
    return best


def path_length(
    path: Sequence[int],
    matrix: Sequence[Sequence[float]],
) -> float:
    return sum(matrix[left][right] for left, right in zip(path, path[1:]))


def order_clusters(
    clusters: Sequence[Sequence[int]],
    points: Sequence[GeoPoint],
    matrix: Sequence[Sequence[float]],
) -> list[int]:
    if len(clusters) <= 1:
        return list(range(len(clusters)))
    medoids = [medoid_index(cluster, matrix) for cluster in clusters]
    cluster_matrix = [[matrix[left][right] for right in medoids] for left in medoids]
    return order_open_path(list(range(len(clusters))), cluster_matrix)


def _cluster_sort_key(
    cluster: Sequence[int],
    points: Sequence[GeoPoint],
) -> tuple[float, float, str]:
    return (
        min(points[index].latitude for index in cluster),
        min(points[index].longitude for index in cluster),
        min(points[index].key for index in cluster),
    )


def nearest_items(
    latitude: float,
    longitude: float,
    items: Iterable[dict[str, object]],
    limit: int = 5,
) -> list[dict[str, object]]:
    ranked: list[tuple[float, str, dict[str, object]]] = []
    for item in items:
        item_latitude = item.get("latitude")
        item_longitude = item.get("longitude")
        if not isinstance(item_latitude, (int, float)) or not isinstance(
            item_longitude, (int, float)
        ):
            continue
        distance = haversine_meters(
            latitude,
            longitude,
            float(item_latitude),
            float(item_longitude),
        )
        ranked.append((distance, str(item.get("id", "")), item))
    result: list[dict[str, object]] = []
    for distance, _, item in sorted(ranked)[:limit]:
        candidate = dict(item)
        candidate["directDistanceMeters"] = round(distance, 1)
        result.append(candidate)
    return result
