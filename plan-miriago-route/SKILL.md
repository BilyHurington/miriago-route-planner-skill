---
name: plan-miriago-route
description: Collect, merge, review, and plan MiriaGo v2 pilgrimage data. Use when an AI coding agent needs to create or extend a `.sjhplan` package from a Bangumi work name, Anitabi points, a public Google My Maps link, KML/KMZ, or an existing MiriaGo package; divide points into practical areas; review boundaries; evaluate pedestrian road-network routes; make auditable time-based point tradeoffs; write reviewed transport, walking-time, and caution notes into the plan memo; bundle reference images; or write an importable MiriaGo package. Enforce explicit user approval at the input, Bangumi selection, source import, area, route-policy, journey-note, route, and export-image checkpoints.
---

# Plan MiriaGo Route

Build a reviewed MiriaGo v2 package from user-provided sources, then divide its
points into geographic areas and visit order. Use deterministic scripts for
collection, validation, merging, and first-pass grouping. Reserve source
selection, work mapping, area boundaries, names, anchors, and final route order
for explicit review.

Follow the Agent Skills standard on Codex, Pi, and Claude Code. Resolve every
`<skill-directory>` placeholder below to the directory containing this
`SKILL.md`; never assume the current working directory is the skill directory.
Read [agent-compatibility.md](references/agent-compatibility.md) only when
installing, publishing, or diagnosing discovery on a supported agent.

## Hard gates

Never infer approval or cross a gate in the same response that asks for it.
Stop and wait for the user's next message at every gate.

1. **Input gate**: collect all missing required inputs before running a command.
2. **Bangumi gate**: show search candidates and wait for a selected Bangumi ID.
3. **Import gate**: show the full import preview and wait before confirming it.
4. **Area gate**: show every proposed area and wait before planning routes.
5. **Route-policy gate**: after area approval, explicitly ask about pedestrian
   road-network evaluation, time-based tradeoffs, and plan-memo journey notes.
6. **Journey-note gate**: when notes were requested, show the complete rendered
   Markdown and wait before confirming it.
7. **Route gate**: show final per-area order and wait before writing the package.
8. **Export-image gate**: before the first package-writing command, ask whether
   to include Anitabi full-resolution reference images and wait for the answer.

The CLI enforces gates 3-8. Do not edit confirmation fields manually; use the
corresponding `confirm-*` command only after explicit user approval. The required
`--full-reference-images include|omit` argument records gate 8.

## Non-negotiable rules

- Support only MiriaGo v2 ZIP packages (`schemaVersion: 2`). Do not guess-convert
  legacy JSON.
- Never overwrite an input package. Write every proposal and output to a
  distinct path.
- Use only public services that need no account or API key. Do not copy an API
  token from the MiriaGo source tree.
- Treat Google My Maps as public-only. For a private map, request an exported
  KML/KMZ instead of cookies or login state.
- Preserve records, completion state, assets, notes, unknown JSON fields, and
  unknown ZIP entries when extending or planning an existing package.
- Do not silently pick a same-name Bangumi result, resolve an ambiguous work
  mapping, delete a cross-source near duplicate, or approve a review gate.
- Account for every plannable point exactly once, either in one group or in an
  explicit `excludedPoints` record with a user-approved reason. Keep invalid and
  MiriaGo pending coordinates only in `unassignedPointIds`.
- Never silently drop an isolated or distant point. Keep excluded points in the
  source plan and final package with no group assignment so the decision remains
  reversible and auditable.
- Preserve all user-written plan memo text. Write journey notes only inside the
  planner's managed Markdown section after the rendered section is approved.
- Require a named source for every non-walking travel segment. Treat user-supplied
  information as `user_confirmed`; never invent a timetable, fare, or disruption.
- Bundle a thumbnail for every point that has a reference image. For Anitabi,
  bundle the full-resolution image only when the user selects `include`. For any
  non-Anitabi external reference image, always bundle both a generated thumbnail
  and the complete image, even when the user selects `omit`.
