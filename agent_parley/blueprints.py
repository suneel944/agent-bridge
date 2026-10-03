"""Blueprints: ordered deterministic and agent steps one claim runs through.

A blueprint is a named, ordered list of nodes an operator records in the
project manifest, so configuring one commits nothing to the target
repository. A run walks one claim through a blueprint. The harness runs the
deterministic nodes itself, with no model call, so a required step always
happens and in the recorded order: a ``run`` node executes an argument list
in the lane's worktree without a shell, a ``push`` node pushes the lane's
branch, and a ``report`` node ends the run as done or blocked. An ``agent``
node writes its prompt into the lane's inbox, with the last failing step's
output attached, and ends when the lane next files a report: ready follows
the success edge and blocked the failure edge.

Each node names the node that follows on success and on failure. A node with
no ``next`` falls through to the following node, and the last one ends the
run as done. A node with no ``on_failure`` retries itself. Every failure is
counted per node, and a node that fails more than its ``retries`` allow ends
the run blocked for the operator.

Only an operator shell in the base checkout records, starts or advances a
blueprint, as with `init set`, so a lane cannot plant commands later lanes
run. A ``run`` node inherits the operator's environment, which is what a
launched lane inherits minus its coordination token, plus
``AGENT_PARLEY_BASE``. No node merges: `participant merge` and its
verification gate stay the only way into the base, and a blueprint cannot
remove that gate. Progress is recorded per claim in the project's coordination
directory, and a claim with no run recorded behaves exactly as before.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import issues, roster, unattended
from agent_parley.state import BridgeError, lock, write_json
from agent_parley.worktrees import VERIFY_TIMEOUT

if TYPE_CHECKING:
    from agent_parley.cli import Bridge

KINDS = ("run", "agent", "push", "report")
"""Node kinds a blueprint may hold; none of them merges."""

FIELDS = {
    "run": {"command", "timeout"},
    "push": {"timeout"},
    "agent": {"prompt"},
    "report": {"outcome", "message"},
}
"""Fields each kind accepts beyond ``name``, ``kind`` and the edges."""

EDGES = {"name", "kind", "next", "on_failure", "retries"}
PUSH = ["git", "push", "--set-upstream", "origin", "HEAD"]
MAX_NODES = 50
MAX_RETRIES = 20
MAX_TEXT = 4000
MAX_VISITS = 200
"""Node visits one run may make, which ends a cycle of passing nodes."""

TAIL_LINES = 40
TAIL_CHARS = 2000
RUNS = "blueprint-runs.json"
RUNNING = "running"
WAITING = "waiting"
DONE = "done"
BLOCKED = "blocked"


def parse(text: str) -> list[dict]:
    """Validates a blueprint file into its ordered nodes.

    Args:
        text: JSON object whose ``nodes`` member lists the nodes in order.

    Returns:
        The nodes with defaults filled in: ``retries`` is 0, a ``run`` or
        ``push`` node's ``timeout`` is the verification limit, and a
        ``report`` node's ``outcome`` is done.

    Raises:
        BridgeError: If the text is not a valid blueprint.
    """
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise BridgeError(f"Blueprint is not valid JSON: {exc}.") from None
    raw = document.get("nodes") if isinstance(document, dict) else None
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_NODES:
        raise BridgeError(
            "A blueprint is a JSON object whose `nodes` list holds 1 to "
            f"{MAX_NODES} nodes."
        )
    nodes = [_node(entry) for entry in raw]
    names = [node["name"] for node in nodes]
    if len(set(names)) != len(names):
        raise BridgeError("Blueprint node names must be unique.")
    for node in nodes:
        for edge in ("next", "on_failure"):
            if edge in node and node[edge] not in names:
                raise BridgeError(
                    f"Blueprint node {node['name']!r} names unknown "
                    f"{edge} node {node[edge]!r}."
                )
    return nodes


def _node(entry: object) -> dict:
    """Validates one blueprint node.

    Args:
        entry: One member of the blueprint's ``nodes`` list.

    Returns:
        The node with its defaults filled in.

    Raises:
        BridgeError: If the node is malformed.
    """
    if not isinstance(entry, dict):
        raise BridgeError("Every blueprint node must be a JSON object.")
    name = entry.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
        raise BridgeError("Every blueprint node needs a name of 1-64 chars.")
    kind = entry.get("kind")
    if kind not in KINDS:
        raise BridgeError(
            f"Blueprint node {name!r} kind must be one of: "
            + ", ".join(KINDS)
            + "."
        )
    unknown = set(entry) - EDGES - FIELDS[kind]
    if unknown:
        raise BridgeError(
            f"Blueprint node {name!r} has unknown fields: "
            + ", ".join(sorted(unknown))
            + "."
        )
    node = dict(entry)
    retries = node.setdefault("retries", 0)
    if type(retries) is not int or not 0 <= retries <= MAX_RETRIES:
        raise BridgeError(
            f"Blueprint node {name!r} retries must be 0-{MAX_RETRIES}."
        )
    for edge in ("next", "on_failure"):
        if edge in node and not isinstance(node[edge], str):
            raise BridgeError(
                f"Blueprint node {name!r} {edge} must name a node."
            )
    if kind == "run":
        command = node.get("command")
        if isinstance(command, str):
            node["command"] = roster.verify_command(
                command, f"Blueprint node {name!r} command"
            )
        elif (
            not isinstance(command, list)
            or not command
            or any(not isinstance(token, str) for token in command)
        ):
            raise BridgeError(
                f"Blueprint node {name!r} needs a command: a string or a "
                "list of argument strings."
            )
        if not node["command"]:
            raise BridgeError(f"Blueprint node {name!r} needs a command.")
    if kind in ("run", "push"):
        limit = node.setdefault("timeout", VERIFY_TIMEOUT)
        if type(limit) not in (int, float) or not 0 < limit <= VERIFY_TIMEOUT:
            raise BridgeError(
                f"Blueprint node {name!r} timeout must be 1-{VERIFY_TIMEOUT} "
                "seconds."
            )
    if kind == "agent":
        prompt = node.get("prompt")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= MAX_TEXT:
            raise BridgeError(
                f"Blueprint node {name!r} needs a prompt of 1-{MAX_TEXT} "
                "characters."
            )
    if kind == "report":
        if node.setdefault("outcome", DONE) not in (DONE, BLOCKED):
            raise BridgeError(
                f"Blueprint node {name!r} outcome must be done or blocked."
            )
        message = node.setdefault("message", "")
        if not isinstance(message, str) or len(message) > MAX_TEXT:
            raise BridgeError(
                f"Blueprint node {name!r} message must be text of at most "
                f"{MAX_TEXT} characters."
            )
    return node


def define(bridge: Bridge, repo: Path, name: str, path: Path | None) -> str:
    """Records, replaces or removes one named blueprint.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        name: Blueprint name.
        path: Blueprint file to record, or None to remove the blueprint.

    Returns:
        An account of the change.

    Raises:
        BridgeError: If the command runs from a lane, the file cannot be
            read, or the blueprint is invalid or unknown.
    """
    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    unattended.operator_only(repo, root, data, "A blueprint is set")
    if not 1 <= len(name) <= 64 or not name.replace("-", "").isalnum():
        raise BridgeError(
            "Blueprint names use 1-64 letters, digits and hyphens."
        )
    nodes = None
    if path is not None:
        try:
            text = path.read_text()
        except OSError as exc:
            raise BridgeError(f"Cannot read blueprint {path}: {exc}.") from None
        nodes = parse(text)
    with lock(directory / "setup.lock"):
        data = roster.read(directory)
        stored = dict(data.get("blueprints") or {})
        if nodes is None:
            if stored.pop(name, None) is None:
                raise BridgeError(
                    f"No blueprint named {name!r}; run agent-parley "
                    "blueprint list."
                )
        else:
            stored[name] = nodes
        data["blueprints"] = stored
        write_json(directory / "project.json", data)
    if nodes is None:
        return f"Removed blueprint {name}."
    return f"Blueprint {name} records {len(nodes)} nodes: " + " -> ".join(
        node["name"] for node in nodes
    )


def describe(bridge: Bridge, repo: Path, name: str | None = None) -> str:
    """Lists the recorded blueprints, or shows one node by node.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository.
        name: Blueprint to show, or None to list every blueprint.

    Returns:
        The listing or the blueprint's nodes with their edges.

    Raises:
        BridgeError: If the project or the named blueprint does not exist.
    """
    root, directory = bridge.project(repo, create=False)
    stored = roster.read(directory).get("blueprints") or {}
    if name is None:
        if not stored:
            return (
                f"{root} has no blueprints; a claim runs as the lane decides."
            )
        return "\n".join(
            f"{key}: " + " -> ".join(node["name"] for node in nodes)
            for key, nodes in sorted(stored.items())
        )
    if name not in stored:
        raise BridgeError(
            f"No blueprint named {name!r}; run agent-parley blueprint list."
        )
    nodes = stored[name]
    lines = [f"Blueprint {name}:"]
    for index, node in enumerate(nodes):
        following = node.get("next") or (
            nodes[index + 1]["name"] if index + 1 < len(nodes) else DONE
        )
        if node["kind"] == "report":
            detail = node["outcome"]
        elif node["kind"] == "run":
            detail = shlex.join(node["command"])
        elif node["kind"] == "push":
            detail = shlex.join(PUSH)
        else:
            detail = node["prompt"].splitlines()[0][:60]
        edges = (
            ""
            if node["kind"] == "report"
            else f" ok->{following} fail->"
            f"{node.get('on_failure', node['name'])}"
            f" retries={node['retries']}"
        )
        lines.append(f"  {node['name']} [{node['kind']}] {detail}{edges}")
    return "\n".join(lines)


def _runs(directory: Path) -> dict:
    """Reads every recorded blueprint run, keyed by issue number."""
    try:
        runs = json.loads((directory / RUNS).read_text())
    except (OSError, ValueError):
        return {}
    return runs if isinstance(runs, dict) else {}


def _save(directory: Path, issue: str, run: dict) -> None:
    """Records one issue's run beside every other recorded run."""
    with lock(directory / f"{RUNS}.lock", timeout=5):
        runs = _runs(directory)
        runs[issue] = run
        write_json(directory / RUNS, runs)


