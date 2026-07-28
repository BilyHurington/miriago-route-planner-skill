from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from miriago_planner.package_io import create_package, load_package, rebuild_package
from miriago_planner.source_import import (
    ImportProposalError,
    apply_import_proposal,
    build_import_proposal,
    validate_import_proposal,
)

from .helpers import point, write_package


def _source(point_id: str = "anitabi-123-p1") -> dict[str, object]:
    return {
        "provider": "anitabi",
        "sourceId": "anitabi:123",
        "label": "Work",
        "metadata": {"bangumiId": 123},
        "works": [
            {
                "sourceWorkRef": "anitabi:123",
                "data": {
                    "id": "bangumi-123",
                    "bangumiId": 123,
                    "bangumiSubjectType": "anime",
                    "coverImageUrl": None,
                    "title": "Work",
                    "subtitle": "Original",
                    "city": "Tokyo",
                    "source": "bangumi",
                },
            }
        ],
        "points": [
            {
                "sourceWorkRef": "anitabi:123",
                "selected": True,
                "data": {
                    "id": point_id,
                    "name": "Scene",
                    "subtitle": "Place",
                    "latitude": 35.1,
                    "longitude": 139.1,
                    "episodeLabel": "EP 1 / 2:05",
                    "referenceLabel": "Anitabi",
                    "source": "anitabi",
                    "sourceId": "p1",
                    "referenceImageUrl": "https://image.anitabi.cn/p1.jpg",
                    "referenceThumbnailPath": None,
                    "referenceFullImagePath": None,
                    "referenceThumbnailAsset": None,
                    "referenceFullReferenceAsset": None,
                    "userReferenceAsset": None,
                    "sourceUrl": None,
                    "note": None,
                    "groupId": None,
                    "groupOrderIndex": None,
                },
            }
        ],
        "warnings": [],
    }


class SourceImportTests(unittest.TestCase):
    def test_import_requires_explicit_review_confirmation(self) -> None:
        proposal = build_import_proposal(
            [_source()],
            plan_name="New Plan",
            area="Tokyo",
        )
        validate_import_proposal(None, proposal, final=False)
        with self.assertRaisesRegex(ImportProposalError, "review gate is closed"):
            validate_import_proposal(None, proposal, final=True)
        proposal["review"]["confirmed"] = True
        validate_import_proposal(None, proposal, final=True)

    def test_new_plan_package_is_importable(self) -> None:
        proposal = build_import_proposal(
            [_source()],
            plan_name="New Plan",
            area="Tokyo",
        )
        proposal["review"]["confirmed"] = True
        plan_root = apply_import_proposal(None, proposal)
        with tempfile.TemporaryDirectory() as directory_value:
            output = Path(directory_value) / "new.sjhplan"
            create_package(plan_root, output, planner_version="test")
            package = load_package(output)
        self.assertEqual(package.plan["name"], "New Plan")
        self.assertEqual(package.plan["works"][0]["bangumiId"], 123)
        self.assertEqual(package.plan["points"][0]["workId"], "bangumi-123")
        self.assertIn("importedBy", package.manifest)

    def test_merge_reuses_base_bangumi_work_and_preserves_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            base_path = write_package(
                directory / "base.sjhplan",
                [point("existing", 35.0, 139.0)],
            )
            base = load_package(base_path)
            base.plan["works"][0]["bangumiId"] = 123
            proposal = build_import_proposal([_source()], base=base)
            self.assertEqual(
                proposal["workMappings"][0]["selectedWorkId"],
                "work-1",
            )
            proposal["review"]["confirmed"] = True
            plan_root = apply_import_proposal(base, proposal)
            output = directory / "merged.sjhplan"
            rebuild_package(
                base,
                plan_root,
                output,
                planner_version="test",
                operation="imported",
            )
            merged = load_package(output)
            self.assertEqual(merged.plan["points"][-1]["workId"], "work-1")
            self.assertEqual(
                merged.entries["custom/unknown.bin"],
                base.entries["custom/unknown.bin"],
            )
            self.assertIn("importedBy", merged.manifest)

    def test_exact_duplicate_is_deselected_in_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            base = load_package(
                write_package(
                    Path(directory_value) / "base.sjhplan",
                    [point("anitabi-123-p1", 35.1, 139.1)],
                )
            )
            proposal = build_import_proposal([_source()], base=base)
        self.assertFalse(proposal["points"][0]["selected"])
        self.assertEqual(proposal["summary"]["exactDuplicatesSkipped"], 1)
        with self.assertRaisesRegex(ImportProposalError, "selects no new points"):
            validate_import_proposal(base, proposal, final=True)


if __name__ == "__main__":
    unittest.main()
