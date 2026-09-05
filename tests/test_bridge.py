import asyncio
import contextlib
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_bridge.checkpoints import checkpoint, mailbox
from agent_bridge.cli import Bridge, BridgeError, git, lock, write_json
from agent_bridge.process import start_ticks


@contextlib.asynccontextmanager
async def client_session(url, auth):
    """Connects the independent official SDK to the real HTTP service."""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {auth}"}, trust_env=False
    ) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


def test_isolation_identity_and_idempotent_setup(bridge, repo):
    data = bridge.setup(repo)
    claude, codex = (Path(data["lanes"][name]) for name in ("claude", "codex"))
    (claude / "shared.txt").write_text("Claude change\n")
    assert (codex / "shared.txt").read_text() == "original\n"
    assert (repo / "shared.txt").read_text() == "original\n"
    assert bridge.setup(codex) == data
    assert bridge.project(claude) == bridge.project(repo)
    assert (
        len(git(repo, "worktree", "list", "--porcelain").split("worktree "))
        == 4
    )


def test_dirty_source_is_not_silently_omitted(bridge, repo):
    (repo / "shared.txt").write_text("uncommitted work\n")
    with pytest.raises(BridgeError, match="pending changes"):
        bridge.setup(repo)
    assert (repo / "shared.txt").read_text() == "uncommitted work\n"


