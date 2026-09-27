"""Unattended integration authorized by an explicit project policy.

`participant merge` is an operator command. A project can also record, in its
private manifest, a standing authorization to integrate the ready work of a
bounded list of issues into one named target branch without an operator at
the keyboard. The authorization is `integration.unattended`: the ``target``
branch the base checkout must have checked out and the ``issues`` whose ready
work it covers. It is absent unless the operator writes it with
`agent-parley unattended set`, which refuses to run from an assigned worktree,
and no served coordination tool reads or writes it, so a lane cannot grant
itself the authority or widen it.

Authority never relaxes the terms of a merge. Every eligible lane is merged by
the same `_integrate_lane` step `participant merge` uses, under the project
merge lock and the lane's session lock, with the configured verification gate,
any required operator approval and the source-commit binding all applied.
What the policy adds is a stricter precondition: the claimed issue is listed,
the claim generation is current, the work is reported ready at a commit the
lane still sits on, the base checkout is on the target branch, every
dependency is verified complete, a verification command is configured, and no
peer's advisory reservation covers a path the work changed.

Every attempt leaves a durable decision record in the lane's report log with
bounded evidence: the claim generation, the source commit, the target branch
and commit, the verification command and the outcome. A refusal names the
condition that failed and what to do about it. Each attempt evaluates those
inputs afresh, so a changed commit, a moved base or a new claim generation is
judged on its own and never inherits an earlier decision. A replay after a
recorded integration merges and completes nothing.

Integration dispatch from the supervision service is deliberately not wired
here. It waits on the durable failed-integration recovery contract of issue
#541, which is expected to hook into `failed`, the one function every merge or
gate failure on this path goes through.
"""

from __future__ import annotations

import shlex
import sqlite3
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import lifecycle, metrics, roster
from agent_parley.state import BridgeError

if TYPE_CHECKING:
    from agent_parley.cli import Bridge

POLICY = "integration.unattended"
"""Manifest path of the authorization, named in every decision record."""

KIND = "unattended"
"""Report-log record kind of an unattended integration decision."""

INTEGRATED = "integrated"
REFUSED = "refused"
FAILED = "failed"
MAX_REASON = 2000
"""Characters of a refusal or failure explanation kept in a record."""


def policy(manifest: dict) -> dict | None:
    """Reads the project's unattended integration authorization strictly.

    Args:
        manifest: Project manifest using the participant roster layout.

    Returns:
        The validated ``target`` and ``issues``, or None when the project
        records no authorization and integration stays operator-only.

    Raises:
        BridgeError: If the recorded policy is invalid; an invalid policy
            is a refusal, never an authorization.
    """
    return roster.integration_policy(manifest.get("integration", {})).get(
        "unattended"
    )


def _operator_only(repo: Path, root: Path, manifest: dict, action: str) -> None:
    """Refuses an operator command run from inside an assigned worktree.

    Args:
        repo: Checkout the command names.
        root: Common repository root, which is the base checkout.
        manifest: Project manifest holding every lane.
        action: What the refused command does, for the explanation.

    Raises:
        BridgeError: If the command runs in a lane or names one as its
            checkout.
    """
    from agent_parley.cli import git

    here = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    lanes = {
        Path(lane["lane"]).resolve()
        for lane in manifest["participants"].values()
    }
    if here in lanes or roster.caller_lane(manifest):
        raise BridgeError(
            f"{action} from the base checkout at {root}, never from an "
            "assigned worktree, so a lane cannot grant or widen its own "
            "integration authority."
        )


def describe(bridge: Bridge, repo: Path) -> str:
    """Reports the project's unattended integration authorization.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository.

    Returns:
        An account of the recorded policy, or of its absence.

    Raises:
        BridgeError: If the repository has no project or the recorded
            policy is invalid.
    """
    root, directory = bridge.project(repo, create=False)
    recorded = policy(roster.read(directory))
    if recorded is None:
        return (
            f"{root} records no unattended integration policy; "
            "`participant merge` stays operator-only."
        )
    return (
        f"{root} authorizes unattended integration into "
        f"{recorded['target']} for issues "
        + ", ".join(f"#{number}" for number in recorded["issues"])
        + ", on the terms of `participant merge`."
    )


def configure(
    bridge: Bridge, repo: Path, target: str | None, issues: list[str]
) -> str:
    """Records or removes the unattended integration authorization.

    Only an operator in the base checkout may run this. The whole policy is
    replaced on every call, so widening it is always an explicit operator
    act, and an empty issue list removes it.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        target: Branch the base checkout must have checked out; required
            unless the policy is being removed.
        issues: Issue numbers, optionally prefixed with ``#``, whose ready
            work may be integrated; empty to remove the policy.

    Returns:
        An account of the recorded policy.

    Raises:
        BridgeError: If the command runs inside a lane, the repository has
            no project, or the policy is invalid.
    """
    from agent_parley.cli import lock, write_json

    root, directory = bridge.project(repo, create=False)
    _operator_only(
        repo, root, roster.read(directory), "Unattended integration is set"
    )
    numbers = [str(number).strip().removeprefix("#") for number in issues]
    if not numbers:
        recorded: dict = {}
    elif target is None:
        raise BridgeError(
            "Name the branch unattended integration merges into with --target."
        )
    else:
        recorded = roster.integration_policy(
            {"unattended": {"target": target, "issues": numbers}}
        )
    with lock(directory / "setup.lock"):
        data = roster.read(directory)
        data["integration"] = recorded
        write_json(directory / "project.json", data)
    return describe(bridge, repo)


