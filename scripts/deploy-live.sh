#!/usr/bin/env bash
# Deploy the shared stack: rebuild it from the main checkout's code and apply new
# migrations. Every post-merge live check deploys with this script (AGENTS.md, gate 3).
#
#   scripts/deploy-live.sh [extra `docker compose up` arguments]
#
# It runs `docker compose up --wait --build` in the main checkout, wherever it is
# called from, and guards against two failures of parallel deploys (#109):
#
# - A lock: a second deploy waits for the first instead of failing on the containers
#   the first is recreating. The lock is flock(2) on a file the deploy keeps open, so
#   the kernel frees it when the deploy's last process exits, even a killed one.
# - Leftover `migrate` containers: an interrupted recreate leaves them under a
#   prefixed name (`08b5ade48ff0_…-migrate-1`), and they block the next one. Only
#   stopped `migrate` containers are removed: the service runs Alembic and exits, so
#   it holds no state. Postgres, Temporal and every volume are left alone.
set -euo pipefail

export PATH="$HOME/.docker/bin:$PATH"

common=$(git rev-parse --path-format=absolute --git-common-dir)
main=$(dirname "$common")
project=$(sed -n 's/^name: *//p' "$main/compose.yaml")
lock="$common/deploy-live.lock"
holder="$common/deploy-live.holder"

# Takes the lock on fd 9; with `nowait`, exits 75 if another deploy holds it.
take_lock() {
    perl -MFcntl=:flock -e '
        open(my $fd, ">&=", 9) or die "deploy-live: cannot open the lock: $!\n";
        flock($fd, LOCK_EX | ($ARGV[0] eq "nowait" ? LOCK_NB : 0)) or exit 75;
    ' "$1"
}

exec 9>>"$lock"
status=0
take_lock nowait || status=$?
if [[ $status -eq 75 ]]; then
    echo "deploy-live: waiting for the deploy in progress ($(cat "$holder" 2>/dev/null || echo "holder unknown"))" >&2
    take_lock wait
elif [[ $status -ne 0 ]]; then
    exit "$status"
fi
echo "PID $$ from $PWD, started $(date '+%Y-%m-%d %H:%M:%S')" >"$holder"

leftovers=$(docker ps --all --quiet \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=migrate" \
    --filter status=created --filter status=exited --filter status=dead)
if [[ -n "$leftovers" ]]; then
    echo "deploy-live: removing stopped migrate containers: $(echo $leftovers)" >&2
    # shellcheck disable=SC2086  # one ID per word
    docker rm --force $leftovers >/dev/null
fi

cd "$main"
docker compose up --wait --build "$@"
