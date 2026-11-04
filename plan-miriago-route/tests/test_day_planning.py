from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from miriago_planner.clustering import profile_with_overrides
from miriago_planner.day_planner import DayPlanError, plan_days
from miriago_planner.journey_notes import (
    render_journey_notes,
    validate_journey_notes_spec,
)
from miriago_planner.package_io import load_package
from miriago_planner.proposal import (
    ProposalError,
    generate_proposal,
    load_proposal,
    record_route_policy,
    upgrade_proposal,
    validate_proposal,
)
from miriago_planner.route_refine import refine_groups
from miriago_planner.stations import (
    assist_stations,
    draft_notes_spec,
    label_stations,
    parse_station_lines,
)
from miriago_planner.walking import StraightBackend

from .helpers import point, write_package

STATIONS = [
    {"id": "osm:node:1", "name": "West", "latitude": 35.0, "longitude": 139.0, "osmType": "node", "osmId": 1},
    {"id": "osm:node:2", "name": "East", "latitude": 35.0, "longitude": 139.1, "osmType": "node", "osmId": 2},
]


def _points() -> list[dict]:
    points = []
    for index in range(12):
        points.append(point(f"w{index}", 35.0 + index * 0.0008, 139.002 + (index % 3) * 0.0006, name=f"西町 {index}"))
    for index in range(8):
        points.append(point(f"e{index}", 35.0 + index * 0.0008, 139.098 + (index % 2) * 0.0006, name=f"東町 {index}"))
    points.append(point("summit", 35.012, 139.004, name="展望台"))
    points.append(point("school", 35.02, 139.05, name="学校内"))
    return points


class DayPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.package = load_package(write_package(self.directory / "input.sjhplan", _points()))
        self.config = {
            "schemaVersion": 1,
            "bundleMeters": 30,
            "areaTargetMinutes": 10,
            "via": [{"nameContains": "展望台", "latitude": 35.011, "longitude": 139.006, "label": "登山口"}],
            "pools": [
                {
                    "select": {"nameContains": ["西町", "展望台"]},
                    "days": [{"label": "Day1", "theme": "西"}],
                    "start": "station:West",
                    "end": "name:展望台",
                },
                {
                    "select": {"remaining": True},
                    "days": [{"label": "Day2", "theme": "東"}],
                    "start": "auto",
                    "end": "auto",
                },
            ],
            "specialGroups": [{"kind": "restricted", "name": "开放日限定", "select": {"pointIds": ["school"]}}],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _plan(self, config: dict | None = None) -> dict:
        stations = label_stations([dict(item) for item in STATIONS])
        proposal, _ = plan_days(
            self.package,
            config or self.config,
            backend=StraightBackend(),
            station_provider=lambda _: stations,
        )
        return proposal

    def test_days_areas_ends_and_special_groups(self) -> None:
        proposal = self._plan()
        validate_proposal(self.package, proposal, final=False, stage="draft")
        routes = [group for group in proposal["groups"] if group["kind"] == "route"]
        self.assertGreater(len(routes), 2)
        day1 = [group for group in routes if group["day"]["label"] == "Day1"]
        self.assertEqual(day1[0]["routeEnds"]["start"]["name"], "West")
        self.assertEqual(day1[-1]["routeEnds"]["end"], {"kind": "point", "pointId": "summit"})
        self.assertEqual(day1[-1]["pointIds"][-1], "summit")
        for later in day1[1:]:
            self.assertEqual(later["routeEnds"]["start"], {"kind": "previous-group"})
        special = [group for group in proposal["groups"] if group["kind"] == "restricted"]
        self.assertEqual(special[0]["pointIds"], ["school"])
        assigned = [point_id for group in proposal["groups"] for point_id in group["pointIds"]]
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(len(assigned), 22)

    def test_uncovered_points_are_reported(self) -> None:
        config = dict(self.config)
        config["pools"] = config["pools"][:1]
        with self.assertRaisesRegex(DayPlanError, "not covered"):
            self._plan(config)

    def test_refine_keeps_route_ends_and_records_evidence(self) -> None:
        proposal = self._plan()
        proposal["review"]["areasConfirmed"] = True
        for group in proposal["groups"]:
            group["name"] = group["name"].replace("候选片区", "片区")
        record_route_policy(proposal, use_road_network=True, adjust_for_time=False)
        geometry: dict = {}
        summary = refine_groups(
            self.package,
            proposal,
            group_ids={group["id"] for group in proposal["groups"]},
            backend=StraightBackend(),
            geometry=geometry,
        )
        self.assertEqual(len(summary["skipped"]), 1)
        last = [group for group in proposal["groups"] if group.get("day", {}).get("label") == "Day1"][-1]
        self.assertEqual(last["pointIds"][-1], "summit")
        self.assertEqual(last["routeEvaluation"]["routeEnds"], last["routeEnds"])
        first = proposal["groups"][0]
        self.assertEqual(first["access"]["entry"]["station"], "West")
        self.assertIn(first["id"], geometry["groups"])
        proposal["review"]["routesConfirmed"] = True
        proposal["review"]["routesConfirmedAt"] = "now"
        validate_proposal(self.package, proposal, final=True, stage="final")
        last["routeEnds"] = {"start": None, "end": None}
        with self.assertRaisesRegex(ProposalError, "stale"):
            validate_proposal(self.package, proposal, final=True, stage="final")

    def test_station_assist_transitions_and_notes_skeleton(self) -> None:
        proposal = generate_proposal(self.package, profile_with_overrides("compact"))
        for index, group in enumerate(proposal["groups"]):
            group["name"] = f"片区 A{index}"
            group["day"] = {"label": "Day1", "index": 0, "theme": None}
            if "school" in group["pointIds"]:
                group["kind"] = "restricted"
        proposal["review"]["areasConfirmed"] = True
        record_route_policy(
            proposal,
            use_road_network=False,
            adjust_for_time=False,
            write_plan_notes=True,
            stations_assist=True,
        )
        stations = label_stations([dict(item) for item in STATIONS])
        lines = {"osm:node:1": ["Line A"], "osm:node:2": ["Line A"]}
        summary = assist_stations(self.package, proposal, stations=stations, lines=lines)
        modes = {item["mode"] for item in proposal["transitions"]}
        self.assertIn("rail_candidate", modes)
        self.assertEqual(proposal["stationAssist"]["status"], "planned")
        self.assertTrue(summary["changedGroupIds"])
        spec = draft_notes_spec(proposal, checked_at="2026-11-04", timezone_name="Asia/Tokyo")
        validate_journey_notes_spec(spec)
        rail = [segment for segment in spec["segments"] if segment["mode"] == "rail"]
        self.assertTrue(rail)
        self.assertNotIn("durationMinutes", rail[0])
        self.assertIn("Line A", render_journey_notes(proposal, spec))

    def test_station_lines_are_parsed_from_route_relations(self) -> None:
        response = {
            "elements": [
                {"type": "node", "id": 10, "lat": 35.0001, "lon": 139.0001},
                {"type": "relation", "id": 5, "tags": {"name": "Line A (West → East)"}, "members": [{"type": "node", "ref": 10}]},
            ]
        }
        self.assertEqual(parse_station_lines(response, STATIONS), {"osm:node:1": ["Line A"]})

    def test_schema_4_proposals_upgrade_without_reopening_gates(self) -> None:
        proposal = generate_proposal(self.package, profile_with_overrides("balanced"))
        proposal["schemaVersion"] = 4
        for key in ("modules", "constraints", "transitions", "stationAssist", "reviewHints"):
            proposal.pop(key)
        for group in proposal["groups"]:
            group.pop("kind")
        path = self.directory / "v4.json"
        path.write_text(json.dumps(proposal), encoding="utf-8")
        upgraded = load_proposal(path)
        self.assertEqual(upgraded["schemaVersion"], 5)
        validate_proposal(self.package, upgraded, final=False, stage="draft")
        self.assertEqual(upgrade_proposal(upgraded), upgraded)


if __name__ == "__main__":
    unittest.main()
