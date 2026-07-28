from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from miriago_planner.clustering import profile_with_overrides
from miriago_planner.journey_notes import render_journey_notes, route_fingerprint
from miriago_planner.package_io import (
    PackageError,
    load_package,
    package_summary,
    rebuild_package,
)
from miriago_planner.proposal import (
    ProposalError,
    apply_proposal,
    exclude_points_for_time,
    generate_proposal,
    record_group_route_evaluation,
    record_route_policy,
    validate_proposal,
)

from .helpers import point, write_package


class PackageAndProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.input = write_package(
            self.directory / "input.sjhplan",
            [
                point("a", 35.6812, 139.7671, name="东京站"),
                point("b", 35.6840, 139.7700, name="丸之内"),
                point("c", 35.0116, 135.7681, name="京都御苑"),
                point("pending", -90, 0, name="待定位"),
            ],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _confirm(self, proposal: dict[str, object]) -> None:
        review = proposal["review"]
        assert isinstance(review, dict)
        review["areasConfirmed"] = True
        review["routesConfirmed"] = True
        record_route_policy(
            proposal,
            use_road_network=False,
            adjust_for_time=False,
        )
        review["routesConfirmed"] = True

    def test_inspect_accepts_pending_location_as_unplannable(self) -> None:
        package = load_package(self.input)
        summary = package_summary(package)
        self.assertEqual(summary["counts"]["plannablePoints"], 3)
        self.assertEqual(summary["counts"]["unplannablePoints"], 1)
        self.assertTrue(any("pending-location" in item for item in summary["warnings"]))

    def test_rejects_path_traversal_zip_entry(self) -> None:
        unsafe = self.directory / "unsafe.sjhplan"
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../escape", b"x")
            archive.writestr("manifest.json", b"{}")
            archive.writestr("plan.json", b"{}")
        with self.assertRaisesRegex(PackageError, "Unsafe ZIP entry"):
            load_package(unsafe)

    def test_rejects_duplicate_ids(self) -> None:
        duplicate = write_package(
            self.directory / "duplicate.sjhplan",
            [point("same", 35, 139), point("same", 35.1, 139.1)],
        )
        with self.assertRaisesRegex(PackageError, "Duplicate point ID"):
            load_package(duplicate)

    def test_rejects_non_finite_json_coordinate(self) -> None:
        invalid = write_package(
            self.directory / "non-finite.sjhplan",
            [point("nan", float("nan"), 139)],
        )
        with self.assertRaisesRegex(PackageError, "Non-finite JSON number"):
            load_package(invalid)

    def test_draft_requires_human_names_before_apply(self) -> None:
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        point_by_id = {point["id"]: point for point in package.plan["points"]}
        for group in proposal["groups"]:
            anchor = group["anchor"]
            source_point = point_by_id[anchor["pointId"]]
            self.assertEqual(anchor["name"], source_point["name"])
            self.assertEqual(anchor["latitude"], source_point["latitude"])
            self.assertEqual(anchor["longitude"], source_point["longitude"])
        validate_proposal(package, proposal, final=False)
        with self.assertRaisesRegex(ProposalError, "placeholder"):
            validate_proposal(package, proposal, final=True)

    def test_final_validation_rejects_mismatched_point_anchor(self) -> None:
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"人工命名片区 {index + 1}"
        self._confirm(proposal)
        proposal["groups"][0]["anchor"]["name"] = "错误锚点名"
        with self.assertRaisesRegex(ProposalError, "anchor.name must match"):
            validate_proposal(package, proposal, final=True)

    def test_final_validation_requires_a_consistent_anchor_kind(self) -> None:
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"人工命名片区 {index + 1}"
        self._confirm(proposal)
        proposal["groups"][0]["anchor"]["kind"] = "station"
        with self.assertRaisesRegex(ProposalError, "must be null for station"):
            validate_proposal(package, proposal, final=True)
        proposal["groups"][0]["anchor"] = None
        with self.assertRaisesRegex(ProposalError, "anchor is required"):
            validate_proposal(package, proposal, final=True)

    def test_apply_preserves_assets_records_and_unknown_fields(self) -> None:
        before_hash = hashlib.sha256(self.input.read_bytes()).hexdigest()
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"人工命名片区 {index + 1}"
        self._confirm(proposal)
        changed_root = apply_proposal(package, proposal)
        output = self.directory / "output.sjhplan"
        rebuild_package(
            package,
            changed_root,
            output,
            planner_version="test",
            additional_entries={
                "assets/full_references/new-reference.jpg": b"\xff\xd8\xff\xd9"
            },
        )
        rebuilt = load_package(output)

        self.assertEqual(
            hashlib.sha256(self.input.read_bytes()).hexdigest(), before_hash
        )
        for name in (
            "assets/thumbnails/shared.jpg",
            "assets/visit_photos/record-1.jpg",
            "custom/unknown.bin",
        ):
            self.assertEqual(rebuilt.entries[name], package.entries[name])
        self.assertEqual(rebuilt.manifest["futureManifestField"], {"preserve": True})
        self.assertEqual(rebuilt.manifest["assetCounts"]["futureAssets"], 7)
        self.assertNotEqual(
            rebuilt.manifest["packageId"], package.manifest["packageId"]
        )
        self.assertEqual(rebuilt.plan_root["futureRootField"], ["preserve"])
        self.assertEqual(rebuilt.plan["futurePlanField"], {"preserve": True})
        self.assertEqual(
            rebuilt.plan["points"][0]["futurePointField"], {"preserve": True}
        )
        self.assertEqual(
            rebuilt.plan_root["visitRecords"][0]["futureRecordField"],
            "preserve",
        )
        self.assertEqual(
            [group["name"] for group in rebuilt.plan["groups"]],
            [group["name"] for group in proposal["groups"]],
        )
        self.assertIsNone(rebuilt.plan["points"][-1]["groupId"])
        self.assertIn("人工命名片区", rebuilt.entries["points.csv"].decode())
        self.assertIn("2:05", rebuilt.entries["points.csv"].decode())
        self.assertIn("record-1", rebuilt.entries["records.csv"].decode())
        self.assertIn("assets/full_references/new-reference.jpg", rebuilt.entries)
        self.assertEqual(rebuilt.manifest["assetCounts"]["fullReferences"], 1)

    def test_apply_rejects_source_hash_mismatch(self) -> None:
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"人工命名片区 {index + 1}"
        self._confirm(proposal)
        proposal["input"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ProposalError, "source hash"):
            apply_proposal(package, proposal)

    def test_never_overwrites_input(self) -> None:
        package = load_package(self.input)
        with self.assertRaisesRegex(PackageError, "overwrite"):
            rebuild_package(
                package,
                package.plan_root,
                self.input,
                planner_version="test",
                force=True,
            )

    def test_hard_gates_require_area_then_route_confirmation(self) -> None:
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"人工命名片区 {index + 1}"
        with self.assertRaisesRegex(ProposalError, "Area review gate is closed"):
            validate_proposal(package, proposal, final=False, stage="areas")
        proposal["review"]["areasConfirmed"] = True
        validate_proposal(package, proposal, final=False, stage="areas")
        with self.assertRaisesRegex(ProposalError, "Route policy gate is closed"):
            validate_proposal(package, proposal, final=False, stage="route-policy")
        record_route_policy(
            proposal,
            use_road_network=False,
            adjust_for_time=False,
        )
        validate_proposal(package, proposal, final=False, stage="route-policy")
        with self.assertRaisesRegex(ProposalError, "Route review gate is closed"):
            validate_proposal(package, proposal, final=True, stage="final")
        proposal["review"]["routesConfirmed"] = True
        validate_proposal(package, proposal, final=True, stage="final")

    def test_requested_road_network_requires_evidence_for_every_group(self) -> None:
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"人工命名片区 {index + 1}"
        proposal["review"]["areasConfirmed"] = True
        record_route_policy(
            proposal,
            use_road_network=True,
            adjust_for_time=False,
        )
        proposal["review"]["routesConfirmed"] = True
        with self.assertRaisesRegex(ProposalError, "routeEvaluation is required"):
            validate_proposal(package, proposal, final=True)
        for group in proposal["groups"]:
            record_group_route_evaluation(
                proposal,
                group["id"],
                status="failed",
                provider="test_pedestrian_matrix",
                reason="test service unavailable",
            )
        proposal["review"]["routesConfirmed"] = True
        validate_proposal(package, proposal, final=True)

    def test_time_tradeoff_preserves_excluded_point_in_source_plan(self) -> None:
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"人工命名片区 {index + 1}"
        proposal["review"]["areasConfirmed"] = True
        record_route_policy(
            proposal,
            use_road_network=False,
            adjust_for_time=True,
            time_budget_minutes=240,
        )
        exclude_points_for_time(
            package,
            proposal,
            [("c", "独立点位距离其他片区过远")],
        )
        proposal["review"]["routesConfirmed"] = True
        validate_proposal(package, proposal, final=True)
        result = apply_proposal(package, proposal)
        source_point = next(point for point in package.plan["points"] if point["id"] == "c")
        result_point = next(
            point for point in result["plan"]["points"] if point["id"] == "c"
        )
        self.assertEqual(source_point["name"], result_point["name"])
        self.assertIsNone(result_point["groupId"])
        self.assertEqual(proposal["excludedPoints"][0]["pointId"], "c")
        self.assertIn("过远", proposal["excludedPoints"][0]["reason"])

    def test_time_tradeoff_rejects_duplicate_and_unknown_ids(self) -> None:
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"人工命名片区 {index + 1}"
        proposal["review"]["areasConfirmed"] = True
        record_route_policy(
            proposal,
            use_road_network=False,
            adjust_for_time=True,
        )
        with self.assertRaisesRegex(ProposalError, "Duplicate point exclusion"):
            exclude_points_for_time(
                package,
                proposal,
                [("a", "reason one"), ("a", "reason two")],
            )
        with self.assertRaisesRegex(ProposalError, "Unknown point exclusion"):
            exclude_points_for_time(
                package,
                proposal,
                [("missing", "not present")],
            )

    def test_confirmed_journey_notes_are_required_and_merged_into_memo(self) -> None:
        package = load_package(self.input)
        proposal = generate_proposal(package, profile_with_overrides("balanced"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"人工命名片区 {index + 1}"
        proposal["review"]["areasConfirmed"] = True
        record_route_policy(
            proposal,
            use_road_network=False,
            adjust_for_time=False,
            write_plan_notes=True,
        )
        proposal["review"]["routesConfirmed"] = True
        with self.assertRaisesRegex(ProposalError, "Journey-note review gate"):
            validate_proposal(package, proposal, final=True)

        spec = {
            "schemaVersion": 1,
            "timezone": "Asia/Tokyo",
            "checkedAt": "2026-07-28",
            "includeAreaWalking": True,
            "globalCautions": ["出发前确认运营时间"],
        }
        proposal["journeyNotes"] = {
            "status": "draft",
            "preparedAt": "2026-07-28T00:00:00Z",
            "confirmedAt": None,
            "routeFingerprint": route_fingerprint(proposal),
            "spec": spec,
            "markdown": render_journey_notes(proposal, spec),
        }
        with self.assertRaisesRegex(ProposalError, "Journey-note review gate"):
            validate_proposal(package, proposal, final=True)
        proposal["journeyNotes"]["status"] = "confirmed"
        proposal["journeyNotes"]["confirmedAt"] = "2026-07-28T00:01:00Z"
        validate_proposal(package, proposal, final=True)
        result = apply_proposal(package, proposal)
        self.assertTrue(result["plan"]["memo"].startswith("keep this memo"))
        self.assertIn("巡礼行程建议", result["plan"]["memo"])
        self.assertIn("出发前确认运营时间", result["plan"]["memo"])


if __name__ == "__main__":
    unittest.main()
