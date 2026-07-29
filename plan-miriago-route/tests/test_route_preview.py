from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from miriago_planner.clustering import profile_with_overrides
from miriago_planner.package_io import load_package
from miriago_planner.proposal import (
    exclude_points_for_time,
    generate_proposal,
    record_group_route_evaluation,
    record_route_policy,
    update_group_with_walking_matrix,
)
from miriago_planner.public_services import PublicServiceError, WalkingRoute
from miriago_planner.route_preview import RoutePreviewError, render_route_preview

from .helpers import point, write_package


class RoutePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.input = write_package(
            self.directory / "input.sjhplan",
            [
                point("a", 35.6812, 139.7671, name="起点"),
                point(
                    "b",
                    35.6840,
                    139.7700,
                    name="</script><script>alert('preview')</script>",
                ),
                point("c", 35.6820, 139.7680, name="取舍点"),
                point("pending", -90, 0, name="待定位"),
            ],
        )
        self.package = load_package(self.input)
        proposal = generate_proposal(
            self.package,
            profile_with_overrides("balanced"),
        )
        self.assertEqual(len(proposal["groups"]), 1)
        proposal["groups"][0]["name"] = "东京中心"
        proposal["review"]["areasConfirmed"] = True
        record_route_policy(
            proposal,
            use_road_network=False,
            adjust_for_time=True,
        )
        exclude_points_for_time(
            self.package,
            proposal,
            [("c", "SECRET_EXCLUSION_REASON")],
        )
        self.proposal = proposal
        self.proposal_path = self.directory / "proposal.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _render(
        self,
        proposal: dict[str, object],
        *,
        output_name: str = "preview.html",
        **kwargs: object,
    ):
        self.proposal_path.write_text(
            json.dumps(proposal, ensure_ascii=False),
            encoding="utf-8",
        )
        proposal_sha256 = hashlib.sha256(self.proposal_path.read_bytes()).hexdigest()
        return render_route_preview(
            self.package,
            proposal,
            proposal_path=self.proposal_path,
            proposal_sha256=proposal_sha256,
            output=self.directory / output_name,
            request_delay_seconds=0,
            **kwargs,
        )

    def test_straight_preview_is_minimal_and_xss_safe(self) -> None:
        result = self._render(
            self.proposal,
            geometry_mode="straight",
            no_basemap=True,
        )
        html = result.output.read_text(encoding="utf-8")
        self.assertEqual(result.groups, 1)
        self.assertEqual(result.routed_groups, 0)
        self.assertEqual(result.fallback_groups, 1)
        self.assertEqual(result.excluded_points, 1)
        self.assertEqual(result.unlocated_points, 1)
        self.assertNotIn("SECRET_EXCLUSION_REASON", html)
        self.assertNotIn("</script><script>alert('preview')</script>", html)
        self.assertIn("\\u003c/script\\u003e", html)
        self.assertIn('"status":"approximate"', html)
        self.assertIn('"noBasemap":true', html)

    def test_authorized_auto_mode_fetches_real_geometry(self) -> None:
        proposal = deepcopy(self.proposal)
        record_route_policy(
            proposal,
            use_road_network=True,
            adjust_for_time=True,
        )
        group = proposal["groups"][0]
        update_group_with_walking_matrix(
            proposal,
            group["id"],
            group["pointIds"],
            distance_meters=420,
            duration_seconds=360,
            provider="test_matrix",
        )
        calls: list[object] = []

        def route_fetcher(locations: object, **_: object) -> WalkingRoute:
            calls.append(locations)
            return WalkingRoute(
                geojson_coordinates=(
                    (139.7671, 35.6812),
                    (139.777777, 35.688888),
                    (139.7700, 35.6840),
                ),
                endpoint="test",
                cache_hit=False,
            )

        result = self._render(
            proposal,
            geometry_mode="auto",
            route_fetcher=route_fetcher,
        )
        html = result.output.read_text(encoding="utf-8")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.routed_groups, 1)
        self.assertEqual(result.fallback_groups, 0)
        self.assertIn("139.777777", html)
        self.assertIn('"status":"routed"', html)

    def test_road_network_mode_requires_explicit_policy_approval(self) -> None:
        with self.assertRaisesRegex(RoutePreviewError, "explicitly confirmed"):
            self._render(self.proposal, geometry_mode="road-network")

    def test_route_fetch_failure_creates_explicit_fallback(self) -> None:
        proposal = deepcopy(self.proposal)
        record_route_policy(
            proposal,
            use_road_network=True,
            adjust_for_time=True,
        )
        group = proposal["groups"][0]
        update_group_with_walking_matrix(
            proposal,
            group["id"],
            group["pointIds"],
            distance_meters=420,
            duration_seconds=360,
            provider="test_matrix",
        )

        def route_fetcher(*_: object, **__: object) -> WalkingRoute:
            raise PublicServiceError("SECRET_ROUTE_DIAGNOSTIC")

        result = self._render(
            proposal,
            geometry_mode="auto",
            route_fetcher=route_fetcher,
        )
        html = result.output.read_text(encoding="utf-8")
        self.assertEqual(result.routed_groups, 0)
        self.assertEqual(result.fallback_groups, 1)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn('"status":"failed"', html)
        self.assertNotIn("SECRET_ROUTE_DIAGNOSTIC", html)

    def test_skipped_evaluation_is_visible_without_its_reason(self) -> None:
        proposal = deepcopy(self.proposal)
        record_route_policy(
            proposal,
            use_road_network=True,
            adjust_for_time=True,
        )
        group = proposal["groups"][0]
        record_group_route_evaluation(
            proposal,
            group["id"],
            status="skipped",
            provider="test_matrix",
            reason="SECRET_SKIP_REASON",
        )
        result = self._render(proposal, geometry_mode="auto")
        html = result.output.read_text(encoding="utf-8")
        self.assertIn('"status":"skipped"', html)
        self.assertIn("路网评估跳过", html)
        self.assertNotIn("SECRET_SKIP_REASON", html)

    def test_output_overwrite_protection_covers_existing_and_input_files(self) -> None:
        output = self.directory / "existing.html"
        output.write_text("original", encoding="utf-8")
        with self.assertRaisesRegex(RoutePreviewError, "already exists"):
            self._render(self.proposal, output_name=output.name)
        self.assertEqual(output.read_text(encoding="utf-8"), "original")

        self.proposal_path.write_text(
            json.dumps(self.proposal, ensure_ascii=False),
            encoding="utf-8",
        )
        before_proposal = self.proposal_path.read_bytes()
        with self.assertRaisesRegex(RoutePreviewError, "input package or proposal"):
            render_route_preview(
                self.package,
                self.proposal,
                proposal_path=self.proposal_path,
                proposal_sha256="0" * 64,
                output=self.proposal_path,
                geometry_mode="straight",
                force=True,
            )
        self.assertEqual(self.proposal_path.read_bytes(), before_proposal)

        before_package = self.input.read_bytes()
        with self.assertRaisesRegex(RoutePreviewError, "input package or proposal"):
            render_route_preview(
                self.package,
                self.proposal,
                proposal_path=self.proposal_path,
                proposal_sha256="0" * 64,
                output=self.input,
                geometry_mode="straight",
                force=True,
            )
        self.assertEqual(self.input.read_bytes(), before_package)


if __name__ == "__main__":
    unittest.main()
