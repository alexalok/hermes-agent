"""Fork CI must schedule every native lane without upstream runner access.

Replay runner and worker expressions throughout the actual reusable-workflow
graph. A fork PR running in upstream still uses upstream's runner allocation:
the workflow repository, not the PR author's repository, owns the resources.
"""

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from tests.ci import _gha_expr as gha

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
_UPSTREAM = "NousResearch/hermes-agent"
# Standard public-repository runners: (OS, architecture, CPU count).
_HOSTED = {
    "ubuntu-24.04": ("Linux", "X64", 4),
    "macos-latest": ("macOS", "ARM64", 3),
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


def _native_jobs():
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
            # Only the OS include matrix affects runner selection. Shard/slice
            # matrices vary the test selection, not the host allocation.
            rows = job.get("strategy", {}).get("matrix", {}).get("include", [{}])
            for row in rows:
                yield f"{filename}/{name}/{row.get('name', '')}", job, row


def _context(repository, row):
    return {
        "github": {
            "repository": repository,
            "event": {"pull_request": {"head": {"repo": {"full_name": "contributor/hermes-agent"}}}},
        },
        "matrix": {key: gha.render(value, {"github": {"repository": repository}})
                   for key, value in row.items()},
    }


@pytest.mark.parametrize("repository", ["alexalok/hermes-agent", "contributor/hermes-agent", "NousResearch/other"])
def test_fork_ci_schedules_every_platform_without_managed_runners(repository):
    jobs = list(_native_jobs())
    assert jobs
    for label, job, row in jobs:
        hosted = gha.render(job["runs-on"], _context(repository, row))
        managed = gha.render(job["runs-on"], _context(_UPSTREAM, row))
        assert hosted in _HOSTED, f"{label}: fork requires unavailable runner {hosted!r}"
        assert managed in _MANAGED, f"{label}: upstream lost its runner allocation {managed!r}"
        assert _HOSTED[hosted][:2] == _MANAGED[managed][:2], f"{label}: fork changed OS/architecture"


def test_fork_workers_do_not_increase_load_per_cpu():
    for label, job, row in _native_jobs():
        ctx = _context("contributor/hermes-agent", row)
        runner = gha.render(job["runs-on"], ctx)
        assert runner in _HOSTED, f"{label}: no standard runner selected"
        os_name, arch, cpus = _HOSTED[runner]
        ctx["runner"] = {"os": os_name, "arch": arch}
        upstream = {**_context(_UPSTREAM, row), "runner": ctx["runner"]}
        managed = gha.render(job["runs-on"], upstream)
        managed_cpus = _MANAGED[managed][2]
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
