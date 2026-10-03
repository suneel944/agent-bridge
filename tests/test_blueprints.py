"""Checks blueprints: ordered deterministic and agent steps for one claim."""

import json
import sys
from pathlib import Path

import pytest

from agent_parley import blueprints, roster, store, unattended
from agent_parley.cli import main
from agent_parley.state import BridgeError, write_json


def step(tmp_path: Path, name: str, status: int = 0) -> str:
    """Writes a step that records its name in a shared log and exits."""
    script = tmp_path / f"{name}.sh"
    log = tmp_path / "steps.log"
    script.write_text(
        f"#!/bin/sh\necho {name} >> '{log}'\necho {name} output\n"
        f"exit {status}\n"
    )
    script.chmod(0o755)
    return str(script)


def ran(tmp_path: Path) -> list[str]:
    """Reads the names of the steps that ran, in order."""
    log = tmp_path / "steps.log"
    return log.read_text().split() if log.exists() else []


def record(tmp_path: Path, repo: Path, name: str, nodes: list) -> Path:
    """Writes a blueprint file outside the repository."""
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"nodes": nodes}))
    return path


@pytest.fixture
def claimed(bridge, repo, paired, monkeypatch):
    """Leaves the codex lane holding issue 42 in an operator shell."""
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    store.initialize(bridge.home)
    for participant in ("claude", "codex"):
        store.register(bridge.home, paired["root"], participant)
    lane = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "42")
    return lane


def test_three_nodes_run_in_order_and_the_agent_node_waits_for_a_report(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "feature",
        [
            {"name": "format", "kind": "run", "command": [step(tmp_path, "f")]},
            {"name": "implement", "kind": "agent", "prompt": "Implement it."},
            {"name": "tests", "kind": "run", "command": [step(tmp_path, "t")]},
        ],
    )
    assert "format -> implement -> tests" in blueprints.define(
        bridge, repo, "feature", path
    )

    waiting = blueprints.start(bridge, repo, "codex", "feature", "#42")

    assert "waiting at implement; passed format" in waiting
    assert ran(tmp_path) == ["f"]
    assert "waiting at implement" in blueprints.advance(bridge, repo, "42")
    assert ran(tmp_path) == ["f"]
    found = bridge.mail(repo, "search", query="Implement", participant="codex")
    assert "Implement it." in json.dumps(found)

    bridge.report(claimed, "ready", "Implemented", "", "make check: passed")
    finished = blueprints.advance(bridge, repo, "42")

    assert "done at tests; passed format, implement, tests" in finished
    assert ran(tmp_path) == ["f", "t"]
    assert "done" in blueprints.progress(bridge, repo, "42")
    with pytest.raises(BridgeError, match="no blueprint run in progress"):
        blueprints.advance(bridge, repo, "42")


def test_a_failing_node_follows_its_failure_edge_until_its_retry_limit(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "fixing",
        [
            {
                "name": "tests",
                "kind": "run",
                "command": [step(tmp_path, "t", 3)],
                "on_failure": "fix",
                "retries": 1,
                "next": "done",
            },
            {
                "name": "fix",
                "kind": "agent",
                "prompt": "Fix the failing checks.",
                "next": "tests",
            },
            {"name": "done", "kind": "report"},
        ],
    )
    blueprints.define(bridge, repo, "fixing", path)

    assert "waiting at fix" in blueprints.start(
        bridge, repo, "codex", "fixing", "42"
    )
    found = bridge.mail(repo, "search", query="Fix", participant="codex")
    assert "t output" in json.dumps(found)

    bridge.report(claimed, "ready", "Fixed", "", "make check: passed")
    blocked = blueprints.advance(bridge, repo, "42")

    assert "blocked at tests" in blocked
    assert "past its retry limit of 1" in blocked
    assert ran(tmp_path) == ["t", "t"]


def test_a_node_without_a_failure_edge_retries_itself(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "flaky",
        [
            {
                "name": "tests",
                "kind": "run",
                "command": [step(tmp_path, "t", 1)],
                "retries": 2,
            }
        ],
    )
    blueprints.define(bridge, repo, "flaky", path)

    blocked = blueprints.start(bridge, repo, "codex", "flaky", "42")

    assert "blocked at tests; passed none" in blocked
    assert ran(tmp_path) == ["t", "t", "t"]


