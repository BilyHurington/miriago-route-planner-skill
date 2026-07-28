# Source import contract

## Providers

### Bangumi

`search-work` sends an anonymous `POST` to
`https://api.bgm.tv/v0/search/subjects`, requests at most 12 match-sorted
results by default, and supports these MiriaGo subject types:

- `book` = 1
- `anime` = 2
- `music` = 3
- `game` = 4
- `real` = 6

Treat the result order as suggestions. Require an explicit Bangumi ID because
sequels, recaps, adaptations, and same-name works can all appear together.

### Anitabi

After selection, read the public static map index from `g.json` and the matching
detail page from `gN.json`, using `www.anitabi.cn` with `anitabi.cn` as fallback.
Match MiriaGo's compact static-data parser:

- use `bangumi-{bangumiId}` for a new work;
- use `anitabi-{bangumiId}-{pointId}` for a point;
- store `source: "anitabi"` and the raw Anitabi point ID in `sourceId`;
- canonicalize images to `image.anitabi.cn` and remove thumbnail `plan` values;
- preserve origin, origin URL, note, episode, and scene time.

The point count in `gN.json` must equal the point IDs in `g.json`. Stop on a
missing work, missing page, zero-point work, or partial response.

### Google My Maps

Accept a public `google.* /maps/d/` URL containing `mid`, or a local KML/KMZ.
Download public maps from Google's `forcekml=1` endpoint. Never accept cookies,
credentials, arbitrary download hosts, XML entities, or an expanded KML larger
than 64 MiB.

Parse nested folders as layer names, ExtendedData, HTML descriptions, and image
URLs. MiriaGo stores one reference image per point, so split a placemark with
multiple images into multiple stable points. Convert a line or polygon to its
path-length midpoint and preserve the original coordinates in the note. Split
multiple geometries in one placemark and report the conversion.

Use `source: "manual"` because MiriaGo's point-source enum contains only
`manual` and `anitabi`. Put a stable `gmm:` identity in `sourceId` so a repeated
import can be recognized.

Remote image URLs remain in the review proposal. At package-write time, download
and validate every referenced image. Bundle an `h160` Anitabi thumbnail for every
Anitabi reference. Bundle Anitabi full-resolution images only after the explicit
export-image choice. For every non-Anitabi external reference, always bundle the
complete image under `assets/user_references/` and generate a real thumbnail
under `assets/thumbnails/`.

Package-writing commands deduplicate identical candidate URL sets, then download
the unique jobs with a bounded thread pool. `--image-download-workers` defaults
to 8 and accepts 1-32; candidate fallbacks within one job remain sequential.
The commands stream one-line JSON progress to stderr and flush every event:

- `image_download_start`: unique job total and effective worker count;
- `image_download_progress`: monotonic completion count, point, image kind,
  status, and downloaded bytes or error;
- `image_download_complete`: final unique-job, byte, and failure counts.

Keep stdout reserved for the final command result. A required job failure is
reported in progress, but still blocks all package writing after the remaining
submitted jobs settle.

## Import proposal

`prepare-import` writes an editable JSON object with:

- `base`: source path, plan ID, and SHA-256, or `null` for a new plan;
- `newPlan`: required name and area when `base` is null;
- `sources`: provider identities, labels, and counts;
- `availableWorks`: existing and imported works available for mapping;
- `importedWorks`: normalized works that can be added;
- `workMappings`: one source-work reference to one `selectedWorkId`;
- `points`: normalized data plus editable `selected` state;
- `duplicates` and `warnings`;
- `review.confirmed`, initially false.

The source hash binds a merge proposal to the exact package it reviewed.
`confirm-import` passes the hard review gate; `apply-import` rejects a draft.

## Mapping and deduplication

Reuse an existing work when its `bangumiId` uniquely matches. Suggest an exact
normalized title match for a My Maps layer, but always display and review the
mapping. Otherwise create a manual work for that map layer.

Automatically deselect only exact identities:

- the same stable point ID; or
- the same source, source ID, and mapped work in the base package.

Do not automatically remove close cross-source points. Report same-name points
within 20 metres under the same mapped work for human review.

## Package behavior

When creating a new package, write a MiriaGo v2 `plan_only` ZIP with
`manifest.json`, `plan.json`, `points.csv`, and required reference assets. When
merging, preserve all source entries byte-for-byte, append imported points
without a group, and add missing required assets. Record source identities and
the explicit image policy in `manifest.json.importedBy`.

Run normal area planning only after the combined package has been written and
inspected.
