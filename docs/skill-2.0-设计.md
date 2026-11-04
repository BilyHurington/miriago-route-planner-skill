# plan-miriago-route 2.0 设计草案

> **实现说明（2.0.0 已实现）**：与下文草案相比的调整——
> - 模块 ID 改为 `osm-network`、`day-route`、`special-groups`、`stations-assist`、`dedupe`、`verify`、`classify`、`review-preview`、`review-server`（见 `plan-miriago-route/references/optional-modules.md`）；
> - schema 4 的 proposal 在读取时自动升级到 5，不需要单独的 `migrate-proposal` 命令；
> - 没有单独的 `fetch-walknet` 命令：`plan-days` / `refine` 按需下载并缓存路网，`--walknet-file` 使用本地 Overpass JSON；
> - 车站数据也可以离线提供（`assist-stations --station-file`、day-route 配置里的 `stations.files`）；
> - Overpass 遇到 429/502/503/504 会自动重试三次。

> 来源：京吹巡礼包制作（1493 点、40 片区）中补充的流程、脚本与审核页面。
> 目标：把这些能力并入 Skill，同时把流程改成「核心必经 + 可选模块」，并新增车站辅助规划。

## 1. 设计原则

1. **核心流程保持最短**：不开任何可选模块时，体验与 1.7 一致（导入 → 片区 → 路线策略 → 路线 → 导出），老 proposal 可以迁移。
2. **可选模块显式开启**：在输入关卡一次性问清要开哪些模块，记录在 `proposal.modules`；没开的模块不运行、不提问、不出现在报告里。
3. **关卡不减少，只按模块增加**：每个开启的模块如果会改变点位归属或排除点位，就带一个自己的确认关卡；纯辅助类（预览、检查报告）不加关卡。
4. **不删数据**：去重、人工删除、取舍统一落到 `excludedPoints`（带 `kind` 与理由），点位始终留在包里，可撤销可审计。
5. **核心零第三方依赖**：路网、聚类、TSP、DP 全部用标准库实现（heapq Dijkstra 等）；Pillow 仍只用于缩略图。
6. **Agent 侧工作与脚本侧工作分离**：看图比对、AI 分类、卫星图核对由 agent / subagent 完成，Skill 只定义输入批次、输出 JSON 格式、合并与应用脚本；没有 subagent 或浏览器的 agent 可以跳过或串行完成。

## 2. 新流程总览

```
0  输入关卡 ── 汇总输入 + 选择可选模块（一次问完）
1  来源收集 ── Bangumi 关卡 → prepare-import
1a [可选 curation.dedupe]   同机位候选 → 手选页 → 写回 import draft（selected=false + 「另见于」备注）
1b [可选 curation.verify]   卫星图核对批次 → verify.json → 坐标修正写回 import draft
   导入关卡 → confirm-import → 导出图片关卡 → apply-import
2  片区规划 ── 选择策略：
     cluster   （现有自适应 MST，默认）
     day-route （按天点池 → 步行路网 TSP → DP 切成首尾相接的片区）[可选]
   特殊片区：restricted（限定开放）/ unverified（位置未确定）不参与路线
2a [可选 curation.classify] AI 分类提示（保留/可选/丢弃 + 知名场面），只写提示，不改归属
2b [可选 review.server]     交互审核页 + 本地后端 + SQLite 临时库
     → apply-review 生成「人工剔除」草案 → 剔除关卡
   片区关卡 → confirm-proposal --stage areas
3  路线策略关卡 ── 原 3 问 + 路网后端选择 + 车站辅助（是否开启）
4  路线规划 ── refine（后端：valhalla / osm / none）
     + 约束（途经点、固定起终点）
     + 质量检查（相交、单点折返、跨水域）
     + [可选 stations.assist] 片区进出站、片区间换乘建议
   时间取舍（原有）
5  行程备注关卡 ── [stations.assist 开启时] 自动生成换乘段骨架，时刻/票价留空待官方来源或用户确认
6  路线关卡 → 导出图片关卡 → apply → validate
```

预览贯穿全程：`render-preview --mode compact|review`，compact 为默认（即现有页面）。

## 3. 模块清单

