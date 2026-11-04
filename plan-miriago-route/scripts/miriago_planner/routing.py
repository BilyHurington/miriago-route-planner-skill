"""Dependency-free open-path route optimisation and route-quality checks.

Every solver here works on a plain distance matrix so it can be fed by
haversine estimates, an offline OpenStreetMap walking network, or a public
Valhalla matrix alike.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

UNREACHABLE = 1e9
EXACT_LIMIT = 9


def symmetric_matrix(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    """Average both directions and replace unreachable cells by a large cost.

    2-opt reverses path segments, which is only exact on a symmetric matrix.
    Walking networks are nearly symmetric, so the average is a safe proxy;
    callers report totals from their original matrix.
    """
    size = len(matrix)
    result = [[0.0] * size for _ in range(size)]
    for left in range(size):
        for right in range(left + 1, size):
            forward = _finite(matrix[left][right])
            backward = _finite(matrix[right][left])
            if forward >= UNREACHABLE and backward < UNREACHABLE:
                value = backward
            elif backward >= UNREACHABLE and forward < UNREACHABLE:
                value = forward
            else:
                value = (forward + backward) / 2
            result[left][right] = value
            result[right][left] = value
    return result


def solve_open_path(
    matrix: Sequence[Sequence[float]],
    *,
    start_costs: Sequence[float] | None = None,
    end_costs: Sequence[float] | None = None,
    restarts: int = 32,
    seed: int = 0,
) -> list[int]:
    """Return a short open visiting order over every matrix index.

    ``start_costs[i]`` is the cost of beginning at node ``i`` (for example the
    walk from a station); ``end_costs`` likewise for finishing. ``None`` leaves
    that end free. Forcing a fixed endpoint is expressed as zero cost for that
    node and :data:`UNREACHABLE` for every other node.
    """
    size = len(matrix)
    if size == 0:
        return []
    if size == 1:
        return [0]
    base = symmetric_matrix(matrix)
    starts = [_finite(value) for value in (start_costs or [0.0] * size)]
    ends = [_finite(value) for value in (end_costs or [0.0] * size)]
    if len(starts) != size or len(ends) != size:
        raise ValueError("start_costs and end_costs must match the matrix size")
    start_node = size
    end_node = size + 1
    augmented = [row + [starts[index], ends[index]] for index, row in enumerate(base)]
    augmented.append(starts + [0.0, UNREACHABLE])
    augmented.append(ends + [UNREACHABLE, 0.0])
    if size <= EXACT_LIMIT:
        path = _held_karp(augmented, start_node, end_node)
    else:
        path = _heuristic_path(augmented, start_node, end_node, max(1, restarts), seed)
    return [node for node in path if node < size]


def path_cost(path: Sequence[int], matrix: Sequence[Sequence[float]]) -> float:
    return sum(matrix[left][right] for left, right in zip(path, path[1:]))


def _finite(value: float) -> float:
    if value is None or not math.isfinite(float(value)):
        return UNREACHABLE
    return min(float(value), UNREACHABLE)


def _held_karp(matrix: list[list[float]], start: int, end: int) -> list[int]:
    inner = [node for node in range(len(matrix)) if node not in (start, end)]
    count = len(inner)
    full = (1 << count) - 1
    best: dict[tuple[int, int], tuple[float, int]] = {}
    for position, node in enumerate(inner):
        best[(1 << position, position)] = (matrix[start][node], -1)
    for mask in range(1, full + 1):
        for last in range(count):
            state = best.get((mask, last))
            if state is None:
                continue
            cost = state[0]
            for nxt in range(count):
                if mask & (1 << nxt):
                    continue
                key = (mask | (1 << nxt), nxt)
                value = cost + matrix[inner[last]][inner[nxt]]
                if key not in best or value < best[key][0] - 1e-9:
                    best[key] = (value, last)
    final_last = min(
        range(count),
        key=lambda last: (best[(full, last)][0] + matrix[inner[last]][end], last),
    )
    order: list[int] = []
    mask, last = full, final_last
    while last != -1:
        order.append(inner[last])
        previous = best[(mask, last)][1]
        mask &= ~(1 << last)
        last = previous
    return [start, *reversed(order), end]


def _heuristic_path(
    matrix: list[list[float]],
    start: int,
    end: int,
    restarts: int,
    seed: int,
) -> list[int]:
    inner = [node for node in range(len(matrix)) if node not in (start, end)]
    generator = random.Random(seed)
    best_path: list[int] | None = None
    best_cost = math.inf
    for attempt in range(restarts):
        if attempt == 0:
            path = _nearest_neighbor(matrix, start, end, inner)
        else:
            order = inner[:]
            generator.shuffle(order)
            path = _cheapest_insertion(matrix, start, end, order)
        path = _local_search(matrix, path)
        cost = path_cost(path, matrix)
        if cost < best_cost - 1e-9:
            best_cost = cost
            best_path = path
    assert best_path is not None
    return best_path


def _nearest_neighbor(
    matrix: list[list[float]],
    start: int,
    end: int,
    inner: Sequence[int],
) -> list[int]:
    path = [start]
    remaining = set(inner)
    while remaining:
        current = path[-1]
        nxt = min(remaining, key=lambda node: (matrix[current][node], node))
        path.append(nxt)
        remaining.remove(nxt)
    path.append(end)
    return path


def _cheapest_insertion(
    matrix: list[list[float]],
    start: int,
    end: int,
    order: Sequence[int],
) -> list[int]:
    path = [start, end]
    for node in order:
        position = min(
            range(1, len(path)),
            key=lambda index: matrix[path[index - 1]][node]
            + matrix[node][path[index]]
            - matrix[path[index - 1]][path[index]],
        )
        path.insert(position, node)
    return path


def _local_search(matrix: list[list[float]], path: list[int]) -> list[int]:
    """2-opt plus Or-opt (segments of 1–3, optionally reversed) with fixed ends."""
    path = list(path)
    last = len(path) - 1
    improved = True
    while improved:
        improved = False
        for left in range(1, last - 1):
            for right in range(left + 1, last):
                a, b = path[left - 1], path[left]
                c, d = path[right], path[right + 1]
                delta = matrix[a][c] + matrix[b][d] - matrix[a][b] - matrix[c][d]
                if delta < -1e-6:
                    path[left : right + 1] = path[left : right + 1][::-1]
                    improved = True
        for length in (1, 2, 3):
            index = 1
            while index + length <= last:
                head, tail = path[index], path[index + length - 1]
                before, after = path[index - 1], path[index + length]
                gain = (
                    matrix[before][head]
                    + matrix[tail][after]
                    - matrix[before][after]
                )
                rest = path[:index] + path[index + length :]
                best_move: tuple[float, int, bool] | None = None
                for position in range(1, len(rest)):
                    x, y = rest[position - 1], rest[position]
                    for reverse in (False, True):
                        first, second = (tail, head) if reverse else (head, tail)
                        delta = matrix[x][first] + matrix[second][y] - matrix[x][y] - gain
                        if delta < -1e-6 and (best_move is None or delta < best_move[0]):
                            best_move = (delta, position, reverse)
                if best_move is not None:
                    _, position, reverse = best_move
                    segment = path[index : index + length]
                    if reverse:
                        segment.reverse()
                    path = rest[:position] + segment + rest[position:]
                    improved = True
                index += 1
    return path


def count_crossings(coordinates: Sequence[tuple[float, float]]) -> int:
    """Count self-intersections of the straight visit-order polyline."""
    segments = list(zip(coordinates, coordinates[1:]))
    total = 0
    for left in range(len(segments)):
        for right in range(left + 2, len(segments)):
            if segments_intersect(*segments[left], *segments[right]):
                total += 1
    return total


def segments_intersect(
    first_a: tuple[float, float],
    first_b: tuple[float, float],
    second_a: tuple[float, float],
    second_b: tuple[float, float],
) -> bool:
    def orientation(
        a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
    ) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    return (
        orientation(first_a, first_b, second_a) * orientation(first_a, first_b, second_b)
        < 0
        and orientation(second_a, second_b, first_a)
        * orientation(second_a, second_b, first_b)
        < 0
    )


def find_spurs(
    path: Sequence[int],
    matrix: Sequence[Sequence[float]],
    weights: Sequence[int],
    *,
    threshold_meters: float = 400,
    ratio: float = 0.6,
    max_points: int = 2,
) -> list[dict[str, float | int]]:
    """Flag small stops that force an out-and-back detour.

    A stop ``b`` between ``a`` and ``c`` is a spur when skipping it would save
    at least ``threshold_meters`` and most of the walk to and from it.
    """
    spurs: list[dict[str, float | int]] = []
    for position in range(1, len(path) - 1):
        a, b, c = path[position - 1], path[position], path[position + 1]
        via = matrix[a][b] + matrix[b][c]
        detour = via - matrix[a][c]
        if (
            weights[b] <= max_points
            and detour >= threshold_meters
            and via > 0
            and detour / via >= ratio
        ):
            spurs.append(
                {"position": position, "stop": b, "detourMeters": round(detour, 1)}
            )
    return spurs


def backtrack_ratio(legs: Sequence[Sequence[tuple[float, float]]]) -> float:
    """Share of a route's geometry that walks an already-used segment again."""
    from .clustering import haversine_meters

    counts: dict[tuple[tuple[float, float], tuple[float, float]], int] = {}
    for leg in legs:
        for left, right in zip(leg, leg[1:]):
            a = (round(left[0], 6), round(left[1], 6))
            b = (round(right[0], 6), round(right[1], 6))
            if a == b:
                continue
            key = (a, b) if a <= b else (b, a)
            counts[key] = counts.get(key, 0) + 1
    total = 0.0
    repeated = 0.0
    for (a, b), count in counts.items():
        length = haversine_meters(a[1], a[0], b[1], b[0])
        total += length * count
        repeated += length * (count - 1)
    return round(repeated / total, 3) if total else 0.0


