"""Checks how the Check workflow chooses legs from a pull request's paths."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_scope

ROOT = Path(__file__).resolve().parents[1]
EVERY_LEG = {"pythons": ["3.12", "3.13", "3.14"], "macos": True, "wsl": True}


def legs(paths):
    return {
        name: json.loads(value) for name, value in ci_scope.plan(paths).items()
    }


def test_every_tracked_path_falls_into_a_category():
    try:
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a Git checkout")
    unknown = [path for path in tracked if ci_scope.category(path) is None]
    assert unknown == []


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md"],
        ["CHANGELOG.md", "docs/operations.md", "docs/assets/demo.svg"],
        [".github/ISSUE_TEMPLATE/bug.yml", ".github/PULL_REQUEST_TEMPLATE.md"],
        [".github/workflows/pages.yml", ".github/workflows/release.yml"],
    ],
)
def test_docs_and_other_workflows_run_one_ubuntu_leg(paths):
    assert legs(paths) == {"pythons": ["3.12"], "macos": False, "wsl": False}


@pytest.mark.parametrize(
    "path", ["scripts/check_policy.py", "tests/test_bridge.py", "Makefile"]
)
def test_tooling_runs_every_python_leg_without_wsl(path):
    assert legs(["README.md", path]) == {**EVERY_LEG, "wsl": False}


@pytest.mark.parametrize(
    "path",
    [
        "agent_parley/launch.py",
        "tests/test_wsl.py",
        "uv.lock",
        ".github/workflows/check.yml",
    ],
)
def test_package_and_process_code_runs_every_leg(path):
    assert legs(["docs/operations.md", path]) == EVERY_LEG


@pytest.mark.parametrize(
    "paths", [None, [], ["docs/x.md", "somewhere/new.txt"], ["notes.txt"]]
)
def test_an_unread_empty_or_unknown_change_runs_every_leg(paths):
    assert legs(paths) == EVERY_LEG


def test_a_push_or_manual_run_selects_every_leg(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("BASE_SHA", "")
    ci_scope.main()
    printed = dict(
        line.split("=", 1) for line in capsys.readouterr().out.splitlines()
    )
    assert {k: json.loads(v) for k, v in printed.items()} == EVERY_LEG


def test_an_unreadable_base_falls_back_to_every_leg(monkeypatch, tmp_path):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("BASE_SHA", "0" * 40)
    ci_scope.main()
    written = dict(
        line.split("=", 1) for line in output.read_text().splitlines()
    )
    assert {k: json.loads(v) for k, v in written.items()} == EVERY_LEG


def test_required_checks_keep_their_names_and_gate_the_aggregator():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/check.yml").read_text()
    )
    jobs = workflow["jobs"]
    assert jobs["macos"]["name"] == "python (3.12, macos-latest)"
    assert {"check", "wsl", "secrets"} <= set(jobs)
    assert "if" not in jobs["secrets"]
    assert set(jobs["check"]["needs"]) == {
        "changes",
        "python",
        "macos",
        "install",
    }
    assert jobs["check"]["if"] == "always()"
    assert jobs["install"]["if"] == jobs["macos"]["if"]
    assert jobs["install"]["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "macos-latest",
    ]
    script = jobs["check"]["steps"][0]["run"]
    assert 'test "$CHANGES" = success' in script
    assert 'test "$MACOS" = skipped' in script
    assert 'test "$INSTALL" = skipped' in script
    assert 'test "$INSTALL" = success' in script
    assert workflow["concurrency"]["cancel-in-progress"] == (
        "${{ github.ref != 'refs/heads/main' }}"
    )


def test_the_required_wsl_job_gates_and_bounds_each_step():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/check.yml").read_text()
    )
    job = workflow["jobs"]["wsl"]
    steps = job["steps"]
    assert "continue-on-error" not in job
    assert all("timeout-minutes" in step for step in steps)
    bounded = sum(step["timeout-minutes"] for step in steps)
    assert bounded <= job["timeout-minutes"]
    setup, retry = (
        step for step in steps if "setup-wsl" in step.get("uses", "")
    )
    assert setup["continue-on-error"] is True
    assert retry["if"] == f"steps.{setup['id']}.outcome != 'success'"
    assert "continue-on-error" not in retry
    assert retry["with"] == setup["with"]
