"""Real Linux peer credentials, including descendants inheriting a socket."""

import contextlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("linux")


WORKER = """
import json, sys, time
from cron.approval_relay_client import WorkerClient
descriptor = json.loads(sys.stdin.readline())
client = WorkerClient(descriptor)
client.bind_session('cron-1')
with client.pending('guard', session_id='cron-1', external_session_id='hm-1',
                    call_id='call-1', deadline=time.monotonic()+30, check=lambda: None):
    print('waiting', flush=True)
    sys.stdin.readline()
print('finished', flush=True)
"""


@contextlib.contextmanager
def launched(relay, script=WORKER):
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    try:
        relay.attach(child.pid)
        child.stdin.write(json.dumps(relay.descriptor) + "\n")
        child.stdin.flush()
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
        relay.close()


def test_exact_worker_presents_once_and_close_retires(tmp_path):
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_server import WorkerApprovalRelay

    seen, opened, retired = [], threading.Event(), threading.Event()

    @contextlib.contextmanager
    def present(request, lifetime):
        seen.append(request)
        opened.set()
        try:
            yield
        finally:
            retired.set()

    registration = PresenterRegistration(str(tmp_path), "guard", present)
    try:
        relay = WorkerApprovalRelay(str(tmp_path), "daily", "exec-1")
        with launched(relay) as child:
            assert opened.wait(10), child.poll()
            assert child.stdout.readline().strip() == "waiting"
            assert seen[0].profile_home == str(tmp_path)
            assert (seen[0].job_id, seen[0].execution_id, seen[0].session_id) == ("daily", "exec-1", "cron-1")
            child.stdin.write("close\n")
            child.stdin.flush()
            assert retired.wait(10)
            assert child.wait(timeout=10) == 0
            assert len(seen) == 1
    finally:
        registration.dispose()


def test_unrelated_same_uid_cannot_connect(tmp_path):
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_client import WorkerClient
    from cron.approval_relay_server import WorkerApprovalRelay

    registration = PresenterRegistration(str(tmp_path), "guard", lambda *_: pytest.fail("Untrusted caller"))
    try:
        relay = WorkerApprovalRelay(str(tmp_path), "daily", "exec-1")
        with launched(relay, "import sys; sys.stdin.readline(); sys.stdin.readline()"):
            with pytest.raises((OSError, ValueError, RuntimeError)):
                WorkerClient(relay.descriptor).bind_session("forged")
    finally:
        registration.dispose()


def test_process_label_change_does_not_change_pinned_identity(tmp_path):
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_server import WorkerApprovalRelay

    opened = threading.Event()

    @contextlib.contextmanager
    def present(*_args):
        opened.set()
        yield

    script = WORKER.replace("client.bind_session('cron-1')", """import ctypes
assert ctypes.CDLL(None).prctl(15, b'cron ) worker', 0, 0, 0) == 0
client.bind_session('cron-1')""")
    registration = PresenterRegistration(str(tmp_path), 'guard', present)
    try:
        relay = WorkerApprovalRelay(str(tmp_path), 'daily', 'exec-1')
        with launched(relay, script):
            assert opened.wait(10)
    finally:
        registration.dispose()


@pytest.mark.parametrize("stop", ["worker_exit", "gateway_disconnect", "plugin_unload"])
def test_pending_prompt_retires_on_owner_loss(tmp_path, stop):
    from cron.approval_relay import ApprovalUnavailable, PresenterRegistration
    from cron.approval_relay_server import WorkerApprovalRelay

    opened, retired, waits = threading.Event(), threading.Event(), []

    @contextlib.contextmanager
    def present(request, lifetime):
        waits.append(lifetime)
        opened.set()
        try:
            yield
        finally:
            retired.set()

    registration = PresenterRegistration(str(tmp_path), "guard", present)
    try:
        relay = WorkerApprovalRelay(str(tmp_path), "daily", "exec-1")
        with launched(relay) as child:
            assert opened.wait(10)
            if stop == "worker_exit":
                child.kill()
                child.wait(timeout=10)
            elif stop == "gateway_disconnect":
                relay.close()
            else:
                registration.dispose()
            assert retired.wait(10)
            with pytest.raises(ApprovalUnavailable):
                waits[0].check()
    finally:
        registration.dispose()