| 模块 ID | 作用 | 默认 | 新命令 | 新关卡 |
|---|---|---|---|---|
| `bundle.complete-linkage` | 机位聚合改全连接，可配阈值（默认 15 m，可到 30 m） | **开**（修 bug） | `propose --bundle-meters` | 无 |
| `routing.valhalla-tiled` | Valhalla 矩阵 10×10 分块、路线 10 点分段 | **开**（修 bug） | 无（`refine` 内部） | 无 |
| `routing.osm` | 离线 OSM 步行路网后端：Overpass 下载 → 缓存 → 大连通分量吸附 → Dijkstra | 关 | `fetch-walknet`、`refine --backend osm` | 无（在路线策略关卡选择后端） |
| `routing.tsp` | 开放路径 TSP：多次重启 + 2-opt + Or-opt；固定 / 自动起终点；途经点 | **开**（替换贪心） | `refine --restarts`、约束文件 | 无 |
| `routing.quality` | 相交检测、单点折返检测、跨水域短距修正、吸附偏移计入 | **开** | 报告写入 `diagnostics` | 无 |
| `areas.day-route` | 按天点池 + DP 切片区；`group.day` 元数据；片区间移动计入下一片区 | 关 | `plan-days CONFIG` | 沿用片区关卡 |
| `areas.special-groups` | `group.kind = route / restricted / unverified`，非 route 片区跳过路线评估 | 关 | `propose --special-group` 或配置 | 沿用片区关卡 |
| `stations.assist` | 车站辅助规划（见第 5 节） | 关 | `assist-stations` | 无（结果在路线关卡审） |
| `curation.dedupe` | 同机位候选组 + 手选页；结果写入 import draft | 关 | `dedupe-candidates`、`apply-dedupe` | 沿用导入关卡 |
| `curation.verify` | 卫星图核对批次 + 结果格式 + 坐标修正 | 关 | `verify-batches`、`apply-verify` | 沿用导入关卡 |
| `curation.classify` | AI 分类批次 + 合并，写 `proposal.reviewHints` | 关 | `classify-batches`、`merge-classify` | 无（仅提示） |
| `review.preview` | 审核模式预览：片区展开、图文弹窗、外链、扇形散开、分类筛选、快速分类面板 | 关 | `render-preview --mode review` | 无 |
| `review.server` | 本地后端 + SQLite 临时判定库（含历史）；确认前不改任何文件 | 关 | `serve-review`、`apply-review` | **新增：人工剔除关卡** |

「默认开」的三项是对现有行为的修正，会在 CHANGELOG 中写明；其余全部需要用户在输入关卡选择。

## 4. 数据结构变化（proposal schemaVersion 4 → 5）

```jsonc
{
  "schemaVersion": 5,
  "modules": {                      // 输入关卡记录，决定后续哪些步骤出现
    "areasStrategy": "cluster",     // cluster | day-route
    "curation": ["dedupe", "verify", "classify"],
    "review": ["preview", "server"],
    "stationsAssist": false
  },
  "routePolicy": {
    "...": "原有字段",
    "networkBackend": "valhalla",   // valhalla | osm | none
    "stationsAssist": true          // 路线策略关卡第 4 问
  },
  "constraints": {                  // 可选，来自约束文件
    "via": [{"stopPointId": "...", "via": [34.89288, 135.81078], "label": "大吉山左侧登山口"}],
    "fixedEnds": [{"groupId": "...", "start": "station:京阪宇治", "end": "point:..."}]
  },
  "groups": [{
    "...": "原有字段",
    "kind": "route",                // route | restricted | unverified
    "day": {"label": "Day1", "index": 0, "theme": "宇治中心"},  // 可选，仅 day-route
    "access": {                     // 可选，仅 stations.assist
      "entry": {"station": "京阪宇治", "walkMeters": 120},
      "exit":  {"station": "JR宇治", "walkMeters": 340}
    },
    "quality": {"crossings": 0, "outAndBack": [], "waterCrossings": 0}
  }],
  "transitions": [                  // 可选，相邻片区之间
    {"from": "groupA", "to": "groupB", "mode": "walk", "walkMeters": 640},
    {"from": "groupB", "to": "groupC", "mode": "rail_candidate",
     "fromStation": "中书岛", "toStation": "三条",
     "lines": ["京阪本线"], "source": {"provider": "openstreetmap_overpass", "scope": "geography_only"}}
  ],
  "excludedPoints": [{
    "...": "原有字段",
    "kind": "time_tradeoff"         // time_tradeoff | curation
  }],
  "reviewHints": {                  // 可选，仅 curation.classify
    "pointId": {"class": "keep|optional|drop", "famous": true, "reason": "...", "similarTo": ["..."]}
  },
  "review": {
    "...": "原有字段",
    "curationConfirmed": false, "curationConfirmedAt": null
  }
}
```

规则调整：

- `excludedPoints.kind = curation` 不需要 `adjustForTime`，但需要 `review.curationConfirmed`（人工剔除关卡）。
- 非 `route` 片区在 `refine` 中记录 `skipped: non_route_group`，满足「每个片区都有评估证据」。
- MiriaGo 导出：`kind/day/access` 不进入包结构（v2 无对应字段），通过片区名前缀（可选）和片区 note 体现；`day` 用于行程备注按天分节。
- 迁移：`migrate-proposal` 把 v4 升到 v5（补默认值），已确认的关卡保持有效。

## 5. 车站辅助规划（`stations.assist`）

只用 OSM 的地理信息（车站、出入口、线路关系），**不生成时刻、票价、所需时间**。