def test_missing_commit_and_existing_branch_preserved(bridge, repo, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init")
    with pytest.raises(BridgeError):
        bridge.setup(empty)
    _, directory = bridge.project(repo)
    branch = f"bridge/{directory.name}/codex"
    git(repo, "branch", branch)
    with pytest.raises(BridgeError, match="Existing lane"):
        bridge.setup(repo)
    assert not (directory / "claude").exists()
    assert git(repo, "rev-parse", branch) == git(repo, "rev-parse", "HEAD")


def test_lock_rejects_second_session(bridge):
    path = bridge.home / "session.lock"
    with lock(path), pytest.raises(BridgeError, match="owns"):
        with lock(path):
            pass


def test_public_state_directory_rejected(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(BridgeError, match="private"):
        Bridge(public)


def test_occupied_port_fails_without_killing_owner(bridge):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", bridge.config["port"]))
        sock.listen()
        with pytest.raises(BridgeError, match="occupied"):
            bridge.up()
        assert sock.getsockname()[1] == bridge.config["port"]


def test_stale_pid_record_cannot_stop_an_unrelated_process(bridge):
    pid = os.getpid()
    write_json(
        bridge.home / "server.json",
        {
            "pid": pid,
            "start_ticks": start_ticks(pid),
        },
    )
    assert bridge.server_process() is None
    bridge.down()
    os.kill(pid, 0)


def test_changed_lane_branch_is_rejected_without_resetting(bridge, repo):
    data = bridge.setup(repo)
    lane = Path(data["lanes"]["codex"])
    git(lane, "switch", "-c", "personal-work")
    with pytest.raises(BridgeError, match="changed branch"):
        bridge.setup(repo)
    assert git(lane, "branch", "--show-current") == "personal-work"


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_launch_preserves_task_and_passes_shared_configuration(
    bridge, repo, monkeypatch, tmp_path, agent
):
    """Exercises native argv, cwd and environment without a model call."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / agent
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'w') as f:\n"
        " json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        "'has_token': bool(os.environ.get('AGENT_BRIDGE_TOKEN'))}, f)\n"
    )
    executable.chmod(0o755)
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def fake_identity(*args):
        return {"registration_token": "test-scoped-credential"}

    monkeypatch.setattr(bridge, "identity", fake_identity)
    task = 'Fix "login"; $(do-not-execute)\nPreserve this newline.'
    assert bridge.launch(agent, repo, task) == 0
    result = json.loads(capture.read_text())
    assert result["cwd"] == bridge.setup(repo)["lanes"][agent]
    assert result["has_token"]
    assert task in result["argv"][-1]
    assert bridge.config["token"] not in json.dumps(result)
    assert str(repo) in " ".join(result["argv"])
    if agent == "claude":
        config = json.loads(Path(result["argv"][1]).read_text())
        assert (
            config["mcpServers"]["agent_bridge"]["url"] == bridge.url + "/mcp/"
        )
        settings = json.loads(
            result["argv"][result["argv"].index("--settings") + 1]
        )
        assert "PreToolUse" in settings["hooks"]
        assert "Stop" in settings["hooks"]
    else:
        assert (
            'mcp_servers.agent_bridge.bearer_token_env_var="AGENT_BRIDGE_TOKEN"'
            in result["argv"]
        )
        overrides = [arg for arg in result["argv"] if arg.startswith("hooks.")]
        parsed = tomllib.loads("\n".join(overrides))
        assert "PreToolUse" in parsed["hooks"]
        assert "SessionEnd" in parsed["hooks"]
    assert "bypass" not in " ".join(result["argv"])


def test_mcp_two_clients_conflict_handoff_auth_and_restart(bridge, repo):
    data = bridge.setup(repo)
    bridge.up()
    pid = bridge.server_process().pid
    bridge.up()
    assert bridge.server_process().pid == pid
    response = httpx.post(
        bridge.url + "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        },
        trust_env=False,
    )
    assert response.status_code == 401

    async def exercise():
        tokens = {}
        for agent, name in [("claude", "GreenCastle"), ("codex", "BlueLake")]:
            identity = await bridge.identity(agent, data)
            tokens[name] = identity["registration_token"]

        async def call(client, tool, arguments):
            arguments.pop("project_key", None)
            arguments.pop("agent_name", None)
            arguments.pop("sender_name", None)
            if tool == "send_message":
                arguments["idempotency_key"] = arguments["subject"]
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, result.content
            return result

        async with client_session(
            bridge.url + "/mcp/", auth=tokens["GreenCastle"]
        ) as claude:
            async with client_session(
                bridge.url + "/mcp/", auth=tokens["BlueLake"]
            ) as codex:
                root = data["root"]
                assert len((await claude.list_tools()).tools) == 6
                granted = await call(
                    claude,
                    "file_reservation_paths",
                    {
                        "project_key": root,
                        "agent_name": "GreenCastle",
                        "paths": ["shared.txt"],
                        "ttl_seconds": 300,
                        "exclusive": True,
                    },
                )
                assert not json.loads(granted.content[0].text)["conflicts"]
                conflict = await call(
                    codex,
                    "file_reservation_paths",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                        "paths": ["shared.txt"],
                        "ttl_seconds": 300,
                        "exclusive": True,
                    },
                )
                assert json.loads(conflict.content[0].text)["conflicts"]
                await call(
                    codex,
                    "release_file_reservations",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                    },
                )
                await call(
                    claude,
                    "send_message",
                    {
                        "project_key": root,
                        "sender_name": "GreenCastle",
                        "to": ["BlueLake"],
                        "subject": "Login contract",
                        "body_md": "Response now includes session_id.",
                        "thread_id": "login-task",
                        "ack_required": True,
                    },
                )
                inbox = await call(
                    codex,
                    "fetch_inbox",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                        "include_bodies": True,
                    },
                )
                messages = json.loads(inbox.content[0].text)["messages"]
                message = next(
                    item
                    for item in messages
                    if item["subject"] == "Login contract"
                )
                assert "session_id" in message["body_md"]
                directory = Path(data["lanes"]["codex"]).parent
                payload = {
                    "hook_event_name": "PreToolUse",
                    "session_id": "codex-test",
                    "cwd": data["lanes"]["codex"],
                    "tool_name": "apply_patch",
                }
                hook = bridge.hooks("codex", directory)["PreToolUse"][0][
                    "hooks"
                ][0]
                result = subprocess.run(
                    shlex.split(hook["command"]),
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                assert result.returncode == 0, result.stderr
                delivered = json.loads(result.stdout)["hookSpecificOutput"]
                assert delivered["permissionDecision"] == "deny"
                assert "session_id" in delivered["additionalContext"]
                assert (
                    checkpoint(bridge.home, directory, "codex", payload) == {}
                )
                assert (
                    mailbox(bridge.home, root, "BlueLake")["pending_ack"] == 1
                )
                await call(
                    codex,
                    "acknowledge_message",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                        "message_id": message["id"],
                    },
                )
                assert (
                    mailbox(bridge.home, root, "BlueLake")["pending_ack"] == 0
                )
                await call(
                    claude,
                    "release_file_reservations",
                    {
                        "project_key": root,
                        "agent_name": "GreenCastle",
                    },
                )
                acquired = await call(
                    codex,
                    "file_reservation_paths",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                        "paths": ["shared.txt"],
                        "ttl_seconds": 300,
                        "exclusive": True,
                    },
                )
                assert not json.loads(acquired.content[0].text)["conflicts"]
                await call(
                    codex,
                    "send_message",
                    {
                        "project_key": root,
                        "sender_name": "BlueLake",
                        "to": ["GreenCastle"],
                        "subject": "Handoff",
                        "body_md": (
                            f"Reviewed commit {data['base']}; "
                            "shared.txt unchanged."
                        ),
                        "thread_id": "login-task",
                    },
                )
                handoff = await call(
                    claude,
                    "fetch_inbox",
                    {
                        "project_key": root,
                        "agent_name": "GreenCastle",
                        "include_bodies": True,
                    },
                )
                assert data["base"] in handoff.content[0].text
                stop = {
                    "hook_event_name": "Stop",
                    "session_id": "claude-test",
                    "cwd": data["lanes"]["claude"],
                }
                assert (
                    checkpoint(bridge.home, directory, "claude", stop)[
                        "decision"
                    ]
                    == "block"
                )
                assert (
                    checkpoint(
                        bridge.home,
                        directory,
                        "claude",
                        {
                            **stop,
                            "stop_hook_active": True,
                        },
                    )
                    == {}
                )
                state = json.loads(
                    (directory / "claude-activity.json").read_text()
                )
                assert state["activity"] == "idle"
                assert "outcome" not in state

    asyncio.run(exercise())
    bridge.down()
    assert bridge.server_process() is None
    bridge.up()

    async def persisted():
        identity = await bridge.identity("codex", data)
        async with client_session(
            bridge.url + "/mcp/", auth=identity["registration_token"]
        ) as client:
            result = await client.call_tool(
                "fetch_inbox",
                {
                    "include_bodies": True,
                },
            )
            assert "session_id" in result.content[0].text

    asyncio.run(persisted())
    assert (Path(data["lanes"]["claude"]) / "shared.txt").exists()


def test_reports_require_remaining_work_or_verification(bridge, repo):
    data = bridge.setup(repo)
    lane = Path(data["lanes"]["claude"])
    with pytest.raises(BridgeError, match="remaining"):
        bridge.report(lane, "partial", "Engine built", "", "")
    with pytest.raises(BridgeError, match="evidence"):
        bridge.report(lane, "ready", "Engine built", "", "")
    with pytest.raises(BridgeError, match="worktree"):
        bridge.report(repo, "ready", "Engine built", "", "pytest: 12 passed")
    bridge.report(
        lane,
        "partial",
        "Engine built",
        "CLI integration missing",
        "12 tests passed",
    )
    state = json.loads((lane.parent / "claude-activity.json").read_text())
    assert state["outcome"] == "partial"
    assert state["remaining"] == "CLI integration missing"


def test_legacy_status_does_not_infer_completion(bridge, repo, capsys):
    data = bridge.setup(repo)
    directory = Path(data["lanes"]["claude"]).parent
    with lock(directory / "claude.session.lock"):
        bridge.status()
    output = capsys.readouterr().out
    assert "running; checkpoints unavailable (relaunch)" in output
    assert "Reported outcome: unknown" in output


def test_hook_failure_pauses_tools_and_foreign_worktree_is_rejected(
    bridge, repo
):
    data = bridge.setup(repo)
    lane = Path(data["lanes"]["claude"])
    write_json(lane.parent / "claude-identity.json", {"name": "GreenCastle"})
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(lane),
        "session_id": "test",
    }
    result = checkpoint(bridge.home, lane.parent, "claude", payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    with pytest.raises(BridgeError, match="cwd"):
        checkpoint(
            bridge.home, lane.parent, "claude", {**payload, "cwd": str(repo)}
        )
    assert (
        checkpoint(
            bridge.home, lane.parent, "claude", {**payload, "agent_id": "child"}
        )
        == {}
    )


def test_issue_claim_race_persistence_and_explicit_handoff(
    bridge, repo, tmp_path
):
    data = bridge.setup(repo)
    lanes = {name: Path(path) for name, path in data["lanes"].items()}
    commands = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_bridge.cli",
                "--home",
                str(bridge.home),
                "issue",
                "claim",
                "432",
            ],
            cwd=lane,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for lane in lanes.values()
    ]
    results = [command.communicate(timeout=10) for command in commands]
    assert sorted(command.returncode for command in commands) == [0, 1], results
    restarted = Bridge(bridge.home)
    record = restarted.issue(repo, "list")["issues"]["432"]
    owner = record["owner"]
    peer = "codex" if owner == "claude" else "claude"
    with pytest.raises(BridgeError, match="owned"):
        bridge.issue(lanes[peer], "claim", "#432")
    with pytest.raises(BridgeError, match="Only"):
        bridge.issue(lanes[peer], "release", "432")
    offered = bridge.issue(
        lanes[owner], "offer", "432", to=peer, summary="Review commit abc"
    )
    offer_id = offered["offer"]["id"]
    assert offered["owner"] == owner
    with pytest.raises(BridgeError, match="recipient"):
        bridge.issue(lanes[owner], "accept", "432", offer_id=offer_id)
    bridge.issue(lanes[owner], "cancel", "432")
    replacement = bridge.issue(
        lanes[owner], "offer", "432", to=peer, summary="Updated handoff"
    )
    with pytest.raises(BridgeError, match="changed"):
        bridge.issue(lanes[peer], "accept", "432", offer_id=offer_id)
    accepted = restarted.issue(
        lanes[peer], "accept", "432", offer_id=replacement["offer"]["id"]
    )
    assert accepted["owner"] == peer
    assert accepted["offer"] is None
    with pytest.raises(BridgeError, match="Only"):
        bridge.issue(lanes[owner], "release", "432")
    bridge.issue(lanes[peer], "release", "432")
    assert bridge.issue(lanes[owner], "claim", "432")["owner"] == owner
    assert len(bridge.issue(repo, "list")["issues"]["432"]["history"]) == 7


def test_issue_decline_no_timeout_and_worktree_authority(
    bridge, repo, monkeypatch
):
    data = bridge.setup(repo)
    claude, codex = (Path(data["lanes"][name]) for name in ("claude", "codex"))
    with pytest.raises(BridgeError, match="worktree"):
        bridge.issue(repo, "claim", "432")
    for number in (
        "0",
        "-1",
        "../432",
        "0432",
        "https://github.com/a/b/issues/432",
    ):
        with pytest.raises(BridgeError, match="positive"):
            bridge.issue(claude, "claim", number)
    bridge.issue(claude, "claim", "432")
    offer = bridge.issue(
        claude, "offer", "432", to="codex", summary="Waiting for review"
    )["offer"]
    monkeypatch.setattr(
        "agent_bridge.issues.time.time", lambda: offer["created"] + 86400
    )
    assert bridge.issue(repo, "list")["issues"]["432"]["owner"] == "claude"
    declined = bridge.issue(codex, "decline", "432", offer_id=offer["id"])
    assert declined["owner"] == "claude"
    assert declined["offer"] is None


def test_issue_crash_releases_operation_lock_but_preserves_owner(bridge, repo):
    data = bridge.setup(repo)
    claude, codex = (Path(data["lanes"][name]) for name in ("claude", "codex"))
    bridge.issue(claude, "claim", "432")
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_bridge.state import lock\n"
            "with lock(Path(sys.argv[1])): os._exit(7)",
            str(claude.parent / "issues.lock"),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert crashed.returncode == 7
    with pytest.raises(BridgeError, match="owned by claude"):
        Bridge(bridge.home).issue(codex, "claim", "432")
    bridge.issue(claude, "release", "432")
    assert bridge.issue(codex, "claim", "432")["owner"] == "codex"


def test_issue_notifications_are_once_per_change_without_empty_reminders(
    bridge, repo, monkeypatch
):
    data = bridge.setup(repo)
    claude, codex = (Path(data["lanes"][name]) for name in ("claude", "codex"))
    directory = claude.parent
    write_json(directory / "codex-identity.json", {"name": "BlueLake"})
    monkeypatch.setattr(
        "agent_bridge.checkpoints.mailbox",
        lambda *args: {"pending_ack": 0, "messages": []},
    )
    bridge.issue(claude, "claim", "432")
    bridge.issue(
        claude,
        "offer",
        "432",
        to="codex",
        summary="Read tests before accepting",
    )
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "test",
        "cwd": str(codex),
    }
    notice = checkpoint(bridge.home, directory, "codex", payload)[
        "hookSpecificOutput"
    ]
    assert notice["permissionDecision"] == "deny"
    assert "handoff to codex" in notice["additionalContext"]
    assert checkpoint(bridge.home, directory, "codex", payload) == {}
    reminder = checkpoint(
        bridge.home,
        directory,
        "codex",
        {**payload, "hook_event_name": "UserPromptSubmit"},
    )
    assert reminder == {}
    bridge.issue(claude, "cancel", "432")
    stop = {**payload, "hook_event_name": "Stop", "stop_hook_active": True}
    assert checkpoint(bridge.home, directory, "codex", stop) == {}
    assert (
        checkpoint(bridge.home, directory, "codex", payload)[
            "hookSpecificOutput"
        ]["permissionDecision"]
        == "deny"
    )


def test_built_wheel_installs_and_coordinates_outside_checkout(tmp_path, repo):
    project = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((project / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    wheel = project / "dist" / f"agent_bridge-{version}-py3-none-any.whl"
    assert wheel.exists(), "Run make build before the installed-package test."
    with zipfile.ZipFile(wheel) as archive:
        assert "agent_bridge/__main__.py" in archive.namelist()
        package_metadata = archive.read(
            f"agent_bridge-{version}.dist-info/METADATA"
        ).decode()
        assert "Requires-Dist:" not in package_metadata
        assert not any(name.startswith("src/") for name in archive.namelist())
    with tarfile.open(
        project / "dist" / f"agent_bridge-{version}.tar.gz"
    ) as archive:
        root = f"agent_bridge-{version}"
        for client in ("codex", "claude"):
            manifest_path = (
                f"{root}/plugins/agent-bridge/.{client}-plugin/plugin.json"
            )
            with archive.extractfile(manifest_path) as stream:
                manifest = json.load(stream)
            assert manifest["name"] == "agent-bridge"
            assert manifest["version"] == version
        assert archive.getmember(
            f"{root}/plugins/agent-bridge/skills/coordinate/SKILL.md"
        ).isfile()
        assert archive.getmember(
            f"{root}/.agents/plugins/marketplace.json"
        ).isfile()
        assert archive.getmember(
            f"{root}/.claude-plugin/marketplace.json"
        ).isfile()
    requirements = tmp_path / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(requirements),
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    environment = {
        **os.environ,
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "AGENT_BRIDGE_HOME": str(tmp_path / "installed-state"),
    }
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        environment["AGENT_BRIDGE_PORT"] = str(sock.getsockname()[1])
    environment.pop("PYTHONPATH", None)
    subprocess.run(
        [
            "uv",
            "tool",
            "install",
            "--python",
            sys.executable,
            str(wheel),
            "--with-requirements",
            str(requirements),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    executable = tmp_path / "bin" / "agent-bridge"
    try:
        subprocess.run(
            [str(executable), "up"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        health = subprocess.run(
            [str(executable), "status"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert "Server: ready" in health.stdout
    finally:
        subprocess.run(
            [str(executable), "down"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    result = subprocess.run(
        [str(executable), "setup", str(repo)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    lanes = json.loads(result.stdout)["lanes"]
    for agent, expected in (("claude", 0), ("codex", 1)):
        result = subprocess.run(
            [str(executable), "issue", "claim", "432"],
            cwd=lanes[agent],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == expected, result.stderr
    installed_python = tmp_path / "tools" / "agent-bridge" / "bin" / "python"
    location = subprocess.run(
        [
            str(installed_python),
            "-I",
            "-c",
            "import agent_bridge; print(agent_bridge.__file__)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert Path(location.stdout.strip()).is_relative_to(tmp_path / "tools")
    subprocess.run(
        [str(installed_python), "-I", "-m", "agent_bridge", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