def start(
    bridge: Bridge, repo: Path, name: str, blueprint: str, issue: str
) -> str:
    """Starts a lane's claim on a blueprint and runs it as far as it can go.

    The blueprint's nodes are copied into the run, so a later `blueprint
    set` changes the next run and never the one in flight.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        name: Participant whose claim runs the blueprint.
        blueprint: Recorded blueprint name.
        issue: Issue the participant claims, optionally prefixed with ``#``.

    Returns:
        An account of where the run stopped.

    Raises:
        BridgeError: If the command runs from a lane, the participant does
            not hold the issue, the blueprint is unknown, or a run on the
            issue is still in progress.
    """
    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    unattended.operator_only(repo, root, data, "A blueprint run is started")
    number = issue.strip().removeprefix("#")
    if name not in data["participants"]:
        raise BridgeError(
            f"{name} is not a participant in this project; run "
            "agent-parley participant list."
        )
    nodes = (data.get("blueprints") or {}).get(blueprint)
    if nodes is None:
        raise BridgeError(
            f"No blueprint named {blueprint!r}; run agent-parley "
            "blueprint list."
        )
    record = issues.snapshot(directory)["issues"].get(number) or {}
    if record.get("owner") != name:
        raise BridgeError(
            f"{name} does not hold issue #{number}; a blueprint runs on the "
            "lane's own claim."
        )
    with lock(directory / f"blueprint-{number}.lock", _busy(number)):
        current = _runs(directory).get(number)
        if current and current["status"] in (RUNNING, WAITING):
            raise BridgeError(
                f"Issue #{number} is already at blueprint node "
                f"{current['node']!r}; run agent-parley blueprint advance "
                f"--issue {number}."
            )
        run = {
            "blueprint": blueprint,
            "lane": name,
            "nodes": nodes,
            "node": nodes[0]["name"],
            "passed": [],
            "failures": {},
            "visits": 0,
            "status": RUNNING,
            "since": 0.0,
            "output": "",
            "message": "",
            "started": time.time(),
        }
        _save(directory, number, run)
        return _walk(bridge, repo, root, directory, data, number, run)


