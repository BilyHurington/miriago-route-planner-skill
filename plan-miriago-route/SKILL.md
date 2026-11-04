---
name: plan-miriago-route
description: Collect, merge, curate, review, visualize, and plan MiriaGo v2 pilgrimage data. Use when an AI coding agent needs to create or extend a `.sjhplan` package from a Bangumi work name, Anitabi points, a public Google My Maps link, KML/KMZ, or an existing MiriaGo package; remove duplicate scenes; verify coordinates; divide points into practical areas or day-based route areas; evaluate pedestrian routes on a public or offline OpenStreetMap walking network; plan station entries, exits, and transfers; review points in an interactive page with a local decision store; make auditable time-based point tradeoffs; write reviewed transport, walking-time, and caution notes into the plan memo; bundle reference images; or write an importable MiriaGo package. Enforce explicit user approval at the input, Bangumi selection, source import, curation, area, route-policy, journey-note, route, and export-image checkpoints.
---

# Plan MiriaGo Route

Build a reviewed MiriaGo v2 package from user-provided sources, then divide its
points into areas and visit order. Deterministic scripts collect, validate,
merge, group, and route. Source selection, curation decisions, area
boundaries, names, anchors, and final route order stay with the user.

The workflow is a short **core path** plus **optional modules**. The user
chooses modules once, at the input gate; a module that was not chosen is never
run, asked about, or reported.

Follow the Agent Skills standard on Codex, Pi, and Claude Code. Resolve every
`<skill-directory>` placeholder to the directory containing this `SKILL.md`;
never assume the current working directory is the skill directory. Read
[agent-compatibility.md](references/agent-compatibility.md) only when
installing, publishing, or diagnosing discovery on a supported agent.

## Hard gates

Never infer approval or cross a gate in the same response that asks for it.
Stop and wait for the user's next message at every gate.

1. **Input gate**: collect required inputs and the module choice before any command.
2. **Bangumi gate**: show search candidates and wait for a selected Bangumi ID.
3. **Import gate**: show the full import preview (including any dedupe or
   coordinate-check changes) and wait before confirming it.
4. **Curation gate** (only when points were removed in review): show every
   removal per area and wait before `confirm-curation`.
5. **Area gate**: show every proposed area and wait before planning routes.
6. **Route-policy gate**: after area approval, explicitly ask about pedestrian
   road-network evaluation, time-based tradeoffs, plan-memo journey notes, and
   (stations-assist module) station planning.
7. **Journey-note gate**: when notes were requested, show the complete rendered
   Markdown and wait before confirming it.
8. **Route gate**: show final per-area order and wait before writing the package.
9. **Export-image gate**: before the first package-writing command, ask whether
   to include Anitabi full-resolution reference images and wait for the answer.

The CLI enforces gates 3–9. Do not edit confirmation fields manually; use the
corresponding `confirm-*` command only after explicit user approval. The
required `--full-reference-images include|omit` argument records gate 9.

## Non-negotiable rules

- Support only MiriaGo v2 ZIP packages (`schemaVersion: 2`). Do not guess-convert
  legacy JSON. Proposals are schema 5; schema-4 proposals upgrade on load with
  every recorded approval intact.
- Never overwrite an input package, import draft, or proposal. Write every
  output to a distinct path.
- Use only public services that need no account or API key. Do not copy an API
  token from the MiriaGo source tree.
- Treat Google My Maps as public-only. For a private map, request an exported
  KML/KMZ instead of cookies or login state.
- Preserve records, completion state, assets, notes, unknown JSON fields, and
  unknown ZIP entries when extending or planning an existing package.
- Do not silently pick a same-name Bangumi result, resolve an ambiguous work
  mapping, delete a near duplicate, or approve a review gate.
- Account for every plannable point exactly once: in one group, or in an
  explicit `excludedPoints` record. Keep invalid and MiriaGo pending
  coordinates only in `unassignedPointIds`.
- Never delete a point. Review removals are `excludedPoints` of kind `curation`
  (approved at the curation gate); route tradeoffs are kind `time_tradeoff`
  (approved per point after `--time-adjustment yes`). Both stay in the package
  without a group and remain reversible.
