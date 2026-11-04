# MiriaGo 路线规划 Skill

一个面向 AI Agent 的路线规划 Skill，可从 Bangumi、Anitabi、Google My
Maps、KML/KMZ 或已有 `.sjhplan` 数据包收集点位，经过逐步确认后生成可导入
[MiriaGo](https://github.com/BilyHurington/MiriaGo) 的 v2 巡礼计划。

本 Skill 支持 Codex、Pi 和 Claude Code。作品选择、片区边界、路线取舍、行程
备注和参考图片策略均设置了明确的人工确认节点，不会由 Agent 静默决定。

## 主要功能

- 根据作品名搜索 Bangumi，并从 Anitabi 导入用户选定的作品。
- 下载并整合公开 Google My Maps 数据或本地 KML/KMZ 文件。
- 在不覆盖源文件的前提下扩展已有 MiriaGo v2 数据包。
- 将点位划分为可复核的地理片区，或按天规划首尾相接的路线片区（可选），并规划各片区内的巡礼顺序。
- 生成单文件交互式 HTML 预览，显示每个片区的路线、编号点位、取舍点和无效坐标。
- 可选用公共 Valhalla 或离线 OpenStreetMap 步行路网精确评估路线；检查路线自相交、单点折返和重复步行。
- 可选根据车站规划每天与各片区的进出站，并标出片区间的乘车候选（只用地理信息，不编造时刻与票价）。
- 可选的点位整理：同机位去重、卫星图与街景坐标核对、AI 保留/可选/丢弃分类，均可交给子 Agent 分批完成。
- 可选的交互审核页：参考图、外部链接、快速分类面板，配合本地后端把判定写入临时数据库，确认后才生效。
- 记录点位取舍和路线时长调整，同时保留原始点位，方便撤销和追溯。
- 将片区间交通、预计步行时间和注意事项写入 MiriaGo 计划备注。
- 强制保存所有参考图片的缩略图，以及策略要求的完整图片。
- 多线程下载参考图片，并通过 stderr 输出 JSONL 实时进度。

## 人工确认节点

工作流会在以下阶段停止并等待用户明确确认：

1. 收集必要输入，并一次性选择可选模块（快速 / 标准 / 完整审核）
2. 选择 Bangumi 搜索结果
3. 复核来源导入结果（含去重与坐标核对的修改）
4. 复核人工剔除的点位（仅在交互审核中删除过点位时）
5. 复核片区划分
6. 选择路线规划策略（路网、取舍、行程备注、车站辅助）
7. 复核行程备注（如需要）
8. 复核最终路线
9. 选择是否包含 Anitabi 完整参考图片

CLI 会记录并校验确认状态，Agent 不能通过直接修改确认字段绕过这些节点。

## 可选模块

2.0 起，工作流分为「核心流程」和「可选模块」。不开任何模块时与 1.7 的体验一致；
需要的模块在输入确认节点一次性选好，没选的模块不会运行，也不会再询问。

| 组合 | 包含的模块 |
|---|---|
| 快速 | 只用核心流程 |
| 标准（多日行程推荐） | 离线路网 `osm-network`、车站辅助 `stations-assist`、审核预览 `review-preview` |
| 完整审核 | 标准 + 去重 `dedupe`、坐标核对 `verify`、AI 分类 `classify`、审核后端 `review-server`、按天规划 `day-route`、特殊片区 `special-groups` |

| 模块 | 作用 |
|---|---|
| `osm-network` | 下载并缓存 OpenStreetMap 步行路网（也可读取本地 Overpass JSON），离线计算步行距离；近距离修正不会跨河 |
| `day-route` | 每个点位池走一条最短路线，按车站位置切分为多天，再按目标时长切成首尾相接的片区；支持途经点（例如指定登山道）和固定起终点 |
| `special-groups` | 「限定开放」「位置待考证」点位单独成组，保留在数据包中但不参与路线规划 |
| `stations-assist` | 为每天和每个片区选择进出站，标出片区间可步行、可乘车或待确认的衔接，并生成行程备注草稿（时刻与票价留空） |
| `dedupe` / `verify` / `classify` | 同机位去重、卫星图与街景坐标核对、保留/可选/丢弃分类；生成分批任务交给子 Agent，结果按固定格式写回 |
| `review-preview` / `review-server` | 交互审核页（参考图、外部链接、快速分类面板）与本地后端；判定写入临时 SQLite，确认后才转为可撤销的「人工剔除」 |

无论是否启用模块，以下改进都始终生效：机位聚合改为全连接，避免把一整条街串成一个机位；
Valhalla 请求按公共服务器上限自动分块；片区内路线改用精确求解（9 个机位以内）或多起点
2-opt / Or-opt 求解，并检查路线自相交与单点折返。

详细说明见 `plan-miriago-route/references/optional-modules.md`，按天规划的配置格式见
`plan-miriago-route/references/day-route-config.md`。

## 推荐：直接交给 ChatGPT Work

最简单的使用方式是在 ChatGPT 网页端或 App 中打开 Work，直接发送本仓库链接：

```text
https://github.com/BilyHurington/miriago-route-planner-skill
```

然后说明希望它读取并使用仓库中的 `plan-miriago-route` Skill。例如：

```text
请读取这个仓库并使用其中的 plan-miriago-route Skill，为我制作一份可导入
MiriaGo 的巡礼计划。开始时先询问所有必要信息，并在每个确认节点等待我的回复。
```

可以在同一条消息中附上作品名、已有 `.sjhplan`、公开 Google My Maps 链接、
KML/KMZ 文件、计划地区和输出要求。缺少的信息会在输入确认节点统一询问。

## 安装

先克隆仓库：

```bash
git clone git@github.com:BilyHurington/miriago-route-planner-skill.git
cd miriago-route-planner-skill
```

### Codex

将 `plan-miriago-route/` 复制或链接到 `~/.codex/skills/`：

```bash
ln -s "$(pwd)/plan-miriago-route" ~/.codex/skills/plan-miriago-route
```

### Pi

直接从 GitHub 安装：

```bash
pi install git:github.com/BilyHurington/miriago-route-planner-skill
```

需要显式调用时使用 `/skill:plan-miriago-route`。

### Claude Code

将本地仓库作为插件加载：

```bash
claude --plugin-dir /absolute/path/to/miriago-route-planner-skill
```

插件调用命令为 `/miriago-route-planner-skill:plan-miriago-route`。也可以将
`plan-miriago-route/` 复制或链接到
`~/.claude/skills/plan-miriago-route`，然后使用 `/plan-miriago-route`。

## 使用示例

请 Agent 使用 `plan-miriago-route`，并提供当前已有的输入。例如：

```text
使用 plan-miriago-route，根据 Anitabi 上的《孤独摇滚！》点位制作一份东京
巡礼计划，并整合我的公开 Google My Maps 链接。请在每个确认节点询问我，
最后导出可导入 MiriaGo 的数据包。
```

Agent 会先询问缺失的计划信息，然后依次确认：是否使用步行路网精确评估、是否
调整路线用时或放弃孤立点位、是否写入行程备注，以及是否包含 Anitabi 完整参考
图片。

仓库也提供确定性 CLI，用于执行各阶段操作：

```bash
python3 plan-miriago-route/scripts/miriago_plan.py --help
```

在片区或路线复核前可生成交互式预览：

```bash
python3 plan-miriago-route/scripts/miriago_plan.py render-preview \
  INPUT.sjhplan PROPOSAL.json --output routes-preview.html --geometry auto
```

`auto` 只会在用户已明确同意步行路网评估，且当前片区已成功评估时
请求 Valhalla 真实线形；其他情况会明确显示直线降级。加上 `--no-basemap`
可在打开 HTML 时不请求在线底图。使用离线路网时，用 `--geometry-file` 传入
`plan-days` 或 `refine` 输出的路网线形。预览生成进度会以 JSONL 输出到 stderr。

启用模块后的常用命令示例：

```bash
# 按天规划片区（离线路网，路网线形另存，供预览使用）
python3 plan-miriago-route/scripts/miriago_plan.py plan-days \
  combined.sjhplan --config day-config.json \
  --output areas-draft.json --geometry-output areas-geometry.json

# 生成交互审核页，并启动本地后端（判定写入临时数据库）
python3 plan-miriago-route/scripts/miriago_plan.py render-preview \
  combined.sjhplan areas-draft.json --mode review \
  --geometry-file areas-geometry.json --output review/areas.html
python3 plan-miriago-route/scripts/miriago_plan.py serve-review \
  review/areas.html --db review.sqlite --package combined.sjhplan

# 审核结束后转成草稿，经确认后生效
python3 plan-miriago-route/scripts/miriago_plan.py apply-review \
  combined.sjhplan areas-draft.json --db review.sqlite --output areas-curated.json

# 路线策略确认后：车站辅助与离线路网评估
python3 plan-miriago-route/scripts/miriago_plan.py assist-stations \
  combined.sjhplan route-policy-confirmed.json --output stations.json
python3 plan-miriago-route/scripts/miriago_plan.py refine \
  combined.sjhplan stations.json --all-groups \
  --geometry-output routes-geometry.json --output routes-draft.json
```

参考图片默认使用 8 个线程下载。生成数据包的命令支持
`--image-download-workers 1-32`；需要严格复现请求顺序时可设为 1。下载进度会
实时刷新到 stderr，最终机器可读结果仍单独输出到 stdout。

## 环境要求

- Python 3.10 或更高版本
- 生成非 Anitabi 图片缩略图、去重与核对用的对照图时需要 Pillow
- 使用 Bangumi、Anitabi、Google My Maps、Overpass、Valhalla 或在线底图时需要
  公共网络访问；离线路网与车站数据可改用本地保存的 Overpass JSON

安装图片处理依赖：

```bash
python3 -m pip install -r plan-miriago-route/scripts/requirements.txt
```

本项目只使用无需账号或 API Key 的公共服务。私有 Google My Maps 地图需要由
用户导出为 KML/KMZ 文件；Skill 不会使用登录 Cookie 或复制 API Token。

## 本地验证

在 Skill 目录中运行：

```bash
cd plan-miriago-route
ruff check scripts tests
PYTHONPATH=scripts python3 -m unittest discover -s tests -t . -v
```

当前 Skill 与插件版本为 `2.0.0`。2.0 的流程设计见
[docs/skill-2.0-设计.md](docs/skill-2.0-设计.md)，各可选模块说明见
`plan-miriago-route/references/optional-modules.md`。

## 开源协议

本项目采用 [MIT License](LICENSE) 开源。你可以自由使用、复制、修改、合并、
发布和分发本项目，但需要保留原始版权声明和许可证文本。软件按“原样”提供，
不附带任何明示或暗示的担保。MIT 协议仅适用于本仓库代码和文档，不包含第三方
数据或用户导入内容。

交互预览内嵌 MapLibre GL JS 5.24.0，其 BSD-3-Clause 及所含组件许可文本
保存在 `plan-miriago-route/assets/route-preview/vendor/LICENSE.txt`。

## 相关项目

- [MiriaGo](https://github.com/BilyHurington/MiriaGo)：本 Skill 输出数据包所对应的
  巡礼计划应用。

## 数据来源说明

本项目在用户选择相应功能时，可能使用或处理来自
[Bangumi](https://bgm.tv/)、[Anitabi](https://anitabi.cn/)、
[Google My Maps](https://www.google.com/maps/about/mymaps/)、
[OpenStreetMap](https://www.openstreetmap.org/copyright) 和
[Valhalla](https://github.com/valhalla/valhalla) 的公开数据，并可在预览中加载
[OpenFreeMap](https://openfreemap.org/) 底图，以及用户主动提供的
MiriaGo 数据包、KML/KMZ、图片和来源页面。第三方数据、图片和商标的权利归各自
权利人所有，不受本仓库 MIT License 授权。使用者应遵守对应来源的许可协议、
服务条款和署名要求，并自行确认导入、下载、再分发及实际使用这些内容的权限。
