from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from miriago_planner.clustering import profile_with_overrides
from miriago_planner.curation import (
    CurationError,
    apply_dedupe,
    apply_verify,
    dedupe_candidates,
    draft_points,
    merge_classify,
)
from miriago_planner.package_io import load_package
from miriago_planner.proposal import (
    ProposalError,
    exclude_points_for_curation,
    generate_proposal,
    restore_curated_points,
    validate_proposal,
)
from miriago_planner.review_server import DecisionStore, load_decisions, make_handler
from miriago_planner.route_preview import render_route_preview

from .helpers import point, write_package


def _draft() -> dict:
    def item(point_id: str, lat: float, lon: float, name: str) -> dict:
        return {
            "sourceWorkRef": "w",
            "selected": True,
            "duplicateOf": None,
            "data": {"id": point_id, "name": name, "latitude": lat, "longitude": lon, "episodeLabel": "EP1", "note": ""},
        }

    return {
        "schemaVersion": 1,
        "availableWorks": [{"origin": "import", "data": {"id": "work-1", "title": "作品"}}],
        "workMappings": [{"sourceWorkRef": "w", "selectedWorkId": "work-1"}],
        "points": [
            item("a", 35.0, 139.0, "桥 A"),
            item("b", 35.00005, 139.00005, "桥 B"),
            item("c", 35.01, 139.01, "远处"),
        ],
        "review": {"confirmed": True, "confirmedAt": "x"},
        "summary": {},
    }


class CurationTests(unittest.TestCase):
    def test_dedupe_marks_dropped_scenes_and_annotates_the_kept_one(self) -> None:
        draft = _draft()
        groups = dedupe_candidates(draft_points(draft), meters=30)
        self.assertEqual(len(groups), 1)
        candidates = {"groups": groups}
        result, log = apply_dedupe(draft, candidates, {"groups": {"D001": {"keep": ["a"], "drop": ["b"]}}})
        items = {item["data"]["id"]: item for item in result["points"]}
        self.assertFalse(items["b"]["selected"])
        self.assertIn("同机位画面另见", items["a"]["data"]["note"])
        self.assertFalse(result["review"]["confirmed"])
        self.assertEqual(log["removed"], ["b"])
        with self.assertRaises(CurationError):
            apply_dedupe(draft, candidates, {"groups": {"D001": {"keep": [], "drop": ["a", "b"]}}})

    def test_verify_moves_coordinates_and_collects_unverifiable(self) -> None:
        draft = _draft()
        batch = {"stops": [{"stopId": 1, "points": [{"pointId": "a"}, {"pointId": "b"}]}, {"stopId": 2, "points": [{"pointId": "c"}]}]}
        verdict = {
            "stops": [
                {"stopId": 1, "status": "offset", "suggestedLat": 35.0002, "suggestedLng": 139.0002, "detail": "桥在北侧"},
                {"stopId": 2, "status": "unverifiable", "detail": "画面是室内"},
            ]
        }
        result, log = apply_verify(draft, [(batch, verdict)])
        items = {item["data"]["id"]: item["data"] for item in result["points"]}
        self.assertAlmostEqual(items["a"]["latitude"], 35.0002)
        self.assertIn("原坐标", items["a"]["note"])
        self.assertEqual(log["unverifiable"], ["c"])

    def test_classify_hints_are_validated(self) -> None:
        merged, counts = merge_classify(
            {"reviewHints": {}},
            [{"points": {"a": {"class": "keep", "famous": True, "reason": "名场面"}}}],
            {"a", "b"},
        )
        self.assertEqual(merged["reviewHints"]["a"]["class"], "keep")
        self.assertEqual(counts["famous"], 1)
        with self.assertRaises(CurationError):
            merge_classify({}, [{"points": {"a": {"class": "maybe"}}}], {"a"})


class ReviewFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = write_package(
            self.directory / "input.sjhplan",
            [point("a", 35.0, 139.0), point("b", 35.0003, 139.0003), point("c", 35.0006, 139.0006)],
            extra_entries={"assets/full_references/a.jpg": b"\xff\xd8full\xff\xd9"},
        )
        self.package = load_package(self.path)
        self.proposal = generate_proposal(self.package, profile_with_overrides("balanced"))
        self.proposal["groups"][0]["name"] = "宇治桥"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_curation_gate_blocks_area_approval_until_confirmed(self) -> None:
        exclude_points_for_curation(self.package, self.proposal, [("b", "重复")])
        self.assertEqual(self.proposal["excludedPoints"][0]["kind"], "curation")
        self.proposal["review"]["areasConfirmed"] = True
        with self.assertRaisesRegex(ProposalError, "Curation gate"):
            validate_proposal(self.package, self.proposal, final=False, stage="areas")
        self.proposal["review"]["curationConfirmed"] = True
        validate_proposal(self.package, self.proposal, final=False, stage="areas")
        restored = restore_curated_points(self.package, self.proposal, ["b"])
        self.assertEqual(restored, ["b"])
        self.assertIn("b", self.proposal["groups"][0]["pointIds"])
        self.assertEqual(self.proposal["excludedPoints"], [])

    def test_decision_store_server_and_exported_json(self) -> None:
        store = DecisionStore(self.directory / "review.sqlite")
        preview = self.directory / "page" / "preview.html"
        preview.parent.mkdir()
        preview.write_text("<html></html>", encoding="utf-8")
        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(preview.parent, store, self.path))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = Request(
                f"{base}/api/decisions/bulk",
                data=json.dumps({"decisions": {"a": "keep", "b": "delete"}}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            self.assertEqual(json.load(urlopen(request))["updated"], 2)
            body = json.load(urlopen(f"{base}/api/decisions"))
            self.assertEqual(body["count"], {"delete": 1, "keep": 1})
            self.assertEqual(urlopen(f"{base}/package-asset/assets/full_references/a.jpg").read(), b"\xff\xd8full\xff\xd9")
            with self.assertRaises(HTTPError):
                urlopen(f"{base}/package-asset/plan.json")
            bad = Request(
                f"{base}/api/decisions/a",
                data=json.dumps({"decision": "maybe"}).encode(),
                method="PUT",
            )
            with self.assertRaises(HTTPError):
                urlopen(bad)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(load_decisions(self.directory / "review.sqlite"), {"a": "keep", "b": "delete"})
        exported = self.directory / "export.json"
        exported.write_text(json.dumps({"decisions": {"c": "delete"}}), encoding="utf-8")
        self.assertEqual(load_decisions(exported), {"c": "delete"})
        self.assertEqual(len(store.history()), 2)

    def test_review_preview_exports_thumbnails_and_relaxes_csp_only_in_review_mode(self) -> None:
        proposal_path = self.directory / "proposal.json"
        proposal_path.write_text(json.dumps(self.proposal), encoding="utf-8")
        digest = hashlib.sha256(proposal_path.read_bytes()).hexdigest()
        common = {
            "proposal_path": proposal_path,
            "proposal_sha256": digest,
            "geometry_mode": "straight",
            "request_delay_seconds": 0,
        }
        compact = render_route_preview(self.package, self.proposal, output=self.directory / "compact.html", **common)
        review = render_route_preview(self.package, self.proposal, output=self.directory / "review.html", mode="review", **common)
        compact_html = compact.output.read_text(encoding="utf-8")
        review_html = review.output.read_text(encoding="utf-8")
        self.assertNotIn("'self'", compact_html)
        self.assertNotIn("qc-open", compact_html)
        self.assertIn("connect-src https: 'self'", review_html)
        self.assertIn("quick", review_html.lower())
        thumbs = list((self.directory / "review-assets" / "thumbs").iterdir())
        self.assertEqual(len(thumbs), 3)

    def test_geometry_file_is_used_only_when_it_matches_the_route(self) -> None:
        group = self.proposal["groups"][0]
        proposal_path = self.directory / "proposal.json"
        proposal_path.write_text(json.dumps(self.proposal), encoding="utf-8")
        digest = hashlib.sha256(proposal_path.read_bytes()).hexdigest()
        geometry = {"groups": {group["id"]: {"pointIds": group["pointIds"], "routeEnds": None, "coordinates": [[139.0, 35.0], [139.123456, 35.0], [139.0006, 35.0006]]}}}
        result = render_route_preview(
            self.package,
            self.proposal,
            proposal_path=proposal_path,
            proposal_sha256=digest,
            output=self.directory / "planned.html",
            geometry_file=geometry,
            request_delay_seconds=0,
        )
        html = result.output.read_text(encoding="utf-8")
        self.assertIn("139.123456", html)
        self.assertIn('"status":"planned"', html)
        geometry["groups"][group["id"]]["pointIds"] = list(reversed(group["pointIds"]))
        stale = render_route_preview(
            self.package,
            self.proposal,
            proposal_path=proposal_path,
            proposal_sha256=digest,
            output=self.directory / "stale.html",
            geometry_file=geometry,
            request_delay_seconds=0,
        )
        self.assertNotIn("139.123456", stale.output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class CuratedCliTests(unittest.TestCase):
    def test_special_groups_review_and_curation_commands(self) -> None:
        import contextlib
        import io

        from miriago_plan import main

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            package = write_package(
                directory / "input.sjhplan",
                [point("a", 35.0, 139.0), point("b", 35.0003, 139.0003), point("s", 35.2, 139.2, name="校内")],
            )
            specials = directory / "specials.json"
            specials.write_text(json.dumps([{"kind": "restricted", "name": "开放日限定", "select": {"nameContains": "校内"}}]), encoding="utf-8")

            def run(*argv: str) -> dict:
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(list(argv)), 0)
                return json.loads(out.getvalue())

            result = run("propose", str(package), "--output", str(directory / "draft.json"), "--special-groups", str(specials), "--bundle-meters", "30")
            self.assertEqual(result["groupKinds"], {"route": 1, "restricted": 1})
            draft = json.loads((directory / "draft.json").read_text(encoding="utf-8"))
            draft["groups"][0]["name"] = "宇治桥"
            (directory / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
            decisions = directory / "decisions.json"
            decisions.write_text(json.dumps({"decisions": {"b": "delete"}}), encoding="utf-8")
            result = run("apply-review", str(package), str(directory / "draft.json"), "--decisions", str(decisions), "--output", str(directory / "curated.json"))
            self.assertEqual(result["removed"], 1)
            result = run("confirm-curation", str(package), str(directory / "curated.json"), "--output", str(directory / "confirmed.json"))
            self.assertTrue(result["curationConfirmed"])
            result = run("confirm-proposal", str(package), str(directory / "confirmed.json"), "--stage", "areas", "--output", str(directory / "areas.json"))
            self.assertEqual(result["status"], "areas_confirmed")
            decisions.write_text(json.dumps({"decisions": {}}), encoding="utf-8")
            result = run("apply-review", str(package), str(directory / "areas.json"), "--decisions", str(decisions), "--output", str(directory / "restored.json"))
            self.assertEqual(result["restored"], 1)