def advance(bridge: Bridge, repo: Path, issue: str) -> str:
    """Moves a claim's blueprint run on as far as it can go.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        issue: Issue whose run moves on, optionally prefixed with ``#``.

    Returns:
        An account of where the run stopped.

    Raises:
        BridgeError: If the command runs from a lane, or the issue has no
            run in progress.
    """
    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    unattended.operator_only(repo, root, data, "A blueprint run is advanced")
    number = issue.strip().removeprefix("#")
    with lock(directory / f"blueprint-{number}.lock", _busy(number)):
        run = _runs(directory).get(number)
        if not run or run["status"] not in (RUNNING, WAITING):
            raise BridgeError(
                f"Issue #{number} has no blueprint run in progress; start "
                "one with agent-parley blueprint run."
            )
        return _walk(bridge, repo, root, directory, data, number, run)


def progress(bridge: Bridge, repo: Path, issue: str | None = None) -> str:
    """Reports the recorded blueprint runs: current node and nodes passed.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository.
        issue: Issue to report, or None for every recorded run.

    Returns:
        One line per run.

    Raises:
        BridgeError: If the project does not exist.
    """
    _, directory = bridge.project(repo, create=False)
    runs = _runs(directory)
    if issue is not None:
        number = issue.strip().removeprefix("#")
        runs = {number: runs[number]} if number in runs else {}
    if not runs:
        return "No blueprint runs are recorded."
    return "\n".join(_line(number, run) for number, run in sorted(runs.items()))


