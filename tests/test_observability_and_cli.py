from __future__ import annotations

import json
import urllib.request

import pytest

from shutter_farm import cli
from shutter_farm.observability import Metrics, log, serve_metrics


def test_logs_are_one_json_object_per_line(capsys):
    log("job_started", folder="/mnt/archive/shoot", media_files=42)
    log("job_failed", level="error", error="exiftool exited 1")
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l]
    records = [json.loads(l) for l in lines]
    assert records[0]["severity"] == "INFO"
    assert records[0]["event"] == "job_started"
    assert records[0]["media_files"] == 42
    assert records[0]["service"] == "shutter-farm"
    assert records[1]["severity"] == "ERROR"  # the spelling Cloud Logging wants


def test_non_serializable_fields_are_stringified_not_crashed(capsys):
    from pathlib import Path

    log("job_started", folder=Path("/mnt/archive"))
    record = json.loads(capsys.readouterr().out.strip())
    assert record["folder"] == "/mnt/archive"


def test_metrics_render_in_prometheus_format():
    m = Metrics()
    m.describe("widget_total", "counter", "Widgets seen")
    m.inc("widget_total", 2, kind="photo")
    m.inc("widget_total", 1, kind="video")
    m.set("widget_ready", 1)
    text = m.render()
    assert "# TYPE widget_total counter" in text
    assert 'widget_total{kind="photo"} 2' in text
    assert 'widget_total{kind="video"} 1' in text
    assert "widget_ready 1" in text
    assert text.endswith("\n")


def test_label_values_are_escaped():
    m = Metrics()
    m.inc("thing_total", 1, path='a "quoted" \\ path')
    assert '\\"quoted\\"' in m.render()


def test_health_and_metrics_endpoints_answer():
    ready = {"ok": False}
    server = serve_metrics(0, ready_check=lambda: ready["ok"])
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert r.status == 200

        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5)
        assert exc.value.code == 503  # not ready before the first sweep

        ready["ok"] = True
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5) as r:
            assert r.status == 200

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            assert r.status == 200
            assert b"shutter_farm_up" in r.read()

        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert exc.value.code == 404
    finally:
        server.shutdown()


# ------------------------------------------------------------------- the CLI


def test_run_without_a_root_is_a_config_error(capsys, monkeypatch):
    monkeypatch.delenv("FARM_ROOT", raising=False)
    assert cli.main(["run"]) == cli.EXIT_CONFIG
    assert "config_error" in capsys.readouterr().out


def test_run_with_a_bogus_root_is_a_config_error(capsys, tmp_path):
    assert cli.main(["run", "--root", str(tmp_path / "missing")]) == cli.EXIT_CONFIG


def test_run_exits_two_when_jobs_failed(archive, monkeypatch, capsys):
    from shutter_farm.runner import RunOutcome

    def always_fail(job, **kwargs):
        return RunOutcome(False, job.tool, 1, 0.1, "boom")

    monkeypatch.setattr("shutter_farm.farm.run_job", always_fail)
    code = cli.main(["run", "--root", str(archive)])
    assert code == cli.EXIT_JOB_FAILURES


def test_run_exits_zero_on_success(archive, monkeypatch):
    from shutter_farm.runner import RunOutcome

    monkeypatch.setattr(
        "shutter_farm.farm.run_job",
        lambda job, **kwargs: RunOutcome(True, job.tool, 0, 0.1, "ok"),
    )
    assert cli.main(["run", "--root", str(archive)]) == cli.EXIT_OK


def test_env_vars_configure_the_run(archive, monkeypatch):
    monkeypatch.setenv("FARM_ROOT", str(archive))
    monkeypatch.setenv("FARM_WRITE", "true")
    monkeypatch.setenv("FARM_MAX_JOBS", "1")
    parser = cli.build_parser()
    args = parser.parse_args(["run"])
    assert args.root == str(archive) and args.write is True and args.max_jobs == 1


def test_status_reports_an_empty_ledger(archive, capsys):
    assert cli.main(["status", "--root", str(archive)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "status_note" in out and "empty" in out


def test_retry_clears_a_record(archive, monkeypatch, capsys):
    from shutter_farm.runner import RunOutcome

    monkeypatch.setattr(
        "shutter_farm.farm.run_job",
        lambda job, **kwargs: RunOutcome(True, job.tool, 0, 0.1, "ok"),
    )
    cli.main(["run", "--root", str(archive)])
    capsys.readouterr()

    target = str(archive / "2026-04-canyon")
    assert cli.main(["retry", "--root", str(archive), target]) == cli.EXIT_OK
    assert "record_cleared" in capsys.readouterr().out

    assert cli.main(["retry", "--root", str(archive), "/nope"]) == cli.EXIT_CONFIG


def test_the_state_file_can_live_outside_the_media_root(archive, tmp_path, monkeypatch):
    """Read-only media mounts are normal. State goes on its own volume."""
    from shutter_farm.runner import RunOutcome

    monkeypatch.setattr(
        "shutter_farm.farm.run_job",
        lambda job, **kwargs: RunOutcome(True, job.tool, 0, 0.1, "ok"),
    )
    state = tmp_path / "statevol" / "farm.json"
    assert cli.main(["run", "--root", str(archive), "--state", str(state)]) == cli.EXIT_OK
    assert state.exists()
