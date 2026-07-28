#!/usr/bin/env python3
"""Plan a real MiriaGo export with deterministic integration-only names."""

from __future__ import annotations

import sys
from pathlib import Path

from miriago_planner.clustering import profile_with_overrides
from miriago_planner.package_io import load_package, rebuild_package
from miriago_planner.proposal import apply_proposal, generate_proposal


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: plan_integration_package.py INPUT OUTPUT")
    source = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    package = load_package(source)
    proposal = generate_proposal(package, profile_with_overrides("balanced"))
    for index, group in enumerate(proposal["groups"]):
        medoid = group["medoid"]["pointId"]
        point_name = next(
            point.get("name") or point["id"]
            for point in package.plan["points"]
            if point["id"] == medoid
        )
        group["name"] = f"{point_name}周边（兼容性验证 {index + 1}）"
    proposal["review"]["areasConfirmed"] = True
    proposal["review"]["routesConfirmed"] = True
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
