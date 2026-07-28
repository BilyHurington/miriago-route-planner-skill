#!/usr/bin/env python3
"""Build a planned package for MiriaGo's own Dart importer integration check."""

from __future__ import annotations

import sys
from pathlib import Path

from miriago_planner.clustering import profile_with_overrides
from miriago_planner.package_io import load_package, rebuild_package
from miriago_planner.proposal import apply_proposal, generate_proposal

from tests.helpers import point, write_package


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: build_integration_fixture.py OUTPUT_DIRECTORY")
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = write_package(
        directory / "source.sjhplan",
        [
            point("tokyo-1", 35.6812, 139.7671, name="东京站"),
            point("tokyo-2", 35.6840, 139.7700, name="丸之内"),
            point("kyoto-1", 35.0116, 135.7681, name="京都御苑"),
            point("pending", -90, 0, name="待定位"),
        ],
    )
    package = load_package(source)
    proposal = generate_proposal(package, profile_with_overrides("balanced"))
    for index, group in enumerate(proposal["groups"]):
        group["name"] = ("东京站・丸之内", "京都御苑")[min(index, 1)]
    proposal["review"]["areasConfirmed"] = True
    proposal["review"]["routesConfirmed"] = True
    output = directory / "planned.sjhplan"
    rebuild_package(
        package,
        apply_proposal(package, proposal),
        output,
        planner_version="integration-test",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