1. **取数**：扩展现有 `fetch_stations`，额外查询 `route=train|subway|light_rail|tram` 关系及其 stop 成员，得到「车站 → 线路」映射；全部走 JsonCache。
2. **片区进出站**：对每个片区，用步行路网（或直线 × 系数，标注为估算）计算候选车站到首/尾机位的距离；把车站作为虚拟起终点放进 TSP，联合选出「进站 + 片区内顺序 + 出站」，结果写入 `group.access`。用户固定的起终点优先。
3. **片区间衔接**：相邻片区出站与下一片区进站之间，步行距离低于阈值（默认 1.5 km）→ `walk`；否则若两站在同一线路或有换乘关系 → `rail_candidate`，列出候选线路；都不满足 → `unknown`，提醒用户补充。
4. **按天规划中的作用**（day-route 开启时）：DP 切分代价加入衔接成本——步行段用路网距离；`rail_candidate` 用可配置的启发式成本（默认 15 分钟，仅用于排序，不写入备注）。可以把一天的起点 / 终点设为「自动选站」。
5. **行程备注**：`prepare-notes --from-transitions` 生成 segments 骨架（from/to、mode、候选线路、到站步行分钟数）；`durationMinutes`、`fare` 留空，需要官方来源或用户确认后才能填写。渲染时继续遵守现有规则：没有来源的非步行段不能通过校验。
6. **预览**：车站显示为方形标记；进出站连线用细虚线；`rail_candidate` 衔接用另一种颜色的虚线，弹窗显示候选线路。

## 6. 审核模式预览与后端（`review.preview` / `review.server`）

- `render-preview --mode review`：在现有模板基础上加入（而不是像京吹项目那样事后改 HTML）：
  - 片区展开 / 点位列表；地图与列表互相定位；高缩放下扇形散开；
  - 弹窗：本地缩略图（从包内资源导出到预览旁的 `assets/`）、原图、anitabi / Google Earth / Google 地图卫星图 / 街景链接；
  - 分类筛选（有 `reviewHints` 时）；快速分类面板（左图右地图、下方缩略图条、快捷键、自动下一张）；
  - CSP 只放开 `'self'` 的图片与 API。
- `serve-review PREVIEW_DIR --db WORK/review.sqlite`：标准库 HTTP 服务；只接受本机连接；判定写 SQLite（`decisions` + `history`）；不写任何 proposal / 包。
- `apply-review INPUT PROPOSAL --db ... --output ...`：把「删除」判定转成 `excludedPoints(kind=curation)` 草案，同时输出变更摘要；**人工剔除关卡**确认后才能进入下一步，确认命令 `confirm-curation`。
- 无后端时页面退回 localStorage + 导出 JSON，`apply-review --decisions FILE` 同样可用。

## 7. 精整模块的 agent 侧约定

| 模块 | 脚本产出 | agent 产出（JSON） | 应用 |
|---|---|---|---|
| dedupe | 候选组（30 m 内 + 同作品 / 同集），每组缩略图拼图路径 | `{groupId: {keep: [...], drop: [...], note}}` | `apply-dedupe` → import draft `selected=false`，保留点的备注写「另见于 …」 |
| verify | 批次文件（每批约 50 点，含 Google Earth 链接与缩略图） | `{pointId: {verdict: ok|move|unknown, lat, lng, note}}` | `apply-verify` → 修正坐标，`unknown` 可归入 unverified 片区 |
| classify | 批次文件（按片区，含缩略图路径） | `{pointId: {class, famous, reason, similarTo}}` | `merge-classify` → `reviewHints` |

`SKILL.md` 写明：subagent 只用 agent 自带的内置浏览器，不用用户的个人浏览器；参考图优先用包内 / 本地图片，不要批量请求 anitabi。

## 8. 输入关卡新增的问题（只问一次）

在原有输入清单后追加「可选模块」一问，给出推荐组合：

- **快速**：只用核心流程（等同 1.7 + 三项修正）。
- **标准（推荐）**：+ 离线路网 + 车站辅助 + 审核预览。
- **完整审核**：+ 去重 + 卫星图核对 + AI 分类 + 审核后端 + 按天规划。

用户也可以逐项勾选。

## 9. 实施阶段

| 阶段 | 内容 | 主要文件 |
|---|---|---|
| P1 | 修正：全连接聚合、Valhalla 分块 / 分段、短距修正 | `clustering.py`、`public_services.py` |
| P2 | 路网与 TSP：`walknet.py`（Overpass 下载 / 缓存 / 吸附 / Dijkstra / 水域）、`tsp.py`、质量检查 | 新模块 + `refine` |
| P3 | proposal v5、`modules`、`kind`、`day`、`curation` 剔除、迁移命令、校验 | `proposal.py`、CLI |
| P4 | 按天规划：`plan-days` 配置、DP 切分、片区间移动计入 | 新模块 `day_planner.py` |
| P5 | 车站辅助：线路关系、进出站、衔接、备注骨架、预览标记 | `stations.py`、`journey_notes.py` |
| P6 | 精整：dedupe / verify / classify 批次与应用 | 新模块 `curation.py` |
| P7 | 审核预览 + 后端 + apply-review + 剔除关卡 | `route_preview.py`、模板 / JS、`review_server.py` |
| P8 | `SKILL.md` 重写、references 更新、CHANGELOG、版本 2.0.0 | 文档 |

每个阶段补对应测试（全部离线，用夹具模拟 Overpass / Valhalla 返回），最后用京吹数据端到端跑一遍：新 Skill 生成的片区与路线和现有 `work/plan_routes.py` 的结果对比。