- Preserve all user-written plan memo text. Write journey notes only inside the
  planner's managed Markdown section after the rendered section is approved.
- Require a named source for every non-walking travel segment. Treat user-supplied
  information as `user_confirmed`; never invent a timetable, fare, ride time,
  or disruption. Station assistance supplies geography only.
- Bundle a thumbnail for every point that has a reference image. For Anitabi,
  bundle the full-resolution image only when the user selects `include`. For any
  non-Anitabi external reference image, always bundle both a generated thumbnail
  and the complete image, even when the user selects `omit`.
- Treat a required image download, validation, or thumbnail-generation failure
  as an export-blocking error. Do not produce a knowingly incomplete package.
- Keep long commands attached until they finish. Relay progress from their
  JSONL events on stderr; read only the final result from stdout.
- Previews and the review server are review aids inside existing gates. Their
  stored decisions change nothing until `apply-review` and the curation gate.
- Subagents doing visual or map checks use their own built-in browser, never
  the user's personal browser. Prefer bundled or cached local images; never
  bulk-load full images from Anitabi in a page.

## 0. Pass the input gate

Before running any command, summarize supplied information and ask only for
missing required values:

- **Plan target**: exact existing `.sjhplan` path, or new plan name and area.
- **Point sources**: work names, confirmed Bangumi IDs, public Google My Maps
  URLs, or local KML/KMZ paths.
- **Scope**: all source points or stated inclusions/exclusions.
- **Planning constraints**: density/profile, daily duration, must-visit points,
  fixed start/end points, required approach paths, or defaults allowed.
- **Journey-note inputs when requested**: trip or check date, timezone, known
  inter-area transport, source links, and cautions.
- **Output location**: working directory and final package path.
- **Optional modules** — ask once, offering three presets plus à-la-carte:

| Preset | Modules |
|---|---|
| Quick | none (core path only) |
| Standard (recommended for multi-day trips) | `osm-network`, `stations-assist`, `review-preview` |
| Full review | Standard + `dedupe`, `verify`, `classify`, `review-server`, `day-route`, `special-groups` |

Module meanings are in [optional-modules.md](references/optional-modules.md).
Record the choice with `--modules a,b,c` on `propose` or `plan-days`.

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

Create an import proposal from the confirmed sources:

```bash
python3 <skill-directory>/scripts/miriago_plan.py prepare-import \
  --input INPUT.sjhplan \
  --anitabi-bangumi-id BANGUMI_ID \
  --my-maps "PUBLIC_URL_OR_KML" \
  --output WORK/import-draft.json
```

To create a package, omit `--input` and add `--plan-name` and `--area`. Repeat
source options to combine works or maps. Read
[source-imports.md](references/source-imports.md) before reviewing work
mappings, duplicates, KML geometry conversion, or provider failures.

### 1a. Optional: dedupe and coordinate checks (`dedupe`, `verify`)

These modules edit the **import draft** before the import gate, so removed
scenes never enter the package. Formats are in
[curation-formats.md](references/curation-formats.md).

```bash
python3 <skill-directory>/scripts/miriago_plan.py dedupe-candidates \
  WORK/import-draft.json --output-dir WORK/dedupe
# compare each group (subagents or WORK/dedupe/dedupe-review.html) → decisions.json
python3 <skill-directory>/scripts/miriago_plan.py apply-dedupe \
  WORK/import-draft.json --candidates WORK/dedupe/candidates.json \
  --decisions WORK/dedupe/decisions.json --output WORK/import-deduped.json

python3 <skill-directory>/scripts/miriago_plan.py verify-batches \
  WORK/import-deduped.json --output-dir WORK/verify
# each batch: check satellite/street imagery, write WORK/verify/Vnn/verify.json
python3 <skill-directory>/scripts/miriago_plan.py apply-verify \
  WORK/import-deduped.json --batches-dir WORK/verify \
  --special-groups-output WORK/unverified.json --output WORK/import-checked.json
```

