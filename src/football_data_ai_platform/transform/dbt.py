"""dbt as an activity: `dbt build` as a subprocess of the worker (ADR 31)."""

import asyncio
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.conninfo import conninfo_to_dict
from temporalio import activity
from temporalio.exceptions import ApplicationError

# dbt's exit code when a model or test fails: running it again fails the same way, unless
# it failed because the build lost its database connection.
_FAILED = 1
# dbt-postgres (psycopg2) messages of a lost or refused database connection.
CONNECTION_ERRORS = (
    "server closed the connection unexpectedly",
    "could not connect to server",
    "terminating connection due to administrator command",
    "connection already closed",
    "SSL connection has been closed unexpectedly",
)
_CONNECTION_FAILED = re.compile(r"connection to server at .* failed")
# A test that warns: `20 of 40 WARN 3 warn_matches_left_out_of_the_marts ... [WARN 3 in 0.05s]`.
_WARNING = re.compile(r"^\d+ of \d+ WARN \d+ ")
_TIME = re.compile(r"^\d\d:\d\d:\d\d\s+")
# dbt's line when it parses the whole project: `Unable to do partial parsing because …`.
FULL_PARSE = "Unable to do partial parsing"


@dataclass(frozen=True)
class DbtBuildResult:
    summary: str  # dbt's last line, e.g. `Done. PASS=40 WARN=1 ERROR=0 SKIP=0 NO-OP=0 TOTAL=41`
    warnings: list[str]  # the lines of the tests that warned


class DbtActivities:
    """Runs `dbt build` on the project in `project_dir` against `database_url`'s database.
    `covered_season` (ADR 35), by its start year, is the dbt var `covered_seasons`: the
    Season the Player marts hold.

    Every build writes to `target_dir`, so dbt parses the project only when it changed
    (partial parsing). One Transform Run at a time builds a database (ADR 31): give each
    database its own `target_dir`, never one shared by concurrent builds."""

    def __init__(
        self,
        database_url: str,
        project_dir: Path = Path("dbt"),
        *,
        covered_season: int,
        target_dir: Path,
    ) -> None:
        conninfo = conninfo_to_dict(database_url)
        self._env = {
            "POSTGRES_HOST": str(conninfo["host"]),
            "POSTGRES_PORT": str(conninfo["port"]),
            "POSTGRES_DB": str(conninfo["dbname"]),
            "POSTGRES_USER": str(conninfo["user"]),
            "POSTGRES_PASSWORD": str(conninfo.get("password", "")),
        }
        self._project_dir = project_dir.resolve()
        self._vars = json.dumps({"covered_seasons": [covered_season]})
        self._target_dir = target_dir.resolve()

    @activity.defn
    async def build_dbt(self, selection: list[str]) -> DbtBuildResult:
        """`dbt build` with `selection`. A test runs only once all its parents are selected
        (`cautious`), so a step never tests a model a later step builds. Exit 1, a failed
        model or test, fails without retries, unless every failure in `run_results.json` is
        a lost database connection. Any other exit, such as dbt's own crash (2), is retried."""
        project = [
            "--project-dir",
            str(self._project_dir),
            "--profiles-dir",
            str(self._project_dir),
            "--vars",
            self._vars,
        ]
        # The worker cannot write into the project in its image: target and logs go elsewhere.
        # The target directory outlives the build, for partial parsing; the logs do not.
        target = self._target_dir
        with tempfile.TemporaryDirectory(prefix="dbt-logs-") as logs:
            paths = ["--target-path", str(target), "--log-path", logs]
            # Only this build's results may decide a retry, not the last build's.
            (target / "run_results.json").unlink(missing_ok=True)
            process = await asyncio.create_subprocess_exec(
                Path(sys.executable).parent / "dbt",
                "build",
                "--no-use-colors",
                *project,
                *paths,
                "--indirect-selection",
                "cautious",
                *selection,
                env=os.environ | self._env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                output, _ = await process.communicate()
            except asyncio.CancelledError:
                # A timed-out attempt's dbt must not build alongside the retry's.
                process.kill()
                await process.wait()
                raise
            results = _read_run_results(target / "run_results.json")
        # Without dbt's `HH:MM:SS` prefix.
        lines = [_TIME.sub("", line) for line in output.decode(errors="replace").splitlines()]
        tail = "\n".join(lines[-40:])
        for line in lines:
            if line.startswith(FULL_PARSE):
                activity.logger.info("dbt: %s", line)
        if process.returncode != 0:
            raise ApplicationError(
                f"dbt build {' '.join(selection)} exited {process.returncode}:\n{tail}",
                type="DbtBuildFailed",
                non_retryable=process.returncode == _FAILED and not lost_connection(results),
            )
        warnings = [line for line in lines if _WARNING.match(line)]
        for line in warnings:
            activity.logger.warning("dbt: %s", line)
        summary = next((line for line in reversed(lines) if line.startswith("Done.")), "")
        activity.logger.info("dbt build %s: %s", " ".join(selection), summary)
        return DbtBuildResult(summary, warnings)


def dbt_target_dir(database_url: str) -> Path:
    """The worker's dbt target directory: one per database, in the temporary directory,
    since the worker's image keeps the project read-only. A new container starts it empty."""
    return Path(tempfile.gettempdir()) / f"dbt-target-{conninfo_to_dict(database_url)['dbname']}"


def _read_run_results(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def lost_connection(run_results: dict[str, Any] | None) -> bool:
    """Whether every failed node of a build failed on a lost database connection, so the
    build may pass when run again. A failing test (`fail`), a failure of any other kind, or
    no results at all means it would not."""
    if not run_results:
        return False
    failures = [r for r in run_results.get("results", []) if r.get("status") in ("error", "fail")]
    return bool(failures) and all(
        failure["status"] == "error" and _is_connection_error(failure.get("message") or "")
        for failure in failures
    )


def _is_connection_error(message: str) -> bool:
    return any(error in message for error in CONNECTION_ERRORS) or bool(
        _CONNECTION_FAILED.search(message)
    )
