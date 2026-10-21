"""Watchdogs, probes and checkout-lock release on the real Windows hand-off."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.scripts.desktop_update.windows_commit_support import ROOT, _handoff
from tests.waiting import wait_until


@pytest.mark.platforms('windows')
def test_watchdog_remap_after_banner_reports_interrupted_followups(tmp_path: Path) -> None:
    code, out, _argv, result, _ = _handoff(
        tmp_path, '-NoGateway', HANDOFF_HANG='Update complete! (v1.0.0)',
        HERMES_UPDATE_STEP_IDLE_SECONDS='3',
    )
    assert code == 0, out
    assert (result['ok'], result['manual']) == (True, True), result
    assert any('interrupted' in w for w in result['warnings']), result


@pytest.mark.platforms('windows')
def test_cpu_busy_pipe_silent_update_is_not_killed_by_the_idle_watchdog(tmp_path: Path) -> None:
    code, out, _argv, result, _ = _handoff(
        tmp_path, '-NoGateway', HANDOFF_BUSY_SECONDS='15', HERMES_UPDATE_STEP_IDLE_SECONDS='4',
    )
    assert code == 0, out
    assert (result['ok'], result['warnings']) == (True, []), result


@pytest.mark.platforms('windows')
def test_hanging_update_help_probe_is_bounded(tmp_path: Path) -> None:
    code, out, argv, _result, _ = _handoff(
        tmp_path, '-NoGateway', '-ProbeTimeoutSeconds', '10', timeout=120, HANDOFF_HELP_HANG='1',
    )
    assert code == 0, out
    assert argv[0][:2] == ['update', '--yes'] and '--keep-stash' not in argv[0], argv


_CHECKOUT_HOLDER = """
import json, msvcrt, os, sys, time
from pathlib import Path
from tests.waiting import wait_until

lock, home, ready, released = map(Path, sys.argv[1:])
log = home / 'logs/desktop-update-handoff.log'

def release_wait_started():
    try:
        return 'keeping the update marker' in log.read_text(encoding='utf-8-sig')
    except (FileNotFoundError, PermissionError):
        return False

fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o644)
try:
    os.lseek(fd, 1 << 20, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # hermes_cli/update_lock.py::_try_lock's byte
    ready.touch()
    wait_until(release_wait_started, timeout=150, what='hand-off to wait on the checkout lock')
    result = json.loads((home / '.hermes-update-result.json').read_text(encoding='utf-8-sig'))
    initial = result['finished_at']
    wait_until(lambda: int(time.time()) > initial, timeout=10, what='result timestamp to advance')
    # The hand-off can restamp immediately after close, so sample just before releasing.
    released_at = int(time.time())
finally:
    os.close(fd)
released.write_text(json.dumps({'initial_finished_at': initial, 'released_at': released_at}),
                    encoding='utf-8')
"""


@pytest.mark.platforms('windows')
def test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path: Path) -> None:
    # The relaunched Desktop drops a non-manual result whose finished_at is 30 minutes old; the
    # R6 wait lasts up to 2 h. The result must carry the time the hand-off actually finished.
    holder = None
    ready = tmp_path / 'checkout-lock.ready'
    released = tmp_path / 'checkout-lock-release.json'
    # Inline source mentions the update command and triggers the live-system guard.
    holder_script = tmp_path / 'checkout_lock_holder.py'
    holder_script.write_text(_CHECKOUT_HOLDER, encoding='utf-8')

    def hold_checkout(home: Path, install: Path) -> None:
        nonlocal holder
        holder = subprocess.Popen(
            [sys.executable, str(holder_script), str(install / '.hermes-update.lock'),
             str(home), str(ready), str(released)],
            cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(ROOT)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        wait_until(lambda: ready.exists() or holder.poll() is not None,
                   timeout=30, what='checkout lock holder readiness')
        assert ready.exists(), holder.communicate(timeout=10)[0]

    try:
        code, out, _, result, home = _handoff(tmp_path, '-NoGateway', prepare=hold_checkout)
        holder_out, _ = holder.communicate(timeout=10)
        assert holder.returncode == 0, holder_out
    finally:
        if holder is not None and holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=10)
    assert code == 0, out
    log = (home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert 'keeping the update marker' in log, log
    release = json.loads(released.read_text(encoding='utf-8'))
    assert result['ok'] is True, result
    assert result['finished_at'] >= release['released_at'] > release['initial_finished_at'], (result, release)
