# Day-route configuration

`plan-days --config FILE` reads this JSON. Every field except `schemaVersion`
and `pools` is optional.

```json
{
  "schemaVersion": 1,
  "network": {"backend": "osm", "files": [], "waterFiles": []},
  "stations": {"files": []},
  "bundleMeters": 30,
  "landmarkMeters": 120,
  "landmarkGenericNames": ["飛び出し注意看板"],
  "speedMetersPerSecond": 1.2,
  "shootSecondsPerPoint": 60,
  "stopOverheadSeconds": 60,
  "areaTargetMinutes": 60,
  "maxAreasPerDay": 12,
  "gapBonus": 1.0,
  "dayStationWeight": 1.0,
  "localCorrectionMeters": 150,
  "restarts": 64,
  "stationSearchMeters": 1500,
  "via": [
    {"nameContains": "大吉山展望台", "latitude": 34.89288, "longitude": 135.81078,
     "label": "左侧之字形登山道入口"}
  ],
  "pools": [
    {
      "select": {"groups": ["宇治桥东诘", "大吉山"]},
      "days": [{"label": "Day1", "theme": "宇治中心"}],
      "start": "auto",
      "end": "name:大吉山展望台",
      "areaTargetMinutes": 55
    },
    {
      "select": {"bbox": [34.90, 135.79, 34.94, 135.81]},
      "days": [{"label": "Day2", "theme": "沿线北上"}, {"label": "Day3", "theme": "六地藏"}],
      "start": "auto",
      "end": "auto"
    },
    {"select": {"remaining": true}, "days": [{"label": "Day4"}], "split": false}
  ],
  "specialGroups": [
    {"kind": "restricted", "name": "开放日限定-校园内", "select": {"nameContains": ["校内"]}},
    {"kind": "unverified", "name": "待考证-位置未确定", "select": {"pointIds": ["…"]}}
  ],
  "names": {"Day1-①": "京阪宇治站・宇治桥东诘"},
  "notes": {"Day1-⑩": "沿左侧之字形登山道上大吉山"},
  "nameFormat": "{day}-{index} {title}",
  "uncovered": "error"
}
```

## Selection

Special groups claim points first, then pools in order; a point belongs to the
first selector that matches it. Selector keys combine as a union and are then
narrowed by `works`:

- `groups`: group names or IDs from `--base-proposal`;
- `pointIds`; `bbox` `[south, west, north, east]`; `nameContains` (list);
- `remaining: true`: everything still unassigned;
- `works`: keep only points of these work IDs.

Points left over raise an error unless `uncovered` is `keep-base-groups`, which
keeps their `--base-proposal` groups. Curation exclusions in the base proposal
carry over unchanged.

## Ends

`start` and `end` accept `auto` (cheapest nearby station), `station:NAME`,
`point:POINT_ID`, `name:SUBSTRING` (the stop whose points match must be
first/last), `location:LAT,LON`, or `null` (free). Station candidates come from
Overpass or from `stations.files` (saved Overpass JSON).

## How a pool is planned

1. Bundle points within `bundleMeters` by complete linkage, then merge stops of
   one landmark (shared name prefix of 3+ characters, all points within
   `landmarkMeters`) so each place is visited once.
2. Build the stop matrix on the backend with short-leg and via corrections.
3. Solve the open path (exact up to 9 stops; otherwise multi-start
   cheapest insertion + 2-opt + Or-opt) with the requested ends.
4. With several `days`, cut the path into days by dynamic programming that
   balances day durations, prefers long gaps, and penalises cuts far from a
   station.
5. Cut each day into `round(duration / areaTargetMinutes)` contiguous areas
   (or `areas`), avoiding cuts inside one landmark.
6. Emit groups with `day`, `routeEnds` (station start for the first area,
   `previous-group` for later areas, the pool end for the last), planned walk
   and minutes, quality checks, and an optional geometry sidecar.

Durations count walking at `speedMetersPerSecond` plus `shootSecondsPerPoint`
per point and `stopOverheadSeconds` per stop. They are planning estimates:
road-network evidence comes only from `refine` after the route-policy gate.
