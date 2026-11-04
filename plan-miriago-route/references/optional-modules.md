# Optional modules

The core path (import → areas → route policy → routes → export) runs without
any module. Modules are chosen once at the input gate and recorded in
`proposal.modules` with `--modules`. Three corrections are always on because
they fix the core: complete-linkage stop bundling, Valhalla request tiling, and
the improved open-path solver with route-quality checks.

| Module | What it adds | Commands | Gate it uses |
|---|---|---|---|
| `osm-network` | Offline OpenStreetMap walking network (Overpass download or local JSON, cached 30 days); short-leg correction that never crosses water; snap offsets above 15 m counted | `plan-days` (`network.backend: osm`), `confirm-route-policy --network-backend osm`, `refine` | route policy |
| `day-route` | Pools walked as one open path, split into days and contiguous areas by exact DP; via points; fixed/station ends; previous-area legs counted in the next area | `plan-days` | area |
| `special-groups` | `restricted` (e.g. open-day campus) and `unverified` groups kept but never routed | `propose --special-groups`, `plan-days` `specialGroups` | area |
| `stations-assist` | Station entries/exits per day and area, `walk` / `rail_candidate` / `transit_unknown` transitions, journey-note skeleton | `assist-stations`, `draft-notes-spec` | route policy (question 4), route, journey notes |
| `dedupe` | Same-shot candidate groups, contact sheets, a picker page; dropped scenes deselected on the import draft | `dedupe-candidates`, `apply-dedupe` | import |
| `verify` | Coordinate-check batches with Google Earth / satellite / Street View links; moves and notes written to the import draft; unverifiable points listed for an `unverified` group | `verify-batches`, `apply-verify` | import |
| `classify` | keep / optional / drop + famous-scene hints per point | `classify-batches`, `merge-classify` | none (hints only) |
| `review-preview` | Review-mode page: thumbnails, full images, external links, expandable lists, fan-out of stacked markers, hint filters, quick-classification panel | `render-preview --mode review` | none |
| `review-server` | Local loopback server with a SQLite decision store and history; full images served from the package; decisions become curation exclusions only via `apply-review` | `serve-review`, `apply-review`, `confirm-curation` | curation |

## Working with subagents

`dedupe`, `verify`, and `classify` prepare batches that one subagent each can
handle. Give every subagent its batch folder, the result format from
[curation-formats.md](curation-formats.md), and these rules:

- use the agent's own built-in browser for Google Earth, satellite, or Street
  View checks — never the user's personal browser or signed-in sessions;
- read contact sheets and thumbnails from the batch folder instead of loading
  Anitabi images one by one;
- write only the batch's result file; never edit drafts or proposals.

An agent without subagents or a browser can process batches itself or skip the
module; the core path never depends on these results.

## Decision store

`serve-review` binds only to a loopback address and serves the preview's folder.
Its API:

- `GET /api/ping`, `GET /api/decisions`, `GET /api/history?limit=N`
- `PUT /api/decisions/<pointId>` with `{"decision": "keep" | "delete" | null}`
- `POST /api/decisions/bulk` with `{"decisions": {pointId: decision}}`
- `GET /package-asset/<assets/...>` for images inside `--package`

The SQLite file is a scratch store: proposals and packages change only after
`apply-review` and an explicit curation approval. `apply-review` also accepts a
JSON export from the page (`--decisions`), so a static page works too.
