"""Start-here screen a bare `agent-parley` prints.

The full reference lists every command, which answers what exists but not
what to type next. This screen reads the working directory and the private
state directory, says what it found, and names the next one to three
commands for exactly that state, with a pointer to `--help` for the rest.

Every reading is read-only and local. Nothing starts the service, registers
a repository, creates the state directory or runs a native CLI: the service
is judged from its published record and the process that record names, the
registration from the recorded manifests, and each native CLI's plugin from
the record that CLI keeps (`plugins.recorded`). The only process started is
one bounded `git status` in the working directory. The text carries no
color or cursor control, so it reads the same with ``NO_COLOR`` set and
without a terminal.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import NamedTuple

from agent_parley import plugins, process
from agent_parley.status import registered_root

GIT_SECONDS = 2.0
DEFAULT_HOME = "~/.local/state/agent-parley"


class Found(NamedTuple):
    """What the screen found about the working directory and the machine.

    Attributes:
        repository: Whether the working directory is inside a Git checkout.
        dirty: Whether that checkout has uncommitted changes, or None when
            Git did not answer in time.
        registered: Whether the checkout belongs to a registered project.
        clis: Each supported native CLI on PATH, mapped to whether its
            plugin record lists the Agent Parley plugin.
        service: Whether the recorded coordination service is running.
    """

    repository: bool
    dirty: bool | None
    registered: bool
    clis: dict[str, bool]
    service: bool


def checkout(directory: Path) -> tuple[bool, bool | None]:
    """Says whether a directory is in a Git checkout and whether it is dirty.

    Args:
        directory: Directory to inspect.

    Returns:
        Whether it is a checkout, and whether it has uncommitted changes,
        None when Git did not answer within `GIT_SECONDS`.
    """
    try:
        finished = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=directory,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return True, None
    except OSError:
        return False, False
    if finished.returncode:
        return False, False
    return True, bool(finished.stdout.strip())


def serving(home: Path) -> bool:
    """Says whether the service the state directory records is running.

    Args:
        home: Private state directory.

    Returns:
        True when the published record names a live process that is still
        this home's service; no request is sent to it.
    """
    try:
        record = json.loads((home / "server.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and bool(process.identify(record, home))


def look(home: Path, directory: Path) -> Found:
    """Reads everything the screen reports, writing nothing.

    Args:
        home: Private state directory, which need not exist.
        directory: Working directory to describe.

    Returns:
        What was found.
    """
    repository, dirty = checkout(directory)
    try:
        registered = repository and bool(registered_root(home, directory))
    except (OSError, ValueError, KeyError, TypeError):
        registered = False
    clis = {
        client.name: plugins.recorded(client.name)
        for client, _ in plugins.detected()
    }
    return Found(repository, dirty, registered, clis, serving(home))


def advice(found: Found) -> list[tuple[str, str]]:
    """Chooses the next commands for one state, most urgent first.

    Args:
        found: What the screen found.

    Returns:
        One to three pairs of a command and what it does.
    """
    if not found.clis:
        supported = " or ".join(client.name for client in plugins.CLIENTS)
        return [
            (
                "agent-parley demo",
                "See lanes coordinate; no native CLI needed.",
            ),
            (
                "agent-parley plugins install",
                f"Run after installing {supported}.",
            ),
        ]
    steps = []
    missing = [name for name, added in found.clis.items() if not added]
    if missing:
        steps.append(
            (
                "agent-parley plugins install",
                f"Add the plugin to {' and '.join(missing)}.",
            )
        )
    chosen = next(
        (name for name, added in found.clis.items() if added),
        next(iter(found.clis)),
    )
    if not found.repository:
        steps.append(("cd REPOSITORY", "Move into the Git checkout to share."))
    elif found.dirty:
        steps.append(
            ("git status", "Lanes start from the last commit; commit first.")
        )
    doing = (
        "Start a lane"
        if found.registered
        else "Register the repository and start a lane"
    )
    steps.append((f"agent-parley run {chosen}", f"{doing}."))
    if found.registered and found.service:
        steps.append(("agent-parley top", "Watch every lane live."))
    return steps[:3]


def render(found: Found) -> str:
    """Renders the screen as plain text within 80 columns and 20 lines.

    Args:
        found: What the screen found.

    Returns:
        The screen, without a trailing newline.
    """
    if not found.repository:
        repository = "not a Git repository"
    elif found.dirty is None:
        repository = "Git repository"
    elif found.dirty:
        repository = "Git repository, uncommitted changes"
    else:
        repository = "Git repository, clean"
    if found.clis:
        clis = ", ".join(
            f"{name} (plugin {'added' if added else 'missing'})"
            for name, added in found.clis.items()
        )
    else:
        names = ", ".join(client.name for client in plugins.CLIENTS)
        clis = f"none on PATH ({names})"
    steps = advice(found)
    width = max(len(command) for command, _ in steps) + 2
    return "\n".join(
        [
            "Agent Parley coordinates native coding CLIs in one repository.",
            "",
            "Found:",
            f"  Here      {repository}",
            "  Project   "
            + ("registered" if found.registered else "not registered"),
            f"  CLIs      {clis}",
            "  Service   " + ("running" if found.service else "not running"),
            "",
            "Next:",
            *(f"  {command.ljust(width)}{what}" for command, what in steps),
            "",
            "Full reference: agent-parley --help",
        ]
    )


def show(home: Path | None = None, directory: Path | None = None) -> int:
    """Prints the start-here screen.

    Args:
        home: Private state directory, which need not exist; when None,
            ``AGENT_PARLEY_HOME`` or the default the parser uses.
        directory: Working directory to describe; the current one if None.

    Returns:
        Exit status zero.
    """
    if home is None:
        home = Path(os.environ.get("AGENT_PARLEY_HOME", DEFAULT_HOME))
    print(render(look(home.expanduser(), directory or Path.cwd())))
    return 0
