from __future__ import annotations

import itertools
import math
import random
import unittest

from miriago_planner.clustering import GeoPoint, bundle_points
from miriago_planner.routing import (
    UNREACHABLE,
    contiguous_cuts,
    count_crossings,
    find_spurs,
    path_cost,
    solve_open_path,
)


def _matrix(points: list[tuple[float, float]]) -> list[list[float]]:
    return [[math.dist(a, b) for b in points] for a in points]


def _brute_force(matrix: list[list[float]], start: list[float] | None, end: list[float] | None) -> float:
    size = len(matrix)
    best = math.inf
    for order in itertools.permutations(range(size)):
        cost = path_cost(order, matrix)
        cost += start[order[0]] if start else 0
        cost += end[order[-1]] if end else 0
        best = min(best, cost)
    return best


class OpenPathTests(unittest.TestCase):
    def test_small_paths_are_exact(self) -> None:
        generator = random.Random(3)
        for _ in range(5):
            points = [(generator.random() * 1000, generator.random() * 1000) for _ in range(7)]
            matrix = _matrix(points)
            start = [generator.random() * 500 for _ in points]
            order = solve_open_path(matrix, start_costs=start)
            cost = path_cost(order, matrix) + start[order[0]]
            self.assertAlmostEqual(cost, _brute_force(matrix, start, None), places=6)
            self.assertEqual(sorted(order), list(range(7)))

    def test_large_path_on_a_line_has_no_backtracking(self) -> None:
        points = [(index * 50.0, 0.0) for index in range(30)]
        shuffled = points[:]
        random.Random(5).shuffle(shuffled)
        matrix = _matrix(shuffled)
        order = solve_open_path(matrix, restarts=8)
        self.assertAlmostEqual(path_cost(order, matrix), 29 * 50.0, places=6)

    def test_forced_end_stop_is_last(self) -> None:
        points = [(0, 0), (100, 0), (200, 0), (300, 0), (150, 400)]
        matrix = _matrix(points)
        end = [UNREACHABLE] * 5
        end[1] = 0
        order = solve_open_path(matrix, end_costs=end)
        self.assertEqual(order[-1], 1)

    def test_crossings_and_spurs(self) -> None:
        self.assertEqual(count_crossings([(0, 0), (1, 1), (1, 0), (0, 1)]), 1)
        self.assertEqual(count_crossings([(0, 0), (1, 0), (2, 0)]), 0)
        matrix = _matrix([(0, 0), (0, 1000), (10, 0)])
        spurs = find_spurs([0, 1, 2], matrix, [1, 1, 1], threshold_meters=400)
        self.assertEqual(len(spurs), 1)
        self.assertEqual(spurs[0]["stop"], 1)

    def test_contiguous_cuts_balance_runs_and_prefer_gaps(self) -> None:
        stops = [600.0] * 6
        gaps = [60.0, 60.0, 900.0, 60.0, 60.0]
        self.assertEqual(contiguous_cuts(stops, gaps, 2), [3])
        self.assertEqual(contiguous_cuts(stops, gaps, 1), [])
        self.assertEqual(len(contiguous_cuts(stops, gaps, 3)), 2)

    def test_bundling_does_not_chain_along_a_street(self) -> None:
        # Ten points 20 m apart: single linkage would make one 180 m stop.
        points = [
            GeoPoint(f"p{index}", 35.0, 139.0 + index * 20 / 91_000, (f"p{index}",))
            for index in range(10)
        ]
        stops = bundle_points(points, 30)
        self.assertGreater(len(stops), 3)
        self.assertEqual(sum(len(stop.point_ids) for stop in stops), 10)


if __name__ == "__main__":
    unittest.main()
