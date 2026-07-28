# Agent compatibility

This skill uses standard `SKILL.md` frontmatter and relative bundled-resource
links. Keep `plan-miriago-route/` intact on every supported agent.

## Codex

Copy or link `plan-miriago-route/` under `~/.codex/skills/`. Codex also reads
the optional `agents/openai.yaml` interface metadata.

## Pi

The repository root contains a `package.json` with `pi.skills`, so install a
checkout with `pi install /absolute/path/to/repository` or a published Git
repository with `pi install git:github.com/OWNER/miriago-route-planner-skill`.
For a one-off run, pass the skill directory with
`pi --skill /absolute/path/to/repository/plan-miriago-route`.

Pi also discovers a linked skill under `~/.pi/agent/skills/`,
`~/.agents/skills/`, `.pi/skills/`, or `.agents/skills/`. Invoke it explicitly
as `/skill:plan-miriago-route` when automatic selection is not desired.

## Claude Code

The repository root contains `.claude-plugin/plugin.json`. Load a checkout with
`claude --plugin-dir /absolute/path/to/repository`, or copy/link the skill
directory to `~/.claude/skills/plan-miriago-route` for personal use or
`.claude/skills/plan-miriago-route` for project use. A plugin checkout uses
`/miriago-route-planner-skill:plan-miriago-route`; a plain personal or project
skill uses `/plan-miriago-route`. Use the applicable command when automatic
selection is not desired.

Claude Code exposes `${CLAUDE_SKILL_DIR}`, but keep commands in `SKILL.md`
platform-neutral. Replace `<skill-directory>` with that value on Claude Code;
on Pi or Codex, use the absolute directory from which `SKILL.md` was loaded.
