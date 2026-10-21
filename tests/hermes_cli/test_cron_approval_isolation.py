"""Cron presentation facades must report and enforce their in-process boundary."""

from pathlib import Path

import pytest

from tests.hermes_cli.test_plugin_host import _home_with_plugins


@pytest.fixture(params=[
    "ctx.cron_approvals.register_presenter(present)",
    "approvals = ctx.cron_approvals\n    approvals.register_presenter(present)",
    "getattr(ctx, 'cron_approvals').register_presenter(present)",
    "context = ctx\n    context.cron_approvals.register_presenter(present)",
    "context = ctx\n    getattr(context, 'cron_approvals').register_presenter(present)",
    "context: object = ctx\n    context.cron_approvals.register_presenter(present)",
    "ctx: object\n    ctx.cron_approvals.register_presenter(present)",
    "ctx = ctx.cron_approvals\n    ctx.register_presenter(present)",
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


@pytest.mark.parametrize("access", [
    "settings.cron_approvals",
    "getattr(settings, 'cron_approvals')",
])
def test_unrelated_approval_settings_remain_host_ready(tmp_path, access):
    from hermes_cli.plugin_isolation_audit import audit_plugin_dir

    plugin = tmp_path / "guard"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(f'''
from types import SimpleNamespace

def register(ctx):
    settings = SimpleNamespace(cron_approvals=True)
    enabled = {access}
''')
    assert audit_plugin_dir(plugin).host_ready


@pytest.mark.parametrize(("source", "host_ready"), [
    ("def register(context):\n    context.cron_approvals", False),
    ("def install(ctx):\n    ctx.cron_approvals", False),
    ("def register(ctx):\n    def callback():\n        ctx.cron_approvals", False),
    ("class Settings:\n    def register(self, item):\n        return self.cron_approvals", True),
    ("def register(ctx):\n    context = ctx\n    context = object()\n    context.cron_approvals", True),
    ("def register(ctx):\n    context = ctx\n    def callback(context):\n        context.cron_approvals", True),
    ("def register(ctx):\n    def register(settings):\n        settings.cron_approvals", True),
])
def test_context_receiver_scope(tmp_path, source, host_ready):
    from hermes_cli.plugin_isolation_audit import audit_plugin_dir

    plugin = tmp_path / "guard"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(source)
    assert audit_plugin_dir(plugin).host_ready is host_ready


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
