from __future__ import annotations

import json
import tempfile
import unittest

from miriago_planner.public_services import (
    JsonCache,
    fetch_walking_matrix,
    fetch_walking_route,
)
from miriago_planner.walking import (
    EndSpec,
    OsmBackend,
    Site,
    StraightBackend,
    cost_matrix,
    route_polyline,
    sequence_stops,
)
from miriago_planner.walknet import (
    LoadedWalkNetwork,
    WalkNetwork,
    WaterIndex,
    covering_boxes,
)

LAT = 35.0
LON = 139.0
STEP = 0.001  # about 111 m north-south, 91 m east-west


def grid_payload(size: int = 6) -> dict:
    """A size × size street grid as Overpass JSON."""
    elements = []
    node_id = lambda row, column: row * 100 + column + 1
    for row in range(size):
        for column in range(size):
            elements.append(
                {"type": "node", "id": node_id(row, column), "lat": LAT + row * STEP, "lon": LON + column * STEP}
            )
    way = 1
    for row in range(size):
        elements.append({"type": "way", "id": way, "nodes": [node_id(row, c) for c in range(size)], "tags": {"highway": "residential"}})
        way += 1
    for column in range(size):
        elements.append({"type": "way", "id": way, "nodes": [node_id(r, column) for r in range(size)], "tags": {"highway": "footway"}})
        way += 1
    return {"elements": elements}


class _Response:
    def __init__(self, value: object) -> None:
        self.data = json.dumps(value).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data


def loaded(water: WaterIndex | None = None) -> LoadedWalkNetwork:
    network = WalkNetwork()
    network.add_overpass(grid_payload())
    network.finalize()
    return LoadedWalkNetwork(network=network, water=water or WaterIndex(), source={})


class WalkNetworkTests(unittest.TestCase):
    def test_grid_distances_follow_streets(self) -> None:
        backend = OsmBackend(loaded())
        a = Site(LAT, LON)
        b = Site(LAT + 2 * STEP, LON + 2 * STEP)
        meters = backend.raw_matrix([a, b]).meters
        # Manhattan distance on the grid, not the diagonal.
        self.assertGreater(meters[0][1], 380)
        self.assertLess(meters[0][1], 420)
        self.assertEqual(meters[0][1], meters[1][0])

    def test_short_legs_are_capped_unless_they_cross_water(self) -> None:
        a = Site(LAT + 0.0005, LON + 0.0002)
        b = Site(LAT + 0.0005, LON + 0.0008)
        plain = cost_matrix(OsmBackend(loaded()), [a, b])
        self.assertIn((0, 1), plain.shortcut_pairs)
        water = WaterIndex()
        water.add_overpass(
            {"elements": [{"type": "way", "geometry": [{"lat": LAT, "lon": LON + 0.0005}, {"lat": LAT + 0.002, "lon": LON + 0.0005}]}]}
        )
        river = cost_matrix(OsmBackend(loaded(water)), [a, b])
        self.assertNotIn((0, 1), river.shortcut_pairs)
        self.assertGreater(river.meters[0][1], plain.meters[0][1])

    def test_via_point_is_forced_for_legs_to_a_stop(self) -> None:
        stops = [Site(LAT, LON), Site(LAT + 5 * STEP, LON)]
        trailhead = Site(LAT, LON + 5 * STEP)
        backend = OsmBackend(loaded())
        direct = sequence_stops(backend, stops)
        forced = sequence_stops(backend, stops, via={1: trailhead})
        self.assertGreater(forced.total_meters, direct.total_meters + 500)
        coordinates = route_polyline(backend, forced)
        self.assertIn([LON + 5 * STEP, LAT], coordinates)

    def test_auto_station_endpoints_pick_the_nearest(self) -> None:
        stops = [Site(LAT + STEP, LON + STEP), Site(LAT + 4 * STEP, LON + 4 * STEP)]
        stations = (("North", Site(LAT + 5 * STEP, LON + 5 * STEP)), ("South", Site(LAT, LON)))
        result = sequence_stops(
            StraightBackend(),
            stops,
            start=EndSpec(kind="auto", candidates=stations),
            end=EndSpec(kind="auto", candidates=stations),
        )
        self.assertEqual({result.start_label, result.end_label}, {"North", "South"})
        self.assertGreater(result.start_meters, 0)

    def test_covering_boxes_split_distant_clusters(self) -> None:
        boxes = covering_boxes([(35.0, 139.0), (35.001, 139.001), (34.0, 135.0)])
        self.assertEqual(len(boxes), 2)


class ValhallaLimitTests(unittest.TestCase):
    def test_matrix_is_tiled_to_the_public_limit(self) -> None:
        calls: list[dict] = []

        def requester(request, timeout):
            body = json.loads(request.data)
            calls.append(body)
            assert len(body["sources"]) * len(body["targets"]) <= 100
            return _Response(
                {
                    "sources_to_targets": [
                        [
                            {"from_index": i, "to_index": j, "distance": 0.1 * (i + j + 1), "time": 60}
                            for j in range(len(body["targets"]))
                        ]
                        for i in range(len(body["sources"]))
                    ]
                }
            )

        locations = [{"latitude": 35 + i * 0.001, "longitude": 139} for i in range(23)]
        with tempfile.TemporaryDirectory() as directory:
            result = fetch_walking_matrix(
                locations,
                cache=JsonCache(directory),
                requester=requester,
                request_delay_seconds=0,
            )
        self.assertEqual(len(calls), 9)
        self.assertEqual(len(result.distances_meters), 23)
        self.assertEqual(result.distances_meters[0][0], 0.0)

    def test_route_is_chunked_to_ten_locations(self) -> None:
        sizes: list[int] = []

        def requester(request, timeout):
            body = json.loads(request.data)
            sizes.append(len(body["locations"]))
            return _Response({"trip": {"legs": [{"shape": "_p~iF~ps|U_ulLnnqC"}]}})

        locations = [{"latitude": 35 + i * 0.001, "longitude": 139} for i in range(25)]
        with tempfile.TemporaryDirectory() as directory:
            fetch_walking_route(
                locations,
                cache=JsonCache(directory),
                requester=requester,
                request_delay_seconds=0,
            )
        self.assertTrue(all(size <= 10 for size in sizes))
        self.assertEqual(sum(size - 1 for size in sizes), 24)


if __name__ == "__main__":
    unittest.main()