def _busy(number: str) -> str:
    """Explains a run another operator command is already moving."""
    return (
        f"Another command is moving the blueprint run on issue #{number}; "
        "retry when it returns."
    )


def _line(number: str, run: dict) -> str:
    """Formats one run as its issue, lane, status, node and nodes passed."""
    passed = ", ".join(run["passed"]) or "none"
    line = (
        f"#{number} {run['lane']} {run['blueprint']}: {run['status']} at "
        f"{run['node']}; passed {passed}"
    )
    return f"{line}. {run['message']}" if run["message"] else line


def _walk(
    bridge: Bridge,
    repo: Path,
    root: Path,
    directory: Path,
    data: dict,
    number: str,
    run: dict,
) -> str:
    """Runs nodes until the run waits on its lane or ends.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Checkout the operator command names.
        root: Base checkout of the repository.
        directory: Project coordination directory.
        data: Project manifest.
        number: Issue the run belongs to.
        run: The run's recorded state, updated and saved as nodes pass.

    Returns:
        An account of where the run stopped.
    """
    nodes = {node["name"]: node for node in run["nodes"]}
    order = [node["name"] for node in run["nodes"]]
    lane = Path(data["participants"][run["lane"]]["lane"])
    while run["status"] in (RUNNING, WAITING):
        node = nodes[run["node"]]
        if node["kind"] == "report":
            run["passed"].append(node["name"])
            run["status"] = node["outcome"]
            run["message"] = node["message"]
            break
        if run["visits"] >= MAX_VISITS:
            run["status"] = BLOCKED
            run["message"] = (
                f"The run visited {MAX_VISITS} nodes without ending; check "
                "the blueprint's edges."
            )
            break
        if node["kind"] == "agent":
            passed = _agent(bridge, repo, directory, number, run, node)
            if passed is None:
                break
        else:
            command = PUSH if node["kind"] == "push" else node["command"]
            passed = _execute(command, lane, root, node["timeout"], run)
        run["visits"] += 1
        if passed:
            run["passed"].append(node["name"])
            index = order.index(node["name"])
            following = node.get("next") or (
                order[index + 1] if index + 1 < len(order) else None
            )
            if following is None:
                run["status"] = DONE
                break
            run["node"] = following
            continue
        failures = run["failures"].get(node["name"], 0) + 1
        run["failures"][node["name"]] = failures
        if failures > node["retries"]:
            run["status"] = BLOCKED
            run["message"] = (
                f"Node {node['name']} failed {failures} times, past its "
                f"retry limit of {node['retries']}."
            )
            break
        run["node"] = node.get("on_failure", node["name"])
    _save(directory, number, run)
    return _line(number, run)