def contiguous_cuts(
    segment_cost: Sequence[float],
    gap_cost: Sequence[float],
    parts: int,
    *,
    cut_penalty: Sequence[float] | None = None,
    gap_bonus: float = 1.0,
) -> list[int]:
    """Cut an ordered sequence into ``parts`` contiguous runs, exactly.

    ``segment_cost[i]`` is the time spent at item ``i`` and ``gap_cost[i]`` the
    travel time from item ``i`` to ``i + 1``. The objective balances run
    durations around their mean (squared relative deviation), rewards cutting
    at long gaps, and adds ``cut_penalty[i]`` for cutting after item ``i``.
    Dynamic programming makes this O(parts · n²).
    """
    count = len(segment_cost)
    parts = max(1, min(parts, count))
    if parts == 1:
        return []
    prefix_stop = [0.0]
    for value in segment_cost:
        prefix_stop.append(prefix_stop[-1] + value)
    prefix_gap = [0.0]
    for value in gap_cost:
        prefix_gap.append(prefix_gap[-1] + value)

    def run(start: int, stop: int) -> float:
        return prefix_stop[stop] - prefix_stop[start] + prefix_gap[stop - 1] - prefix_gap[start]

    target = max(run(0, count) / parts, 1.0)

    def cut_cost(position: int) -> float:
        value = -gap_cost[position - 1] * gap_bonus
        if cut_penalty is not None:
            value += cut_penalty[position - 1]
        return value

    infinity = math.inf
    best = [[infinity] * (count + 1) for _ in range(parts + 1)]
    back = [[0] * (count + 1) for _ in range(parts + 1)]
    best[0][0] = 0.0
    for part in range(1, parts + 1):
        for stop in range(part, count + 1):
            for start in range(part - 1, stop):
                previous = best[part - 1][start]
                if previous == infinity:
                    continue
                value = (
                    previous
                    + ((run(start, stop) - target) / target) ** 2 * 3600
                    + (cut_cost(start) if start > 0 else 0.0)
                )
                if value < best[part][stop] - 1e-9:
                    best[part][stop] = value
                    back[part][stop] = start
    cuts: list[int] = []
    stop = count
    for part in range(parts, 0, -1):
        start = back[part][stop]
        if start > 0:
            cuts.append(start)
        stop = start
    return sorted(cuts)
