"""Desktop bridge adoption and orphaned-update ownership on real Windows processes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests.installation_launcher_fixture import publish_fixture_launcher
from tests.scripts.desktop_update.legacy_desktop_reader import legacy_read
from tests.scripts.desktop_update.windows_handoff_support import _HeldLock, _creation_time
from tests.scripts.desktop_update.windows_marker_support import (
    CLI,
    MARKER,
    SCRIPT,
    _dead_pid,
    _result,
    _run,
    sleeper as sleeper,  # noqa: PLC0414 -- fixture self-alias avoids F811 for injected parameters
)


@pytest.mark.platforms('windows')
@pytest.mark.parametrize('bridge', ['absent', 'someone-else'])
def test_desktop_started_handoff_only_adopts_its_bridge(
    tmp_path: Path, sleeper: subprocess.Popen, bridge: str,
) -> None:
    """A4: the Desktop gave up on a late script: it must not claim fresh, run, or leave a result."""
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    try:
        body = f'{other.pid}\n{int(time.time())}\nct:{_creation_time(other.pid)}\n'.encode()
        if bridge == 'someone-else':
            (tmp_path / MARKER).write_bytes(body)
        _, code, out = _run(tmp_path, '-DesktopPid', str(sleeper.pid))
    finally:
        other.kill(); other.wait()
    assert code == 2, out
    assert not (tmp_path / '.hermes-update-result.json').exists()
    if bridge == 'absent':
        assert not (tmp_path / MARKER).exists()
    else:
        assert (tmp_path / MARKER).read_bytes() == body


@pytest.mark.platforms('windows')
def test_live_desktop_bridge_marker_is_adopted_keeping_its_started_at(
    tmp_path: Path, sleeper: subprocess.Popen,
) -> None:
    started = int(time.time()) - 30
    (tmp_path / MARKER).write_bytes(f'{sleeper.pid}\n{started}\nct:{_creation_time(sleeper.pid)}\n'.encode())
    pid, code, out = _run(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup', '-DesktopPid', str(sleeper.pid))
    assert code == 0, out
    lines = (tmp_path / MARKER).read_bytes().decode().split('\n')
    assert lines[:2] == [str(pid), str(started)], lines
    assert lines[2].startswith('ct:'), lines
    assert _result(tmp_path)['started_at'] == started


@pytest.mark.platforms('windows')
def test_desktop_that_never_exits_is_not_relaunched_over(
    tmp_path: Path, sleeper: subprocess.Popen, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The verdict (refuse with 4, never relaunch) is the same at any ceiling; the production
    # 150 s one alone pushed this file to the runner's 300 s per-file cap on a loaded runner.
    monkeypatch.setenv('HERMES_UPDATE_DESKTOP_EXIT_SECONDS', '5')
    install = tmp_path / 'checkout'
    publish_fixture_launcher(install, CLI)
    home = tmp_path / 'home'; home.mkdir()
    # The Desktop's bridge claim, which a -DesktopPid hand-off adopts (A4).
    (home / MARKER).write_bytes(f'{sleeper.pid}\n{int(time.time())}\nct:{_creation_time(sleeper.pid)}\n'.encode())
    relaunch = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'hostname.exe'
    _, code, out = _run(home, '-DesktopPid', str(sleeper.pid), '-RelaunchExe', str(relaunch),
                        install=install)
    assert code == 4, out
    log = (home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert 'did not exit within 5s' in log, log   # the override reached the script's wait
    assert 'relaunching desktop' not in log, log


@pytest.mark.platforms('windows')
def test_ui_profile_sweep_keeps_dirs_of_live_handoffs(tmp_path: Path, sleeper: subprocess.Popen) -> None:
    live = tmp_path / f'hermes-update-ui-{sleeper.pid}'
    dead = tmp_path / f'hermes-update-ui-{_dead_pid()}'
    live.mkdir(); dead.mkdir()
    home = tmp_path / 'home'; home.mkdir()
    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT), '-SelfTestUi', '-NoUi'],
        cwd=tmp_path, env={**os.environ, 'HERMES_HOME': str(home), 'TEMP': str(tmp_path),
                           'HERMES_SELFTEST_HOLD_SECONDS': '0'},
        capture_output=True, text=True, timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert live.exists(), 'another live hand-off lost its browser profile'
    assert not dead.exists()


HOLD_CLI = """
import os, sys, time
from pathlib import Path
def main():
    if '--version' in sys.argv:
        print('Install directory: ' + str(Path(__file__).resolve().parents[1])); return 0
    if '--help' in sys.argv:
        print('--keep-stash'); return 0
    if sys.argv[1:2] == ['update']:   # still starting up: report the pid, wait to be released
        hold = Path(os.environ['HANDOFF_HOLD'])
        Path(str(hold) + '.pid').write_text(str(os.getpid()), encoding='utf-8')
        while not hold.exists():
            time.sleep(0.05)
    return 0
