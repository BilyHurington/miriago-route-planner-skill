# Public services and fair use

These integrations require no API key. Overpass station lookup, the offline
walking network, and station-line lookup are optional.
Valhalla route evaluation is conditional on the user's explicit route-policy
choice and becomes required for the reviewed groups when that choice is `yes`.

## OpenStreetMap Overpass

Default endpoint:

`https://overpass-api.de/api/interpreter`

The skill asks for rail stations, halts, tram stops, rail-related
`public_transport=station` features, and subway entrances inside merged group
bounding boxes. It sends boxes in batches of up to 30 in serial requests and
caches successful JSON for seven days.

The public-instance list and usage guidance are maintained on the
[OpenStreetMap Overpass API page](https://wiki.openstreetmap.org/wiki/Overpass_API).
The public service can reject work under load, so an unavailable response is a
normal fallback condition.

Results come from OpenStreetMap under ODbL. Preserve attribution as
`© OpenStreetMap contributors`. Station names, entrances, operators, and
locations can be incomplete or stale.

The `osm-network` module downloads walkable ways (footways, paths, steps,
residential and service roads, platforms, and roads that allow pedestrians)
plus rivers, canals, and water areas for each cluster of points. Bounding
boxes are rounded outward to 0.01° so later runs reuse the 30-day cache, and
`--walknet-file` / `--water-file` accept saved Overpass JSON for offline work.
The `stations-assist` module looks up route relations (train, subway, light
rail, tram, monorail) whose stop positions lie within 250 m of the candidate
stations; line names are geography only and never imply a timetable.

Overload responses (HTTP 429, 502, 503, 504) are retried after 3, 8, and 20
seconds before being reported.

Never:

- issue one station query per point;
- parallelize requests to the public instance;
- query user-account metadata;
- infer that the nearest straight-line station is the best station.

## FOSSGIS Valhalla demo

Default matrix endpoint:

`https://valhalla1.openstreetmap.de/sources_to_targets`

Default route-shape endpoint:

`https://valhalla1.openstreetmap.de/route`

The integration sends a pedestrian many-to-many matrix for one reviewed group,
sets `X-Client-Id: plan-miriago-route`, converts Valhalla's default
kilometre distances to metres, and caches each response for seven days.
The public demo accepts at most 100 matrix cells and 10 route locations per
request, so matrices are tiled into 10 × 10 blocks and route shapes into
overlapping 10-location chunks, sent serially 0.25 s apart.
It normally uses Python's HTTPS client; on older macOS system Python builds
whose LibreSSL cannot negotiate with the demo server, it can fall back to the
system `curl` transport. If neither transport works, local ordering remains.

After matrix ordering has been reviewed, `render-preview --geometry auto` may
send that ordered group to the route endpoint to obtain a pedestrian polyline6
shape. This call supplies display geometry only; it never changes membership or
order. Successful route responses use the same seven-day cache. A route-shape
failure leaves an orange dashed straight-line fallback in the HTML and does not
block preview generation.

Valhalla documents the matrix request and result fields in its
[matrix API reference](https://valhalla.github.io/valhalla/api/matrix/).
The Valhalla project documents the public FOSSGIS demo, fair-use expectation,
and identifying header in its
[official repository](https://github.com/valhalla/valhalla#demo-server).

The public endpoint is a demo, not an SLA. Use it only after explicit approval
and only for reviewed groups. A failure, rate limit, or disconnected point
leaves the local order unchanged and must be persisted and shown as `failed`.
Singletons and groups above 58 bundled stops (60 matrix sites including ends)
must be persisted and shown as `skipped`; use the offline OSM backend for them.
Never describe either status as a precise routed result.

## OpenFreeMap preview basemap

Interactive previews use OpenFreeMap's Positron style by default. The generated
HTML makes tile, style, glyph, and sprite requests only when it is opened in a
browser. Preserve the attribution displayed by the embedded MapLibre control.
Use `render-preview --no-basemap` to produce the same route and point interaction
without requesting an online basemap.

## Cache and endpoint controls

The default cache is:

`~/.cache/plan-miriago-route`

Set `MIRIAGO_PLANNER_CACHE_DIR` or pass `--cache-dir` to change it. Endpoint
flags exist for diagnostics or a user-operated compatible instance; do not
silently route data to an unknown third party.
