# Agent skills

Official vendor skills that help coding agents work with this stack. Decision and rules: [ADR 26](adr/0026-agent-skills.md).

Skills are enabled just in time — at the start of the milestone that needs them — so every conversation carries only the skill descriptions it can use.

## Enabled

| Skill | Source | How | Reviewed at | Enabled |
|---|---|---|---|---|
| `temporal-developer` (+ `temporal-ops`, `temporal-cloud-setup`, `temporal-serverless`) | [temporalio/claude-temporal-plugin](https://github.com/temporalio/claude-temporal-plugin) | plugin `temporal@temporal-marketplace` in `.claude/settings.json` | `be14524` (v0.4.3) | M1 |
| `pydantic` | [pydantic/skills](https://github.com/pydantic/skills) | plugin `pydantic@pydantic-skills` in `.claude/settings.json` | `585b93f` (v0.1.0) | M1 |
| `langfuse` | [langfuse/skills](https://github.com/langfuse/skills) | vendored in `.claude/skills/langfuse/` (MIT; the repo publishes no plugin marketplace) | `b86532b` (v1.10.0) | M1 |
| `dbt` (`using-dbt-for-analytics-engineering`, `adding-dbt-unit-test`, `running-dbt-commands`, …) | [dbt-labs/dbt-agent-skills](https://github.com/dbt-labs/dbt-agent-skills) | plugin `dbt@dbt-agent-marketplace` in `.claude/settings.json`; not `dbt-migration`, which reports telemetry | `b8facde` (v1.6.0) | M3 |
| `claude-api` | bundled with Claude Code | — | — | always |

## Planned

| Milestone | Skill | Source |
|---|---|---|
| M4 | `mcp-builder` | [anthropics/skills](https://github.com/anthropics/skills) |
| M6 | `react-best-practices`, `composition-patterns`, `deploy-to-vercel`; `next-best-practices` | [vercel-labs/agent-skills](https://github.com/vercel-labs/agent-skills), [vercel-labs/next-skills](https://github.com/vercel-labs/next-skills) |
| M7 | `neon-postgres` plugin (includes the Neon MCP server) | [neondatabase/agent-skills](https://github.com/neondatabase/agent-skills) |
| as needed | `playwright-cli` (Sofascore fallback, UI end-to-end tests) | [microsoft/playwright-cli](https://github.com/microsoft/playwright-cli) |
| optional | `supabase-postgres-best-practices` | [supabase/agent-skills](https://github.com/supabase/agent-skills) |

No official skill exists for FastAPI, uv/ruff, Fly.io, Auth.js or GitHub Actions (checked 2026-10-12); use their official docs.

## Adding or updating a skill

1. Install only from the vendor's own repository, never from a directory listing or copy.
2. Read the `SKILL.md`, its references, and any `hooks/`, `.mcp.json` or `scripts/` before enabling it.
3. Record the reviewed commit in the table above.
4. Commit as `chore(skills): enable <skill>`.
