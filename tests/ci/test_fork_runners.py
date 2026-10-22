"""Fork CI must schedule its enabled native lanes without upstream runner access.

Replay runner and worker expressions throughout the actual reusable-workflow
graph. A fork PR running in upstream still uses upstream's runner allocation:
the workflow repository, not the PR author's repository, owns the resources.
"""

from itertools import product
from pathlib import Path
import shlex

import pytest
from ruamel.yaml import YAML

from tests.ci import _gha_expr as gha

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
_UPSTREAM = "NousResearch/hermes-agent"
# Standard public-repository runners: (OS, architecture, CPU count).
_HOSTED = {
    "ubuntu-24.04": ("Linux", "X64", 4),
    "macos-latest": ("macOS", "ARM64", 3),
    "macos-15": ("macOS", "ARM64", 3),
    "macos-15-intel": ("macOS", "X64", 4),
    "windows-2025": ("Windows", "X64", 4),
    "windows-11-arm": ("Windows", "ARM64", 4),
}
_MANAGED = {
    "blacksmith-4vcpu-ubuntu-2404": ("Linux", "X64", 4),
    "blacksmith-32vcpu-ubuntu-2404": ("Linux", "X64", 32),
    "blacksmith-12vcpu-macos-latest": ("macOS", "ARM64", 12),
    "blacksmith-2vcpu-windows-2025": ("Windows", "X64", 2),
    "blacksmith-32vcpu-windows-2025": ("Windows", "X64", 32),
    "windows-latest-32-arm-core": ("Windows", "ARM64", 32),
}
_UPSTREAM_RUNNERS = {**_HOSTED, **_MANAGED}


def _native_jobs(repository="contributor/hermes-agent"):
    pending, seen = ["ci.yaml"], set()
    yaml = YAML(typ="safe")
    while pending:
        filename = pending.pop()
        if filename in seen:
            continue
        seen.add(filename)
        workflow = yaml.load((_WORKFLOWS / filename).read_text(encoding="utf-8"))
        for name, job in workflow["jobs"].items():
            called = job.get("uses", "")
            if called:
                assert called.startswith("./.github/workflows/"), called
                pending.append(called.rsplit("/", 1)[-1])
                continue
            condition = job.get("if")
            assert gha.condition(condition, _context(_UPSTREAM, {})), f"{filename}/{name}: unmodelled gate {condition!r}"
            if not gha.condition(condition, _context(repository, {})):
                continue
            # Only the OS include matrix affects runner selection. Shard/slice
            # matrices vary the test selection, not the host allocation.
            rows = job.get("strategy", {}).get("matrix", {}).get("include", [{}])
            for row in rows:
                yield f"{filename}/{name}/{row.get('name', '')}", job, row
    yield from _independent_pr_jobs(yaml, seen)


def _independent_pr_jobs(yaml, seen):
    context = _context("contributor/hermes-agent", {})
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        if path.name in seen:
            continue
        workflow = yaml.load(path.read_text(encoding="utf-8"))
        if "pull_request" not in workflow.get("on", {}):
            continue
        for name, job in workflow["jobs"].items():
            # Model a labelled PR with all applicable checks requested. Keep
            # existing upstream-only builds and release-only jobs excluded.
            if not gha.condition(job.get("if"), context):
                continue
            assert "runs-on" in job, f"{path.name}/{name}: model reusable PR workflow"
            matrix = job.get("strategy", {}).get("matrix", {})
            assert "exclude" not in matrix, f"{path.name}/{name}: model matrix exclusions"
            rows = matrix.get("include")
            if rows is None:
                assert all(isinstance(values, list) for values in matrix.values())
                rows = [dict(zip(matrix, values)) for values in product(*matrix.values())]
            for row in rows:
                yield f"{path.name}/{name}/{row}", job, row


def _render_matrix(value, context):
    if isinstance(value, dict):
        return {key: _render_matrix(item, context) for key, item in value.items()}
    return gha.render(value, context)


