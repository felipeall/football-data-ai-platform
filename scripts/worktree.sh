#!/usr/bin/env bash
# Give a linked worktree (an Orca workspace) its own database and task queue on
# the shared local stack, so its agent never touches live data or the live worker.
#
#   scripts/worktree.sh setup       write .env, link data/, uv sync, create the database
#   scripts/worktree.sh refresh-db  replace the worktree database with a copy of live
#   scripts/worktree.sh teardown    drop the worktree database and its test database
#
# Run it from the worktree. The main checkout owns the stack; see AGENTS.md.
set -euo pipefail

export PATH="$HOME/.docker/bin:$PATH"

worktree=$(git rev-parse --show-toplevel)
main=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")

if [[ "$worktree" == "$main" ]]; then
    echo "This is the main checkout; it uses live settings. Run this from a worktree." >&2
    exit 1
fi

# Postgres truncates names at 63 bytes. 29 + 19 leaves 15 for suffixes a test adds to
# the database name (`_test` from tests/conftest.py, then a migration test's own).
slug=$(basename "$worktree" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9\n' '_' | cut -c1-19)
database="football_data_ai_platform_wt_${slug}"
queue="wt-${slug}"

# A worktree set up earlier keeps the names its .env already holds, so teardown
# drops the database it actually has.
if [[ -f "$worktree/.env" ]]; then
    existing=$(sed -n 's/^POSTGRES_DB=\(football_data_ai_platform_wt_[a-z0-9_]*\)$/\1/p' "$worktree/.env" | tail -1)
    if [[ -n "$existing" ]]; then
        database=$existing
        queue=$(sed -n 's/^TEMPORAL_TASK_QUEUE=\(wt-[a-z0-9_]*\)$/\1/p' "$worktree/.env" | tail -1)
        queue=${queue:-wt-${slug}}
    fi
fi

compose() {
    docker compose --project-directory "$main" -f "$main/compose.yaml" "$@"
}

psql_live() {
    # Runs inside the Postgres container, as the live database's owner.
    compose exec -T -e PGOPTIONS="-c client_min_messages=warning" postgres sh -c "psql -v ON_ERROR_STOP=1 -q -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" $*"
}

postgres_up() {
    compose exec -T postgres pg_isready -q 2>/dev/null
}

create_database() {
    if ! postgres_up; then
        echo "Postgres is not running; start the stack from $main, then run: scripts/worktree.sh setup" >&2
        return 0
    fi
    if [[ -z $(psql_live "-Atc \"select 1 from pg_database where datname = '$database'\"") ]]; then
        psql_live "-c 'create database $database'"
        echo "Created empty database $database"
    fi
}

case "${1:-}" in
    setup)
        if [[ ! -f "$main/.env" ]]; then
            echo "$main/.env is missing; copy .env.example there first." >&2
            exit 1
        fi
        {
            grep -vE '^(POSTGRES_DB|TEMPORAL_TASK_QUEUE)=' "$main/.env"
            echo
            echo "# Written by scripts/worktree.sh: this worktree's own database and task queue."
            echo "POSTGRES_DB=$database"
            echo "TEMPORAL_TASK_QUEUE=$queue"
        } >"$worktree/.env"
        ln -sfn "$main/data" "$worktree/data"
        uv sync
        create_database
        echo "Worktree database $database, task queue $queue"
        ;;
    refresh-db)
        if ! postgres_up; then
            echo "Postgres is not running; start the stack from $main." >&2
            exit 1
        fi
        psql_live "-c 'drop database if exists $database with (force)'"
        psql_live "-c 'create database $database'"
        compose exec -T postgres sh -c \
            "pg_dump -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" --no-owner | psql -v ON_ERROR_STOP=1 -q -U \"\$POSTGRES_USER\" -d $database >/dev/null"
        echo "Copied live into $database"
        ;;
    teardown)
        if postgres_up; then
            psql_live "-c 'drop database if exists $database with (force)'"
            psql_live "-c 'drop database if exists ${database}_test with (force)'"
            echo "Dropped $database and ${database}_test"
        fi
        ;;
    *)
        sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
