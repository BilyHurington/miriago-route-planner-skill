# Journey-note format

Use this format only when the user selected plan-note writing at the route-policy
gate. It renders a managed Markdown section into `plan.memo`; it does not add a
public-transport optimizer.

## Data policy

- Use Valhalla route metrics already stored in the proposal for per-area walking
  distance and time.
- Use official operator data, public GTFS/GTFS-JP, or explicit user information
  for non-walking segments.
- Set `source.provider` to `user_confirmed` for user-supplied travel details.
- Record when data was checked. Timetables, fares, disruptions, and opening
  hours are time-sensitive and must not be presented as timeless facts.
- Use Overpass only for station and entrance geography, never for schedules.
- Do not scrape Google Maps, NAVITIME, or another closed journey planner.
- Do not invent missing times. Omit them and state the limitation during review.

## JSON contract

```json
{
  "schemaVersion": 1,
  "title": "巡礼行程建议",
  "timezone": "Asia/Tokyo",
  "checkedAt": "2026-07-28",
  "includeAreaWalking": true,
  "summaryNotes": [
    "上午先完成宇治站片区"
  ],
  "segments": [
    {
      "from": "宇治站片区终点",
      "to": "黄檗站片区起点",
      "mode": "rail",
      "operator": "JR西日本",
      "service": "奈良线",
      "direction": "京都方向",
      "durationMinutes": {
        "min": 12,
        "max": 18
      },
      "walkingMinutes": 6,
      "transferMinutes": 0,
      "bufferMinutes": 10,
      "departureWindow": "13:00-14:00",
      "fare": {
        "amount": 190,
        "currency": "JPY"
      },
      "instructions": [
        "宇治站乘车，黄檗站下车"
      ],
      "alternatives": [
        "全程步行约45分钟"
      ],
      "cautions": [
        "周末班次可能减少"
      ],
      "actionItems": [
        "出发前确认当日班次"
      ],
      "source": {
        "provider": "JR西日本官方网站",
        "url": "https://example.com/official-route-page",
        "checkedAt": "2026-07-28"
      },
      "confidence": "medium"
    }
  ],
  "globalCautions": [
    "恶劣天气时重新确认运行信息"
  ],
  "actionItems": [
    "准备可用的IC卡"
  ]
}
```

Required top-level fields are `schemaVersion`, `timezone`, and `checkedAt`.
`includeAreaWalking` defaults to `true`. Supported modes are `walk`, `rail`,
`subway`, `tram`, `bus`, `ferry`, `taxi`, `bicycle`, and `other`.

`durationMinutes` accepts either one non-negative number or an object with
non-negative `min` and `max`. `walkingMinutes`, `transferMinutes`, and
`bufferMinutes` are separate so the rendered memo does not hide transfer cost.

Every non-walking segment requires `source.provider`. `source.url` is optional
but, when present, must be HTTP(S). Confidence is `high`, `medium`, or `low`.

## Merge behavior

The renderer uses managed start and end markers. On first write it appends the
section after the existing memo. On later writes it replaces only that section,
preserving user text before and after it. It also preserves checked task state
when the new task text is unchanged.

Never edit the rendered Markdown directly in the proposal. Change the structured
JSON, run `prepare-notes` again, show the complete new rendering, and obtain new
approval. A route fingerprint prevents confirmed notes from surviving a changed
group name, membership, point order, route metric, or exclusion decision.
