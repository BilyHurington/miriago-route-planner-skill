# Curation formats

Batch commands write `input.json` (plus thumbnails and numbered contact sheets
when Pillow is available). The agent or a subagent writes exactly one result
file per batch. Unknown point or stop IDs are rejected when results are applied.

## Dedupe (`dedupe-candidates` → `apply-dedupe`)

`candidates.json` lists groups of selected points within `--meters` of each
other (complete linkage). Decide per group which scenes to keep — prefer the
shot that can actually be reproduced on site and the scene that is famous in
the work — and write:

```json
{
  "schemaVersion": 1,
  "groups": {
    "D001": {"keep": ["anitabi-1-a"], "drop": ["anitabi-1-b"], "note": "同一构图"}
  }
}
```

Every group needs at least one kept point when anything is dropped. Omitted
groups are unchanged. `dedupe-review.html` is a self-contained picker that
exports this file.

## Coordinate check (`verify-batches` → `apply-verify`)

Each `Vnn/input.json` has stops (points within `--meters`) with links to Google
Earth, satellite view, and Street View. Write `Vnn/verify.json`:

```json
{
  "stops": [
    {"stopId": 1, "status": "ok"},
    {"stopId": 2, "status": "offset", "suggestedLat": 34.89, "suggestedLng": 135.80,
     "detail": "桥在道路北侧"},
    {"stopId": 3, "status": "unverifiable", "detail": "室内画面"}
  ],
  "points": [
    {"pointId": "anitabi-1-c", "suggestedLat": 34.9, "suggestedLng": 135.8,
     "suggestedName": "朝雾桥东诘", "detail": "单点修正", "status": "offset"}
  ]
}
```

`status` is `ok`, `offset`, `wrong`, or `unverifiable`. Point entries override
their stop. `unverifiable` points can be written to a special-group file with
`--special-groups-output`.

## Classification (`classify-batches` → `merge-classify`)

Each `Cnn/input.json` groups an area's points into stops. Write
`Cnn/classify.json`:

```json
{
  "points": {
    "anitabi-1-a": {"class": "keep", "famous": true, "reason": "名场面，现场可复现",
                    "similarTo": ["anitabi-1-b"]},
    "anitabi-1-b": {"class": "drop", "reason": "与 a 同构图，画面更暗"}
  }
}
```

`class` is `keep`, `optional`, or `drop`. Hints are shown in the review page
and never change membership by themselves.