def _record(
    directory: Path, name: str, outcome: str, evidence: dict, reason: str = ""
) -> dict:
    """Appends one durable unattended integration decision.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose lane the decision concerns.
        outcome: `INTEGRATED`, `REFUSED` or `FAILED`.
        evidence: Inputs the decision was bound to.
        reason: Explanation of a refusal or failure.

    Returns:
        The appended record.
    """
    return metrics.record_report(
        directory,
        name,
        {
            "kind": KIND,
            "action": f"unattended {outcome}",
            "outcome": outcome,
            "policy": POLICY,
            "issue": evidence.get("issue"),
            "claim_id": evidence.get("claim_id"),
            "evidence": dict(evidence),
            "reason": reason[:MAX_REASON],
        },
    )


def refuse(
    directory: Path, name: str, evidence: dict, reason: str
) -> BridgeError:
    """Records why a lane is not eligible and builds the refusal to raise.

    Nothing was merged when this is called.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose lane was evaluated.
        evidence: Inputs read before the unmet condition was found.
        reason: The unmet condition and what to do about it.

    Returns:
        The refusal, naming the policy and the condition.
    """
    _record(directory, name, REFUSED, evidence, reason)
    return BridgeError(
        f"Unattended integration of {name} refused; nothing was merged. "
        f"{reason}"
    )


def failed(
    directory: Path, name: str, evidence: dict, reason: str
) -> BridgeError:
    """Records a merge or gate failure on an authorized unattended attempt.

    Every failure after eligibility was established goes through here: a
    busy session, a missing approval, a failing verification gate, a merge
    conflict or post-merge verification that leaves the integration
    standing but not complete. The failed-integration recovery path of
    issue #541 is expected to hook in here before any automatic dispatch is
    enabled.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose lane was being integrated.
        evidence: Inputs the attempt was authorized on.
        reason: The failure as the merge step reported it.

    Returns:
        The refusal to raise, naming the failure and the recorded evidence.
    """
    _record(directory, name, FAILED, evidence, reason)
    return BridgeError(
        f"Unattended integration of {name} failed and was recorded for "
        f"recovery: {reason}"
    )


def _replayed(directory: Path, name: str, key: tuple) -> dict | None:
    """Finds an earlier recorded integration this attempt would repeat.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose lane is evaluated.
        key: Claim generation and source commit, or None fields when the
            lane holds no claim.

    Returns:
        The earlier integrated decision, or None.
    """
    from agent_parley.cli import snapshot

    issues = snapshot(directory)["issues"]
    for record in reversed(metrics.report_records(directory, name)):
        if record.get("kind") != KIND or record.get("outcome") != INTEGRATED:
            continue
        evidence = record.get("evidence") or {}
        bound = (evidence.get("claim_id"), evidence.get("source_commit"))
        if key[0] is not None and bound != key:
            continue
        completed = lifecycle.state(issues.get(str(evidence.get("issue")), {}))
        if (
            completed["state"] == lifecycle.COMPLETE
            and completed["claim_id"] == bound[0]
        ):
            return record
    return None


