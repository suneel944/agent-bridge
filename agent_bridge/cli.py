"""Launches native agents with Git worktrees and MCP Agent Mail."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
from fastmcp import Client
from fastmcp.exceptions import ToolError

from agent_bridge.checkpoints import EVENTS, mailbox
from agent_bridge.issues import change, describe, snapshot
from agent_bridge.state import BridgeError, lock, write_json

AGENTS = {"claude": "GreenCastle", "codex": "BlueLake"}


def git(repo: Path, *args: str) -> str:
    """Runs Git in a repository and returns stripped stdout.

    Args:
        repo: Working directory for Git.
        *args: Individual Git arguments, never shell-expanded.

    Returns:
        Command output with surrounding whitespace removed.

    Raises:
        BridgeError: If Git exits unsuccessfully.
        subprocess.TimeoutExpired: If Git exceeds the command timeout.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise BridgeError(result.stderr.strip() or "Git command failed.")
    return result.stdout.strip()


class Bridge:
    """Coordinates native agent worktrees using one private local state root.

    Attributes:
        home: Resolved private state directory.
        config: Local HTTP port and bearer credential.
        url: Loopback HTTP origin of the mail server.
    """

    def __init__(self, home: Path) -> None:
        """Loads or initializes private configuration under home."""
        self.home = home.expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The directory contains a local bearer credential and private messages.
        if self.home.stat().st_mode & 0o077:
            raise BridgeError(
                f"State directory must be private: chmod 700 {self.home}"
            )
        with lock(self.home / "config.lock"):
            path = self.home / "config.json"
            if not path.exists():
                port = int(os.environ.get("AGENT_BRIDGE_PORT", "8876"))
                if not 1024 <= port <= 65535:
                    raise BridgeError(
                        "AGENT_BRIDGE_PORT must be between 1024 and 65535."
                    )
                write_json(
                    path, {"port": port, "token": secrets.token_urlsafe(32)}
                )
            self.config = json.loads(path.read_text())
        self.url = f"http://127.0.0.1:{self.config['port']}"

    def server_environment(self) -> dict[str, str]:
        """Returns the authenticated loopback server's process environment."""
        return {
            **os.environ,
            "HTTP_HOST": "127.0.0.1",
            "HTTP_PORT": str(self.config["port"]),
            "HTTP_PATH": "/mcp/",
            "HTTP_BEARER_TOKEN": self.config["token"],
            "HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED": "false",
            "HTTP_RBAC_DEFAULT_ROLE": "writer",
            "DATABASE_URL": f"sqlite+aiosqlite:///{self.home / 'mail.sqlite3'}",
            "STORAGE_ROOT": str(self.home / "mail"),
            "LLM_ENABLED": "false",
            "TOOLS_LOG_ENABLED": "false",
            "LOG_RICH_ENABLED": "false",
        }

    def server_process(self) -> psutil.Process | None:
        """Returns the recorded server only if its process identity matches."""
        record = self.home / "server.json"
        if not record.exists():
            return None
        data = json.loads(record.read_text())
        try:
            process = psutil.Process(data["pid"])
            if (
                process.create_time() == data["created"]
                and "mcp_agent_mail.cli" in process.cmdline()
                and process.status() != psutil.STATUS_ZOMBIE
            ):
                return process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return None

    def ready(self) -> bool:
        """Checks authenticated readiness without routing through proxies."""
        request = urllib.request.Request(
            self.url + "/health/readiness",
            headers={"Authorization": f"Bearer {self.config['token']}"},
        )
        # Never route the local credential through a configured HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=1) as response:
                return json.load(response).get("status") == "ready"
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def up(self) -> None:
        """Starts the mail server with bounded readiness checking.

        Raises:
            BridgeError: If the port is occupied or startup fails.
        """
        with lock(self.home / "server.lock"):
            process = self.server_process()
            if process:
                if not self.ready():
                    raise BridgeError(
                        "Server is running but unhealthy. "
                        f"Inspect {self.home}/server.log"
                    )
                return
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", self.config["port"]))
                except OSError:
                    raise BridgeError(
                        f"Port {self.config['port']} "
                        "is occupied by another service."
                    ) from None
            with (self.home / "server.log").open("ab") as log:
                child = subprocess.Popen(
                    [sys.executable, "-m", "mcp_agent_mail.cli", "serve-http"],
                    cwd=self.home,
                    env=self.server_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            process = psutil.Process(child.pid)
            write_json(
                self.home / "server.json",
                {"pid": child.pid, "created": process.create_time()},
            )
            # Bound startup checking; the server is detached from the terminal.
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    break
                if self.ready():
                    return
                time.sleep(0.2)
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            raise BridgeError(
                "Coordination server failed to start. "
                f"Inspect {self.home}/server.log"
            )

    def down(self) -> None:
        """Stops the identified server while retaining all persistent state.

        Raises:
            BridgeError: If locking fails or the server does not stop in time.
        """
        with lock(self.home / "server.lock"):
            process = self.server_process()
            if process:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except psutil.TimeoutExpired:
                    raise BridgeError(
                        "Server did not stop within 10s; inspect server.log."
                    ) from None
            (self.home / "server.json").unlink(missing_ok=True)

    def project(self, repo: Path) -> tuple[Path, Path]:
        """Returns main worktree and shared state paths for a Git repository."""
        common = Path(
            git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        )
        if git(repo, "rev-parse", "--is-bare-repository") == "true":
            raise BridgeError(
                "Use a non-bare repository with an initial commit."
            )
        # Git lists the main worktree first; linked worktrees share its key.
        root = Path(
            git(repo, "worktree", "list", "--porcelain", "-z").split("\0")[0][
                9:
            ]
        )
        key = hashlib.sha256(str(common.resolve()).encode()).hexdigest()[:16]
        directory = self.home / "projects" / key
        directory.mkdir(parents=True, exist_ok=True)
        return root, directory

    def setup(self, repo: Path) -> dict:
        """Creates or verifies both persistent agent worktrees.

        Args:
            repo: Main checkout or linked worktree of the target repository.

        Returns:
            Manifest containing the common root, base, branches, and lanes.

        Raises:
            BridgeError: If the checkout or lanes cannot be used safely.
        """
        root, directory = self.project(repo)
        with lock(directory / "setup.lock"):
            manifest = directory / "project.json"
            if manifest.exists():
                data = json.loads(manifest.read_text())
                for agent, lane in data["lanes"].items():
                    if (
                        git(Path(lane), "branch", "--show-current")
                        != data["branches"][agent]
                    ):
                        raise BridgeError(
                            f"{agent} worktree changed branch; "
                            "restore its bridge branch."
                        )
                return data
            base = git(root, "rev-parse", "--verify", "HEAD")
            if git(root, "status", "--porcelain"):
                raise BridgeError(
                    "Commit or preserve your pending changes first; "
                    "worktrees start at HEAD."
                )
            lanes = {agent: str(directory / agent) for agent in AGENTS}
            branches = {
                agent: f"bridge/{directory.name}/{agent}" for agent in AGENTS
            }
            # Preflight both lanes to avoid predictable partial setups.
            existing = git(
                root, "for-each-ref", "--format=%(refname:short)", "refs/heads"
            ).splitlines()
            for agent in AGENTS:
                if Path(lanes[agent]).exists() or branches[agent] in existing:
                    raise BridgeError(
                        f"Existing lane or branch for {agent}; "
                        "preserve it before setup."
                    )
            created = []
            try:
                for agent in AGENTS:
                    git(
                        root,
                        "worktree",
                        "add",
                        "-b",
                        branches[agent],
                        lanes[agent],
                        base,
                    )
                    created.append(agent)
                data = {
                    "root": str(root),
                    "base": base,
                    "lanes": lanes,
                    "branches": branches,
                }
                write_json(manifest, data)
            except Exception:
                # Keep any created work intact, including setup-hook changes.
                if created:
                    print(
                        f"Partial setup preserved in {directory}: "
                        f"{', '.join(created)}",
                        file=sys.stderr,
                    )
                raise
            return data

    async def identity(self, agent: str, data: dict) -> dict:
        """Registers a lane and authorizes its paired identity through MCP.

        Args:
            agent: Native CLI lane name.
            data: Project manifest from setup.

        Returns:
            Private registration data, including its credential.
        """
        path = Path(data["lanes"][agent]).parent / f"{agent}-identity.json"
        credentials = json.loads(path.read_text()) if path.exists() else {}
        async with Client(
            self.url + "/mcp/", auth=self.config["token"], timeout=15
        ) as client:
            await client.call_tool(
                "ensure_project", {"human_key": data["root"]}
            )
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": data["root"],
                    "name": AGENTS[agent],
                    "program": agent,
                    "model": "native-cli-default",
                    "registration_token": credentials.get("registration_token"),
                },
            )
            write_json(path, result.data)
            peer = "codex" if agent == "claude" else "claude"
            peer_path = path.parent / f"{peer}-identity.json"
            if peer_path.exists():
                peer_identity = json.loads(peer_path.read_text())
                # Authorize only this project pair through the contact API.
                # The launcher owns both identities; enforcement stays enabled.
                for sender, recipient in [
                    (result.data, peer_identity),
                    (peer_identity, result.data),
                ]:
                    await client.call_tool(
                        "respond_contact",
                        {
                            "project_key": data["root"],
                            "from_agent": sender["name"],
                            "to_agent": recipient["name"],
                            "accept": True,
                            "registration_token": recipient[
                                "registration_token"
                            ],
                        },
                    )
        return result.data

    def protocol(self, agent: str, data: dict) -> str:
        """Builds coordination instructions without embedding tokens."""
        peer = AGENTS["codex" if agent == "claude" else "claude"]
        return f"""Agent Bridge protocol (also follow repository instructions):
You are {AGENTS[agent]} using {agent}; your peer is {peer}.
Use the agent_bridge MCP server. Canonical project key: {data["root"]}
Your editable worktree: {data["lanes"][agent]}
The canonical project key is an identity, NOT a directory to edit.
Your private identity credential file (outside the repo):
{Path(data["lanes"][agent]).parent / (agent + "-identity.json")}

The launcher registered your identity. Read your credential file; keep its token
private; never commit or send it in messages. Pass registration_token on tools
that accept it, sender_token for send_message, and agent_token for resources.
Use register_agent with that credential to update your actual model
and task_description.
Read your inbox and active file reservations. Discover registered
peers via resource://agents/<project_key>; do not assume the peer is online.
Publish your task and intended files in a thread when the peer is available.
Before working on a numbered issue, run `agent-bridge issue claim NUMBER` from
your worktree. A conflict means choose another issue or request a handoff.
Use `agent-bridge issue list` before each editing phase to verify ownership.
To hand off: stop work on that issue, then `agent-bridge issue offer NUMBER
--to claude|codex --summary "commit, checks, remaining work"`. Stay paused until
it is accepted, declined, or you cancel it. The recipient reviews the summary
and runs `agent-bridge issue accept NUMBER --offer-id ID` before starting.
Decline with `issue decline NUMBER --offer-id ID`.
The owner can `issue cancel NUMBER`.
No timeout transfers ownership. Release finished responsibility with
`agent-bridge issue release NUMBER`; release does not mean merged or complete.
Reserve repo-relative file paths before editing. Reservations are advisory:
if conflicts are returned, stop overlapping work, release the conflicting grant,
and agree on ownership with the peer. Do not treat a granted lease as permission
to ignore conflicts. Renew reservations before expiry while work continues.

Check your inbox before each new editing phase and before committing. Announce
interface changes, decisions, and blockers; request acknowledgement for changes
the peer depends on. When finished, send a handoff containing the exact commit
(if committed), changed files, verification commands/results, and limitations,
then release your reservations. Avoid repeated empty inbox polling.

Edit only your worktree. Do not reset, clean, switch, merge, or modify the peer
worktree or the main checkout. Preserve existing work on your branch. Shared
ports/databases need coordination; worktrees do not isolate those resources.
Follow repository commit rules. Integration into the main branch remains a
separate reviewed action with combined verification. If coordination is down,
report it and pause edits rather than silently continuing without coordination.

Native checkpoints deliver peer messages and track activity automatically.
Delivery does not acknowledge a message. After reviewing, explicitly call
acknowledge_message. Use mark_message_read after reviewing ordinary messages
to keep restart briefings current.
Before a handoff, run `agent-bridge --home {shlex.quote(str(self.home))} report`
with `--state partial --summary "..." --remaining "..."`
or `--state ready --summary "..." --evidence "commands and results"`.
Use --state blocked with --remaining to explain a blocker. Ready means ready for
review, not merged or independently verified. An idle turn is not completion.
"""

    def hooks(self, agent: str, directory: Path) -> dict:
        """Builds native lifecycle hook definitions for a lane."""
        command = shlex.join(
            [
                sys.executable,
                "-m",
                "agent_bridge.checkpoints",
                "--home",
                str(self.home),
                "--directory",
                str(directory),
                "--agent",
                agent,
            ]
        )
        return {
            event: [
                {
                    "hooks": [
                        {"type": "command", "command": command, "timeout": 3}
                    ]
                }
            ]
            for event in EVENTS
        }

    def report(
        self,
        repo: Path,
        outcome: str,
        summary: str,
        remaining: str,
        evidence: str,
    ) -> None:
        """Records an explicitly reported outcome independently of activity.

        Args:
            repo: Assigned agent worktree.
            outcome: Partial, blocked, or ready-for-review state.
            summary: Nonempty account of the result.
            remaining: Required unfinished work for partial or blocked reports.
            evidence: Required verification evidence for ready reports.

        Raises:
            BridgeError: If the lane or required report fields are invalid.
        """
        if not summary.strip():
            raise BridgeError("Reports require a nonempty --summary.")
        _, directory = self.project(repo)
        data = json.loads((directory / "project.json").read_text())
        lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        agent = next(
            (
                name
                for name, path in data["lanes"].items()
                if Path(path) == lane
            ),
            None,
        )
        if agent is None:
            raise BridgeError(
                "Report from the agent's worktree, not the main checkout."
            )
        if outcome in ("partial", "blocked") and not remaining.strip():
            raise BridgeError("Partial/blocked reports require --remaining.")
        if outcome == "ready" and not evidence.strip():
            raise BridgeError("Ready-for-review reports require --evidence.")
        with lock(directory / f"{agent}-checkpoint.lock"):
            path = directory / f"{agent}-activity.json"
            state = json.loads(path.read_text()) if path.exists() else {}
            state.update(
                outcome=outcome,
                summary=summary,
                remaining=remaining,
                evidence=evidence,
                reported_at=time.time(),
            )
            write_json(path, state)

    def issue(
        self,
        repo: Path,
        action: str,
        number: str = "",
        *,
        to: str | None = None,
        summary: str = "",
        offer_id: str | None = None,
    ) -> dict:
        """Reads the issue ledger or applies a transition as the selected lane.

        Args:
            repo: Repository for listing, or assigned worktree for mutations.
            action: List, claim, release, offer, accept, decline, or cancel.
            number: Repository issue number for a mutation.
            to: Handoff recipient.
            summary: Handoff context supplied by the owner.
            offer_id: Exact current offer ID for acceptance or decline.

        Returns:
            The whole ledger for list, or the resulting issue record.

        Raises:
            BridgeError: If lane, ownership, or transition checks fail.
        """
        _, directory = self.project(repo)
        data = json.loads((directory / "project.json").read_text())
        if action == "list":
            return snapshot(directory)
        lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        agent = next(
            (
                name
                for name, path in data["lanes"].items()
                if Path(path) == lane
            ),
            None,
        )
        if agent is None:
            raise BridgeError(
                "Change issue ownership from the agent's worktree."
            )
        return change(
            directory,
            agent,
            action,
            number,
            to=to,
            summary=summary,
            offer_id=offer_id,
        )

    def status(self) -> None:
        """Prints activity, reported outcomes, and coordination state."""
        import sqlite3

        healthy = self.server_process() and self.ready()
        print(f"Server: {'ready' if healthy else 'not ready'}")
        print(f"State: {self.home}")
        for path in sorted((self.home / "projects").glob("*/project.json")):
            data = json.loads(path.read_text())
            print(f"\nProject: {data['root']}")
            print(describe(snapshot(path.parent)))
            for agent, name in AGENTS.items():
                state_path = path.parent / f"{agent}-activity.json"
                state = (
                    json.loads(state_path.read_text())
                    if state_path.exists()
                    else {}
                )
                try:
                    with lock(path.parent / f"{agent}.session.lock"):
                        activity = "stopped"
                except BridgeError:
                    activity = state.get(
                        "activity",
                        "running; checkpoints unavailable (relaunch)",
                    )
                age = (
                    f"; event {int(time.time() - state['updated'])}s ago"
                    if state.get("updated")
                    else ""
                )
                print(f"  {agent} ({name}): {activity}{age}")
                print(
                    f"    Reported outcome: {state.get('outcome', 'unknown')}"
                )
                if state.get("reported_at"):
                    report_age = int(time.time() - state["reported_at"])
                    print(f"    Report age: {report_age}s")
                if state.get("summary"):
                    print(f"    Summary: {state['summary']}")
                if state.get("remaining"):
                    print(f"    Remaining: {state['remaining']}")
                if state.get("evidence"):
                    print(f"    Reported verification: {state['evidence']}")
                try:
                    mail = mailbox(
                        self.home, data["root"], name, state.get("cursor", 0)
                    )
                    print(
                        f"    Unread: {mail['unread']}; "
                        f"pending acknowledgements: {mail['pending_ack']}; "
                        f"active reservations: {mail['reservations']}"
                    )
                    print(
                        "    Last coordination: "
                        f"{mail['last_coordination']} UTC"
                    )
                    task = (
                        state.get("last_prompt")
                        or mail["reported_task"]
                        or state.get("task", "")
                    )
                    print(f"    Latest prompt/task (reported): {task[:240]}")
                    if mail["messages"]:
                        print(
                            "    Awaiting checkpoint delivery: "
                            f"{len(mail['messages'])} "
                            "(batch capped at 5)"
                        )
                except (sqlite3.Error, BridgeError, OSError) as exc:
                    print(f"    Coordination unavailable: {exc}")

    def launch(self, agent: str, repo: Path, task: str) -> int:
        """Runs one native agent in its persistent lane.

        Args:
            agent: Native executable name, claude or codex.
            repo: Target Git repository.
            task: User task passed as an argument without shell expansion.

        Returns:
            The native process exit code.

        Raises:
            BridgeError: If setup fails or the lane already has a launcher.
        """
        executable = shutil.which(agent)
        if executable is None:
            raise BridgeError(
                f"Install and sign in to the native {agent} CLI first."
            )
        data = self.setup(repo)
        lane = Path(data["lanes"][agent])
        with lock(lane.parent / f"{agent}.session.lock"):
            self.up()
            asyncio.run(self.identity(agent, data))
            prompt = self.protocol(agent, data)
            hooks = self.hooks(agent, lane.parent)
            env = {
                **os.environ,
                "AGENT_BRIDGE_TOKEN": self.config["token"],
                "AGENT_BRIDGE_HOME": str(self.home),
            }
            if agent == "claude":
                config = lane.parent / "claude-mcp.json"
                write_json(
                    config,
                    {
                        "mcpServers": {
                            "agent_bridge": {
                                "type": "http",
                                "url": self.url + "/mcp/",
                                "headers": {
                                    "Authorization": (
                                        "Bearer ${AGENT_BRIDGE_TOKEN}"
                                    )
                                },
                            }
                        }
                    },
                )
                command = [
                    executable,
                    "--mcp-config",
                    str(config),
                    "--append-system-prompt",
                    prompt,
                    "--settings",
                    json.dumps({"hooks": hooks}),
                    "--",
                    task,
                ]
            else:
                command = [
                    executable,
                    "-c",
                    "mcp_servers.agent_bridge.url="
                    + json.dumps(self.url + "/mcp/"),
                    "-c",
                    'mcp_servers.agent_bridge.bearer_token_env_var="AGENT_BRIDGE_TOKEN"',
                ]
                for event, groups in hooks.items():
                    hook = groups[0]["hooks"][0]
                    value = (
                        '[{hooks=[{type="command",command='
                        + json.dumps(hook["command"])
                        + ",timeout=3}]}]"
                    )
                    command.extend(["-c", f"hooks.{event}={value}"])
                command.append(prompt + "\nUser task:\n" + task)
            print(
                f"{agent}: {lane}\nShared project: {data['root']}", flush=True
            )
            activity_path = lane.parent / f"{agent}-activity.json"
            previous = (
                json.loads(activity_path.read_text())
                if activity_path.exists()
                else {}
            )
            previous.update(
                activity="starting; awaiting native hook",
                task=task,
                updated=time.time(),
                session_id="",
                cursor=0,
            )
            previous.pop("last_prompt", None)
            write_json(activity_path, previous)
            try:
                return subprocess.call(command, cwd=lane, env=env)
            finally:
                with lock(lane.parent / f"{agent}-checkpoint.lock"):
                    state = json.loads(activity_path.read_text())
                    state.update(activity="stopped", updated=time.time())
                    write_json(activity_path, state)