if __name__ == '__main__':
    sys.exit(main())
"""


def _marker_lines(path: Path) -> list[str]:
    try:   # the script may be mid-replace (sharing violation): read again next poll
        return path.read_bytes().decode().splitlines()
    except OSError:
        return []


def _parent_pid(pid: int) -> int:
    out = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ParentProcessId"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    return int(out or 0)


@pytest.mark.platforms('windows')
def test_script_killed_right_after_spawning_the_update_leaves_a_live_marker(tmp_path: Path) -> None:
    """C1 rule 6, written by the script itself: windows.ps1 is killed (taskkill /F, no /T) while
    its `hermes update` child is only starting up and has not taken the update lock. The marker
    must still read LIVE through the child named on line 4 -- also to an old packaged Desktop,
    which judges line 1 alone, so line 1 names the hand-off's custodian before the update starts
    and the first old-reader read right after the kill (the marker lock held, so nothing can
    take over yet) already sees a live owner (review 5423056011) -- and be released once that
    child is gone."""
    install = tmp_path / 'checkout'
    publish_fixture_launcher(install, HOLD_CLI)
    home = tmp_path / 'home'; home.mkdir()
    hold = tmp_path / 'release-update'
    marker = home / MARKER
    script = subprocess.Popen(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
         '-InstallRoot', str(install), '-NoUi'],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, 'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(tmp_path / 'empty-store'),
             'HANDOFF_HOLD': str(hold)},
    )
    child_pid_file = Path(str(hold) + '.pid')
    try:
        deadline = time.monotonic() + 120
        while not child_pid_file.exists():
            assert time.monotonic() < deadline and script.poll() is None, 'update child never started'
            time.sleep(0.02)
        # Kill the script the moment the delegate line is there (at most 3 s after the spawn).
        settle = time.monotonic() + 3
        while time.monotonic() < settle and not any(line.startswith('delegate:') for line in _marker_lines(marker)):
            time.sleep(0.01)
        a7 = _HeldLock(home)   # no marker mutation (a takeover included) until we let go
        try:
            subprocess.run(['taskkill', '/F', '/PID', str(script.pid)], capture_output=True, check=True)
            script.wait(timeout=30)
            seen = legacy_read(home)
            assert seen['live'] is not None and seen['kept'], ('hand-off just died', seen)
        finally:
            a7.release()
        child = int(child_pid_file.read_text(encoding='utf-8-sig'))
        deadline = time.monotonic() + 60
        while (lines := _marker_lines(marker))[:1] in ([], [str(script.pid)]):   # the custodian takes over
            assert time.monotonic() < deadline, lines
            time.sleep(0.1)
        assert lines[3].startswith('delegate:'), lines
        delegate = int(lines[3].split()[0].split(':')[1])
        assert delegate in (child, _parent_pid(child), _parent_pid(_parent_pid(child)))  # update or its launcher
        assert lines[3] == f'delegate:{delegate} ct:{_creation_time(delegate)}'

        # A real reader (the script itself) sees an update in progress and refuses.
        _, code, out = _run(home, '-SelfTestMarker', '-NoMarkerCleanup', install=install)
        assert code == 2, out
        seen = legacy_read(home)
        assert seen['live'] == {'pid': int(lines[0]), 'ageMs': seen['live']['ageMs']} and seen['kept'], seen

        hold.touch()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and subprocess.run(
                ['tasklist', '/FI', f'PID eq {delegate}', '/NH'], capture_output=True, text=True,
                check=False,
        ).stdout.find(str(delegate)) >= 0:
            time.sleep(0.2)
        while marker.exists():   # the custodian releases once the delegate is gone
            assert time.monotonic() < deadline, _marker_lines(marker)
            time.sleep(0.2)
        assert legacy_read(home)['live'] is None
        _, code, out = _run(home, '-SelfTestMarker', '-NoMarkerCleanup', install=install)
        assert code == 0, out
    finally:
        hold.touch()
        if script.poll() is None:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(script.pid)], capture_output=True, check=False)
            script.wait()