def _evaluate(
    bridge: Bridge, root: Path, directory: Path, data: dict, name: str
) -> tuple[dict, str]:
    """Reads the inputs of one decision and names the first unmet condition.

    Args:
        bridge: Coordination runtime owning the project state.
        root: Common repository root, which is the base checkout.
        directory: Private state directory for the common repository.
        data: Project manifest read under the setup lock.
        name: Participant whose lane is evaluated.

    Returns:
        The evidence read so far, and the unmet condition, which is empty
        when the lane is eligible.
    """
    from agent_parley.cli import (
        current_branch,
        exact_claim,
        git,
        reserved_overlaps,
        snapshot,
        store,
    )

    evidence: dict = {"policy": POLICY, "participant": name}
    try:
        recorded = policy(data)
    except BridgeError as exc:
        return evidence, f"{exc} Correct it with `agent-parley unattended set`."
    if recorded is None:
        return evidence, (
            "This project records no unattended integration policy, so "
            "integration stays operator-only: run `participant merge`, or "
            "authorize issues with `agent-parley unattended set`."
        )
    evidence["target"] = recorded["target"]
    try:
        claim = exact_claim(directory, name)
    except BridgeError as exc:
        return evidence, str(exc)
    if claim["issue"] is None:
        return evidence, f"{name} holds no claimed issue to integrate."
    issue = str(claim["issue"])
    evidence.update(issue=issue, claim_id=claim["claim_id"])
    if issue not in recorded["issues"]:
        return evidence, (
            f"Issue #{issue} is not listed in {POLICY}; an operator adds it "
            "with `agent-parley unattended set` or runs `participant merge`."
        )
    ledger = snapshot(directory)
    record = ledger["issues"][issue]
    execution = lifecycle.state(record)
    if execution["claim_id"] != claim["claim_id"]:
        return evidence, (
            f"Issue #{issue} changed claim generation while it was read."
        )
    if execution["state"] != lifecycle.READY:
        return evidence, (
            f"Issue #{issue} is {execution['state']}, not ready; the lane "
            "records a ready report first."
        )
    source = str(execution.get("source_commit") or execution["commit"] or "")
    evidence["source_commit"] = source
    if not lifecycle.COMMIT.fullmatch(source):
        return evidence, f"Issue #{issue} names no ready source commit."
    lane = Path(data["participants"][name]["lane"])
    if git(lane, "rev-parse", "HEAD") != source:
        return evidence, (
            f"{name} committed since issue #{issue} was reported ready at "
            f"{source[:12]}; record a new ready report."
        )
    branch = current_branch(root)
    if branch != recorded["target"]:
        return evidence, (
            f"The base checkout has {branch} checked out, not the policy "
            f"target {recorded['target']}."
        )
    target_commit = git(root, "rev-parse", "HEAD")
    evidence["target_commit"] = target_commit
    if record.get("blocked_by") or not lifecycle.dependencies_complete(
        ledger, record
    ):
        waits = ", ".join(f"#{item}" for item in record.get("blocked_by", []))
        return evidence, (
            f"Issue #{issue} still waits on {waits or 'a dependency'} "
            "to be verified complete."
        )
    if not data["verify"]:
        return evidence, (
            "This project configures no verification command, so no green "
            "gate can authorize unattended integration. Set one with "
            "`agent-parley verify set`."
        )
    evidence["verify"] = shlex.join(data["verify"])
    changed = [
        line
        for line in git(
            root, "diff", "--name-only", f"{target_commit}...{source}"
        ).splitlines()
        if line
    ]
    evidence["changed_paths"] = len(changed)
    try:
        held = store.active_reservations(bridge.home, data["root"])
    except (BridgeError, OSError, sqlite3.Error) as exc:
        return evidence, (
            "The advisory reservations could not be read, so no overlap "
            f"with a peer can be ruled out: {exc}"
        )
    held.pop(data["participants"][name]["display"], None)
    if overlaps := reserved_overlaps(changed, held):
        return evidence, (
            "A peer reservation covers what the work changed: "
            + "; ".join(overlaps[:5])
            + ". Hand the work over or wait for the release."
        )
    return evidence, ""


def integrate(bridge: Bridge, repo: Path, name: str) -> str:
    """Integrates one lane under the project's unattended policy.

    The attempt holds the project merge lock throughout, so a concurrent
    attempt or operator merge is refused with the merge-busy explanation.
    Eligibility is evaluated under that lock, immediately before the lane is
    merged by the step `participant merge` uses, and every outcome is
    recorded as a decision.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        name: Participant whose ready work is integrated.

    Returns:
        An account of the merge, or of the earlier integration a replay
        found, in which case nothing is merged or completed again.

    Raises:
        BridgeError: If the command runs inside a lane, another merge holds
            the merge lock, the lane is not eligible, or the merge or its
            gate fails; the last two are recorded durably first.
    """
    from agent_parley.cli import git, lock
    from agent_parley.integration import MERGE_BUSY

    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    if name not in data["participants"]:
        raise BridgeError(
            f"{name} is not a participant in this project; "
            "run agent-parley participant list."
        )
    _operator_only(repo, root, data, "Unattended integration runs")
    with lock(directory / "merge.lock", MERGE_BUSY):
        with lock(directory / "setup.lock"):
            data = bridge._project(root, directory, verify={name})
        evidence, unmet = _evaluate(bridge, root, directory, data, name)
        key = (
            evidence.get("claim_id"),
            evidence.get("source_commit"),
        )
        if earlier := _replayed(directory, name, key):
            bound = earlier["evidence"]
            return (
                f"Issue #{bound.get('issue')} of {name} was already "
                f"integrated at {str(bound.get('integrated_commit'))[:12]} "
                "and recorded complete; nothing was merged again."
            )
        if unmet:
            raise refuse(directory, name, evidence, unmet)
        try:
            merged = bridge._integrate_lane(root, directory, data, name)
        except subprocess.TimeoutExpired as exc:
            raise failed(
                directory,
                name,
                evidence,
                f"Verification exceeded its {exc.timeout}s timeout.",
            ) from exc
        except BridgeError as exc:
            raise failed(directory, name, evidence, str(exc)) from exc
        evidence["integrated_commit"] = git(root, "rev-parse", "HEAD")
        _record(directory, name, INTEGRATED, evidence)
    return (
        f"{merged}\nIntegrated unattended under {POLICY}: issue "
        f"#{evidence['issue']} at {evidence['source_commit'][:12]} into "
        f"{evidence['target']}."
    )
