# 36. Repository renamed to football-data-ai-platform; internal names stay

Status: Accepted — 2026-11-01. Supersedes the repository name in ADR 2. Internal names amended by ADR 37 and ADR 38.

## Context
ADR 2 started a fresh repository under a short working name. That name says little about what the project is to someone who finds it on GitHub. The v1 repository, `football-data-platform`, was still unarchived, although ADR 2 decided to archive it.

The working name is also wired into the code and the stack: the Python package, the CLI, the Compose project name (which prefixes every Docker volume, including the live database), the Postgres database and user, the dbt project and profile, the Langfuse organisation and project, CI and `scripts/worktree.sh`. That is about 270 references in some 50 files.

## Decision
- **The GitHub repository is `felipeall/football-data-ai-platform`.** GitHub redirects the old URLs, clones and issue links.
- **`football-data-platform` (v1) is archived,** its README pointing here.
- **Internal names keep the working name** for now: package, CLI, Compose project, database, dbt and Langfuse. Renaming them is planned separately, because the Compose project name keys the Docker volumes and the database name keys the live data.

## Consequences
- Docs that name the repository (README, `GLOSSARY.md`, AGENTS.md) use the new name. Docs that name the package, CLI, database or Compose project keep the working name, which is still correct.
- Until the internal names are renamed, the repository and the code carry different names.
- Local checkouts keep working through GitHub's redirect; `git remote set-url origin git@github.com:felipeall/football-data-ai-platform.git` makes it explicit.
