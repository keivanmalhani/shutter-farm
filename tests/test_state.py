"""The ledger is what makes an hourly cron safe. These are its guarantees."""

from __future__ import annotations

import json
import time

from shutter_farm.discovery import discover
from shutter_farm.state import BACKOFF_BASE_SECONDS, Ledger, fingerprint
from tests.conftest import make_media


def job_named(archive, name):
    return next(j for j in discover(archive) if j.folder.name == name)


def test_a_new_folder_is_always_work(archive, ledger):
    job = job_named(archive, "2026-04-canyon")
    should, why = ledger.should_run(job, time.time())
    assert should and why == "never processed"


def test_a_finished_unchanged_folder_is_not_work_again(archive, ledger):
    job = job_named(archive, "2026-04-canyon")
    ledger.record_success(job, duration=1.0, now=time.time(), outputs=[])
    should, why = ledger.should_run(job, time.time())
    assert not should and "already done" in why


def test_adding_a_photo_makes_it_work_again(archive, ledger):
    job = job_named(archive, "2026-04-canyon")
    ledger.record_success(job, duration=1.0, now=time.time(), outputs=[])
    make_media(job.folder, ["DSC0004.ARW"])
    job = job_named(archive, "2026-04-canyon")
    should, why = ledger.should_run(job, time.time())
    assert should and "contents changed" in why


def test_the_tools_own_outputs_do_not_retrigger_work(archive, ledger):
    # The whole point of fingerprinting media only: shutter-cull writes
    # sidecars into the same folder, and that must not make the folder
    # look new on the next sweep.
    job = job_named(archive, "2026-04-canyon")
    ledger.record_success(job, duration=1.0, now=time.time(), outputs=[])
    (job.folder / "DSC0001.xmp").write_text("<x:xmpmeta/>")
    (job.folder / "cull-report.txt").write_text("plan")
    (job.folder / "_selects").mkdir()
    should, _ = ledger.should_run(job_named(archive, "2026-04-canyon"), time.time())
    assert not should


def test_touching_a_photo_does_make_it_work_again(archive, ledger):
    # mtime is part of the fingerprint on purpose: a re-exported or
    # re-copied file is genuinely different input.
    job = job_named(archive, "2026-04-canyon")
    ledger.record_success(job, duration=1.0, now=time.time(), outputs=[])
    target = job.folder / "DSC0001.ARW"
    stat = target.stat()
    import os
    os.utime(target, (stat.st_atime, stat.st_mtime + 60))
    should, _ = ledger.should_run(job_named(archive, "2026-04-canyon"), time.time())
    assert should


def test_a_copy_of_a_folder_is_a_different_job(archive, ledger, tmp_path):
    job = job_named(archive, "2026-04-canyon")
    ledger.record_success(job, duration=1.0, now=time.time(), outputs=[])
    import shutil
    shutil.copytree(job.folder, archive / "2026-04-canyon-copy")
    copy = job_named(archive, "2026-04-canyon-copy")
    should, why = ledger.should_run(copy, time.time())
    assert should and why == "never processed"


# ------------------------------------------------------------------ failures


def test_a_failure_backs_off_before_retrying(archive, ledger):
    job = job_named(archive, "2026-04-canyon")
    now = time.time()
    ledger.record_failure(job, error="exiftool exited 1", now=now)

    should, why = ledger.should_run(job, now + 1)
    assert not should and "backing off" in why

    should, why = ledger.should_run(job, now + BACKOFF_BASE_SECONDS + 1)
    assert should and "retrying" in why


def test_backoff_grows_with_attempts(archive, ledger):
    job = job_named(archive, "2026-04-canyon")
    now = time.time()
    ledger.record_failure(job, error="boom", now=now)
    ledger.record_failure(job, error="boom", now=now)
    # Second attempt waits twice as long as the first.
    should, _ = ledger.should_run(job, now + BACKOFF_BASE_SECONDS + 1)
    assert not should
    should, _ = ledger.should_run(job, now + (BACKOFF_BASE_SECONDS * 2) + 1)
    assert should


def test_repeated_failures_quarantine_rather_than_loop(archive, ledger):
    job = job_named(archive, "2026-04-canyon")
    now = time.time()
    for _ in range(3):
        record = ledger.record_failure(job, error="card is unreadable", now=now)
    assert record.status == "quarantined"
    should, why = ledger.should_run(job, now + 10_000_000)
    assert not should and "quarantined" in why
    assert ledger.quarantined()[0][1].last_error == "card is unreadable"


def test_fixing_a_quarantined_folder_releases_it(archive, ledger):
    job = job_named(archive, "2026-04-canyon")
    now = time.time()
    for _ in range(3):
        ledger.record_failure(job, error="bad file", now=now)
    make_media(job.folder, ["DSC0005.ARW"])  # the operator replaces the card
    should, why = ledger.should_run(job_named(archive, "2026-04-canyon"), now)
    assert should and "contents changed" in why


def test_an_operator_can_clear_one_record(archive, ledger):
    job = job_named(archive, "2026-04-canyon")
    for _ in range(3):
        ledger.record_failure(job, error="bad", now=time.time())
    assert ledger.forget(str(job.folder))
    should, why = ledger.should_run(job, time.time())
    assert should and why == "never processed"
    assert not ledger.forget("/not/a/folder")


# --------------------------------------------------------------- persistence


def test_the_ledger_survives_a_restart(archive, tmp_path):
    path = tmp_path / "state.json"
    first = Ledger(path)
    job = job_named(archive, "2026-04-canyon")
    first.record_success(job, duration=2.0, now=time.time(), outputs=["x"])
    first.save()

    second = Ledger(path)
    should, why = second.should_run(job, time.time())
    assert not should and "already done" in why


def test_a_corrupt_ledger_means_redo_not_crash(archive, tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")
    ledger = Ledger(path)
    job = job_named(archive, "2026-04-canyon")
    should, _ = ledger.should_run(job, time.time())
    assert should  # wasteful, never wrong


def test_a_future_schema_is_ignored_rather_than_misread(archive, tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 99, "records": {"x": {}}}))
    ledger = Ledger(path)
    assert ledger.counts() == {"done": 0, "failed": 0, "quarantined": 0}


def test_saving_is_atomic(archive, tmp_path):
    path = tmp_path / "nested" / "state.json"
    ledger = Ledger(path)
    job = job_named(archive, "2026-04-canyon")
    ledger.record_success(job, duration=1.0, now=time.time(), outputs=[])
    ledger.save()
    assert json.loads(path.read_text())["schema_version"] == 1
    assert list(path.parent.glob("*.part")) == []  # no temp file left behind


def test_fingerprint_of_an_unreadable_folder_is_stable(tmp_path):
    from shutter_farm.discovery import Job

    missing = Job(folder=tmp_path / "gone", kind="photo", media_count=0)
    assert fingerprint(missing) == "unreadable"