def test_inherited_connection_cannot_submit_as_supervised_worker(tmp_path):
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_server import WorkerApprovalRelay

    seen = []
    registration = PresenterRegistration(str(tmp_path), "guard", lambda *args: seen.append(args))
    script = '''
import json, os, sys, time
from cron.approval_relay_client import WorkerClient
from cron.approval_relay_wire import send_frame, receive_frame
client = WorkerClient(json.loads(sys.stdin.readline()))
client.bind_session('cron-1')
with client._connect() as connection:
    pid = os.fork()
    if pid == 0:
        send_frame(connection, {'op':'open', 'provider':'guard', 'session_id':'cron-1',
            'external_session_id':'hm-1', 'call_id':'forged', 'invocation_id':'forked',
            'deadline':time.monotonic()+30})
        os._exit(0)
    os.waitpid(pid, 0)
    try:
        receive_frame(connection, client.peer)
    except (OSError, ValueError):
        print('rejected', flush=True)
    else:
        raise AssertionError('Inherited descriptor admitted a child')
'''
    try:
        relay = WorkerApprovalRelay(str(tmp_path), "daily", "exec-1")
        with launched(relay, script) as child:
            assert child.wait(timeout=10) == 0, child.stderr.read()
            assert child.stdout.read().strip() == "rejected"
            assert seen == []
    finally:
        registration.dispose()


@pytest.mark.parametrize("fault", ["session", "provider", "origin", "expired", "deadline_extension"])
def test_unbound_or_extended_request_never_reaches_presenter(tmp_path, fault):
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_server import WorkerApprovalRelay

    seen = []
    registration = PresenterRegistration(str(tmp_path), "guard", lambda *args: seen.append(args))
    script = '''
import json, sys, time
from cron.approval_relay_client import WorkerClient
from cron.approval_relay_wire import send_frame, receive_frame
client = WorkerClient(json.loads(sys.stdin.readline()))
client.bind_session('cron-1')
frame = dict(op='open', provider='guard', session_id='cron-1', external_session_id='hm-1',
             call_id='call-1', invocation_id='invocation-1', deadline=time.monotonic()+30)
FAULT
with client._connect() as connection:
    send_frame(connection, frame)
    try:
        receive_frame(connection, client.peer)
    except (OSError, ValueError):
        pass
    else:
        raise AssertionError('Invalid request admitted')
'''.replace("FAULT", {
        "session": "frame['session_id']='other-session'",
        "provider": "frame['provider']='other-profile-plugin'",
        "origin": "frame['origin']={'chat_id':'-999'}",
        "expired": "frame['deadline']=time.monotonic()-1",
        "deadline_extension": "frame['deadline']=time.monotonic()+601",
    }[fault])
    try:
        relay = WorkerApprovalRelay(str(tmp_path), "daily", "exec-1")
        with launched(relay, script) as child:
            assert child.wait(timeout=10) == 0, child.stderr.read()
            assert seen == []
    finally:
        registration.dispose()


def test_duplicate_call_and_ambiguous_presenter_failure_never_replay(tmp_path):
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_server import WorkerApprovalRelay

    seen, attempted = [], threading.Event()

    @contextlib.contextmanager
    def ambiguous_send(request, lifetime):
        seen.append(request)
        attempted.set()
        raise TimeoutError("Send outcome unknown")
        yield  # pragma: no cover -- contextmanager that fails on entry

    script = '''
import json, sys, time
from cron.approval_relay_client import WorkerClient
client = WorkerClient(json.loads(sys.stdin.readline()))
client.bind_session('cron-1')
for i in range(2):
    try:
        with client.pending('guard', session_id='cron-1', external_session_id='hm-1',
                            call_id='call-1', deadline=time.monotonic()+30, check=lambda: None):
            print('opened', flush=True)
            sys.stdin.readline()
    except (OSError, ValueError, RuntimeError):
        pass
'''
    registration = PresenterRegistration(str(tmp_path), "guard", ambiguous_send)
    try:
        relay = WorkerApprovalRelay(str(tmp_path), "daily", "exec-1")
        with launched(relay, script) as child:
            assert attempted.wait(10)
            child.stdin.write('continue\n')
            child.stdin.flush()
            assert child.wait(timeout=10) == 0, child.stderr.read()
            assert len(seen) == 1
            assert len(relay._seen) == 2  # Retains call + invocation fences even after failure.
    finally:
        registration.dispose()