- Treat a required image download, validation, or thumbnail-generation failure
  as an export-blocking error. Do not produce a knowingly incomplete package.
- Keep `apply-import` and `apply` attached until they finish. Monitor their
  JSONL image-download events on stderr and relay useful progress during a long
  export. Read only the final command result from stdout.

## 0. Pass the input gate

Before running any command, summarize supplied information and ask only for
missing required values:

- **Plan target**: exact existing `.sjhplan` path, or new plan name and area.
- **Point sources**: one or more work names, confirmed Bangumi IDs, public
  Google My Maps URLs, or local KML/KMZ paths.
- **Scope**: all source points or stated inclusions/exclusions.
- **Planning constraints**: desired density/profile, daily point limit,
  must-visit points, fixed start/end points, or state that defaults are allowed.
- **Journey-note inputs when requested**: trip date or check date, timezone,
  known inter-area transport, source links, and cautions.
- **Output location**: working directory and final package path.

If any required value is missing, ask for it and stop. Do not inspect, search,
download, cluster, or create a proposal before this gate passes.

## 1. Resolve works and collect sources

For each work supplied by name, search Bangumi:

```bash
python3 <skill-directory>/scripts/miriago_plan.py search-work "WORK NAME" \
  --type anime --type game
```

Present candidate ID, title, original title, type, and date. **Stop at the
Bangumi gate.** Continue only with the ID explicitly selected by the user.

Create an import proposal from any confirmed sources. To extend a package:

```bash
python3 <skill-directory>/scripts/miriago_plan.py prepare-import \
  --input INPUT.sjhplan \
  --anitabi-bangumi-id BANGUMI_ID \
  --my-maps "PUBLIC_URL_OR_KML" \
  --output WORK/import-draft.json
```

To create a package, omit `--input` and add `--plan-name` and `--area`. Repeat
source options to combine multiple works or maps.

Read [source-imports.md](references/source-imports.md) before reviewing work
mappings, duplicates, KML geometry conversion, or provider failures.

## 2. Pass the import gate

Open `import-draft.json` and report:

- every source and fetched point count;
- each `workMappings` source label, suggested work, and selected work;
- selected/excluded point counts;
- exact duplicates skipped;
- every conversion, partial-data, or near-duplicate warning.

For multiple My Maps layers, require the user to confirm each layer-to-work
mapping. Edit `selectedWorkId` or point `selected` values as requested, then run
draft validation:

```bash
python3 <skill-directory>/scripts/miriago_plan.py validate-import \
  WORK/import-draft.json --input INPUT.sjhplan --draft
```

Omit `--input` for a new plan. **Stop at the import gate.** After explicit
approval, record it to a new file:

```bash
python3 <skill-directory>/scripts/miriago_plan.py confirm-import \
  WORK/import-draft.json --input INPUT.sjhplan \
  --output WORK/import-confirmed.json
```

Before applying, report the number of Anitabi and non-Anitabi reference images,
then ask: "Should the package include full-resolution Anitabi reference images?"
**Stop at the export-image gate.** Explain that thumbnails are always included
and non-Anitabi external images always include both thumbnail and complete image.
Do not infer the answer from package size or prior defaults.

Apply only the confirmed proposal with the explicit answer:

```bash
python3 <skill-directory>/scripts/miriago_plan.py apply-import \
  WORK/import-confirmed.json --input INPUT.sjhplan \
  --full-reference-images include \
  --image-download-workers 8 \
  --output WORK/combined.sjhplan
```

Use `omit` instead of `include` only when the user declines Anitabi full images.
Reuse this explicit choice for the final package unless the user changes it.
Use 8 concurrent image downloads by default; lower
`--image-download-workers` when the source limits concurrency, or use 1 to
diagnose request ordering. The accepted range is 1-32.

Omit `--input` in both commands for a new plan. Inspect the combined package and
report its works, points, warnings, and any unusable coordinates.

## 3. Generate and review areas

