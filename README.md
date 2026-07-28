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
- 将点位划分为可复核的地理片区，并规划各片区内的巡礼顺序。
- 可选用公共 Valhalla 路网精确评估步行路线。
- 记录点位取舍和路线时长调整，同时保留原始点位，方便撤销和追溯。
- 将片区间交通、预计步行时间和注意事项写入 MiriaGo 计划备注。
- 强制保存所有参考图片的缩略图，以及策略要求的完整图片。
- 多线程下载参考图片，并通过 stderr 输出 JSONL 实时进度。

## 人工确认节点

工作流会在以下阶段停止并等待用户明确确认：

1. 收集必要输入
2. 选择 Bangumi 搜索结果
3. 复核来源导入结果
4. 复核片区划分
5. 选择路线规划策略
6. 复核行程备注（如需要）
7. 复核最终路线
8. 选择是否包含 Anitabi 完整参考图片

CLI 会记录并校验确认状态，Agent 不能通过直接修改确认字段绕过这些节点。

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

参考图片默认使用 8 个线程下载。生成数据包的命令支持
`--image-download-workers 1-32`；需要严格复现请求顺序时可设为 1。下载进度会
实时刷新到 stderr，最终机器可读结果仍单独输出到 stdout。

## 环境要求

- Python 3.10 或更高版本
- 生成非 Anitabi 图片缩略图时需要 Pillow
- 使用 Bangumi、Anitabi、Google My Maps、Overpass 或 Valhalla 功能时需要
  公共网络访问

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

当前 Skill 与插件版本为 `1.6.0`。

## 相关项目

- [MiriaGo](https://github.com/BilyHurington/MiriaGo)：本 Skill 输出数据包所对应的
  巡礼计划应用。