def test_a_lane_cannot_set_start_or_advance_a_blueprint(
    bridge, repo, claimed, tmp_path, monkeypatch
):
    path = record(
        tmp_path,
        repo,
        "plant",
        [{"name": "x", "kind": "run", "command": [step(tmp_path, "x")]}],
    )
    with pytest.raises(BridgeError, match="operator shell"):
        blueprints.define(bridge, claimed, "plant", path)
    monkeypatch.setenv(unattended.LANE_TOKEN, "lane-token")
    for attempt in (
        lambda: blueprints.define(bridge, repo, "plant", path),
        lambda: blueprints.start(bridge, repo, "codex", "plant", "42"),
        lambda: blueprints.advance(bridge, repo, "42"),
    ):
        with pytest.raises(BridgeError, match="operator shell"):
            attempt()
    monkeypatch.delenv(unattended.LANE_TOKEN)
    assert "no blueprints" in blueprints.describe(bridge, repo)
    assert ran(tmp_path) == []


def test_a_run_needs_the_lane_to_hold_the_issue(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "one",
        [{"name": "x", "kind": "run", "command": [step(tmp_path, "x")]}],
    )
    blueprints.define(bridge, repo, "one", path)
    with pytest.raises(BridgeError, match="does not hold issue #42"):
        blueprints.start(bridge, repo, "claude", "one", "42")
    with pytest.raises(BridgeError, match="No blueprint named"):
        blueprints.start(bridge, repo, "codex", "missing", "42")


def test_a_manifest_without_blueprints_reads_and_behaves_as_before(
    bridge, repo, claimed, tmp_path
):
    _, directory = bridge.project(repo)
    stored = json.loads((directory / "project.json").read_text())
    assert "blueprints" not in stored
    write_json(directory / "project.json", stored)
    assert "blueprints" not in roster.read(directory)
    assert blueprints.progress(bridge, repo) == (
        "No blueprint runs are recorded."
    )
    bridge.verification(repo, step(tmp_path, "gate"))
    path = record(
        tmp_path,
        repo,
        "kept",
        [{"name": "x", "kind": "push"}],
    )
    blueprints.define(bridge, repo, "kept", path)
    data = roster.read(directory)
    assert data["blueprints"]["kept"][0]["kind"] == "push"
    assert data["verify"] == [step(tmp_path, "gate")]
    assert "Removed blueprint kept." == blueprints.define(
        bridge, repo, "kept", None
    )
    assert "blueprints" not in roster.read(directory)


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([{"name": "m", "kind": "merge"}], "kind must be one of"),
        (
            [{"name": "a", "kind": "push", "next": "nowhere"}],
            "unknown next node",
        ),
        ([{"name": "a", "kind": "run"}], "needs a command"),
        ([{"name": "a", "kind": "agent"}], "needs a prompt"),
        (
            [{"name": "a", "kind": "push"}, {"name": "a", "kind": "push"}],
            "unique",
        ),
        ([{"name": "a", "kind": "push", "retries": -1}], "retries"),
        ([{"name": "a", "kind": "push", "shell": True}], "unknown fields"),
        ([], "1 to"),
    ],
)
def test_an_invalid_blueprint_is_refused(nodes, message):
    with pytest.raises(BridgeError, match=message):
        blueprints.parse(json.dumps({"nodes": nodes}))


def test_the_command_line_sets_and_shows_a_blueprint(
    bridge, repo, claimed, tmp_path, monkeypatch, capsys
):
    path = record(
        tmp_path,
        repo,
        "cli",
        [
            {"name": "lint", "kind": "run", "command": "ruff check ."},
            {"name": "stop", "kind": "report", "outcome": "blocked"},
        ],
    )
    home = ["agent-parley", "--home", str(bridge.home), "blueprint"]
    monkeypatch.setattr(
        sys, "argv", [*home, "set", "cli", str(path), "--repo", str(repo)]
    )
    assert main() == 0
    monkeypatch.setattr(
        sys, "argv", [*home, "show", "cli", "--repo", str(repo)]
    )
    assert main() == 0
    shown = capsys.readouterr().out
    assert "lint [run] ruff check . ok->stop fail->lint retries=0" in shown
    assert "stop [report] blocked" in shown
