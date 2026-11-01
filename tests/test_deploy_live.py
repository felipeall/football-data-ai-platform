"""scripts/deploy-live.sh, run against a stub `docker` that logs its calls."""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "deploy-live.sh"

STUB = """#!/usr/bin/env bash
case "$1" in
    ps) echo "ps ${*:2}" >>"$STUB_LOG"; printf '%s\\n' $STUB_LEFTOVERS ;;
    rm) echo "rm ${*:2}" >>"$STUB_LOG" ;;
    compose)
        echo "up-start $PPID" >>"$STUB_LOG"
        sleep "${STUB_UP_SECONDS:-0}"
        echo "up-end $PPID" >>"$STUB_LOG"
        exit "${STUB_UP_EXIT:-0}" ;;
esac
"""


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A main checkout holding the script and a compose.yaml, with a stub `docker`."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "compose.yaml").write_text("name: football-data-ai-platform\nservices: {}\n")
    shutil.copy(SCRIPT, repo / "scripts" / "deploy-live.sh")
    bin_dir = tmp_path / "home" / ".docker" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "docker").write_text(STUB)
    (bin_dir / "docker").chmod(0o755)
    return repo


def deploy(checkout: Path, **stub: str) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "HOME": str(checkout.parent / "home"),
        "STUB_LOG": str(checkout.parent / "docker.log"),
        **stub,
    }
    return subprocess.Popen(
        ["scripts/deploy-live.sh"],
        cwd=checkout,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def log(checkout: Path) -> list[str]:
    path = checkout.parent / "docker.log"
    return path.read_text().splitlines() if path.exists() else []


def wait_for_up(checkout: Path, starts: int = 1) -> None:
    deadline = time.monotonic() + 10
    while sum(line.startswith("up-start") for line in log(checkout)) < starts:
        assert time.monotonic() < deadline, "docker compose up never started"
        time.sleep(0.05)


def test_second_deploy_waits_for_the_first(checkout: Path) -> None:
    first = deploy(checkout, STUB_UP_SECONDS="1")
    wait_for_up(checkout)
    second = deploy(checkout)
    _, second_err = second.communicate(timeout=10)
    first.communicate(timeout=10)

    assert (first.returncode, second.returncode) == (0, 0)
    assert f"waiting for the deploy in progress (PID {first.pid} " in second_err
    ups = [line for line in log(checkout) if line.startswith("up-")]
    assert ups == [
        f"up-start {first.pid}",
        f"up-end {first.pid}",
        f"up-start {second.pid}",
        f"up-end {second.pid}",
    ]


def test_killed_deploy_leaves_no_lock(checkout: Path) -> None:
    killed = deploy(checkout, STUB_UP_SECONDS="30")
    wait_for_up(checkout)
    os.killpg(killed.pid, signal.SIGKILL)
    killed.wait(timeout=5)

    _, err = (after := deploy(checkout)).communicate(timeout=10)

    assert after.returncode == 0
    assert "waiting" not in err


def test_failed_deploy_releases_the_lock(checkout: Path) -> None:
    failed = deploy(checkout, STUB_UP_EXIT="1")
    failed.communicate(timeout=10)
    _, err = (after := deploy(checkout)).communicate(timeout=10)

    assert (failed.returncode, after.returncode) == (1, 0)
    assert "waiting" not in err


def test_removes_only_stopped_migrate_containers_before_up(checkout: Path) -> None:
    deploy(checkout, STUB_LEFTOVERS="08b5ade48ff0 57f8c496c9f2").communicate(timeout=10)

    ps, rm, up = log(checkout)[:3]
    assert ps == (
        "ps --all --quiet"
        " --filter label=com.docker.compose.project=football-data-ai-platform"
        " --filter label=com.docker.compose.service=migrate"
        " --filter status=created --filter status=exited --filter status=dead"
    )
    assert rm == "rm --force 08b5ade48ff0 57f8c496c9f2"
    assert up.startswith("up-start")


def test_removes_nothing_without_leftovers(checkout: Path) -> None:
    deploy(checkout).communicate(timeout=10)

    assert not any(line.startswith("rm") for line in log(checkout))
