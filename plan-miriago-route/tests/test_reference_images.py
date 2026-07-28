from __future__ import annotations

import base64
import tempfile
import threading
import time
import unittest
from pathlib import Path

from miriago_planner.package_io import create_package, load_package
from miriago_planner.reference_images import (
    ReferenceImageError,
    bundle_reference_images,
)

from .helpers import package_dicts, point


JPEG_BYTES = b"\xff\xd8\xff\xd9"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


def _root(source: str, url: str) -> dict[str, object]:
    value = point("point-1", 35.1, 139.1)
    value["source"] = source
    value["referenceImageUrl"] = url
    value["referenceThumbnailAsset"] = None
    _, root = package_dicts([value])
    return root


def _root_for_urls(source: str, urls: list[str]) -> dict[str, object]:
    values = []
    for index, url in enumerate(urls, start=1):
        value = point(f"point-{index}", 35.1 + index / 1000, 139.1)
        value["source"] = source
        value["referenceImageUrl"] = url
        value["referenceThumbnailAsset"] = None
        values.append(value)
    _, root = package_dicts(values)
    return root


class ReferenceImageTests(unittest.TestCase):
    def test_anitabi_thumbnail_is_mandatory_when_full_images_are_omitted(self) -> None:
        requested: list[str] = []

        def fetcher(url: str, **_: object) -> bytes:
            requested.append(url)
            return JPEG_BYTES

        bundle = bundle_reference_images(
            _root(
                "anitabi",
                "https://image.anitabi.cn/points/p1.jpg?plan=w300&token=x",
            ),
            include_full_reference_images=False,
            fetcher=fetcher,
        )
        bundled_point = bundle.plan_root["plan"]["points"][0]
        self.assertEqual(
            requested,
            ["https://image.anitabi.cn/points/p1.jpg?token=x&plan=h160"],
        )
        self.assertIn("assets/thumbnails/point-1.jpg", bundle.entries)
        self.assertEqual(
            bundled_point["referenceThumbnailAsset"],
            "assets/thumbnails/point-1.jpg",
        )
        self.assertIsNone(bundled_point["referenceFullReferenceAsset"])

    def test_anitabi_full_image_is_bundled_only_when_included(self) -> None:
        requested: list[str] = []

        def fetcher(url: str, **_: object) -> bytes:
            requested.append(url)
            return JPEG_BYTES

        bundle = bundle_reference_images(
            _root("anitabi", "https://image.anitabi.cn/points/p1.jpg"),
            include_full_reference_images=True,
            fetcher=fetcher,
            max_workers=1,
        )
        self.assertEqual(
            requested,
            [
                "https://image.anitabi.cn/points/p1.jpg?plan=h160",
                "https://image.anitabi.cn/points/p1.jpg",
            ],
        )
        self.assertIn("assets/thumbnails/point-1.jpg", bundle.entries)
        self.assertIn("assets/full_references/point-1.jpg", bundle.entries)

    def test_external_point_always_bundles_full_image_and_thumbnail(self) -> None:
        requested: list[str] = []

        def fetcher(url: str, **_: object) -> bytes:
            requested.append(url)
            return PNG_BYTES

        bundle = bundle_reference_images(
            _root("manual", "https://images.example.org/place.png"),
            include_full_reference_images=False,
            fetcher=fetcher,
        )
        bundled_point = bundle.plan_root["plan"]["points"][0]
        self.assertEqual(requested, ["https://images.example.org/place.png"])
        self.assertEqual(
            bundled_point["userReferenceAsset"],
            "assets/user_references/point-1.png",
        )
        self.assertEqual(
            bundled_point["referenceThumbnailAsset"],
            "assets/thumbnails/point-1.jpg",
        )
        self.assertIn("assets/user_references/point-1.png", bundle.entries)
        self.assertTrue(
            bundle.entries["assets/thumbnails/point-1.jpg"].startswith(b"\xff\xd8\xff")
        )

        with tempfile.TemporaryDirectory() as directory_value:
            output = Path(directory_value) / "images.sjhplan"
            create_package(
                bundle.plan_root,
                output,
                planner_version="test",
                additional_entries=bundle.entries,
            )
            package = load_package(output)
        self.assertEqual(package.manifest["assetCounts"]["thumbnails"], 1)
        self.assertEqual(package.manifest["assetCounts"]["userReferenceImages"], 1)

    def test_required_image_download_failure_blocks_export(self) -> None:
        events: list[dict[str, object]] = []
        with self.assertRaisesRegex(ReferenceImageError, "required thumbnail"):
            bundle_reference_images(
                _root("anitabi", "https://image.anitabi.cn/points/p1.jpg"),
                include_full_reference_images=False,
                fetcher=lambda _url, **_kwargs: b"not an image",
                progress=events.append,
            )
        self.assertEqual(
            [event["event"] for event in events],
            [
                "image_download_start",
                "image_download_progress",
                "image_download_complete",
            ],
        )
        self.assertEqual(events[1]["status"], "failed")
        self.assertEqual(events[-1]["failed"], 1)

    def test_downloads_run_concurrently_with_bounded_workers(self) -> None:
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        active = 0
        peak_active = 0

        def fetcher(_url: str, **_: object) -> bytes:
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                barrier.wait(timeout=2)
                time.sleep(0.01)
                return JPEG_BYTES
            finally:
                with lock:
                    active -= 1

        bundle_reference_images(
            _root_for_urls(
                "anitabi",
                [
                    "https://image.anitabi.cn/points/p1.jpg",
                    "https://image.anitabi.cn/points/p2.jpg",
                ],
            ),
            include_full_reference_images=False,
            fetcher=fetcher,
            max_workers=2,
        )
        self.assertEqual(peak_active, 2)

    def test_one_worker_remains_sequential(self) -> None:
        lock = threading.Lock()
        active = 0
        peak_active = 0

        def fetcher(_url: str, **_: object) -> bytes:
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return JPEG_BYTES

        bundle_reference_images(
            _root_for_urls(
                "anitabi",
                [
                    "https://image.anitabi.cn/points/p1.jpg",
                    "https://image.anitabi.cn/points/p2.jpg",
                ],
            ),
            include_full_reference_images=False,
            fetcher=fetcher,
            max_workers=1,
        )
        self.assertEqual(peak_active, 1)

    def test_duplicate_downloads_are_coalesced_and_progress_is_monotonic(self) -> None:
        requested: list[str] = []
        events: list[dict[str, object]] = []

        def fetcher(url: str, **_: object) -> bytes:
            requested.append(url)
            return JPEG_BYTES

        bundle = bundle_reference_images(
            _root_for_urls(
                "anitabi",
                [
                    "https://image.anitabi.cn/points/shared.jpg",
                    "https://image.anitabi.cn/points/shared.jpg",
                ],
            ),
            include_full_reference_images=False,
            fetcher=fetcher,
            progress=events.append,
        )
        self.assertEqual(len(requested), 1)
        self.assertEqual(events[0]["event"], "image_download_start")
        self.assertEqual(events[0]["total"], 1)
        progress_events = [
            event
            for event in events
            if event["event"] == "image_download_progress"
        ]
        self.assertEqual(
            [event["completed"] for event in progress_events],
            [1],
        )
        self.assertEqual(events[-1]["event"], "image_download_complete")
        self.assertEqual(events[-1]["downloadedBytes"], len(JPEG_BYTES))
        self.assertIn("assets/thumbnails/point-1.jpg", bundle.entries)
        self.assertIn("assets/thumbnails/point-2.jpg", bundle.entries)

    def test_invalid_worker_count_is_rejected(self) -> None:
        for worker_count in (0, 33, True):
            with self.subTest(worker_count=worker_count):
                with self.assertRaisesRegex(
                    ReferenceImageError,
                    "between 1 and 32",
                ):
                    bundle_reference_images(
                        _root(
                            "anitabi",
                            "https://image.anitabi.cn/points/p1.jpg",
                        ),
                        include_full_reference_images=False,
                        fetcher=lambda _url, **_kwargs: JPEG_BYTES,
                        max_workers=worker_count,
                    )


if __name__ == "__main__":
    unittest.main()
