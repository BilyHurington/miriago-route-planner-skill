from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request

from miriago_planner.bangumi_anitabi import (
    SourceImportError,
    collect_anitabi_source,
    search_bangumi,
)
from miriago_planner.google_my_maps import (
    collect_google_my_maps_source,
    extract_google_my_maps_id,
    parse_kml,
)


class _Response(io.BytesIO):
    def __init__(self, value: object, *, status: int = 200) -> None:
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class SourceCollectorTests(unittest.TestCase):
    def test_search_bangumi_returns_review_candidates(self) -> None:
        captured: list[Request] = []

        def requester(request: Request, *, timeout: float) -> _Response:
            captured.append(request)
            return _Response(
                {
                    "data": [
                        {
                            "id": 328609,
                            "type": 2,
                            "name": "Bocchi the Rock!",
                            "name_cn": "孤独摇滚！",
                            "date": "2022-10-08",
                            "images": {"common": "http://lain.bgm.tv/cover.jpg"},
                        }
                    ]
                }
            )

        candidates = search_bangumi(
            "孤独摇滚",
            subject_types=("anime",),
            requester=requester,
        )
        self.assertEqual(candidates[0]["bangumiId"], 328609)
        self.assertEqual(candidates[0]["subjectType"], "anime")
        self.assertEqual(candidates[0]["title"], "孤独摇滚！")
        self.assertTrue(candidates[0]["coverImageUrl"].startswith("https://"))
        self.assertEqual(captured[0].get_method(), "POST")
        body = json.loads(captured[0].data or b"{}")
        self.assertEqual(body["filter"]["type"], [2])

    def test_collect_anitabi_matches_static_index_and_detail_page(self) -> None:
        index = [
            [
                [
                    999001,
                    "测试作品",
                    0,
                    "Fixture Work",
                    "测试市",
                    "#fff",
                    "/images/bangumi/999001.jpg",
                    7.5,
                    "TV",
                    34.2,
                    134.2,
                    12,
                    ["p1", 34.1, 134.1, 1],
                ]
            ],
            250,
            "fixture-version",
        ]
        page = [
            [
                999001,
                0,
                [
                    [
                        "p1",
                        "Place",
                        "地点一",
                        0,
                        0,
                        0,
                        "/images/points/999001/p1.jpg?plan=h160",
                        0,
                        1,
                        125,
                        "备注",
                        "Anitabi",
                        "https://anitabi.cn/map?bangumiId=999001&pid=p1",
                    ]
                ],
                1,
            ]
        ]
        subject = {
            "id": 999001,
            "type": 2,
            "name": "Fixture Work",
            "name_cn": "测试作品",
            "date": "2026-01-01",
            "images": {"common": "https://lain.bgm.tv/fixture.jpg"},
        }

        def requester(request: str | Request, *, timeout: float) -> _Response:
            url = request.full_url if isinstance(request, Request) else request
            if "/d/g.json" in url:
                return _Response(index)
            if "/d/g0.json" in url:
                return _Response(page)
            if "/v0/subjects/999001" in url:
                return _Response(subject)
            raise AssertionError(url)

        source = collect_anitabi_source(999001, requester=requester)
        work = source["works"][0]["data"]
        point = source["points"][0]["data"]
        self.assertEqual(work["bangumiId"], 999001)
        self.assertEqual(work["bangumiSubjectType"], "anime")
        self.assertEqual(point["id"], "anitabi-999001-p1")
        self.assertEqual(point["episodeLabel"], "EP 1 / 2:05")
        self.assertEqual(
            point["referenceImageUrl"],
            "https://image.anitabi.cn/points/999001/p1.jpg",
        )

    def test_collect_anitabi_rejects_partial_static_data(self) -> None:
        index = [
            [[1, "Work", 0, "Work", "City", 0, 0, 0, 0, 35, 135, 12, ["p1", 35, 135, 1]]],
            250,
            "v1",
        ]

        def requester(request: str | Request, *, timeout: float) -> _Response:
            url = request.full_url if isinstance(request, Request) else request
            if "/d/g.json" in url:
                return _Response(index)
            if "/d/g0.json" in url:
                return _Response([[1, 0, [], 1]])
            return _Response({"id": 1, "type": 2, "name": "Work"})

        with self.assertRaisesRegex(SourceImportError, "0 of 1 expected"):
            collect_anitabi_source(1, requester=requester)

    def test_extract_google_my_maps_id_rejects_non_mymaps_url(self) -> None:
        self.assertEqual(
            extract_google_my_maps_id(
                "https://www.google.com/maps/d/u/0/viewer?mid=abc_DEF-123"
            ),
            "abc_DEF-123",
        )
        with self.assertRaises(SourceImportError):
            extract_google_my_maps_id("https://example.com/maps/d/viewer?mid=abc123")

    def test_parse_nested_kml_splits_images_and_non_point_geometry(self) -> None:
        kml = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Fixture Map</name>
<Folder><name>Fixture Work</name><Placemark id="place-1"><name>Scene</name>
<description><![CDATA[<p>note</p><img src="https://example.test/a.jpg"><img src="https://example.test/b.jpg">]]></description>
<LineString><coordinates>139.0,35.0 139.2,35.2</coordinates></LineString>
</Placemark></Folder></Document></kml>"""
        title, features, warnings = parse_kml(kml)
        self.assertEqual(title, "Fixture Map")
        self.assertEqual(features[0]["layer"], "Fixture Work")
        self.assertEqual(features[0]["geometry"], "LineString")
        self.assertEqual(len(features[0]["imageUrls"]), 2)
        self.assertFalse(warnings)

        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "map.kml"
            path.write_bytes(kml)
            source = collect_google_my_maps_source(str(path))
        self.assertEqual(source["metadata"]["expandedPoints"], 2)
        self.assertEqual(len(source["works"]), 1)
        self.assertTrue(any("KML" in value for value in source["warnings"]))

    def test_kml_rejects_entity_declarations(self) -> None:
        with self.assertRaisesRegex(SourceImportError, "entity declarations"):
            parse_kml(b'<!DOCTYPE x [<!ENTITY y "z">]><kml/>')


if __name__ == "__main__":
    unittest.main()
