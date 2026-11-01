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

### To evaluate

Suggestions from the [withqwerty](https://github.com/withqwerty) repositories (reviewed 2026-11-01), the makers of the Reep register (ADR 32). Weigh each one at the start of its milestone. They are not official vendor skills, so adopting one needs an ADR amending ADR 26.

| Milestone | Suggestion | What to evaluate | Source |
|---|---|---|---|
| M4 | `football-docs` MCP server | Provider docs for coding agents (Understat, FBref, Transfermarkt, soccerdata) and metric cards: checked definitions of xG, xA, progressive passes and carries, PPDA and xT, each variant under its own ID. Candidate source for the Style Profile metric definitions (ADR 12). Shared as `.mcp.json` with a pinned version, approved via `enabledMcpjsonServers` in `.claude/settings.json`. | [withqwerty/football-docs](https://github.com/withqwerty/football-docs) |
| M4 | nutmeg `data-reviewer` agent | Reviews football data code: coordinate systems, own goals, set-piece filters, per-90 with a minimum-minutes threshold, sample size, xG source. Candidate for gate-2 reviews of marts and Style Profiles, copied into `.claude/agents/` without the plugin's paths. | [withqwerty/nutmeg](https://github.com/withqwerty/nutmeg) (`agents/data-reviewer.md`) |
| M4, M5 | nutmeg analysis rules | Minimum minutes, possession-adjusted defensive actions, one named metric variant per stat, and "the stat must carry the claim". Candidates for the Style Profile ADR, the scouting agent's instructions, and eval cases (ADR 9). They become our rules, not a vendored skill. | [withqwerty/nutmeg](https://github.com/withqwerty/nutmeg) (`hooks/session-start.json`, `skills/compute`, `skills/analyse`) |
| M6 | nutmeg `chart-reviewer` agent, with Campos | Reviews football chart code, including [Campos](https://github.com/withqwerty/campos) (MIT) React pitch and shot-map components, a candidate for the chat's inline charts. | [withqwerty/nutmeg](https://github.com/withqwerty/nutmeg) (`agents/chart-reviewer.md`, `skills/_shared/campos-bridge.md`) |

Do not enable the whole nutmeg plugin: it adds 11 analyst-workflow skills plus hooks that run on every shell command and at the end of every turn. Its README says MIT, but the repository has no `LICENSE` file; keep the licence text with anything copied from it.

No official skill exists for FastAPI, uv/ruff, Fly.io, Auth.js or GitHub Actions (checked 2026-10-12); use their official docs.

## Adding or updating a skill

1. Install only from the vendor's own repository, never from a directory listing or copy.
2. Read the `SKILL.md`, its references, and any `hooks/`, `.mcp.json` or `scripts/` before enabling it.
3. Record the reviewed commit in the table above.
4. Commit as `chore(skills): enable <skill>`.
