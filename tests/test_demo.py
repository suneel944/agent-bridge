"""Runs `agent-parley demo` end to end and checks that it leaves nothing."""

import io
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_parley import demo, demo_scenario

STAGES = (
    ("One loopback coordination server", "$ agent-parley up"),
    ("ada runs on claude", "$ agent-parley run ada"),
    ("grace runs on codex", "$ agent-parley run grace"),
    ("ada claims #41", "$ agent-parley issue claim 41"),
    ("a named collision", "$ grace: file_reservation_paths"),
    ("grace queues behind ada", "$ grace: request_reservation"),
    ("refused before it runs", "$ ada: PreToolUse Bash: git checkout"),
    ("ada offers #41 to grace", "$ agent-parley issue offer 41 --to grace"),
    ("only then does ownership move", "$ agent-parley issue accept 41"),
    ("The dashboard", "$ agent-parley top --once"),
    ("Done in", "the sandbox is removed"),
)
SANDBOX = re.compile(r"agent-parley setup (\S+)/payments-api")


def test_captured_output_keeps_its_lines_and_drops_trailing_blanks():
    assert demo.lines("one\ntwo\n\n\n") == ("one", "two")


def test_a_line_wider_than_the_frame_wraps_the_way_a_terminal_wraps():
    body = "x" * (demo.COLUMNS + 3)
    assert demo.lines(body) == ("x" * demo.COLUMNS, "xxx")


def test_the_tour_is_a_cut_of_the_recorded_story():
    toured = set(demo_scenario.tour.__code__.co_names)
    recorded = set(demo_scenario.record.__code__.co_names)
    assert toured == {
        "lanes",
        "claims",
        "guardrails",
        "handoffs",
        "dashboard",
    }
    assert toured <= recorded


def test_the_demo_tells_every_stage_and_removes_everything(monkeypatch):
    created = []
    remaining_before_sweep = []
    recorders = []
    making = demo.sandbox_directory
    sweeping = demo.sweep
    closing = demo.Recorder.close

    def sandbox():
        path = making()
        created.append(path)
        return path

    def sweep(base):
        remaining_before_sweep.extend(demo.leftovers(base))
        sweeping(base)

    def close(self):
        recorders.append(self)
        closing(self)

    monkeypatch.setattr(demo, "sandbox_directory", sandbox)
    monkeypatch.setattr(demo, "sweep", sweep)
    monkeypatch.setattr(demo.Recorder, "close", close)
    printed = io.StringIO()
    started = time.monotonic()
    assert demo.main(stream=printed, keys=io.StringIO()) == 0
    assert time.monotonic() - started < 90
    text = printed.getvalue()
    for caption, command in STAGES:
        assert any(
            caption in row and command in row for row in text.splitlines()
        ), (caption, command, text)
    assert "PARTICIPANT" in text
    assert "\x1b[" not in text
    (base,) = created
    assert not base.exists()
    assert remaining_before_sweep == []
    assert demo.leftovers(base) == []
    (recorder,) = recorders
    assert all(lane.poll() is not None for lane in recorder.sessions)
    assert not Path(os.environ["AGENT_PARLEY_HOME"]).exists()


def read_until(stream, marker, deadline):
    seen = b""
    while marker not in seen:
        remaining = deadline - time.monotonic()
        assert remaining > 0, seen.decode()
        if select.select([stream], [], [], remaining)[0]:
            chunk = os.read(stream.fileno(), 4096)
            assert chunk, seen.decode()
            seen += chunk
    return seen


@pytest.mark.parametrize("stop", ["q", "ctrl-c"])
def test_q_or_ctrl_c_stops_the_demo_and_removes_everything(stop):
    controller, keyboard = pty.openpty()
    child = subprocess.Popen(
        [sys.executable, "-m", "agent_parley", "demo"],
        stdin=keyboard if stop == "q" else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    os.close(keyboard)
    try:
        deadline = time.monotonic() + 60
        seen = read_until(child.stdout, b"ada runs on claude", deadline)
        if stop == "q":
            os.write(controller, b"q")
        else:
            os.killpg(child.pid, signal.SIGINT)
        rest, _ = child.communicate(timeout=60)
    finally:
        os.close(controller)
        if child.poll() is None:
            child.kill()
            child.wait()
    text = (seen + rest).decode()
    assert child.returncode == 130, text
    assert "Stopped." in text
    assert "The sandbox is removed." in text
    found = SANDBOX.search(text)
    assert found, text
    base = Path(found.group(1))
    assert not base.exists()
    assert demo.leftovers(base) == []
