"""Update-marker ownership (contract C1 v2) on the real Windows hand-off script.

Every case runs Windows PowerShell against a marker owned by a real process.
Body parsing and Desktop lifecycle contracts live in separate bounded groups.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import time

import pytest

from tests.scripts.desktop_update.windows_marker_support import (
    MARKER,
    _creation_time,
    _dead_pid,
    _run,
    sleeper as sleeper,  # noqa: PLC0414 -- fixture self-alias avoids F811 for injected parameters
)


def _protected_pid() -> int:
    """A live pid that grants only limited query rights (a SYSTEM/protected service): what
    Get-Process .StartTime needs is denied, what CIM/GetProcessTimes need is granted."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    names = ('csrss.exe', 'smss.exe', 'wininit.exe', 'services.exe', 'lsass.exe', 'MsMpEng.exe')
    rows = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Get-CimInstance Win32_Process | ForEach-Object { \'{0} {1}\' -f $_.ProcessId, $_.Name }'],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.splitlines()
    for row in rows:
        pid, _, name = row.strip().partition(' ')
        if name not in names:
            continue
        full = k32.OpenProcess(0x0400, False, int(pid))         # PROCESS_QUERY_INFORMATION
        limited = k32.OpenProcess(0x1000, False, int(pid))      # ..._LIMITED_INFORMATION
        for handle in (full, limited):
            if handle:
                k32.CloseHandle(ctypes.c_void_p(handle))
        if limited and not full:
            return int(pid)
    pytest.fail('no process here grants only limited query rights: ' + ', '.join(rows[:40]))


@pytest.mark.platforms('windows')
def test_claim_is_the_first_action_and_writes_a_v2_body(tmp_path: Path) -> None:
    pid, code, out = _run(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
    assert code == 0, out
    lines = (tmp_path / MARKER).read_bytes().decode().split('\n')
    assert lines[0] == str(pid)
    assert lines[2].startswith('ct:') and len(lines[2].split('.')[-1]) == 3, lines
    log = (tmp_path / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig').splitlines()
    # Nothing (UI, Add-Type'd console helpers, probes) logs before the claim.
    assert 'claimed update marker' in log[0], log


@pytest.mark.platforms('windows')
@pytest.mark.parametrize('ct_matches', [True, False])
def test_live_foreign_owner_refuses_and_reused_pid_is_reclaimed(
    tmp_path: Path, sleeper: subprocess.Popen, ct_matches: bool,
) -> None:
    ct = _creation_time(sleeper.pid) if ct_matches else '1000.000'   # wrong ct = pid reuse
    body = f'{sleeper.pid}\n{int(time.time())}\nct:{ct}\n'.encode()
    (tmp_path / MARKER).write_bytes(body)
    pid, code, out = _run(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
    if ct_matches:
        assert code == 2, out
        assert (tmp_path / MARKER).read_bytes() == body
        # A refused run changed nothing and owns no result (A4): the other update reports.
        assert not (tmp_path / '.hermes-update-result.json').exists()
    else:
        assert code == 0, out
        lines = (tmp_path / MARKER).read_bytes().decode().split('\n')
        assert lines[0] == str(pid) and lines[2].startswith('ct:'), lines


@pytest.mark.platforms('windows')
@pytest.mark.parametrize('ct_matches', [True, False])
def test_owner_creation_time_is_read_for_system_processes(tmp_path: Path, ct_matches: bool) -> None:
    """A1: a marker naming a SYSTEM/protected pid (a reused pid after a reboot) is judged by its
    real creation time, not treated as live forever because StartTime is access-denied."""
    owner = _protected_pid()
    ct = _creation_time(owner) if ct_matches else '1000.000'
    body = f'{owner}\n{int(time.time())}\nct:{ct}\n'.encode()
    (tmp_path / MARKER).write_bytes(body)
    pid, code, out = _run(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
    if ct_matches:
        assert code == 2, out
        assert (tmp_path / MARKER).read_bytes() == body
    else:
        assert code == 0, out
        assert (tmp_path / MARKER).read_bytes().decode().split('\n')[0] == str(pid)


@pytest.mark.platforms('windows')
def test_dead_owner_marker_is_reclaimed_and_released(tmp_path: Path) -> None:
    (tmp_path / MARKER).write_bytes(f'{_dead_pid()}\n{int(time.time())}\nct:5.000\n'.encode())
    pid, code, out = _run(tmp_path, '-SelfTestMarker')
    assert code == 0, out
    assert not (tmp_path / MARKER).exists()
    log = (tmp_path / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert f'claimed update marker (pid {pid})' in log
    assert 'removed update marker (owned)' in log
