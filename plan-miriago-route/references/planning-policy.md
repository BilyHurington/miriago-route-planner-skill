# Planning policy

## Objective

The first pass should create defensible, walking-sized areas from as many as 200
points. It is not a full itinerary optimizer. Exact opening hours, train
schedules, accessibility, trip dates, and user accommodation belong to later
detailed planning.

## Local deterministic method

1. Validate coordinates and set unusable points aside.
2. Bundle points within 12–20 metres so duplicate or near-identical scenes do
   not distort density.
3. Compute pairwise great-circle (Haversine) distances.
4. Estimate each stop's local scale from nearby neighbors.
5. Build a minimum spanning tree over mutual-reachability distance.
6. Cut a tree edge when it crosses the profile's hard distance or is a robust
   density gap relative to both local neighborhoods.
7. Recursively split any result exceeding point-count, P90-radius, or diameter
   limits.
8. Merge adjacent small results only when the combined group still satisfies
   every hard limit.
9. Order groups by medoids and order each group's stops with a deterministic
   nearest-neighbor plus 2-opt open path.

This adapts to dense city centers and sparse outskirts without asking for a
fixed group count.

## Profiles

| Profile | Maximum points | P90 radius | Diameter | Hard link |
|---|---:|---:|---:|---:|
| compact | 15 | 650 m | 1,500 m | 800 m |
| balanced | 25 | 1,100 m | 2,600 m | 1,400 m |
| broad | 35 | 2,000 m | 4,800 m | 2,600 m |

These are first-pass guardrails, not promises about actual walking distance.

## Distance interpretation

- `directPathMeters`: sum of Haversine segments along the local order.
- `roughWalkingMeters`: `directPathMeters × 1.25`.
- `walkingPathMeters`: street-network matrix result, present only after
  successful Valhalla refinement.

The 1.25 factor is an explicitly labeled heuristic. Rivers, rail yards,
expressways, restricted sites, elevation, and building entrances can make it
wrong. Never describe it as a routed distance.

## Route-policy review

After area approval and before route planning, obtain two explicit decisions:

1. whether to evaluate routes with the pedestrian road network;
2. whether to make route-time tradeoffs or duration adjustments.

Recommend road-network evaluation and present `yes` as the default choice, but
never infer it from silence or continue in the same response that asks. Record
both decisions with `confirm-route-policy`. This workflow does not select a
driving or other travel model.

When road-network evaluation is accepted, every current group needs persisted
evidence before final route confirmation. Evidence is one of:

- `routed`, with the evaluated point order, walking distance, and duration;
- `skipped`, with a structural reason such as a singleton or matrix size limit;
- `failed`, with the public-service failure reason.

A changed point order or membership invalidates evidence for that group. Refine
it again before route confirmation. Present every skip and failure to the user;
evidence records the limitation and does not turn it into an accurate distance.

## Time-based tradeoffs

A `yes` decision permits proposing changes; it does not authorize a particular
point exclusion. Use the optional time budget and notes as review constraints.
Flag isolated groups, large detours, and points far from the rest, then show the
specific effect of keeping or excluding them.

Exclude a point only after the user approves both its ID and a non-empty reason.
Store the decision in `excludedPoints` with its prior group and order. Remove it
from route membership, but keep the complete point in the plan with no group
assignment. Never use `unassignedPointIds` for this purpose; that field remains
reserved for invalid or pending coordinates.

After a tradeoff, recompute the affected group's local metrics and repeat road
evaluation when it was requested. The final report must list all excluded points
and reasons so the decision remains reversible and auditable.

## Agent review cues

Review a group more carefully when:

- confidence is low;
- P90 radius or diameter is close to its profile limit;
- only one or two points form a remote group;
- a boundary crosses water, a rail yard, a mountain, or a limited-access site;
- station candidates are far away or have only entrance-level data;
- two neighboring groups share a more natural district or transit hub.

Moving a boundary point is acceptable when evidence improves usability and all
hard invariants still hold. Do not force a remote point into an urban group just
to avoid a singleton.

## Naming

Prefer stable, recognizable names:

1. district plus landmark;
2. station plus side/neighborhood;
3. municipality plus subarea;
4. a major landmark when it truly represents the group.

Names must be unique. Avoid numeric placeholders, work titles that do not
identify a place, and a station name that is not actually useful for accessing
the group.
