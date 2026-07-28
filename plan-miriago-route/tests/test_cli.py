from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .helpers import point, write_package


class CliTests(unittest.TestCase):
    def test_propose_never_overwrites_source_even_with_force(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "miriago_plan.py"
        with tempfile.TemporaryDirectory() as directory_value:
            source = write_package(
                Path(directory_value) / "source.sjhplan",
                [point("a", 35.68, 139.76)],
            )
            before = source.read_bytes()
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "propose",
                    str(source),
                    "--output",
                    str(source),
                    "--force",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_bytes(), before)

    def test_offline_end_to_end(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "miriago_plan.py"
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            points = [
                point("a", 35.68, 139.76),
                point("b", 35.681, 139.761),
                point("c", 35.01, 135.76),
            ]
            for item in points:
                item["source"] = "anitabi"
            source = write_package(directory / "source.sjhplan", points)
            proposal_path = directory / "proposal.json"
            proposed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "propose",
                    str(source),
                    "--output",
                    str(proposal_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            proposal = json.loads(proposal_path.read_text())
            for index, group in enumerate(proposal["groups"]):
                group["name"] = f"Area {index + 1}"
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
            areas_path = directory / "areas-confirmed.json"
            areas_confirmed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-proposal",
                    str(source),
                    str(proposal_path),
                    "--stage",
                    "areas",
                    "--output",
                    str(areas_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(areas_confirmed.returncode, 0, areas_confirmed.stderr)
            policy_path = directory / "route-policy-confirmed.json"
            policy_confirmed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-route-policy",
                    str(source),
                    str(areas_path),
                    "--road-network",
                    "no",
                    "--time-adjustment",
                    "no",
                    "--plan-notes",
                    "no",
                    "--output",
                    str(policy_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(policy_confirmed.returncode, 0, policy_confirmed.stderr)
            routes_path = directory / "routes-confirmed.json"
            routes_confirmed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-proposal",
                    str(source),
                    str(policy_path),
                    "--stage",
                    "routes",
                    "--output",
                    str(routes_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(routes_confirmed.returncode, 0, routes_confirmed.stderr)
            output = directory / "planned.sjhplan"
            blocked_export = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "apply",
                    str(source),
                    str(routes_path),
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked_export.returncode, 2)
            self.assertIn("--full-reference-images", blocked_export.stderr)
            applied = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "apply",
                    str(source),
                    str(routes_path),
                    "--output",
                    str(output),
                    "--full-reference-images",
                    "omit",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            progress_events = [
                json.loads(line) for line in applied.stderr.splitlines()
            ]
            self.assertEqual(
                [event["event"] for event in progress_events],
                ["image_download_start", "image_download_complete"],
            )
            self.assertEqual(progress_events[0]["total"], 0)
            self.assertEqual(json.loads(applied.stdout)["status"], "package_created")
            validated = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate",
                    str(output),
                    "--source",
                    str(source),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(json.loads(validated.stdout)["status"], "valid")

    def test_refine_requires_an_explicit_affirmative_road_policy(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "miriago_plan.py"
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source = write_package(
                directory / "source.sjhplan",
                [point("a", 35.68, 139.76), point("b", 35.681, 139.761)],
            )
            draft = directory / "draft.json"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "propose",
                    str(source),
                    "--output",
                    str(draft),
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            proposal = json.loads(draft.read_text())
            proposal["groups"][0]["name"] = "Reviewed Area"
            draft.write_text(json.dumps(proposal), encoding="utf-8")
            areas = directory / "areas.json"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-proposal",
                    str(source),
                    str(draft),
                    "--stage",
                    "areas",
                    "--output",
                    str(areas),
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            group_id = proposal["groups"][0]["id"]
            before_policy = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "refine",
                    str(source),
                    str(areas),
                    "--group-id",
                    group_id,
                    "--output",
                    str(directory / "blocked-before-policy.json"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(before_policy.returncode, 2)
            self.assertIn("Route policy gate is closed", before_policy.stderr)
            declined = directory / "declined.json"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-route-policy",
                    str(source),
                    str(areas),
                    "--road-network",
                    "no",
                    "--time-adjustment",
                    "no",
                    "--plan-notes",
                    "no",
                    "--output",
                    str(declined),
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            after_decline = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "refine",
                    str(source),
                    str(declined),
                    "--group-id",
                    group_id,
                    "--output",
                    str(directory / "blocked-after-decline.json"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(after_decline.returncode, 2)
            self.assertIn("evaluation was declined", after_decline.stderr)

    def test_journey_notes_cli_requires_explicit_review(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "miriago_plan.py"
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source = write_package(
                directory / "source.sjhplan",
                [point("a", 35.68, 139.76)],
            )
            draft = directory / "draft.json"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "propose",
                    str(source),
                    "--output",
                    str(draft),
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            proposal = json.loads(draft.read_text())
            proposal["groups"][0]["name"] = "Reviewed Area"
            draft.write_text(json.dumps(proposal), encoding="utf-8")
            areas = directory / "areas.json"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-proposal",
                    str(source),
                    str(draft),
                    "--stage",
                    "areas",
                    "--output",
                    str(areas),
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            policy = directory / "policy.json"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-route-policy",
                    str(source),
                    str(areas),
                    "--road-network",
                    "no",
                    "--time-adjustment",
                    "no",
                    "--plan-notes",
                    "yes",
                    "--output",
                    str(policy),
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            notes_spec = directory / "notes.json"
            notes_spec.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "timezone": "Asia/Tokyo",
                        "checkedAt": "2026-07-28",
                        "globalCautions": ["出发前确认开放时间"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            notes_draft = directory / "notes-draft.json"
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "prepare-notes",
                    str(source),
                    str(policy),
                    str(notes_spec),
                    "--output",
                    str(notes_draft),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            prepared_output = json.loads(prepared.stdout)
            self.assertIn("巡礼行程建议", prepared_output["markdown"])
            self.assertTrue(prepared_output["memoPreview"].startswith("keep this memo"))
            blocked = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-proposal",
                    str(source),
                    str(notes_draft),
                    "--stage",
                    "routes",
                    "--output",
                    str(directory / "blocked.json"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("Journey-note review gate is closed", blocked.stderr)
            notes_confirmed = directory / "notes-confirmed.json"
            confirmation = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-notes",
                    str(source),
                    str(notes_draft),
                    "--output",
                    str(notes_confirmed),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(confirmation.returncode, 0, confirmation.stderr)
            routes = directory / "routes.json"
            routes_confirmed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-proposal",
                    str(source),
                    str(notes_confirmed),
                    "--stage",
                    "routes",
                    "--output",
                    str(routes),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(routes_confirmed.returncode, 0, routes_confirmed.stderr)
            self.assertEqual(
                json.loads(routes.read_text())["journeyNotes"]["status"],
                "confirmed",
            )

    def test_import_cli_refuses_to_apply_before_confirmation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "miriago_plan.py"
        kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Fixture</name>
<Folder><name>Work</name><Placemark><name>Scene</name>
<Point><coordinates>139.1,35.1</coordinates></Point>
</Placemark></Folder></Document></kml>"""
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            kml_path = directory / "map.kml"
            kml_path.write_text(kml, encoding="utf-8")
            draft = directory / "import-draft.json"
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "prepare-import",
                    "--my-maps",
                    str(kml_path),
                    "--plan-name",
                    "Fixture Plan",
                    "--area",
                    "Tokyo",
                    "--output",
                    str(draft),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            blocked = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "apply-import",
                    str(draft),
                    "--output",
                    str(directory / "blocked.sjhplan"),
                    "--full-reference-images",
                    "omit",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("review gate is closed", blocked.stderr)

            confirmed = directory / "import-confirmed.json"
            confirmation = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "confirm-import",
                    str(draft),
                    "--output",
                    str(confirmed),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(confirmation.returncode, 0, confirmation.stderr)
            output = directory / "imported.sjhplan"
            blocked_export = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "apply-import",
                    str(confirmed),
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked_export.returncode, 2)
            self.assertIn("--full-reference-images", blocked_export.stderr)
            applied = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "apply-import",
                    str(confirmed),
                    "--output",
                    str(output),
                    "--full-reference-images",
                    "omit",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(json.loads(applied.stdout)["counts"]["points"], 1)

    def test_apply_rejects_invalid_image_download_worker_count(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "miriago_plan.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "apply",
                "input.sjhplan",
                "proposal.json",
                "--output",
                "output.sjhplan",
                "--full-reference-images",
                "omit",
                "--image-download-workers",
                "0",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be between 1 and 32", result.stderr)


if __name__ == "__main__":
    unittest.main()
