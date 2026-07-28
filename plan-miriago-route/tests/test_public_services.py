from __future__ import annotations

import json
import tempfile
import unittest

from miriago_planner.public_services import (
    JsonCache,
    WalkingMatrix,
    build_station_query,
    fetch_stations,
    optimize_walking_order,
    parse_overpass_stations,
    parse_valhalla_matrix,
    station_boxes_from_proposal,
)


class _Response:
    def __init__(self, value: object) -> None:
        self.data = json.dumps(value).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data


class PublicServiceTests(unittest.TestCase):
    def test_station_query_is_bulk_and_rail_scoped(self) -> None:
        query = build_station_query([(35, 139, 35.1, 139.1), (36, 140, 36.1, 140.1)])
        self.assertEqual(query.count('["railway"~'), 2)
        self.assertIn('["public_transport"="station"]', query)
        self.assertIn('["railway"="subway_entrance"]', query)

    def test_station_boxes_split_at_dateline(self) -> None:
        proposal = {
            "groups": [
                {
                    "medoid": {"latitude": 35, "longitude": 179.95},
                    "metrics": {
                        "bbox": {
                            "south": 35,
                            "west": 179.9,
                            "north": 35.1,
                            "east": -179.9,
                        }
                    },
                }
            ]
        }
        boxes = station_boxes_from_proposal(proposal, padding_meters=1_000)
        self.assertEqual(len(boxes), 2)
        self.assertTrue(all(west <= east for _, west, _, east in boxes))

    def test_parse_station_centers_and_deduplicates(self) -> None:
        response = {
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 35.0,
                    "lon": 139.0,
                    "tags": {"name": "東京駅", "railway": "station"},
                },
                {
                    "type": "way",
                    "id": 2,
                    "center": {"lat": 35.0001, "lon": 139.0001},
                    "tags": {"name": "東京", "railway": "station"},
                },
                {
                    "type": "node",
                    "id": 3,
                    "lat": 35.1,
                    "lon": 139.1,
                    "tags": {"name": "別駅", "railway": "halt"},
                },
                {
                    "type": "node",
                    "id": 4,
                    "lat": 35.2,
                    "lon": 139.2,
                    "tags": {"name": "3-1", "railway": "subway_entrance"},
                },
            ]
        }
        stations = parse_overpass_stations(response)
        self.assertEqual(len(stations), 2)
        self.assertTrue(any(item["name"] in {"東京", "東京駅"} for item in stations))
        self.assertNotIn("3-1", [item["name"] for item in stations])

    def test_fetch_stations_uses_cache_after_first_request(self) -> None:
        calls: list[str] = []

        def requester(request: object, timeout: float) -> _Response:
            calls.append(str(request))
            return _Response(
                {
                    "elements": [
                        {
                            "type": "node",
                            "id": 1,
                            "lat": 35,
                            "lon": 139,
                            "tags": {"name": "Station", "railway": "station"},
                        }
                    ]
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            cache = JsonCache(directory)
            first = fetch_stations(
                [(35, 139, 35.1, 139.1)],
                cache=cache,
                requester=requester,
            )
            second = fetch_stations(
                [(35, 139, 35.1, 139.1)],
                cache=cache,
                requester=requester,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.stations, second.stations)
        self.assertEqual(second.source["cacheHits"], 1)

    def test_parse_concise_valhalla_matrix_converts_km_to_m(self) -> None:
        distances, durations = parse_valhalla_matrix(
            {
                "sources_to_targets": {
                    "distances": [[0, 1.2], [1.1, 0]],
                    "durations": [[0, 900], [850, 0]],
                }
            },
            2,
        )
        self.assertEqual(distances[0][1], 1200)
        self.assertEqual(durations[1][0], 850)

    def test_walking_order_returns_all_points(self) -> None:
        matrix = WalkingMatrix(
            distances_meters=[
                [0, 100, 300],
                [100, 0, 100],
                [300, 100, 0],
            ],
            durations_seconds=[
                [0, 80, 240],
                [80, 0, 80],
                [240, 80, 0],
            ],
            endpoint="test",
            cache_hit=False,
        )
        order, distance, duration = optimize_walking_order(["a", "b", "c"], matrix)
        self.assertEqual(set(order), {"a", "b", "c"})
        self.assertEqual(distance, 200)
        self.assertEqual(duration, 160)


if __name__ == "__main__":
    unittest.main()
