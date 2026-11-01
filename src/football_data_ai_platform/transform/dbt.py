"""dbt as an activity: `dbt build` as a subprocess of the worker (ADR 31)."""

import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict
from temporalio import activity
from temporalio.exceptions import ApplicationError

# dbt's exit code when a model or test fails: running it again fails the same way.
_FAILED = 1
# A test that warns: `20 of 40 WARN 3 warn_matches_left_out_of_the_marts ... [WARN 3 in 0.05s]`.
_WARNING = re.compile(r"^\d+ of \d+ WARN \d+ ")
_TIME = re.compile(r"^\d\d:\d\d:\d\d\s+")


@dataclass(frozen=True)
class DbtBuildResult:
    summary: str  # dbt's last line, e.g. `Done. PASS=40 WARN=1 ERROR=0 SKIP=0 NO-OP=0 TOTAL=41`
    warnings: list[str]  # the lines of the tests that warned


class DbtActivities:
    """Runs `dbt build` on the project in `project_dir` against `database_url`'s database."""

    def __init__(self, database_url: str, project_dir: Path = Path("dbt")) -> None:
        conninfo = conninfo_to_dict(database_url)
        self._env = {
            "POSTGRES_HOST": str(conninfo["host"]),
            "POSTGRES_PORT": str(conninfo["port"]),
            "POSTGRES_DB": str(conninfo["dbname"]),
            "POSTGRES_USER": str(conninfo["user"]),
            "POSTGRES_PASSWORD": str(conninfo.get("password", "")),
        }
        self._project_dir = project_dir.resolve()

    @activity.defn
    async def build_dbt(self, selection: list[str]) -> DbtBuildResult:
        """`dbt build` with `selection`. A test runs only once all its parents are selected
        (`cautious`), so a step never tests a model a later step builds. Exit 1, a failed
        model or test, fails without retries; dbt reports a lost connection the same way.
        Any other exit, such as dbt's own crash (2), is retried."""
        project = [
            "--project-dir",
            str(self._project_dir),
            "--profiles-dir",
            str(self._project_dir),
        ]
        # The worker cannot write into the project in its image: target and logs go elsewhere.
        with tempfile.TemporaryDirectory(prefix="dbt-") as scratch:
            paths = ["--target-path", f"{scratch}/target", "--log-path", f"{scratch}/logs"]
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
        # Without dbt's `HH:MM:SS` prefix.
        lines = [_TIME.sub("", line) for line in output.decode(errors="replace").splitlines()]
        tail = "\n".join(lines[-40:])
        if process.returncode != 0:
            raise ApplicationError(
                f"dbt build {' '.join(selection)} exited {process.returncode}:\n{tail}",
                type="DbtBuildFailed",
                non_retryable=process.returncode == _FAILED,
            )
        warnings = [line for line in lines if _WARNING.match(line)]
        for line in warnings:
            activity.logger.warning("dbt: %s", line)
        summary = next((line for line in reversed(lines) if line.startswith("Done.")), "")
        activity.logger.info("dbt build %s: %s", " ".join(selection), summary)
        return DbtBuildResult(summary, warnings)