def _context(repository, row):
    return {
        "github": {
            "repository": repository,
            "event_name": "pull_request",
            "ref": "refs/pull/2/merge",
            "event": {
                "pull_request": {"head": {"repo": {"full_name": "contributor/hermes-agent"}}},
                "label": {"name": "run-e2e"},
            },
        },
        "inputs": {
            "release": False, "release-phase": "", "event_name": "pull_request",
            "python": True, "e2e": True, "e2e_upgrade": True, "scan": True, "deps": True,
        },
        "needs": {
            "mode": {"result": "success", "outputs": {"phase": "standalone", "release": "false"}},
            "detect": {"result": "success", "outputs": {"nix": "true", "build": "true"}},
        },
        "__status__": {"always": True, "success": True, "failure": False, "cancelled": False},
        "matrix": _render_matrix(row, {"github": {"repository": repository}}),
    }


@pytest.mark.parametrize("repository", ["alexalok/hermes-agent", "contributor/hermes-agent", "NousResearch/other"])
def test_fork_ci_schedules_enabled_platforms_without_managed_runners(repository):
    jobs = list(_native_jobs(repository))
    assert jobs
    for label, job, row in jobs:
        hosted = gha.render(job["runs-on"], _context(repository, row))
        managed = gha.render(job["runs-on"], _context(_UPSTREAM, row))
        assert hosted in _HOSTED, f"{label}: fork requires unavailable runner {hosted!r}"
        assert managed in _UPSTREAM_RUNNERS, f"{label}: unknown upstream runner {managed!r}"
        assert _HOSTED[hosted][:2] == _UPSTREAM_RUNNERS[managed][:2], f"{label}: fork changed OS/architecture"


@pytest.mark.parametrize("repository", [_UPSTREAM, "contributor/hermes-agent"])
def test_macos_tests_only_run_upstream_and_windows_selection_is_preserved(repository):
    def selections(repo, marker):
        return {(row["name"], row.get("slice")) for label, _, row in _native_jobs(repo)
                if label.startswith("tests-os.yml/") and row.get("marker") == marker}

    assert selections(repository, "windows") == selections(_UPSTREAM, "windows")
    assert selections(repository, "windows")
    if repository == _UPSTREAM:
        assert {part for _, part in selections(repository, "macos")} == {"1/2", "2/2"}
        for label, job, row in _native_jobs(repository):
            if label.startswith("tests-os.yml/") and row.get("marker") == "macos":
                runner = gha.render(job["runs-on"], _context(repository, row))
                assert _UPSTREAM_RUNNERS[runner][0] == "macOS"
    else:
        assert not selections(repository, "macos")


def test_fork_workers_do_not_increase_load_per_cpu():
    for label, job, row in _native_jobs():
        ctx = _context("contributor/hermes-agent", row)
        runner = gha.render(job["runs-on"], ctx)
        assert runner in _HOSTED, f"{label}: no standard runner selected"
        os_name, arch, cpus = _HOSTED[runner]
        ctx["runner"] = {"os": os_name, "arch": arch}
        upstream = {**_context(_UPSTREAM, row), "runner": ctx["runner"]}
        managed = gha.render(job["runs-on"], upstream)
        managed_cpus = _UPSTREAM_RUNNERS[managed][2]
        for step in job.get("steps", []):
            env = {**job.get("env", {}), **step.get("env", {})}
            setting = env.get("HERMES_TEST_WORKERS", "")
            # Empty uses the canonical runner's CPU-count default. A process
            # tree that needs several upstream CPUs must keep that headroom
            # on a fork, down to one worker when the VM is smaller than it.
            workers = int(gha.render(setting, ctx) or cpus)
            managed_workers = int(gha.render(setting, upstream) or managed_cpus)
            capacity = max(1, managed_workers * cpus // managed_cpus)
            assert 0 < workers <= min(cpus, capacity), f"{label}: {workers} workers exceed capacity {capacity}"


@pytest.mark.parametrize("repository, concurrency", [(_UPSTREAM, None), ("contributor/hermes-agent", 1)])
def test_workspace_worker_pools_do_not_compete_on_fork_runners(repository, concurrency):
    workflow = YAML(typ="safe").load((_WORKFLOWS / "js-tests.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["check"]["steps"]
                if step.get("name") == "Run all workspace checks")
    command = shlex.split(gha.render(step["run"], _context(repository, {})))
    selected = int(command[command.index("--concurrency") + 1]) if "--concurrency" in command else None
    assert selected == concurrency