def main() -> int:
    """Dispatches the CLI and returns an operational exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(
            os.environ.get("AGENT_BRIDGE_HOME", "~/.local/state/agent-bridge")
        ),
        help="Private state directory (or AGENT_BRIDGE_HOME).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "up", help="Start the local coordination server in the background."
    )
    commands.add_parser(
        "down",
        help="Stop the coordination server; retain all data and worktrees.",
    )
    commands.add_parser(
        "status", help="Show server health and registered workspaces."
    )
    setup = commands.add_parser(
        "setup", help="Create Claude and Codex worktrees from committed HEAD."
    )
    setup.add_argument("repo", type=Path)
    run = commands.add_parser(
        "run", help="Launch a native agent in this terminal."
    )
    run.add_argument("agent", choices=AGENTS)
    run.add_argument("--repo", type=Path, default=Path.cwd())
    run.add_argument(
        "--task", default="Check shared coordination state and await my task."
    )
    report = commands.add_parser(
        "report", help="Record a partial, blocked, or ready-for-review handoff."
    )
    report.add_argument("--repo", type=Path, default=Path.cwd())
    report.add_argument(
        "--state", choices=("partial", "blocked", "ready"), required=True
    )
    report.add_argument("--summary", required=True)
    report.add_argument("--remaining", default="")
    report.add_argument("--evidence", default="")
    issue = commands.add_parser(
        "issue", help="Claim issues and explicitly hand off ownership."
    )
    actions = issue.add_subparsers(dest="action", required=True)
    for action in (
        "list",
        "claim",
        "release",
        "offer",
        "accept",
        "decline",
        "cancel",
    ):
        command = actions.add_parser(action)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        if action != "list":
            command.add_argument("number")
        if action == "offer":
            command.add_argument("--to", choices=AGENTS, required=True)
            command.add_argument("--summary", required=True)
        if action in ("accept", "decline"):
            command.add_argument("--offer-id", required=True)
    args = parser.parse_args()
    try:
        bridge = Bridge(args.home)
        if args.command == "up":
            bridge.up()
            print(f"Coordination server ready at {bridge.url}/mcp/")
        elif args.command == "down":
            bridge.down()
            print(
                "Coordination server stopped. Worktrees and messages retained."
            )
        elif args.command == "setup":
            print(json.dumps(bridge.setup(args.repo.resolve()), indent=2))
        elif args.command == "run":
            return bridge.launch(args.agent, args.repo.resolve(), args.task)
        elif args.command == "report":
            bridge.report(
                args.repo.resolve(),
                args.state,
                args.summary,
                args.remaining,
                args.evidence,
            )
            print(f"Recorded outcome: {args.state}")
        elif args.command == "issue":
            result = bridge.issue(
                args.repo.resolve(),
                args.action,
                getattr(args, "number", ""),
                to=getattr(args, "to", None),
                summary=getattr(args, "summary", ""),
                offer_id=getattr(args, "offer_id", None),
            )
            print(
                describe(result)
                if args.action == "list"
                else json.dumps(result, indent=2)
            )
        else:
            bridge.status()
        return 0
    except (
        BridgeError,
        ToolError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"agent-bridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
