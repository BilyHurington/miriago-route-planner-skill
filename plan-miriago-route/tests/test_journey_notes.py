from __future__ import annotations

import unittest

from miriago_planner.journey_notes import (
    JourneyNotesError,
    MANAGED_SECTION_END,
    MANAGED_SECTION_START,
    merge_managed_journey_notes,
    render_journey_notes,
    route_fingerprint,
    validate_journey_notes_spec,
)


def _proposal() -> dict[str, object]:
    return {
        "groups": [
            {
                "id": "group-1",
                "name": "宇治站片区",
                "pointIds": ["a", "b"],
                "metrics": {
                    "walkingPathMeters": 3200,
                    "walkingDurationSeconds": 2700,
                    "roughWalkingMeters": 3000,
                },
                "routeEvaluation": {
                    "status": "routed",
                    "pointIds": ["a", "b"],
                },
            }
        ],
        "excludedPoints": [],
    }


def _spec() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "timezone": "Asia/Tokyo",
        "checkedAt": "2026-07-28",
        "summaryNotes": ["上午先完成宇治站片区"],
        "segments": [
            {
                "from": "宇治站",
                "to": "黄檗站",
                "mode": "rail",
                "operator": "JR西日本",
                "service": "奈良线",
                "durationMinutes": {"min": 12, "max": 18},
                "walkingMinutes": 6,
                "bufferMinutes": 10,
                "fare": {"amount": 190, "currency": "JPY"},
                "cautions": ["周末班次可能减少"],
                "actionItems": ["确认当日班次"],
                "source": {
                    "provider": "JR西日本官方网站",
                    "url": "https://example.com/timetable",
                    "checkedAt": "2026-07-28",
                },
                "confidence": "medium",
            }
        ],
        "globalCautions": ["出发前重新确认运行信息"],
        "actionItems": ["准备 IC 卡"],
    }


class JourneyNotesTests(unittest.TestCase):
    def test_renders_walking_transit_sources_and_cautions(self) -> None:
        markdown = render_journey_notes(_proposal(), _spec())
        self.assertIn("路网步行距离**：3.2 公里", markdown)
        self.assertIn("预计步行时间**：45 分钟", markdown)
        self.assertIn("宇治站 → 黄檗站", markdown)
        self.assertIn("约 12-18 分钟", markdown)
        self.assertIn("[JR西日本官方网站](https://example.com/timetable)", markdown)
        self.assertIn("> 注意：周末班次可能减少", markdown)
        self.assertIn("- [ ] 准备 IC 卡", markdown)

    def test_non_walking_segment_requires_a_source(self) -> None:
        spec = _spec()
        del spec["segments"][0]["source"]
        with self.assertRaisesRegex(JourneyNotesError, "source is required"):
            validate_journey_notes_spec(spec)

    def test_merge_preserves_user_text_and_checked_tasks(self) -> None:
        first = render_journey_notes(_proposal(), _spec())
        existing = merge_managed_journey_notes("用户手写内容", first).replace(
            "- [ ] 准备 IC 卡", "- [x] 准备 IC 卡"
        )
        spec = _spec()
        spec["summaryNotes"] = ["更新后的总览"]
        second = render_journey_notes(_proposal(), spec)
        merged = merge_managed_journey_notes(existing, second)
        self.assertTrue(merged.startswith("用户手写内容"))
        self.assertIn("更新后的总览", merged)
        self.assertNotIn("上午先完成宇治站片区", merged)
        self.assertIn("- [x] 准备 IC 卡", merged)
        self.assertEqual(merged.count(MANAGED_SECTION_START), 1)
        self.assertEqual(merged.count(MANAGED_SECTION_END), 1)

    def test_route_fingerprint_changes_with_route_metrics(self) -> None:
        proposal = _proposal()
        before = route_fingerprint(proposal)
        proposal["groups"][0]["metrics"]["walkingDurationSeconds"] = 2800
        self.assertNotEqual(route_fingerprint(proposal), before)


if __name__ == "__main__":
    unittest.main()