Dropped scenes become `selected: false`; the kept scene's note lists where else
it appears. Moved coordinates keep the original in the note. Thumbnails come
from a given `--package` or a one-time cached download per point
(`--no-download` to forbid network).

## 2. Pass the import gate

Open the latest import draft and report every source and fetched point count;
each `workMappings` label, suggested work, and selected work; selected/excluded
counts; exact duplicates skipped; dedupe and coordinate changes; and every
warning. For multiple My Maps layers, require the user to confirm each
layer-to-work mapping. Validate:

```bash
python3 <skill-directory>/scripts/miriago_plan.py validate-import \
  WORK/import-draft.json --input INPUT.sjhplan --draft
```

**Stop at the import gate.** After explicit approval:

```bash
python3 <skill-directory>/scripts/miriago_plan.py confirm-import \
  WORK/import-draft.json --input INPUT.sjhplan \
  --output WORK/import-confirmed.json
```

Report the number of Anitabi and non-Anitabi reference images, then ask:
"Should the package include full-resolution Anitabi reference images?"
**Stop at the export-image gate.** Thumbnails are always included and
non-Anitabi external images always include both thumbnail and complete image.

```bash
python3 <skill-directory>/scripts/miriago_plan.py apply-import \
  WORK/import-confirmed.json --input INPUT.sjhplan \
  --full-reference-images include \
  --image-download-workers 8 \
  --output WORK/combined.sjhplan
```

Use `omit` only when the user declines. Reuse the choice for the final package.
Omit `--input` for a new plan.

## 3. Generate areas

Choose the strategy the user selected:

**Clustered areas (core).** Adaptive MST clustering with `balanced` unless the
constraints call for `compact` or `broad`:

```bash
python3 <skill-directory>/scripts/miriago_plan.py propose WORK/combined.sjhplan \
  --output WORK/areas-draft.json --profile balanced \
  [--bundle-meters 30] [--special-groups WORK/special-groups.json] \
  [--modules review-preview]
```

**Day-route areas (`day-route`).** Each pool is walked as one optimised open
path (fixed or station start/end, via points such as a required trail), split
into days at station-friendly gaps, and cut into contiguous areas of a target
duration — so areas never cross and never detour to one far point. Write the
config described in [day-route-config.md](references/day-route-config.md):

```bash
python3 <skill-directory>/scripts/miriago_plan.py plan-days WORK/combined.sjhplan \
  --config WORK/day-config.json --output WORK/areas-draft.json \
  --geometry-output WORK/areas-geometry.json \
  --modules osm-network,stations-assist,review-preview
```

`special-groups` keeps restricted sites (open days only) and unverified
locations as their own groups that are never routed. Read
[planning-policy.md](references/planning-policy.md) for boundary review and
[public-services.md](references/public-services.md) before using online data.

### 3a. Optional: review hints and interactive review

`classify` prepares per-area batches for keep/optional/drop hints (prefer
shootable shots and famous scenes); results never change membership:

```bash
python3 <skill-directory>/scripts/miriago_plan.py classify-batches \
  WORK/combined.sjhplan WORK/areas-draft.json --output-dir WORK/classify
python3 <skill-directory>/scripts/miriago_plan.py merge-classify \
  WORK/combined.sjhplan WORK/areas-draft.json --batches-dir WORK/classify \
  --output WORK/areas-hints.json
```

`review-preview` renders images, links (Anitabi, Google Earth, satellite, Street
View), expandable point lists, hint filters, and a quick-classification panel
(image left, map right, strip below; ←/→, K keep, D delete, U clear, [ ]
switch area). `review-server` stores every decision in a temporary SQLite file
with history and serves full images from the package:

```bash
python3 <skill-directory>/scripts/miriago_plan.py render-preview \
  WORK/combined.sjhplan WORK/areas-hints.json --mode review \
  --geometry-file WORK/areas-geometry.json --output WORK/review/areas.html
python3 <skill-directory>/scripts/miriago_plan.py serve-review \
  WORK/review/areas.html --db WORK/review.sqlite \
  --package WORK/combined.sjhplan --port 8791
```

Run the server in the background and give the user its URL. Without the
server, the page keeps decisions in the browser and exports them as JSON.
When the user says the review is finished, turn the decisions into a draft:

```bash
python3 <skill-directory>/scripts/miriago_plan.py apply-review \
  WORK/combined.sjhplan WORK/areas-hints.json --db WORK/review.sqlite \
  --output WORK/areas-curated.json
```

Present removals and restorations per area. **Stop at the curation gate.**
After explicit approval run `confirm-curation ... --output
WORK/areas-curation-confirmed.json`. Re-running `apply-review` later restores
points whose decision is no longer `delete`.

## 4. Pass the area gate

Review every group: replace placeholder names, inspect low-confidence edges,
confirm membership, and choose a point/station/custom anchor. Render the area
preview (compact or review mode) and present the preview path plus every
area's name, day, point count, anchor, planned walk, quality warnings
(crossings, single-point detours), special groups, and unassigned points.
**Stop at the area gate.** After explicit approval:

```bash
python3 <skill-directory>/scripts/miriago_plan.py confirm-proposal \
  WORK/combined.sjhplan WORK/areas-draft.json --stage areas \
  --output WORK/areas-confirmed.json
```

## 5. Pass the route-policy gate

After recording area approval, ask these questions together:

1. "Should I use the pedestrian road network to re-estimate and accurately
   evaluate every area route? (Default/recommended: yes)" — with `osm-network`,
   also say whether the offline OpenStreetMap network or public Valhalla is used.
2. "Should I make tradeoffs or adjust route duration, such as dropping isolated
   points or points far from the other areas?"
3. "Should I write inter-area transport, estimated walking time, and cautions
   into the MiriaGo plan memo?"
4. (`stations-assist` only) "Should I plan station entries/exits for each day
   and area and mark rail transfer candidates between areas?"

**Stop at the route-policy gate.** The recommended default for road-network
evaluation is `yes`, but no answer is recorded automatically. This gate
concerns pedestrian routing only; do not introduce a driving model.

```bash
python3 <skill-directory>/scripts/miriago_plan.py confirm-route-policy \
  WORK/combined.sjhplan WORK/areas-confirmed.json \
  --road-network yes --time-adjustment no --plan-notes yes \
  [--network-backend osm] [--stations-assist yes] \
  --output WORK/route-policy-confirmed.json
```

Add `--time-budget-minutes` and `--tradeoff-notes` when the user wants duration
adjustments. A `yes` permits review; it never approves a specific removal.

## 6. Plan routes

**Station assistance (`stations-assist`).** Fill missing day starts/ends and
area transitions with stations; existing ends (for example a summit finish)
are kept. Consecutive areas within walking distance continue on foot; others
get `rail_candidate` (shared OSM line) or `transit_unknown`:

```bash
python3 <skill-directory>/scripts/miriago_plan.py assist-stations \
  WORK/combined.sjhplan WORK/route-policy-confirmed.json \
  --output WORK/stations.json
```

**Road-network evaluation.** When accepted, refine every group; route ends,
via points, and the previous area's last stop are honoured, and each group
stores `routed`, `skipped`, or `failed` evidence plus quality checks:

```bash
python3 <skill-directory>/scripts/miriago_plan.py refine \
  WORK/combined.sjhplan WORK/stations.json --all-groups \
  --geometry-output WORK/routes-geometry.json \
  --output WORK/routes-draft.json
```

The backend defaults to the one approved at the policy gate (`--walknet-file`
uses local Overpass JSON offline). Valhalla is tiled to its public limits and
skips groups above 58 stops with a reason; the OSM backend has no such limit.
Use local order only when road-network evaluation was declined.

Present each area's ordered points, start/end and station access, walking
distance and duration evidence, crossings and single-point detours, skipped or
failed refinements, and isolated points. Never describe straight-line
estimates as routed walking distance.

If time adjustment was accepted, propose concrete reordering, budget, and
exclusion choices. **Obtain explicit approval for every point to exclude and
its reason**, then:

```bash
python3 <skill-directory>/scripts/miriago_plan.py exclude-points \
  WORK/combined.sjhplan WORK/routes-draft.json \
  --exclude-point POINT_ID "USER-APPROVED REASON" \
  --output WORK/routes-adjusted.json
```

