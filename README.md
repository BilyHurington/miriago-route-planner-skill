# MiriaGo Route Planner Skill

An Agent Skill for building reviewed MiriaGo v2 pilgrimage plans from Bangumi,
Anitabi, Google My Maps, KML/KMZ, and existing `.sjhplan` packages.

The skill is designed for Codex, Pi, and Claude Code. It keeps user decisions at
explicit checkpoints instead of silently choosing works, area boundaries,
route tradeoffs, journey notes, or reference-image policy.

## Features

- Search Bangumi by work name and import the selected work from Anitabi.
- Download and merge public Google My Maps data or local KML/KMZ files.
- Extend an existing MiriaGo v2 package without overwriting the source.
- Divide points into reviewable geographic areas and plan per-area visit order.
- Optionally evaluate walking routes through a public Valhalla road network.
- Record explicit point exclusions and route-duration tradeoffs without deleting
  source points.
- Write reviewed inter-area transport, walking-time estimates, and cautions into
  the MiriaGo plan memo.
- Bundle mandatory thumbnails and required full reference images.
- Download reference images concurrently with JSONL progress on stderr.

## Review Gates

The workflow stops for explicit user approval at these points:

1. Required input collection
2. Bangumi result selection
3. Source-import review
4. Area review
5. Route-policy choices
6. Journey-note review, when requested
7. Final route review
8. Full-resolution Anitabi image choice

The CLI stores and validates confirmation state so an agent cannot bypass these
gates by directly writing approval fields.

## Installation

Clone the repository first:

```bash
git clone git@github.com:BilyHurington/miriago-route-planner-skill.git
```

### Codex

Link or copy `plan-miriago-route/` into `~/.codex/skills/`:

```bash
ln -s "$(pwd)/miriago-route-planner-skill/plan-miriago-route" \
  ~/.codex/skills/plan-miriago-route
```

### Pi

Install directly from GitHub:

```bash
pi install git:github.com/BilyHurington/miriago-route-planner-skill
```

Invoke the skill explicitly with `/skill:plan-miriago-route` when needed.

### Claude Code

Load the checkout as a plugin:

```bash
claude --plugin-dir /absolute/path/to/miriago-route-planner-skill
```

The plugin command is
`/miriago-route-planner-skill:plan-miriago-route`. Alternatively, copy or link
`plan-miriago-route/` into `~/.claude/skills/plan-miriago-route` and use
`/plan-miriago-route`.

## Usage

Ask the agent to use `plan-miriago-route` and provide the available inputs, for
example:

```text
Use plan-miriago-route to create a Tokyo pilgrimage plan for Bocchi the Rock!
from Anitabi, and merge my public Google My Maps link. Ask me at every review
gate and export an importable MiriaGo package.
```

The agent will ask for missing plan details before collecting sources. It will
also ask whether to use pedestrian road-network evaluation, whether route time
or distant points should be adjusted, whether to write journey notes, and
whether Anitabi full-resolution images should be included.

The bundled CLI is available for deterministic workflow steps:

```bash
python3 plan-miriago-route/scripts/miriago_plan.py --help
```

Reference-image downloads use 8 workers by default. Package-writing commands
accept `--image-download-workers 1-32`; use 1 when strict request ordering is
needed. Progress is flushed as JSON Lines to stderr while the final result stays
on stdout.

## Requirements

- Python 3.10 or newer
- Pillow when non-Anitabi images need generated thumbnails
- Public network access for Bangumi, Anitabi, Google My Maps, Overpass, or
  Valhalla features

Install the optional image dependency with:

```bash
python3 -m pip install -r plan-miriago-route/scripts/requirements.txt
```

Only public, keyless services are supported. Private Google My Maps data must be
provided as an exported KML/KMZ file; the skill does not use login cookies or API
tokens.

## Validation

Run the Python checks from the skill directory:

```bash
cd plan-miriago-route
ruff check scripts tests
PYTHONPATH=scripts python3 -m unittest discover -s tests -t . -v
```

The current package and plugin version is `1.6.0`.
