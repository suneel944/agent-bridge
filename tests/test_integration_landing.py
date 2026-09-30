"""Checks that work merged into the integration base lands its claim."""

import json
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    BridgeError,
    cli,
    forge,
    issues,
    notify,
    problems,
    roster,
    status,
    store,
    supervision,
)

BASE = "integration/1.0.0"


@pytest.fixture
def claimed(bridge, paired, repo):
    """Records the integration base and claims issue 1 for the Claude lane."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
    bridge.integration_branch(repo, BASE)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    return lane


def landed_by(monkeypatch, at=None):
    """Reports issue 1 open on the forge and merged into the base."""
    monkeypatch.setattr(
        supervision.forge, "branch_completion", lambda *args: None
    )
    monkeypatch.setattr(
        supervision.forge,
        "issue_completion",
        lambda *args: {"state": "OPEN"},
    )
    reading = {
        "state": "MERGED",
        "closed_at": time.time() + 1 if at is None else at,
        "pull_request": 775,
        "url": "https://example.invalid/pull/775",
        "branch": "fix/1-c2",
        "commit": "abcdef1234567",
        "base": BASE,
    }
    monkeypatch.setattr(
        supervision.forge,
        "integration_landings",
        lambda repo, base: {"1": reading} if base == BASE else {},
    )


def test_the_forge_reads_closing_keywords_of_pull_requests_into_the_base(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    asked = []
    replies = [
        {
            "number": 775,
            "url": "https://example.invalid/pull/775",
            "headRefName": "fix/754-c2",
            "mergeCommit": {"oid": "abcdef1234567"},
            "mergedAt": "2026-09-29T21:20:00Z",
            "body": "Closes #754\nFixes #755, refs #756",
        },
        {
            "number": 770,
            "url": "https://example.invalid/pull/770",
            "headRefName": "fix/754-c1",
            "mergeCommit": {"oid": "1234567abcdef"},
            "mergedAt": "2026-09-29T20:00:00Z",
            "body": "Resolves #754 and #7540",
        },
    ]

    def run(args, timeout):
        asked.append(args)
        return json.dumps(replies)

    monkeypatch.setattr(forge, "_run", run)
    landed = forge.integration_landings(tmp_path, BASE)
    assert asked[0][asked[0].index("--base") + 1] == BASE
    assert asked[0][asked[0].index("--state") + 1] == "merged"
    assert sorted(landed) == ["754", "755"]
    assert landed["754"] == {
        "state": "MERGED",
        "closed_at": forge._epoch("2026-09-29T21:20:00Z"),
        "pull_request": 775,
        "url": "https://example.invalid/pull/775",
        "branch": "fix/754-c2",
        "commit": "abcdef1234567",
        "base": BASE,
    }
    monkeypatch.setattr(forge, "_run", lambda *args: None)
    assert forge.integration_landings(tmp_path, BASE) is None
    monkeypatch.setattr(forge, "_run", lambda *args: "not json")
    assert forge.integration_landings(tmp_path, BASE) is None


def test_a_merge_into_the_base_ends_the_claim_and_names_the_pull_request(
    bridge, claimed, monkeypatch
):
    landed_by(monkeypatch)
    supervision.poll(bridge.home, claimed.parent)
    record = issues.snapshot(claimed.parent)["issues"]["1"]
    assert record["owner"] is None
    assert record["resolution"]["actor"] == "supervisor"
    assert record["resolution"]["evidence"]["base"] == BASE
    assert record["resolution"]["evidence"]["pull_request"] == 775
    assert BASE in record["resolution"]["reason"]
    ledger = issues.snapshot(claimed.parent)
    assert supervision.lifecycle.actionable(ledger, "claude") == []
    assert supervision.lifecycle.actionable(ledger) == []
    project = bridge.status_snapshot()["projects"][0]
    assert project["integration"]["landed"][0]["issue"] == 1
    assert project["integration"]["held"] == []
    line = status.landed_line(project["integration"])
    assert line.startswith(f"Landed in {BASE}: #1 (PR #775)")


def test_without_an_integration_base_an_open_issue_keeps_its_claim(
    bridge, claimed, repo, monkeypatch
):
    bridge.integration_branch(repo, "")
    assert "integration_base" not in roster.read(claimed.parent)
    landed_by(monkeypatch)
    supervision.poll(bridge.home, claimed.parent)
    assert issues.snapshot(claimed.parent)["issues"]["1"]["owner"] == "claude"
    assert "integration" not in bridge.status_snapshot()["projects"][0]


def test_a_merge_into_the_base_before_the_claim_does_not_end_it(
    bridge, claimed, monkeypatch
):
    landed_by(monkeypatch, at=time.time() - 3600)
    supervision.poll(bridge.home, claimed.parent)
    assert issues.snapshot(claimed.parent)["issues"]["1"]["owner"] == "claude"


def test_the_operator_is_asked_once_to_cross_a_fully_landed_base(
    bridge, claimed, paired, monkeypatch
):
    other = Path(paired["lanes"]["codex"])
    bridge.issue(other, "claim", "2")
    landed_by(monkeypatch)
    supervision.poll(bridge.home, claimed.parent)
    rows = problems.derive(bridge.home, bridge.status_snapshot())
    assert not [row for row in rows if row["condition"] == problems.CROSSING]
    bridge.issue(other, "release", "2")
    rows = problems.derive(bridge.home, bridge.status_snapshot())
    crossing = [row for row in rows if row["condition"] == problems.CROSSING]
    assert len(crossing) == 1
    assert crossing[0]["actor"] == problems.BY_OPERATOR
    assert "#1" in crossing[0]["detail"]
    assert f"--head {BASE}" in crossing[0]["command"]
    assert problems.CROSSING in notify.PROBLEM_CONDITIONS
    assert issues.snapshot(claimed.parent)["issues"]["2"]["owner"] is None


def test_a_landed_issue_the_forge_closed_no_longer_waits_on_the_crossing():
    ledger = {
        "issues": {
            "1": {
                "owner": None,
                "execution": {"state": "complete"},
                "resolution": {
                    "outcome": "complete",
                    "at": 5.0,
                    "evidence": {"base": BASE, "pull_request": 775},
                },
            }
        }
    }
    assert [item["issue"] for item in issues.landed(ledger, BASE, None)] == [1]
    assert issues.landed(ledger, BASE, {"2": {}}) == []
    assert issues.landed(ledger, "", None) == []


def test_only_the_operator_records_a_valid_integration_base(
    bridge, paired, repo, monkeypatch, capsys
):
    with pytest.raises(BridgeError, match="one Git branch name"):
        bridge.integration_branch(repo, "bad..name")
    with pytest.raises(BridgeError, match="base checkout"):
        bridge.integration_branch(Path(paired["lanes"]["claude"]), BASE)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "branch",
            "integration",
            BASE,
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() == 0
    assert BASE in capsys.readouterr().out
    directory = Path(paired["lanes"]["claude"]).parent
    assert roster.read(directory)["integration_base"] == BASE