def test_worker_agent_rotation_keeps_exact_session_binding(tmp_path):
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_server import WorkerApprovalRelay

    seen, opened = [], threading.Event()

    @contextlib.contextmanager
    def present(request, lifetime):
        seen.append(request.session_id)
        opened.set()
        yield

    script = '''
import json, sys, time
from types import SimpleNamespace
from cron.approval_relay_client import WorkerClient
client = WorkerClient(json.loads(sys.stdin.readline()))
agent = SimpleNamespace(session_id='cron-root')
client.bind_agent(agent)
agent.session_id = 'cron-compressed'
try:
    with client.pending('guard', session_id='forged', external_session_id='hm-1',
                        call_id='bad', deadline=time.monotonic()+30, check=lambda:None):
        raise AssertionError('Forged session admitted')
except RuntimeError:
    pass
with client.pending('guard', session_id=agent.session_id, external_session_id='hm-2',
                    call_id='good', deadline=time.monotonic()+30, check=lambda:None):
    sys.stdin.readline()
'''
    registration = PresenterRegistration(str(tmp_path), 'guard', present)
    try:
        relay = WorkerApprovalRelay(str(tmp_path), 'daily', 'exec-1')
        with launched(relay, script) as child:
            assert opened.wait(10), child.poll()
            assert seen == ['cron-compressed']
    finally:
        registration.dispose()


@pytest.mark.parametrize('end', ['cancel', 'expire'])
def test_one_invocation_ending_keeps_other_wait_and_later_calls_live(tmp_path, end):
    from cron.approval_relay import PresenterRegistration
    from cron.approval_relay_server import WorkerApprovalRelay

    opened = {key: threading.Event() for key in ('short', 'long', 'later')}
    retired = {key: threading.Event() for key in opened}
    lifetimes = {}

    @contextlib.contextmanager
    def present(request, lifetime):
        lifetimes[request.call_id] = lifetime
        opened[request.call_id].set()
        try:
            yield
        finally:
            retired[request.call_id].set()

    script = '''
import contextlib, json, sys, threading, time
from cron.approval_relay_client import WorkerClient
client = WorkerClient(json.loads(sys.stdin.readline()))
client.bind_session('cron-1')
cancelled = threading.Event()
def check():
    if cancelled.is_set():
        raise RuntimeError('cancelled')
with contextlib.ExitStack() as stack:
    for key in ('short', 'long'):
        stack.enter_context(client.pending('guard', session_id='cron-1', external_session_id='hm',
            call_id=key, deadline=time.monotonic()+(3 if key=='short' and END=='expire' else 30),
            check=check if key=='short' else lambda:None))
    sys.stdin.readline()
    if END == 'cancel':
        cancelled.set()
    sys.stdin.readline()
    with client.pending('guard', session_id='cron-1', external_session_id='hm',
                        call_id='later', deadline=time.monotonic()+30, check=lambda:None):
        sys.stdin.readline()
'''.replace('END', repr(end))
    registration = PresenterRegistration(str(tmp_path), 'guard', present)
    try:
        relay = WorkerApprovalRelay(str(tmp_path), 'daily', 'exec-1')
        with launched(relay, script) as child:
            assert opened['short'].wait(10) and opened['long'].wait(10)
            child.stdin.write('end short\n')
            child.stdin.flush()
            assert retired['short'].wait(10)
            lifetimes['long'].check()
            child.stdin.write('open later\n')
            child.stdin.flush()
            assert opened['later'].wait(10), child.poll()
            lifetimes['long'].check()
    finally:
        registration.dispose()


