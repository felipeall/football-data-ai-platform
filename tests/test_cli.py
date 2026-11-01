"""The `fdap` entry point, run as a subprocess with stdout to a pipe."""

import os
import selectors
import subprocess
import sys

# Runs the real `main()` for `fdap transform`, with the Transform Run swapped for a
# stub that prints the start line and then waits, as the real command waits for the run.
CHILD = """
import sys
from football_data_ai_platform.ingestion import cli

async def transform(settings):
    print("Started Transform Run stub; waiting for it to finish")
    sys.stdin.readline()
    print("done")

cli.transform = transform
sys.argv = ["fdap", "transform"]
cli.main()
"""


def test_start_line_reaches_a_pipe_before_the_command_finishes() -> None:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"}
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            ready = selector.select(timeout=10)
        assert ready, "the start line did not reach the pipe while the command was waiting"
        assert child.stdout.readline() == "Started Transform Run stub; waiting for it to finish\n"
        assert child.poll() is None
    finally:
        child.stdin.write("\n")
        child.stdin.close()
        child.wait(timeout=10)
    assert child.stdout.read() == "done\n"
    assert child.returncode == 0
