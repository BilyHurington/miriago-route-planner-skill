# MiriaGo v2 package contract

## Container

A `.sjhplan` v2 export is a ZIP archive containing:

- `manifest.json`
- `plan.json`
- `points.csv`
- `records.csv` when visit records are included
- optional assets under `assets/`
- possible future or application-specific entries

The reader requires:

```json
{
  "format": "miriago_export_package",
  "schemaVersion": 2
}
```

`plan.json.schemaVersion` must also be `2`, and its `plan` field must be an
object.

## Authoritative structure

`plan.json.plan` contains:

- plan identity and metadata: `id`, `name`, `area`, `memo`, timestamps and
  current selection IDs;
- `completedPointIds`;
- `works`;
- `groups`;
- `points`.

`plan.json.visitRecords` is optional. Asset-reference fields can occur on points
and records and refer to other ZIP entries.

Reference image assets use MiriaGo's v2 fields and directories:

- `referenceThumbnailAsset` -> `assets/thumbnails/` for every image-bearing point;
- `referenceFullReferenceAsset` -> `assets/full_references/` for optional
  Anitabi full-resolution cache;
- `userReferenceAsset` -> `assets/user_references/` for mandatory complete
  non-Anitabi external images.

A group written by the planner uses:

```json
{
  "id": "planned-…",
  "name": "东京站・丸之内",
  "orderIndex": 0,
  "orderMode": "manual",
  "anchorName": "东京站",
  "anchorLatitude": 35.6812,
  "anchorLongitude": 139.7671,
  "anchorPointId": null,
  "note": "…",
  "createdAt": "ISO-8601 timestamp"
}
```

Each assigned point receives `groupId` and zero-based `groupOrderIndex`.
Unassigned points receive `null` for both.

MiriaGo uses `(-90, 0)` as a pending-location sentinel. It is valid stored data
but is not a usable planning coordinate.

## Preservation policy

Route planning replaces only:

- group objects;
- point `groupId` and `groupOrderIndex`;
- the current group selection where needed;
- the planner-managed section of `plan.memo`, only after explicit journey-note
  approval;
- `updatedAt`;
- derived `manifest.json`, `points.csv`, and `records.csv`.

It preserves:

- the source package itself;
- work and point identity/content;
- completion state and visit records;
- asset bytes;
- user-written memo text, point/group notes, and reference paths;
- unknown JSON fields;
- unknown ZIP entries.

Package writing may add required reference-image assets and update the
corresponding point asset fields and manifest asset counts.

The output manifest retains the source application version and gains a separate
`plannedBy` object. It receives a new `packageId` so restored assets do not
collide with a prior import of the source package. It does not pretend that
MiriaGo itself produced the planning change.

A source-integration output uses a separate `importedBy` object. It preserves
existing groups and point assignments, appends new points as ungrouped, and
leaves the later route-planning step to replace grouping. A package created
without a base uses `appVersion: external-skill/...` rather than claiming that
MiriaGo exported it.

## Safety checks

The reader rejects malformed JSON, encrypted entries, duplicate ZIP names,
absolute/traversal paths, excessive entry count, excessive expanded size,
duplicate entity IDs, bad work references, and schema mismatches. The final
validator reloads the output. With `validate --source`, every source
non-derived ZIP entry must remain byte-identical; newly bundled image entries
are reported separately and are allowed.

The MiriaGo implementation used to establish this contract is in
`Seichi-Junrei-Helper/lib/plan_transfer/plan_export_v2.dart` and
`plan_import_package.dart`.
