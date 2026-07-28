from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any


def point(
    point_id: str,
    latitude: float,
    longitude: float,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": point_id,
        "workId": "work-1",
        "name": name or point_id,
        "subtitle": "",
        "latitude": latitude,
        "longitude": longitude,
        "episodeLabel": "EP 1 / 125s",
        "referenceLabel": "",
        "source": "manual",
        "sourceId": None,
        "referenceImageUrl": "https://example.invalid/reference.jpg",
        "referenceThumbnailPath": None,
        "referenceFullImagePath": None,
        "referenceThumbnailAsset": "assets/thumbnails/shared.jpg",
        "referenceFullReferenceAsset": None,
        "userReferenceAsset": None,
        "sourceUrl": None,
        "note": "preserve point note",
        "groupId": "old-group",
        "groupOrderIndex": 99,
        "futurePointField": {"preserve": True},
    }


def package_dicts(
    points: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = {
        "format": "miriago_export_package",
        "container": "zip",
        "schemaVersion": 2,
        "appName": "MiriaGo",
        "appVersion": "1.1.0+4",
        "exportedAt": "2026-06-07T00:00:00.000",
        "exportMode": "plan_with_records",
        "packageId": "test-package",
        "planId": "plan-1",
        "planName": "Test Plan",
        "includedContent": {
            "plan": True,
            "works": True,
            "groups": True,
            "points": True,
            "visitRecords": True,
        },
        "counts": {
            "works": 1,
            "groups": 1,
            "points": len(points),
            "visitRecords": 1,
        },
        "assetCounts": {"thumbnails": 1, "futureAssets": 7},
        "warnings": [],
        "futureManifestField": {"preserve": True},
    }
    plan_root = {
        "schemaVersion": 2,
        "exportMode": "plan_with_records",
        "futureRootField": ["preserve"],
        "plan": {
            "id": "plan-1",
            "name": "Test Plan",
            "area": "Tokyo",
            "memo": "keep this memo",
            "createdAt": "2026-06-01T00:00:00.000",
            "updatedAt": "2026-06-07T00:00:00.000",
            "currentPointId": points[0]["id"] if points else None,
            "currentGroupId": "old-group",
            "completedPointIds": [points[0]["id"]] if points else [],
            "works": [
                {
                    "id": "work-1",
                    "bangumiId": 1,
                    "bangumiSubjectType": "anime",
                    "coverImageUrl": None,
                    "title": "Work",
                    "subtitle": "",
                    "city": "Tokyo",
                    "source": "manual",
                    "futureWorkField": "preserve",
                }
            ],
            "groups": [
                {
                    "id": "old-group",
                    "name": "Old Group",
                    "orderIndex": 0,
                    "orderMode": "manual",
                    "anchorName": None,
                    "anchorLatitude": None,
                    "anchorLongitude": None,
                    "anchorPointId": None,
                    "note": None,
                    "createdAt": "2026-06-01T00:00:00.000",
                }
            ],
            "points": points,
            "futurePlanField": {"preserve": True},
        },
        "visitRecords": [
            {
                "id": "record-1",
                "planId": "plan-1",
                "pointId": points[0]["id"] if points else "",
                "workId": "work-1",
                "workTitle": "Work",
                "workSubtitle": "",
                "pointName": points[0]["name"] if points else "",
                "pointSubtitle": "",
                "photoPath": "/old/visit/photo.jpg",
                "originalPhotoPath": None,
                "gradedPhotoPath": None,
                "colorGradingMode": None,
                "colorGradingParamsJson": None,
                "colorGradingIntensity": None,
                "referenceImagePath": None,
                "referenceImageUrl": None,
                "visitPhotoAsset": "assets/visit_photos/record-1.jpg",
                "gradedPhotoAsset": None,
                "referenceMode": "remote",
                "capturedAt": "2026-06-07T01:02:03.000",
                "futureRecordField": "preserve",
            }
        ],
    }
    return manifest, plan_root


def write_package(
    path: Path,
    points: list[dict[str, Any]],
    *,
    extra_entries: dict[str, bytes] | None = None,
) -> Path:
    manifest, plan_root = package_dicts(points)
    entries = {
        "manifest.json": (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode(),
        "plan.json": (
            json.dumps(plan_root, ensure_ascii=False, indent=2) + "\n"
        ).encode(),
        "points.csv": b"old points csv",
        "records.csv": b"old records csv",
        "assets/thumbnails/shared.jpg": b"\xff\xd8\xff\xd9",
        "assets/visit_photos/record-1.jpg": b"\xff\xd8\xff\xd9",
        "custom/unknown.bin": b"\x00\x01preserve",
        **(extra_entries or {}),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path