def test_connection_limit_preserves_pending_calls_and_releases_capacity(tmp_path, monkeypatch):
    from cron.approval_relay import PresenterRegistration
    from cron import approval_relay_server
    from tests.waiting import wait_until

    monkeypatch.setattr(approval_relay_server, "MAX_ACTIVE", 2)
    opened = {key: threading.Event() for key in ("first", "second", "later")}
    retired = {key: threading.Event() for key in opened}
    lifetimes = {}

    @contextlib.contextmanager
    def present(request, lifetime):
        lifetimes[request.call_id] = lifetime
        opened[request.call_id].set()
        try:
            yield
        finally:
            retired[request.call_id].set()

    script = '''
import json, sys, time
from cron.approval_relay_client import WorkerClient
client = WorkerClient(json.loads(sys.stdin.readline()))
client.bind_session('cron-1')
print('bound', flush=True)
sys.stdin.readline()
def pending(key):
    return client.pending('guard', session_id='cron-1', external_session_id='hm',
                          call_id=key, deadline=time.monotonic()+30, check=lambda:None)
first = pending('first')
with pending('second'):
    with first:
        try:
            with pending('excess'):
                raise AssertionError('Connection cap admitted excess wait')
        except (OSError, ValueError):
            print('refused', flush=True)
        sys.stdin.readline()
    print('released', flush=True)
    sys.stdin.readline()
    with pending('later'):
        sys.stdin.readline()
'''
    registration = PresenterRegistration(str(tmp_path), "guard", present)
    try:
        relay = approval_relay_server.WorkerApprovalRelay(str(tmp_path), "daily", "exec-1")
        with launched(relay, script) as child:
            assert child.stdout.readline().strip() == "bound"
            wait_until(lambda: not relay._connections, what="session binding closed")
            child.stdin.write("open\n")
            child.stdin.flush()
            assert opened["first"].wait(10) and opened["second"].wait(10)
            assert child.stdout.readline().strip() == "refused"
            lifetimes["first"].check()
            lifetimes["second"].check()
            child.stdin.write("release\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "released"
            assert retired["first"].wait(10)
            wait_until(lambda: len(relay._connections) == 1, what="connection capacity released")
            child.stdin.write("reuse\n")
            child.stdin.flush()
            assert opened["later"].wait(10)
            lifetimes["second"].check()
            assert set(lifetimes) == {"first", "second", "later"}
    finally:
        registration.dispose()


def test_invocation_limit_rejects_new_calls_without_evicting_replay_fences(tmp_path, monkeypatch):
    from cron.approval_relay import PresenterRegistration
    from cron import approval_relay_server
    from tests.waiting import wait_until

    monkeypatch.setattr(approval_relay_server, "MAX_INVOCATIONS", 2)
    opened = {key: threading.Event() for key in ("first", "second")}
    retired = {key: threading.Event() for key in opened}
    seen = []

    @contextlib.contextmanager
    def present(request, lifetime):
        seen.append(request.call_id)
        opened[request.call_id].set()
        try:
            yield
        finally:
            retired[request.call_id].set()

    script = '''
import json, sys, time
from cron.approval_relay_client import WorkerClient
client = WorkerClient(json.loads(sys.stdin.readline()))
client.bind_session('cron-1')
def pending(key):
    return client.pending('guard', session_id='cron-1', external_session_id='hm',
                          call_id=key, deadline=time.monotonic()+30, check=lambda:None)
for key in ('first', 'second'):
    with pending(key):
        sys.stdin.readline()
    print('retired', flush=True)
    sys.stdin.readline()
for key in ('excess', 'first'):
    try:
        with pending(key):
            raise AssertionError('Invocation cap or replay fence lost')
    except (OSError, ValueError):
        print('refused', flush=True)
'''
    registration = PresenterRegistration(str(tmp_path), "guard", present)
    try:
        relay = approval_relay_server.WorkerApprovalRelay(str(tmp_path), "daily", "exec-1")
        with launched(relay, script) as child:
            for key in ("first", "second"):
                assert opened[key].wait(10)
                child.stdin.write("close\n")
                child.stdin.flush()
                assert child.stdout.readline().strip() == "retired"
                assert retired[key].wait(10)
                wait_until(lambda: not relay._connections, what="completed invocation closed")
                child.stdin.write("next\n")
                child.stdin.flush()
            assert child.wait(timeout=10) == 0, child.stderr.read()
            assert child.stdout.read().splitlines() == ["refused", "refused"]
            assert seen == ["first", "second"]
    finally:
        registration.dispose()
