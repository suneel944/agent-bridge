"""Decides which legs of the Check workflow a pull request needs.

Every change runs the full test suite on one Ubuntu leg. The remaining Python
versions and macOS run only when a change touches what can behave differently
across platforms or interpreters, and the WSL leg only when package, launch or
process code changes. A path this module does not recognise, a push to
``main``, a manual run and any failure to read the diff all select every leg,
so uncertainty never skips a check.

The ruleset on ``main`` requires the macOS and WSL check names, so the
workflow skips those jobs with a job-level condition rather than a trigger
filter: a skipped job still reports its check, while a workflow that never
starts leaves the check pending.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PYTHON_VERSIONS = ("3.12", "3.13", "3.14")
WSL = "wsl"
FULL = "full"
LIGHT = "light"
WSL_PATHS = (
    "agent_parley/",
    "tests/test_wsl.py",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/check.yml",
)
FULL_PATHS = (
    "plugins/",
    "scripts/",
    "tests/",
    "Makefile",
    ".github/actions/",
    ".claude-plugin/",
    ".agents/",
    ".release-manifest.json",
    "server.json",
    "glama.json",
    ".gitignore",
    "LICENSE",
)
LIGHT_PATHS = (
    "docs/",
    ".github/ISSUE_TEMPLATE/",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    ".github/release-history.json",
    ".github/workflows/pages.yml",
    ".github/workflows/pr-hygiene.yml",
    ".github/workflows/release.yml",
    ".github/workflows/release-version.yml",
    "_config.yml",
    ".pre-commit-config.yaml",
)


def matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """Tells whether a path is one of the entries or inside one of them.

    Args:
        path: Repository-relative path with forward slashes.
        prefixes: Exact file paths, or directory paths ending in a slash.

    Returns:
        True when the path equals a file entry or lies under a directory one.
    """
    return any(
        path.startswith(prefix) if prefix.endswith("/") else path == prefix
        for prefix in prefixes
    )


def category(path: str) -> str | None:
    """Names the widest set of legs a changed path needs.

    Args:
        path: Repository-relative path with forward slashes.

    Returns:
        ``wsl`` for package, launch or process code, ``full`` for other code
        and tooling, ``light`` for documentation and workflows other than
        Check, or None for a path no list recognises.
    """
    if matches(path, WSL_PATHS):
        return WSL
    if matches(path, FULL_PATHS):
        return FULL
    if matches(path, LIGHT_PATHS) or ("/" not in path and path.endswith(".md")):
        return LIGHT
    return None


def plan(paths: list[str] | None) -> dict[str, str]:
    """Chooses the Check legs for a set of changed paths.

    Args:
        paths: Changed paths, or None when the change could not be read.

    Returns:
        Workflow outputs: ``pythons``, a JSON list of Ubuntu Python versions,
        and ``macos`` and ``wsl``, each ``true`` or ``false``.
    """
    found = {category(path) for path in paths or ()}
    wide = not paths or None in found
    full = wide or bool(found & {WSL, FULL})
    wsl = wide or WSL in found
    versions = PYTHON_VERSIONS if full else PYTHON_VERSIONS[:1]
    return {
        "pythons": json.dumps(list(versions)),
        "macos": json.dumps(full),
        "wsl": json.dumps(wsl),
    }


def changed_paths(base: str) -> list[str] | None:
    """Lists the paths a pull request changes against its base.

    Args:
        base: Commit the pull request targets.

    Returns:
        The changed paths, or None when Git cannot compare the commits.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.splitlines()


def main() -> None:
    """Writes the chosen legs to the step outputs, or prints them."""
    base = os.environ.get("BASE_SHA", "")
    pull = os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
    outputs = plan(changed_paths(base) if pull and base else None)
    lines = "".join(f"{name}={value}\n" for name, value in outputs.items())
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with Path(target).open("a") as output:
            output.write(lines)
    sys.stdout.write(lines)


if __name__ == "__main__":
    main()