def _execute(
    command: list[str], lane: Path, root: Path, timeout: float, run: dict
) -> bool:
    """Runs one deterministic node in the lane's worktree without a shell.

    Args:
        command: Argument tokens to run.
        lane: Worktree the command runs in.
        root: Base checkout, offered as ``AGENT_PARLEY_BASE``.
        timeout: Seconds the command may take.
        run: Run state whose ``output`` receives the bounded output tail.

    Returns:
        Whether the command exited zero within its timeout.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != unattended.LANE_TOKEN
    }
    environment["AGENT_PARLEY_BASE"] = str(root)
    try:
        result = subprocess.run(
            command,
            cwd=lane,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        run["output"] = f"`{shlex.join(command)}` could not run: {exc}"
        return False
    except subprocess.TimeoutExpired:
        run["output"] = f"`{shlex.join(command)}` ran past {timeout:g} seconds."
        return False
    lines = (result.stdout + result.stderr).splitlines()[-TAIL_LINES:]
    run["output"] = (
        f"`{shlex.join(command)}` exited {result.returncode}:\n"
        + "\n".join(lines)[-TAIL_CHARS:]
    )
    return not result.returncode


def _agent(
    bridge: Bridge,
    repo: Path,
    directory: Path,
    number: str,
    run: dict,
    node: dict,
) -> bool | None:
    """Hands an agent node's prompt to the lane, or reads its outcome.

    The first visit writes the prompt into the lane's inbox, with the last
    failing node's output attached, and waits. A later visit reads the
    lane's latest report: ready since the prompt passes the node, blocked
    fails it, and anything else keeps waiting.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Checkout the operator command names.
        directory: Project coordination directory.
        number: Issue the run belongs to.
        run: The run's recorded state.
        node: The agent node.

    Returns:
        True when the lane reported ready, False when it reported blocked,
        and None while the run waits on the lane.
    """
    if run["status"] == WAITING:
        try:
            activity = json.loads(
                (directory / f"{run['lane']}-activity.json").read_text()
            )
        except (OSError, ValueError):
            activity = {}
        if float(activity.get("reported_at") or 0) > run["since"]:
            if activity.get("outcome") in ("ready", "blocked"):
                run["status"] = RUNNING
                run["output"] = str(activity.get("summary") or "")
                return activity["outcome"] == "ready"
        return None
    text = node["prompt"]
    if run["output"] and run["failures"]:
        text = f"{text}\n\nLast failing step output:\n{run['output']}"
    run["since"] = time.time()
    bridge.say(
        repo,
        run["lane"],
        text,
        subject=f"Blueprint {run['blueprint']} step {node['name']} "
        f"for #{number}",
        key=f"blueprint-{number}-{node['name']}-{run['visits']}",
    )
    run["status"] = WAITING
    return None
