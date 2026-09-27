"""Checks the opt-in run budget that gates wakes, dispatch and launches."""

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_parley import (
    budgets,
    cli,
    lanes,
    problems,
    process,
    roster,
    store,
    supervision,
    terminal,
)
from agent_parley.checkpoints import checkpoint
from agent_parley.state import BridgeError, lock, write_json


def transcript(lane, tokens, name="session.jsonl"):
    """Writes a Claude transcript whose client counts that many tokens."""
    directory = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / re.sub(r"[^A-Za-z0-9]", "-", str(lane))
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": f"msg-{name}",
                    "usage": {"input_tokens": tokens, "output_tokens": 0},
                },
            }
        )
        + "\n"
    )
    return path


def actors(bridge, paired):
    """Registers both lanes and returns their authenticated identities."""
    store.initialize(bridge.home)
    return {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "codex")
    }


def serve(bridge, actor, count):
    """Serves that many coordination calls to one lane."""
    for _ in range(count):
        store.call(bridge.home, actor, "list_participants", {})


def enforced(bridge, repo, **limits):
    """Records a run budget and returns the directory and manifest."""
    bridge.enforce(repo, limits)
    directory = bridge.project(repo, create=False)[1]
    return directory, roster.read(directory)


def idle_codex(bridge, paired, monkeypatch, pending=True):
    """Leaves codex idle, optionally with mail, and records terminal wakes."""
    identities = actors(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    write_json(
        lane.parent / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    if pending:
        store.call(
            bridge.home,
            identities["claude"],
            "send_message",
            {
                "to": ["codex"],
                "subject": "Review",
                "body_md": "Review the result",
                "idempotency_key": "pending",
                "ack_required": True,
            },
        )
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(lane.parent, "codex", 1)
    with store.connect(bridge.home, write=True) as db:
        lanes.sample(
            db,
            paired["root"],
            "codex",
            observed,
            dead_after=supervision.DEFAULTS["stalled_after"],
        )
    return calls, config, observed


def main(monkeypatch, bridge, *arguments):
    """Runs the command line against the test state root."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    return cli.main()


def test_budgets_stay_advisory_until_a_run_budget_is_recorded(
    bridge, repo, paired
):
    directory = bridge.project(repo, create=False)[1]
    bridge.budget(repo, "project", "", {"calls": 1})
    serve(bridge, actors(bridge, paired)["claude"], 3)
    manifest = roster.read(directory)
    assert "run_budget" not in manifest
    assert budgets.admit(directory, manifest, "claude") == ""
    assert not (directory / budgets.LEDGER).exists()
    assert "enforces no run budget" in bridge.enforce(repo)
    bridge.enforce(repo, {"calls": 5, "tokens": None, "hours": None})
    assert roster.read(directory)["run_budget"] == {"calls": 5}
    assert (directory / budgets.LEDGER).exists()
    bridge.enforce(repo, {"calls": 0, "tokens": None, "hours": None})
    assert "run_budget" not in roster.read(directory)


def test_concurrent_admissions_share_one_allowance(
    bridge, repo, paired, monkeypatch
):
    identities = actors(bridge, paired)
    serve(bridge, identities["claude"], 2)
    serve(bridge, identities["codex"], 2)
    directory, manifest = enforced(bridge, repo, calls=5)
    serve(bridge, identities["codex"], 1)
    escalations = []
    monkeypatch.setattr(
        budgets.notify,
        "deliver",
        lambda *args: escalations.append(args) or "",
    )
    refusals: list[str] = []

    def poll_then_admit(name):
        budgets.account(bridge.home, directory, manifest)
        refusals.append(budgets.admit(directory, manifest, name))

    threads = [
        threading.Thread(target=poll_then_admit, args=(name,))
        for name in ("claude", "codex") * 4
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(refusals) == 8
    assert all(
        refusal.startswith("run budget exhausted") for refusal in refusals
    )
    assert len(escalations) == 1
    ledger = json.loads((directory / budgets.LEDGER).read_text())
    assert ledger["calls"]["total"] == 5
    assert ledger["exhausted"]["fields"] == ["calls"]


def test_concurrent_accounting_never_double_counts(bridge, repo, paired):
    serve(bridge, actors(bridge, paired)["claude"], 3)
    directory, manifest = enforced(bridge, repo, calls=100)
    threads = [
        threading.Thread(
            target=budgets.account, args=(bridge.home, directory, manifest)
        )
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["calls"] == 3
    assert reading["exhausted"] is None


def test_exhaustion_blocks_wakes_and_retries_without_spending_attempts(
    bridge, repo, paired, monkeypatch
):
    calls, config, observed = idle_codex(bridge, paired, monkeypatch)
    directory, manifest = enforced(bridge, repo, calls=1000)
    supervision.wake(
        bridge.home, directory, manifest, "codex", observed, config
    )
    assert len(calls) == 1
    record = supervision.wake_record(bridge.home, paired["root"], "codex")
    supervision.store_wake(
        bridge.home, directory, paired["root"], "codex", {**record, "at": 0}
    )
    directory, manifest = enforced(bridge, repo, calls=1)
    supervision.wake(
        bridge.home, directory, manifest, "codex", observed, config
    )
    assert len(calls) == 1
    parked = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert parked["attempts"] == 1
    assert parked["blocked"].startswith("run budget exhausted")
    assert budgets.halted(directory, manifest)["fields"] == ["calls"]


def test_exhaustion_survives_restart_and_is_one_actionable_record(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    transcript(lane, 600)
    directory, manifest = enforced(bridge, repo, tokens=500)
    first = budgets.account(bridge.home, directory, manifest)
    assert first["used"]["tokens"] == 600
    budgets._READINGS.clear()
    second = budgets.account(bridge.home, directory, manifest)
    assert second["used"]["tokens"] == 600
    assert second["exhausted"]["at"] == first["exhausted"]["at"]
    rows = problems._run_rows(directory, manifest, paired["root"], time.time())
    assert [row["condition"] for row in rows] == [problems.RUN_BUDGET]
    assert "agent-parley budget resume" in rows[0]["command"]


def test_lane_replacement_and_retries_keep_counting(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    old = transcript(lane, 300)
    directory, manifest = enforced(bridge, repo, tokens=10_000)
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"]
        == 300
    )
    old.unlink()
    transcript(lane, 200, "retry.jsonl")
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"]
        == 500
    )
    replaced = {
        **manifest,
        "participants": {
            name: participant
            for name, participant in manifest["participants"].items()
            if name != "claude"
        },
    }
    assert (
        budgets.account(bridge.home, directory, replaced)["used"]["tokens"]
        == 500
    )


def test_missing_or_corrupt_usage_never_grants_allowance(bridge, repo, paired):
    transcript(Path(paired["lanes"]["claude"]), 10)
    directory, manifest = enforced(bridge, repo, tokens=1000)
    assert budgets.admit(directory, manifest, "claude") == ""
    refused = budgets.admit(directory, manifest, "codex")
    assert "cannot be metered" in refused
    (directory / budgets.LEDGER).write_text("{not json")
    refused = budgets.admit(directory, manifest, "claude")
    assert refused.startswith("run budget exhausted")
    assert "unreadable" in refused
    budgets.account(bridge.home, directory, manifest)
    assert (directory / f"{budgets.LEDGER}.corrupt").read_text() == "{not json"
    assert "unreadable" in budgets.halted(directory, manifest)["cause"]
    assert budgets.admit(directory, manifest, "claude")
    (directory / budgets.LEDGER).write_text("[]")
    with pytest.raises(BridgeError, match="unreadable"):
        budgets.resume(bridge.home, directory, manifest)
    assert "Resumed" in budgets.resume(
        bridge.home, directory, manifest, reset=True
    )
    assert budgets.admit(directory, manifest, "claude") == ""


def test_a_deleted_ledger_reads_as_exhausted_not_as_none(bridge, repo, paired):
    serve(bridge, actors(bridge, paired)["claude"], 3)
    directory, manifest = enforced(bridge, repo, calls=100)
    ledger = json.loads((directory / budgets.LEDGER).read_text())
    assert ledger["calls"]["total"] == 3
    assert budgets.halted(directory, manifest) is None
    (directory / budgets.LEDGER).unlink()
    assert "missing" in budgets.halted(directory, manifest)["cause"]
    assert budgets.admit(directory, manifest, "claude").startswith(
        "run budget exhausted"
    )
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["calls"] == 3
    assert "missing" in reading["exhausted"]["cause"]
    assert "missing" in budgets.halted(directory, manifest)["cause"]
    assert "missing" in budgets.admit(directory, manifest, "claude")


def test_reset_on_an_unreadable_ledger_offsets_the_use_read_so_far(
    bridge, repo, paired
):
    serve(bridge, actors(bridge, paired)["claude"], 3)
    directory, manifest = enforced(bridge, repo, calls=100)
    (directory / budgets.LEDGER).write_text("{not json")
    budgets.resume(bridge.home, directory, manifest, reset=True)
    ledger = json.loads((directory / budgets.LEDGER).read_text())
    assert ledger["offset"]["calls"] == 3
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["calls"] == 0
    assert reading["exhausted"] is None


def test_concurrent_admissions_at_the_last_call_admit_exactly_one(
    bridge, repo, paired
):
    serve(bridge, actors(bridge, paired)["claude"], 4)
    directory, manifest = enforced(bridge, repo, calls=5)
    refusals: list[str] = []
    threads = [
        threading.Thread(
            target=lambda name=name: refusals.append(
                budgets.admit(directory, manifest, name)
            )
        )
        for name in ("claude", "codex") * 4
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(refusals) == 8
    assert refusals.count("") == 1
    assert all(
        "reserved by turns already admitted" in refusal
        for refusal in refusals
        if refusal
    )
    ledger = json.loads((directory / budgets.LEDGER).read_text())
    assert ledger["reserved"] == 1
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["exhausted"] is None
    assert budgets.admit(directory, manifest, "claude") == ""


def test_a_busy_ledger_parks_the_wake_instead_of_raising(
    bridge, repo, paired, monkeypatch
):
    calls, config, observed = idle_codex(bridge, paired, monkeypatch)
    directory, manifest = enforced(bridge, repo, calls=1000)
    monkeypatch.setattr(budgets, "LOCK_SECONDS", 0.05)
    with lock(directory / "run-budget.lock"):
        supervision.wake(
            bridge.home, directory, manifest, "codex", observed, config
        )
    assert calls == []
    parked = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert "ledger stayed busy" in parked["blocked"]
    supervision.wake(
        bridge.home, directory, manifest, "codex", observed, config
    )
    assert len(calls) == 1


def test_a_first_refusal_for_an_unmetered_lane_is_parked_and_shown(
    bridge, repo, paired, monkeypatch
):
    calls, config, observed = idle_codex(bridge, paired, monkeypatch)
    transcript(Path(paired["lanes"]["claude"]), 10)
    directory, manifest = enforced(bridge, repo, tokens=1000)
    assert supervision.wake_record(bridge.home, paired["root"], "codex") == {}
    supervision.wake(
        bridge.home, directory, manifest, "codex", observed, config
    )
    assert calls == []
    parked = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert "cannot be metered" in parked["blocked"]
    rows = problems._run_rows(directory, manifest, paired["root"], time.time())
    assert [(row["condition"], row["participant"]) for row in rows] == [
        (problems.RUN_UNMETERED, "codex")
    ]


def test_in_flight_lanes_are_asked_to_checkpoint_and_stop(bridge, repo, paired):
    identities = actors(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    (lane / "unsaved.txt").write_text("work in progress\n")
    write_json(
        directory / "claude-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
            "activity": "working",
            "updated": time.time(),
        },
    )
    write_json(directory / "claude-identity.json", {"name": "claude"})
    serve(bridge, identities["claude"], 2)
    _, manifest = enforced(bridge, repo, calls=1)
    budgets.account(bridge.home, directory, manifest)
    event = {
        "session_id": "claude-run",
        "cwd": str(lane),
        "tool_name": "Read",
        "tool_input": {"file_path": str(lane / "shared.txt")},
    }
    reply = checkpoint(
        bridge.home,
        directory,
        "claude",
        {**event, "hook_event_name": "PreToolUse"},
    )
    details = reply["hookSpecificOutput"]
    assert "Run budget exhausted" in details["additionalContext"]
    assert "permissionDecision" not in details
    write_json(directory / "issues.json", {"revision": 1, "issues": {}})
    stop = {
        "hook_event_name": "Stop",
        "session_id": "claude-run",
        "cwd": str(lane),
    }
    assert checkpoint(bridge.home, directory, "claude", stop) == {}
    assert (lane / "unsaved.txt").read_text() == "work in progress\n"
    budgets.resume(bridge.home, directory, manifest, reset=True)
    write_json(directory / "issues.json", {"revision": 2, "issues": {}})
    assert (
        checkpoint(bridge.home, directory, "claude", stop).get("decision")
        == "block"
    )


def test_only_the_operator_resumes_or_raises_the_run(
    bridge, repo, paired, monkeypatch, capsys
):
    serve(bridge, actors(bridge, paired)["claude"], 3)
    directory, manifest = enforced(bridge, repo, calls=2)
    budgets.account(bridge.home, directory, manifest)
    with pytest.raises(BridgeError, match="run budget is exhausted"):
        bridge.launch("claude", repo, "continue")
    monkeypatch.setenv("AGENT_PARLEY_TOKEN", "lane-token")
    assert main(monkeypatch, bridge, "budget", "resume", "--repo", str(repo))
    assert main(
        monkeypatch,
        bridge,
        "budget",
        "enforce",
        "--calls",
        "100",
        "--repo",
        str(repo),
    )
    assert "operator authority" in capsys.readouterr().err
    assert budgets.halted(directory, roster.read(directory))
    monkeypatch.delenv("AGENT_PARLEY_TOKEN")
    assert main(monkeypatch, bridge, "budget", "resume", "--repo", str(repo))
    assert "still at or over" in capsys.readouterr().err
    assert not main(
        monkeypatch,
        bridge,
        "budget",
        "enforce",
        "--calls",
        "100",
        "--repo",
        str(repo),
    )
    assert budgets.halted(directory, roster.read(directory))
    assert not main(
        monkeypatch, bridge, "budget", "resume", "--repo", str(repo)
    )
    assert "Resumed the run" in capsys.readouterr().out
    manifest = roster.read(directory)
    assert budgets.halted(directory, manifest) is None
    assert budgets.admit(directory, manifest, "claude") == ""
    bridge.enforce(repo, {"calls": 3, "tokens": None, "hours": None})
    manifest = roster.read(directory)
    assert budgets.admit(directory, manifest, "claude")
    assert "new accounting period" in budgets.resume(
        bridge.home, directory, manifest, reset=True
    )
    assert budgets.admit(directory, manifest, "claude") == ""