Run `inspect`, then create a local draft with `balanced` unless the user's
constraints indicate `compact` or `broad`:

```bash
python3 <skill-directory>/scripts/miriago_plan.py inspect INPUT.sjhplan
python3 <skill-directory>/scripts/miriago_plan.py propose INPUT.sjhplan \
  --output WORK/areas-draft.json --profile balanced
```

Use `--fetch-stations` only when public station candidates help with area names
or anchors. Service failure must not block the local draft.

Read [planning-policy.md](references/planning-policy.md) for boundary review and
[public-services.md](references/public-services.md) before using online data.

Review every group: replace placeholder names, inspect low-confidence edges,
confirm membership, and choose a point/station/custom anchor. Present every
area's name, point count, anchor, notable boundary decisions, and all unassigned
points. **Stop at the area gate.** Do not reorder routes or call `refine` yet.

After explicit area approval, record it:

```bash
python3 <skill-directory>/scripts/miriago_plan.py confirm-proposal \
  INPUT.sjhplan WORK/areas-draft.json --stage areas \
  --output WORK/areas-confirmed.json
```

## 4. Pass the route-policy gate

After recording area approval, ask these three questions together:

1. "Should I use the pedestrian road network to re-estimate and accurately
   evaluate every area route? (Default/recommended: yes)"
2. "Should I make tradeoffs or adjust route duration, such as dropping isolated
   points or points far from the other areas?"
3. "Should I write inter-area transport, estimated walking time, and cautions
   into the MiriaGo plan memo?"

**Stop at the route-policy gate.** The recommended default for road-network
evaluation is `yes`, but no answer is recorded automatically. Require an
explicit answer to all three questions. This gate concerns pedestrian routing only;
do not introduce a driving or other travel model.

Record both answers only after the user responds:

```bash
python3 <skill-directory>/scripts/miriago_plan.py confirm-route-policy \
  INPUT.sjhplan WORK/areas-confirmed.json \
  --road-network yes --time-adjustment no --plan-notes yes \
  --output WORK/route-policy-confirmed.json
```

When the user wants duration adjustments, also record an optional budget and
their constraints with `--time-budget-minutes MINUTES` and
`--tradeoff-notes "NOTES"`. A `yes` answer permits review; it does not approve
any specific point removal.

## 5. Plan routes only after route-policy approval

Plan the order inside each confirmed area without changing membership. Use the
local order only when road-network evaluation was declined. When it was
accepted, pass every group ID to `refine`, including singleton or oversized
groups so their explicit skip reason is stored:

```bash
python3 <skill-directory>/scripts/miriago_plan.py refine \
  INPUT.sjhplan WORK/route-policy-confirmed.json \
  --group-id GROUP_ID_1 --group-id GROUP_ID_2 \
  --output WORK/routes-draft.json
```

Valhalla refines groups of 2-25 points. The proposal stores `routed`, `skipped`,
or `failed` evidence for every requested group. Route confirmation is blocked
when road-network evaluation was accepted but any current group has no evidence.

Review the route output again. Present each area's ordered points, start/end,
distance and duration evidence, skipped or failed refinements, isolated/distant
points, and unresolved issues. Do not describe straight-line estimates as routed
walking distance.

If time adjustment was accepted, propose concrete reordering, time-budget, and
point-exclusion choices. **Stop and obtain explicit approval for every point to
exclude and its reason.** Then record approved exclusions without deleting the
source points:

```bash
python3 <skill-directory>/scripts/miriago_plan.py exclude-points \
  INPUT.sjhplan WORK/routes-draft.json \
  --exclude-point POINT_ID "USER-APPROVED REASON" \
  --output WORK/routes-adjusted.json
```

Repeat `--exclude-point` as needed. If an exclusion changes a routed group, run
`refine` again for that group.

## 6. Prepare notes and pass the review gates

When plan-note writing was accepted, read
[journey-notes-format.md](references/journey-notes-format.md). Create a
structured JSON file from user-confirmed information and cited public sources.
Do not scrape closed route-planning sites or infer missing fares and timetables.
The renderer automatically adds available per-area walking distances and routed
walking times from the current proposal.

