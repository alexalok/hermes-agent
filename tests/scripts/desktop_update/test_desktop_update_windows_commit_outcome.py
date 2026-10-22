"""Post-commit results and marker ownership on the real Windows hand-off script.

A committed update stays successful even when its follow-up verification fails.
Receipt reconciliation and supervision have separate files so each group fits
within the runner's per-file subprocess deadline on loaded Windows hosts.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import time

import pytest

from tests.scripts.desktop_update.windows_commit_support import _handoff


@pytest.mark.platforms('windows')
def test_verify_failure_after_a_committed_update_is_ok_with_warnings(tmp_path: Path) -> None:
    code, out, argv, result, _ = _handoff(tmp_path, verify='raise SystemExit(3)\n')
    assert code == 0, out
    assert (result['ok'], result['exit_code'], result['manual']) == (True, 0, True), result
    assert [w.split(':')[0] for w in result['warnings']] == ['verify'], result
    assert result['message'].startswith('Hermes was updated, but'), result
    assert 'previous version' not in result['message']
    # The fleet the Desktop stopped is still brought back.
    assert argv[-1] == ['gateway', 'start', '--all'], argv


@pytest.mark.platforms('windows')
def test_result_is_atomic_json_carrying_started_at_and_warnings(tmp_path: Path) -> None:
    started = int(time.time()) - 5
    code, out, _, result, home = _handoff(tmp_path, HERMES_UPDATE_STARTED_AT=str(started))
    assert code == 0, out
    assert (result['ok'], result['started_at'], result['warnings']) == (True, started, []), result
    assert not list(home.glob('.hermes-update-result.json.*.tmp'))


@pytest.mark.platforms('windows')
def test_a_result_that_cannot_be_published_leaves_no_tmp_file(tmp_path: Path) -> None:
    # Write-Result's Move/Replace fail (the result path is a directory): its <result>.<pid>.tmp
    # must not be left beside it, one per hand-off, forever.
    code, out, _, result, home = _handoff(
        tmp_path, '-NoGateway', prepare=lambda home, _install: (home / '.hermes-update-result.json').mkdir())
    assert code == 0, out
    assert result is None
    assert not list(home.glob('.hermes-update-result.json.*.tmp')), list(home.iterdir())


@pytest.mark.platforms('windows')
def test_marker_with_a_live_delegate_is_handed_to_it_at_finish(tmp_path: Path) -> None:
    # A7 release rule: the owner deletes its claim unless a delegate still runs; then that
    # delegate becomes the owner (canonical body, started_at kept, no delegate line).
    pid_file = tmp_path / 'delegate.pid'
    try:
        code, out, _, _result, home = _handoff(tmp_path, HANDOFF_DELEGATE=str(pid_file))
        assert code == 0, out
        marker = (home / '.hermes-update-in-progress').read_bytes().decode().split('\n')
        assert marker[0] == pid_file.read_text(encoding='utf-8-sig'), marker
        assert marker[2].startswith('ct:') and marker[3:] == [''], marker
    finally:
        if pid_file.exists():
            subprocess.run(['taskkill', '/F', '/PID', pid_file.read_text(encoding='utf-8-sig')], capture_output=True, check=False)
