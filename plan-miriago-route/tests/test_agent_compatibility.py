from __future__ import annotations

import json
import unittest
from pathlib import Path

from miriago_planner import __version__


class AgentCompatibilityTests(unittest.TestCase):
    def test_pi_and_claude_manifests_target_the_canonical_skill(self) -> None:
        skill = Path(__file__).resolve().parents[1]
        repository = skill.parent
        package = json.loads((repository / "package.json").read_text())
        plugin = json.loads((repository / ".claude-plugin" / "plugin.json").read_text())

        self.assertEqual(package["name"], "miriago-route-planner-skill")
        self.assertEqual(package["version"], __version__)
        self.assertEqual(package["pi"]["skills"], ["./plan-miriago-route"])
        self.assertTrue((repository / "plan-miriago-route" / "SKILL.md").is_file())

        self.assertEqual(plugin["name"], "miriago-route-planner-skill")
        self.assertEqual(plugin["version"], __version__)
        self.assertEqual(plugin["skills"], "./")

    def test_route_policy_default_is_recommended_but_never_inferred(self) -> None:
        skill = Path(__file__).resolve().parents[1]
        instructions = (skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Default/recommended: yes", instructions)
        self.assertIn("no answer is recorded automatically", instructions)
        self.assertIn("--road-network yes --time-adjustment no", instructions)


if __name__ == "__main__":
    unittest.main()