Refine any changed group again.

## 7. Prepare notes and pass the review gates

When plan-note writing was accepted, read
[journey-notes-format.md](references/journey-notes-format.md). With station
assistance, start from a skeleton whose rail segments carry stations and line
candidates but no times or fares:

```bash
python3 <skill-directory>/scripts/miriago_plan.py draft-notes-spec \
  WORK/combined.sjhplan WORK/routes-draft.json \
  --checked-at YYYY-MM-DD --timezone Asia/Tokyo --output WORK/journey-notes.json
```

Complete it only from official sources or user-confirmed details, then:

```bash
python3 <skill-directory>/scripts/miriago_plan.py prepare-notes \
  WORK/combined.sjhplan WORK/FINAL-ROUTE-DRAFT.json WORK/journey-notes.json \
  --output WORK/notes-draft.json
```

Present the complete `memoPreview`. **Stop at the journey-note gate.** After
explicit approval run `confirm-notes ... --output WORK/notes-confirmed.json`.
Any later membership, order, route-metric, station, or exclusion change makes
the notes stale; regenerate and review them.

Render the final review preview (`--geometry-file WORK/routes-geometry.json`
for offline routes; `--geometry auto` fetches Valhalla shapes for Valhalla
evidence) and present the preview path, final route, tradeoffs with reasons,
station plan, and note status. **Stop at the route gate.** After approval:

```bash
python3 <skill-directory>/scripts/miriago_plan.py confirm-proposal \
  WORK/combined.sjhplan WORK/notes-confirmed.json --stage routes \
  --output WORK/routes-confirmed.json
```

## 8. Pass the export-image gate, write, and verify

Reuse or ask the export-image choice, then:

```bash
python3 <skill-directory>/scripts/miriago_plan.py apply \
  WORK/combined.sjhplan WORK/routes-confirmed.json \
  --full-reference-images include \
  --image-download-workers 8 \
  --output OUTPUT-planned.sjhplan
python3 <skill-directory>/scripts/miriago_plan.py validate \
  OUTPUT-planned.sjhplan --source WORK/combined.sjhplan
```

Report the final path, source/import counts, groups by kind and day, assigned,
unassigned, and excluded counts by kind with reasons, image policy and asset
counts, network backend, station plan, journey-note status, public-service
status, unresolved warnings, and confirmation that input entries were
preserved. Read [miriago-v2-schema.md](references/miriago-v2-schema.md) when
diagnosing package or preservation failures.

## Troubleshooting

- On a source hash mismatch, regenerate the proposal from the current input.
- On a closed gate error, return to that review; never set approval to pass
  validation.
- On a stale route evaluation (route ends changed after routing), run `refine`
  again for that group.
- On a missing route-evaluation error, refine every current group or present the
  failure and ask whether the user wants to change the route policy.
- On a time tradeoff, require `--time-adjustment yes` and a non-empty,
  user-approved reason. Never delete the point from the package.
- On a curation gate error, show the removals and run `confirm-curation` only
  after approval.
- On a stale journey-note error, regenerate notes and pass the gate again.
- On a malformed managed memo section, stop rather than overwrite user text.
- On an Anitabi partial-points error, stop instead of importing a truncated work.
- On a private or invalid My Maps response, request KML/KMZ from the user.
- On an unresolved `selectedWorkId`, show the available works and ask the user.
- On a required image failure, stop and identify the point and URL.
- Overpass may answer 429/502/504 under load; the CLI retries briefly, then
  reports it. Use `--walknet-file` / `--station-file` (or a config's
  `stations.files`) with saved Overpass JSON to continue offline.
- Monitor `image_download_*`, `route_preview_*`, `walknet_progress`,
  `day_plan_progress`, and `refine_progress` JSON events on stderr.
- If an online preview basemap is unavailable or must not be requested,
  regenerate with `--no-basemap`.
- Install `scripts/requirements.txt` for thumbnails and contact sheets.
- Use `--force` only when the user explicitly intends to replace that output;
  it can never replace the input package.
