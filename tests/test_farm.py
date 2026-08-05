"""Sweeps: idempotency, failure isolation, shutdown, metrics."""

from __future__ import annotations

import json
import time

from shutter_farm.farm import Farm
from shutter_farm.observability import METRICS
from shutter_farm.state import Ledger
from tests.conftest import make_media


def farm_for(archive, ledger, fake_run, **kwargs) -> Farm:
    return Farm(archive, ledger, run_fn=fake_run, **kwargs)


def test_a_sweep_runs_every_shoot_once(archive, ledger, fake_run):
    result = farm_for(archive, ledger, fake_run).sweep()
    assert result.discovered == 3
    assert result.ran == 3 and result.succeeded == 3 and result.failed == 0
    assert sorted(name for name, _ in fake_run.calls) == [
        "2026-04-canyon", "2026-05-studio", "2026-06-interview"
    ]


def test_the_second_sweep_does_nothing(archive, ledger, fake_run):
    farm = farm_for(archive, ledger, fake_run)
    farm.sweep()
    fake_run.calls.clear()
    result = farm.sweep()
    assert result.ran == 0 and result.skipped == 3
    assert fake_run.calls == []


def test_new_footage_is_picked_up_on_the_next_sweep(archive, ledger, fake_run):
    farm = farm_for(archive, ledger, fake_run)
    farm.sweep()
    fake_run.calls.clear()
    make_media(archive / "2026-08-new-shoot", ["B001.MOV", "B002.MOV"])
    result = farm.sweep()
    assert result.ran == 1
    assert [name for name, _ in fake_run.calls] == ["2026-08-new-shoot"]


def test_write_is_off_by_default_and_passed_through_when_asked(archive, ledger, fake_run):
    farm_for(archive, ledger, fake_run).sweep()
    assert all(write is False for _, write in fake_run.calls)

    fake_run.calls.clear()
    ledger2 = Ledger(ledger.path.parent / "second.json")
    farm_for(archive, ledger2, fake_run, write=True).sweep()
    assert all(write is True for _, write in fake_run.calls)


def test_one_bad_folder_does_not_fail_the_batch(archive, ledger, fake_run):
    fake_run.fail = {"2026-05-studio": "exiftool exited 1"}
    result = farm_for(archive, ledger, fake_run).sweep()
    assert result.ran == 3
    assert result.succeeded == 2 and result.failed == 1
    assert any("2026-05-studio" in e for e in result.errors)


def test_a_timeout_is_recorded_as_a_failure_not_a_crash(archive, ledger, fake_run):
    fake_run.timeout = {"2026-06-interview"}
    result = farm_for(archive, ledger, fake_run).sweep()
    assert result.failed == 1 and result.succeeded == 2


def test_a_missing_tool_fails_only_its_own_jobs(archive, ledger, fake_run):
    fake_run.raise_missing = {"2026-06-interview"}
    result = farm_for(archive, ledger, fake_run).sweep()
    assert result.succeeded == 2 and result.failed == 1


def test_repeated_failure_quarantines_and_stops_costing_time(archive, ledger, fake_run):
    fake_run.fail = {"2026-05-studio": "broken"}
    farm = farm_for(archive, ledger, fake_run)
    for _ in range(3):
        # Force past the backoff window each time.
        for record in ledger._records.values():
            record.last_run_at = 0.0
        farm.sweep()
    assert ledger._records[str(archive / "2026-05-studio")].status == "quarantined"

    fake_run.calls.clear()
    farm.sweep()
    assert "2026-05-studio" not in [n for n, _ in fake_run.calls]


def test_state_is_saved_after_every_job_not_at_the_end(archive, ledger, fake_run):
    """A preempted node must not lose the whole batch."""
    saves = []
    original = ledger.save

    def counting_save():
        saves.append(len(ledger._records))
        original()

    ledger.save = counting_save
    farm_for(archive, ledger, fake_run).sweep()
    # One save per job plus the end-of-sweep save.
    assert len(saves) >= 4
    assert saves[0] == 1  # persisted after the very first job


def test_max_jobs_caps_a_sweep(archive, ledger, fake_run):
    result = farm_for(archive, ledger, fake_run, max_jobs=2).sweep()
    assert result.ran == 2
    # The rest are simply queued for next time, not lost.
    fake_run.calls.clear()
    assert farm_for(archive, ledger, fake_run, max_jobs=2).sweep().ran == 1


def test_sigterm_stops_cleanly_between_jobs(archive, ledger, fake_run):
    farm = farm_for(archive, ledger, fake_run)
    farm.request_stop()
    result = farm.sweep()
    assert result.ran == 0
    assert ledger.path.exists()  # still persisted a consistent ledger


def test_metrics_reflect_the_sweep(archive, ledger, fake_run):
    fake_run.fail = {"2026-05-studio": "nope"}
    farm_for(archive, ledger, fake_run).sweep()
    text = METRICS.render()
    assert "shutter_farm_jobs_total" in text
    assert 'result="success"' in text
    assert 'result="failure"' in text
    assert "shutter_farm_folders" in text
    assert "shutter_farm_last_run_timestamp_seconds" in text


def test_an_empty_root_is_a_clean_no_op(tmp_path, fake_run):
    empty = tmp_path / "nothing"
    empty.mkdir()
    ledger = Ledger(tmp_path / "state.json")
    result = Farm(empty, ledger, run_fn=fake_run).sweep()
    assert result.discovered == 0 and result.ran == 0
    assert json.loads(ledger.path.read_text())["records"] == {}
