"""Cron presentation facades must report and enforce their in-process boundary."""

from pathlib import Path

import pytest

from tests.hermes_cli.test_plugin_host import _home_with_plugins


@pytest.fixture(params=[
    "ctx.cron_approvals.register_presenter(present)",
    "approvals = ctx.cron_approvals\n    approvals.register_presenter(present)",
    "getattr(ctx, 'cron_approvals').register_presenter(present)",
])
def cron_plugin(request):
    return f'''
from contextlib import contextmanager

def register(ctx):
    @contextmanager
    def present(request, lifetime):
        yield
    {request.param}
'''


def test_cron_facade_audit_reports_in_process_only(tmp_path, cron_plugin):
    from hermes_cli.plugin_isolation_audit import audit_plugin_dir

    plugin = tmp_path / "guard"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(cron_plugin)
    report = audit_plugin_dir(plugin)
    assert report.verdict == "in_process"
    assert any("cron_approvals" in reason for reason in report.reasons)


@pytest.mark.platforms("any")  # Exercise the real per-profile child host.
def test_isolated_cron_plugin_load_reports_boundary_reason(tmp_path, monkeypatch, cron_plugin):
    from cron.approval_relay import presenters_for
    from hermes_cli.plugins import PluginManager

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "os-home")
    home = _home_with_plugins(tmp_path, monkeypatch, {"cronprobe": cron_plugin})
    manager = PluginManager()
    try:
        manager.discover_and_load()
        error = str(manager._plugins["cronprobe"].error)
        assert "ctx.cron_approvals" in error
        assert "cannot run in the plugin host" in error
        assert not presenters_for(str(home))
    finally:
        manager.unload()
        manager._plugin_host().shutdown()
