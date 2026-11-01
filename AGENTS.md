# AGENTS.md

Football data platform: Temporal ingestion from scraped sources into Postgres, dbt transforms, and a Claude scouting agent served over MCP.

## Read before working

- `GLOSSARY.md` — the domain glossary. Name code, tables and tests with its terms (Player, Source Record, Raw Response, Appearance, Action, Style Profile…).
- `docs/adr/` — accepted decisions. Read the ADRs touching your area before changing it; a change that contradicts one needs a new ADR first.
- `docs/agent-skills.md` — vendor skills enabled for this repo (Temporal, Pydantic, Langfuse, …). Load the matching skill before working with that technology; add new ones by its checklist.

## Rules the code does not show

- Store every source response as a Raw Response before parsing it, so parsers can be re-run without re-fetching.
- Every source goes through the common source interface, which owns rate limits, retries and the circuit breaker.
- Keep secrets in `.env` only. Strip credentials from URLs before logging or storing them.
- Scraped data lives under `data/` or in the database, both outside version control.
- Temporal workflows stay deterministic: all I/O (HTTP, database, LLM calls) runs in activities.
- The agent is provider-neutral (ADR 24): core code talks to the `AgentRunner` and `LLMClient` ports and imports provider SDKs only inside `agent/providers/<provider>/`.
- Pick models by Model Role through configuration; model IDs live in configuration only.
- A new provider is done when it passes the shared runner conformance suite.
- Chat runs the runner directly; Temporal is for ingestion and long-running jobs.
- A new domain term goes into `GLOSSARY.md` in the same change.

## Critical review

Three gates. Each ends in a written verdict, and the work moves on only after it.

1. **Before implementing an issue, or accepting a new ADR: pre-mortem.** Assume the design shipped and failed, and find why. Check every assumption the issue and its ADRs rest on against what the code, live runs and other ADRs now show, and look for a smaller design that meets the same acceptance criteria. Post the pre-mortem as a comment on the issue (on the pull request for a new ADR): each finding with its evidence, then a verdict — *proceed*, *proceed with changes* (listed), or *needs a new ADR*. On *needs a new ADR*, stop and ask the user.
2. **Before asking to merge a pull request: review the full diff** against the issue's acceptance criteria and the ADRs — correctness, outcomes the tests leave unpinned, docs and glossary drift. Fix each finding, or list it under **Open questions** in the pull request body. The gate is done when every finding is fixed or listed.
3. **After merging a pull request: verify live.** From the main checkout, pull `main` and run `docker compose up --wait --build`, so the worker runs the merged code and new migrations apply. Exercise each acceptance criterion against the live stack with the smallest real run that covers it (one Match, one club, one agent question), and read the outcome from live data, worker logs or the Temporal UI. Post a **Live check** comment on the merged pull request with:
   - the commands and evidence, and a verdict: *verified*, or *failed*;
   - **Findings**: every bug, gap or improvement the run surfaced beyond the acceptance criteria, each with its evidence and a proposed issue title.

   On *failed*, reopen the issue, link the comment there, tell the user, and fix it on a new branch. Then put the findings to the user and create a GitHub issue for each one they pick, linked to the comment. A change with no runtime surface (docs, CI) gets a *no live surface* comment instead.

## Commits

[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/): `<type>(<scope>): <description>`.

- **description**: imperative, lowercase, no trailing period, ≤ 72 characters (`add sofascore lineups activity`).
- **body**: explains *why* when the subject does not; wrapped at 72 characters.
- **footers**: `Refs: ADR-0006`, `BREAKING CHANGE: <what changed and how to migrate>`. Breaking changes also take `!` after the type or scope.
- **types**: `feat`, `fix`, `docs` (README, ADRs, `GLOSSARY.md`), `refactor`, `perf`, `test` (tests and evals), `build` (dependencies, packaging, Docker images), `ci`, `chore` (touches no source or test code), `revert`.
- **scopes**: `sources`, `transfermarkt`, `sofascore`, `understat`, `workflows`, `identity`, `dbt`, `profiles`, `agent`, `mcp`, `evals`, `web`, `infra`, `skills`. Omit the scope when a change spans several areas.

## Decisions

- Every architectural decision gets an ADR in `docs/adr/NNNN-short-title.md` and a line in `docs/adr/README.md`.
- To supersede a decision, write a new ADR and set the old one's status to `Superseded by ADR-NNNN`.

## Branches and pull requests

- `main` is always releasable. One branch per change, branched from `main`, in its own worktree. Name it `<type>/<short-description>` (`feat/sofascore-heatmaps`; `orca worktree create --branch`); Orca's default `felipeall/<slug>` is fine too, since squash-merging keeps branch names off `main`.
- Pull request titles follow the commit format; squash-merge, so the title becomes the commit on `main`.
- Issues run in parallel when none has an open blocker (GitHub's native issue dependencies). Each gets one worktree, one branch and one pull request. Dependent work starts once its blocker has merged.
- When a pull request must build on an unmerged one, retarget it to `main` (`gh pr edit <n> --base main`) before merging its base. Squash-merging with `--delete-branch` closes every pull request still based on the deleted branch, and GitHub refuses to reopen one whose branch was force-pushed since.
- CI (`.github/workflows/ci.yml`) runs `ruff`, `pyright`, `pytest`, `import-linter` and `gitleaks` on every pull request and push to `main`. Merge only with all of them green, read from the run on the pull request's current head commit (`gh run list --branch <branch>`); a rebase or force-push starts a new run.
- `main` is protected: merges go through a pull request, need all five checks green on a branch up to date with `main`, and keep history linear. Force-pushes, deletion and direct pushes are refused, admins included.
- Pull requests close their issues with `Closes #N` in the body, never in a commit message: a commit pushed straight to `main` closes the issue on push, before its remaining criteria are met. After `gh issue reopen`, confirm the state with `gh issue view`.

## Worktrees and the shared stack

Several agents work at once, each in an Orca worktree, against one local stack (Postgres, Temporal and the `worker` container). The main checkout owns the stack and the live database; a worktree gets its own database and task queue from `scripts/worktree.sh`. Before running tests, migrations, the CLI or a worker from a worktree, read `docs/agents/worktrees.md`.

## Progress

Work is tracked in GitHub Issues, one GitHub milestone per roadmap milestone (M1–M7).

- At session start: read the open issues of the current milestone (`gh issue list --milestone "<title>"`) and the latest comment on the issue you pick up. Pick an issue with no open blocker and no assignee, then claim it with `gh issue edit <n> --add-assignee @me` before any other write.
- At session end: comment on that issue with what was done, what is next, and any blockers.
- A message to another agent's session states the state you leave behind: branch, head commit, and what is committed, pushed or uncommitted. Each worktree belongs to the agent working in it; change another agent's worktree only when that agent asks.
- When a milestone closes, update its row in the README roadmap.

## Agent skills

### Issue tracker

GitHub Issues on `felipeall/football-intel`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