Render a memo draft only after route order and exclusions are stable:

```bash
python3 <skill-directory>/scripts/miriago_plan.py prepare-notes \
  INPUT.sjhplan WORK/FINAL-ROUTE-DRAFT.json WORK/journey-notes.json \
  --output WORK/notes-draft.json
```

Present the complete `memoPreview` returned by the command, including preserved
user text and all generated source,
freshness, confidence, and unavailable-time labels. **Stop at the journey-note
gate.** After explicit approval, record it:

```bash
python3 <skill-directory>/scripts/miriago_plan.py confirm-notes \
  INPUT.sjhplan WORK/notes-draft.json \
  --output WORK/notes-confirmed.json
```

Any later membership, order, route-metric, or exclusion change makes the notes
stale. Regenerate and review them instead of bypassing the fingerprint error.
When note writing was declined, skip both note commands.

Present the final route, tradeoffs, and approved note status. **Stop at the route
gate.** Do not combine a request for approval with route confirmation.

After explicit approval, record it:

```bash
python3 <skill-directory>/scripts/miriago_plan.py confirm-proposal \
  INPUT.sjhplan WORK/notes-confirmed.json --stage routes \
  --output WORK/routes-confirmed.json
```

Use `FINAL-ROUTE-DRAFT.json` instead when plan-note writing was declined. If
road-network evaluation was also declined, this can be the route-policy-confirmed
proposal after completing the manual order review.

## 7. Pass the export-image gate, write, and verify

If no package-writing command has run yet, pass the export-image gate now. If a
combined package was already written, restate and reuse its explicit choice.
Apply only a route-confirmed proposal:

```bash
python3 <skill-directory>/scripts/miriago_plan.py apply \
  INPUT.sjhplan WORK/routes-confirmed.json \
  --full-reference-images include \
  --image-download-workers 8 \
  --output OUTPUT-planned.sjhplan
python3 <skill-directory>/scripts/miriago_plan.py validate \
  OUTPUT-planned.sjhplan --source INPUT.sjhplan
```

Report the final path, source/import counts, group, assigned, unassigned, and
explicitly excluded counts and reasons, image policy and asset counts,
profile and overrides, journey-note status, public-service status, unresolved warnings, and
confirmation that the input entries were preserved. Confirm that every
image-bearing point has a bundled thumbnail and every non-Anitabi external image
has a bundled complete image.

Read [miriago-v2-schema.md](references/miriago-v2-schema.md) when diagnosing
package or preservation failures.

## Troubleshooting

- On a source hash mismatch, regenerate the proposal from the current input.
- On a closed gate error, return to the corresponding review; never set approval
  just to make validation pass.
- On a missing route-evaluation error, refine every current group or present the
  failure and ask whether the user wants to change the route policy.
- On a point tradeoff, require both `--time-adjustment yes` and a non-empty,
  user-approved reason. Never delete the point from the package.
- On a stale journey-note error, regenerate notes from the current reviewed
  route and pass the journey-note gate again.
- On a malformed managed memo section, stop rather than overwrite user text.
- On an Anitabi partial-points error, stop instead of importing a truncated work.
- On a private or invalid My Maps response, request KML/KMZ from the user.
- On an unresolved `selectedWorkId`, show the available works and ask the user.
- On a required image failure, stop and identify the point and URL. Do not retry
  export with missing assets or reinterpret `omit` as permission to omit
  thumbnails or non-Anitabi complete images.
- While images download, monitor `image_download_start`,
  `image_download_progress`, and `image_download_complete` JSON events on
  stderr. A progress event on stderr is informational unless its `status` is
  `failed`; stdout remains reserved for the final result.
- Install `scripts/requirements.txt` before exporting non-Anitabi images when
  Pillow is unavailable; it is required to generate actual thumbnails.
- Use `--force` only when the user explicitly intends to replace that output;
  it can never replace the input package.
