# Public services and fair use

Both integrations require no API key. Overpass station lookup is optional.
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

Never:

- issue one station query per point;
- parallelize requests to the public instance;
- query user-account metadata;
- infer that the nearest straight-line station is the best station.

## FOSSGIS Valhalla demo

Default endpoint:

`https://valhalla1.openstreetmap.de/sources_to_targets`

The integration sends a pedestrian many-to-many matrix for one reviewed group,
sets `X-Client-Id: plan-miriago-route`, converts Valhalla's default
kilometre distances to metres, and caches the result for seven days.
It normally uses Python's HTTPS client; on older macOS system Python builds
whose LibreSSL cannot negotiate with the demo server, it can fall back to the
system `curl` transport. If neither transport works, local ordering remains.

Valhalla documents the matrix request and result fields in its
[matrix API reference](https://valhalla.github.io/valhalla/api/matrix/).
The Valhalla project documents the public FOSSGIS demo, fair-use expectation,
and identifying header in its
[official repository](https://github.com/valhalla/valhalla#demo-server).

The public endpoint is a demo, not an SLA. Use it only after explicit approval
and only for reviewed groups. A failure, rate limit, or disconnected point
leaves the local order unchanged and must be persisted and shown as `failed`.
Singletons and groups over the 25-point matrix limit must be persisted and shown
as `skipped`. Never describe either status as a precise routed result.

## Cache and endpoint controls

The default cache is:

`~/.cache/plan-miriago-route`

Set `MIRIAGO_PLANNER_CACHE_DIR` or pass `--cache-dir` to change it. Endpoint
flags exist for diagnostics or a user-operated compatible instance; do not
silently route data to an unknown third party.
