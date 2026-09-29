"""Checks that a lane is told once when its pull request's state changes."""

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from agent_parley import forge, issues, problems, store, supervision, tables

SHA = "a" * 40
NEXT = "b" * 40


@pytest.fixture
def project(bridge, paired, monkeypatch):
    """Registers both lanes and reads the forge on every call."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    monkeypatch.setattr(supervision, "PULL_REQUEST_SECONDS", 0.0)
    return Path(paired["lanes"]["claude"]).parent


def manifest(directory):
    """Reads the current participant manifest."""
    return supervision.roster.read(directory)


def reading(directory, **fields):
    """Builds one open pull request reading from the Claude lane branch."""
    base = {
        "number": 7,
        "url": "https://example.test/pull/7",
        "branch": manifest(directory)["participants"]["claude"]["branch"],
        "sha": SHA,
        "checks": "pending",
        "failing": [],
        "reviews": [],
        "mergeable": "MERGEABLE",
        "issues": [],
    }
    return {**base, **fields}


def observe(bridge, directory, monkeypatch, *readings):
    """Runs the pull request stage once with a fixed forge answer."""
    monkeypatch.setattr(
        supervision.forge, "open_pull_requests", lambda root: list(readings)
    )
    supervision.pull_request_wakes(bridge.home, directory, manifest(directory))


def received(bridge, name):
    """Returns the pull request notices one lane received, oldest first."""
    with store.connect(bridge.home) as db:
        return [
            row[0]
            for row in db.execute(
                "SELECT m.body_md FROM messages m "
                "JOIN message_recipients r ON r.message_id=m.id "
                "JOIN agents a ON a.id=r.agent_id WHERE a.name=? AND "
                "m.subject LIKE 'Pull request #%' ORDER BY m.id",
                (name,),
            )
        ]


def test_green_checks_wake_the_lane_once(bridge, project, monkeypatch):
    observe(bridge, project, monkeypatch, reading(project))
    assert received(bridge, "claude") == []
    observe(bridge, project, monkeypatch, reading(project, checks="green"))
    observe(bridge, project, monkeypatch, reading(project, checks="green"))
    notices = received(bridge, "claude")
    assert len(notices) == 1
    assert "#7" in notices[0] and SHA in notices[0]
    assert "checks passed" in notices[0]
    assert received(bridge, "codex") == []


def test_red_then_green_names_the_failing_job_then_the_pass(
    bridge, project, monkeypatch
):
    red = reading(project, checks="red", failing=["lint", "test (3.12)"])
    observe(bridge, project, monkeypatch, red)
    observe(bridge, project, monkeypatch, red)
    observe(
        bridge,
        project,
        monkeypatch,
        reading(project, sha=NEXT, checks="green"),
    )
    notices = received(bridge, "claude")
    assert len(notices) == 2
    assert "checks failed: lint, test (3.12)" in notices[0]
    assert SHA in notices[0]
    assert "checks passed" in notices[1] and NEXT in notices[1]


def test_a_submitted_review_wakes_the_lane(bridge, project, monkeypatch):
    observe(bridge, project, monkeypatch, reading(project))
    review = {"author": "alice", "state": "APPROVED", "at": "2026-09-27"}
    observe(bridge, project, monkeypatch, reading(project, reviews=[review]))
    observe(bridge, project, monkeypatch, reading(project, reviews=[review]))
    notices = received(bridge, "claude")
    assert len(notices) == 1
    assert "review submitted: alice APPROVED" in notices[0]


def test_becoming_mergeable_after_a_rebase_wakes_the_lane(
    bridge, project, monkeypatch
):
    observe(
        bridge, project, monkeypatch, reading(project, mergeable="CONFLICTING")
    )
    observe(bridge, project, monkeypatch, reading(project, sha=NEXT))
    notices = received(bridge, "claude")
    assert len(notices) == 1
    assert "merge state is now MERGEABLE" in notices[0] and NEXT in notices[0]


def test_nothing_changed_wakes_nobody(bridge, project, monkeypatch):
    quiet = reading(project, mergeable="UNKNOWN")
    observe(bridge, project, monkeypatch, quiet)
    observe(bridge, project, monkeypatch, quiet)
    assert received(bridge, "claude") == []


def test_a_pull_request_closing_a_claimed_issue_wakes_its_holder(
    bridge, paired, project, monkeypatch
):
    bridge.issue(Path(paired["lanes"]["codex"]), "claim", "1")
    observe(
        bridge,
        project,
        monkeypatch,
        reading(project, branch="feat/1-x", issues=["1"], checks="green"),
    )
    assert len(received(bridge, "codex")) == 1
    assert received(bridge, "claude") == []


def test_an_unreachable_forge_keeps_the_last_reading(
    bridge, project, monkeypatch
):
    observe(bridge, project, monkeypatch, reading(project, checks="green"))
    monkeypatch.setattr(
        supervision.forge, "open_pull_requests", lambda root: None
    )
    supervision.pull_request_wakes(bridge.home, project, manifest(project))
    observe(bridge, project, monkeypatch, reading(project, checks="green"))
    assert len(received(bridge, "claude")) == 1


def test_a_failing_forge_reader_never_stops_the_poll(
    bridge, project, monkeypatch
):
    def broken(root):
        raise RuntimeError("forge down")

    monkeypatch.setattr(supervision.forge, "open_pull_requests", broken)
    supervision.poll(bridge.home, project)
    assert "pull requests" in (project / issues.SUPERVISION_ERROR).read_text()
    assert "pull requests" in supervision.last_poll(project)["stages"]


def test_the_forge_reading_reduces_checks_to_one_verdict(monkeypatch, tmp_path):
    forge.select(tmp_path, {"forge": "github"})
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    records = [
        {
            "number": 1,
            "headRefName": "one",
            "headRefOid": SHA,
            "statusCheckRollup": [
                {
                    "name": "lint",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                },
                {"context": "ci/legacy", "state": "SUCCESS"},
            ],
            "latestReviews": [
                {
                    "author": {"login": "alice"},
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-09-27T00:00:00Z",
                }
            ],
            "mergeable": "CONFLICTING",
            "closingIssuesReferences": [{"number": 4}],
        },
        {
            "number": 2,
            "statusCheckRollup": [
                {"name": "test", "status": "IN_PROGRESS", "conclusion": ""},
                {
                    "name": "lint",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                },
            ],
        },
        {
            "number": 3,
            "statusCheckRollup": [
                {
                    "name": "test",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                },
                {
                    "name": "docs",
                    "status": "COMPLETED",
                    "conclusion": "SKIPPED",
                },
            ],
        },
        {"number": 4, "statusCheckRollup": []},
    ]
    monkeypatch.setattr(forge, "_run", lambda *args: json.dumps(records))
    one, two, three, four = forge.open_pull_requests(tmp_path)
    assert (one["checks"], one["failing"]) == ("red", ["lint"])
    assert one["issues"] == ["4"] and one["sha"] == SHA
    assert one["reviews"][0]["author"] == "alice"
    assert one["mergeable"] == "CONFLICTING"
    assert (two["checks"], two["failing"]) == ("pending", [])
    assert three["checks"] == "green" and four["checks"] == "none"
    assert four["mergeable"] == "UNKNOWN"


def test_the_forge_reading_reports_absence_instead_of_raising(
    monkeypatch, tmp_path
):
    forge.select(tmp_path, {"forge": "github"})
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    monkeypatch.setattr(forge, "_run", lambda *args: "not json")
    assert forge.open_pull_requests(tmp_path) is None
    monkeypatch.setattr(forge, "_run", lambda *args: json.dumps({"a": 1}))
    assert forge.open_pull_requests(tmp_path) is None
    monkeypatch.setattr(forge, "_run", lambda *args: None)
    assert forge.open_pull_requests(tmp_path) is None
    forge.select(tmp_path, {"forge": "null"})
    assert forge.open_pull_requests(tmp_path) is None


STARTED = [{"name": "wsl", "state": "queued", "started": 1.0}]


def backdate(directory, seconds):
    """Moves the recorded pending clock of pull request 7 into the past."""
    path = directory / supervision.PULL_REQUEST_RECORD
    record = json.loads(path.read_text())
    record["pull_requests"]["7"]["pending_since"] -= seconds
    path.write_text(json.dumps(record))


def stalls(bridge):
    """Returns the stalled-checks notices the Claude lane received."""
    return [
        notice
        for notice in received(bridge, "claude")
        if "checks pending over" in notice
    ]


def stalled_rows(directory):
    """Returns the checks-stalled problem rows the record yields."""
    return problems._checks_rows(directory, "root", time.time())


def test_a_head_pending_past_the_ceiling_is_announced_once(
    bridge, project, monkeypatch
):
    pending = reading(project, pending=STARTED)
    observe(bridge, project, monkeypatch, pending)
    observe(bridge, project, monkeypatch, pending)
    assert stalls(bridge) == []
    backdate(project, supervision.CHECKS_STALLED_SECONDS + 1)
    observe(bridge, project, monkeypatch, pending)
    observe(bridge, project, monkeypatch, pending)
    notices = stalls(bridge)
    assert len(notices) == 1
    assert "checks pending over 60 min: wsl queued" in notices[0]
    assert "gh run rerun --failed" in notices[0]
    rows = stalled_rows(project)
    assert [row["participant"] for row in rows] == ["claude"]
    assert rows[0]["condition"] == problems.CHECKS
    assert "#7 checks pending: wsl queued" in rows[0]["detail"]
    assert rows[0]["seconds"] >= supervision.CHECKS_STALLED_SECONDS


def test_a_new_head_restarts_the_pending_clock(bridge, project, monkeypatch):
    observe(bridge, project, monkeypatch, reading(project, pending=STARTED))
    backdate(project, supervision.CHECKS_STALLED_SECONDS + 1)
    observe(
        bridge,
        project,
        monkeypatch,
        reading(project, sha=NEXT, pending=STARTED),
    )
    assert stalls(bridge) == []
    assert stalled_rows(project) == []
    backdate(project, supervision.CHECKS_STALLED_SECONDS + 1)
    observe(bridge, project, monkeypatch, reading(project, checks="green"))
    observe(bridge, project, monkeypatch, reading(project, pending=STARTED))
    assert stalls(bridge) == []
    record = json.loads((project / supervision.PULL_REQUEST_RECORD).read_text())
    assert time.time() - record["pull_requests"]["7"]["pending_since"] < 60


def test_a_forge_without_start_times_is_never_stalled(
    bridge, project, monkeypatch
):
    unknown = [{"name": "wsl", "state": "queued", "started": None}]
    observe(bridge, project, monkeypatch, reading(project, pending=unknown))
    backdate(project, supervision.CHECKS_STALLED_SECONDS + 1)
    observe(bridge, project, monkeypatch, reading(project, pending=unknown))
    observe(bridge, project, monkeypatch, reading(project))
    assert received(bridge, "claude") == []
    assert stalled_rows(project) == []


def test_the_ceiling_doubles_the_longest_declared_job_timeout(tmp_path):
    assert (
        supervision.checks_ceiling(tmp_path)
        == supervision.CHECKS_STALLED_SECONDS
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "check.yml").write_text(
        "jobs:\n  a:\n    timeout-minutes: 20\n  b:\n    timeout-minutes: 45\n"
    )
    assert supervision.checks_ceiling(tmp_path) == 5400.0


def test_status_shows_the_pending_age_and_the_stall():
    pull = {"number": 7, "checks": "pending", "mergeable": "MERGEABLE"}
    assert tables.pull_cell({"pull_request": pull}) == "#7 CI pending"
    pull.update(pending_seconds=4500, stalled=True)
    assert tables.pull_cell({"pull_request": pull}) == (
        "#7 CI pending 75m, stalled"
    )


def test_the_forge_reading_keeps_each_pending_check_start(
    monkeypatch, tmp_path
):
    forge.select(tmp_path, {"forge": "github"})
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    records = [
        {
            "number": 1,
            "statusCheckRollup": [
                {
                    "name": "wsl",
                    "status": "QUEUED",
                    "startedAt": "2026-09-29T12:45:00Z",
                },
                {
                    "name": "macos",
                    "status": "IN_PROGRESS",
                    "startedAt": "0001-01-01T00:00:00Z",
                },
                {"context": "ci/legacy", "state": "PENDING"},
                {
                    "name": "lint",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                },
            ],
        }
    ]
    monkeypatch.setattr(forge, "_run", lambda *args: json.dumps(records))
    (one,) = forge.open_pull_requests(tmp_path)
    assert one["checks"] == "pending"
    assert one["pending"] == [
        {
            "name": "wsl",
            "state": "queued",
            "started": datetime.fromisoformat(
                "2026-09-29T12:45:00+00:00"
            ).timestamp(),
        },
        {"name": "macos", "state": "in_progress", "started": None},
        {"name": "ci/legacy", "state": "pending", "started": None},
    ]
