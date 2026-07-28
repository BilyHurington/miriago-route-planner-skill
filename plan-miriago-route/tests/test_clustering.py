from __future__ import annotations

import random
import time
import unittest

from miriago_planner.clustering import (
    GeoPoint,
    bundle_points,
    cluster_metrics,
    cluster_points,
    distance_matrix,
    haversine_meters,
    order_open_path,
    profile_with_overrides,
)


class ClusteringTests(unittest.TestCase):
    def test_haversine_across_dateline_uses_short_arc(self) -> None:
        distance = haversine_meters(35, 179.9, 35, -179.9)
        self.assertLess(distance, 20_000)

    def test_metrics_bbox_wraps_the_dateline(self) -> None:
        points = [
            GeoPoint("east", 35, 179.9, ("east",)),
            GeoPoint("west", 35, -179.9, ("west",)),
        ]
        metrics = cluster_metrics([0, 1], points, distance_matrix(points))
        self.assertGreater(metrics["bbox"]["west"], metrics["bbox"]["east"])

    def test_bundle_near_duplicate_coordinates(self) -> None:
        points = [
            GeoPoint("a", 35.0, 139.0, ("a",)),
            GeoPoint("b", 35.00001, 139.00001, ("b",)),
            GeoPoint("c", 35.01, 139.01, ("c",)),
        ]
        bundled = bundle_points(points, 15)
        self.assertEqual(len(bundled), 2)
        self.assertEqual(set(bundled[0].point_ids), {"a", "b"})

    def test_two_distant_urban_areas_are_separated(self) -> None:
        points = [
            *[
                GeoPoint(
                    f"tokyo-{index}", 35.68 + index * 0.001, 139.76, (f"t{index}",)
                )
                for index in range(5)
            ],
            *[
                GeoPoint(
                    f"kyoto-{index}", 35.01 + index * 0.001, 135.76, (f"k{index}",)
                )
                for index in range(5)
            ],
        ]
        clusters, _ = cluster_points(points, profile_with_overrides("balanced"))
        self.assertEqual(len(clusters), 2)
        self.assertEqual(sorted(map(len, clusters)), [5, 5])

    def test_constraints_split_a_long_chain(self) -> None:
        points = [
            GeoPoint(str(index), 35.0, 139.0 + index * 0.008, (str(index),))
            for index in range(12)
        ]
        profile = profile_with_overrides(
            "balanced", max_points=4, max_radius_meters=900
        )
        clusters, _ = cluster_points(points, profile)
        self.assertGreaterEqual(len(clusters), 3)
        self.assertTrue(all(len(cluster) <= 4 for cluster in clusters))

    def test_order_is_deterministic_and_complete(self) -> None:
        points = [
            GeoPoint(
                str(index), 35 + (index % 3) * 0.001, 139 + index * 0.001, (str(index),)
            )
            for index in range(8)
        ]
        matrix = distance_matrix(points)
        first = order_open_path(list(range(8)), matrix)
        second = order_open_path(list(range(8)), matrix)
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(range(8)))

    def test_two_stop_order_uses_shorter_directed_cost(self) -> None:
        self.assertEqual(order_open_path([0, 1], [[0, 20], [10, 0]]), [1, 0])

    def test_200_points_finish_quickly_and_preserve_all(self) -> None:
        randomizer = random.Random(1234)
        points = [
            GeoPoint(
                f"p-{index:03d}",
                35.0 + randomizer.random() * 0.12,
                139.0 + randomizer.random() * 0.12,
                (f"p-{index:03d}",),
            )
            for index in range(200)
        ]
        start = time.monotonic()
        clusters, _ = cluster_points(points, profile_with_overrides("balanced"))
        elapsed = time.monotonic() - start
        flattened = [index for cluster in clusters for index in cluster]
        self.assertEqual(sorted(flattened), list(range(200)))
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
