"""Nested cron work cannot rebind its parent's supervised channel."""

from types import SimpleNamespace

import pytest

from cron.approval_relay_scope import _worker, bind_worker_agent
from cron.execution_identity import enter_cron_execution, exit_cron_execution


@pytest.mark.parametrize("mismatch", [None, "job", "execution", "profile"])
def test_binding_requires_exact_current_execution(tmp_path, monkeypatch, mismatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []
    descriptor = dict(job_id="outer", execution_id="first", profile_home=str(tmp_path))
    job = dict(id="nested" if mismatch == "job" else "outer")
    if mismatch == "profile":
        descriptor["profile_home"] = str(tmp_path / "other")
    execution = enter_cron_execution(job, "second" if mismatch == "execution" else "first", {})
    worker = _worker.set(SimpleNamespace(descriptor=descriptor, bind_agent=calls.append))
    agent = SimpleNamespace(session_id="cron-session")
    try:
        bind_worker_agent(agent)
        assert calls == ([agent] if mismatch is None else [])
    finally:
        _worker.reset(worker)
        exit_cron_execution(execution)


@pytest.mark.platforms("linux")
def test_shutdown_fences_inflight_publication_even_across_new_start(tmp_path, monkeypatch):
    import contextlib
    import threading

    from agent.memory_provider import spawn_context_thread
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_scope import close_gateway_relays, dispatch_descriptor, dispatch_scope, start_gateway_relays
    from cron import approval_relay_server

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    original = approval_relay_server.WorkerApprovalRelay

    def delayed(*args):
        relay = original(*args)
        entered.set()
        assert release.wait(10)
        return relay

    @contextlib.contextmanager
    def present(*_args):
        yield

    seen = []

    def dispatch():
        with dispatch_scope(dict(id="daily", execution_id="first")):
            seen.append(dispatch_descriptor())

    registration = PresenterRegistration(str(tmp_path), "guard", present)
    start_gateway_relays()
    monkeypatch.setattr(approval_relay_server, "WorkerApprovalRelay", delayed)
    thread = spawn_context_thread(dispatch, name="test-cron-dispatch")
    try:
        thread.start()
        assert entered.wait(10)
        close_gateway_relays()
        start_gateway_relays()
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert seen == [None]
        close_gateway_relays()
        with dispatch_scope(dict(id="daily", execution_id="second")):
            assert dispatch_descriptor() is None
    finally:
        release.set()
        thread.join(10)
        close_gateway_relays()
        registration.dispose()


@pytest.mark.platforms("linux")
def test_undiscovered_profile_never_borrows_another_presenter(tmp_path, monkeypatch):
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_scope import close_gateway_relays, dispatch_descriptor, dispatch_scope, start_gateway_relays
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registration = PresenterRegistration(str(tmp_path), "guard", lambda *_: None)
    token = set_hermes_home_override(tmp_path / "undiscovered")
    start_gateway_relays()
    try:
        with dispatch_scope(dict(id="daily", execution_id="first")):
            assert dispatch_descriptor() is None
    finally:
        close_gateway_relays()
        reset_hermes_home_override(token)
        registration.dispose()
