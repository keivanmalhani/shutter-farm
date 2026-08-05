"""Tests for doctor, written as machines that are broken in one way each.

The point of doctor is that it is right about someone else's computer, so
every test builds the specific breakage rather than asserting on whatever the
test runner happens to be sitting on. The environment is injected, so these
run identically on a laptop with ffmpeg and in a container without it.
"""

from __future__ import annotations

from pathlib import Path

from shutter_farm import doctor
from shutter_farm.cli import main


def ctx(tmp_path: Path, **overrides) -> doctor.Context:
    """A machine where everything works, then broken on purpose per test.

    A root the test did not name is created, because most tests are not about
    the root. A root the test named is left exactly as the test left it."""
    if "root" in overrides:
        root = overrides.pop("root")
    else:
        root = tmp_path / "media"
        root.mkdir(exist_ok=True, parents=True)
    defaults = dict(
        root=root,
        state=tmp_path / "state.json",
        which=lambda name: f"/usr/bin/{name}",
        access=lambda path, mode: True,
        version_of=lambda name: (0, f"{name} 1.2.3"),
        disk_free_gb=lambda path: 100.0,
        port_free=lambda port: True,
        python_version=(3, 12),
    )
    defaults.update(overrides)
    return doctor.Context(**defaults)


def deny(*paths: Path):
    """An access() that refuses exactly these paths and allows everything else."""
    blocked = {str(p) for p in paths}
    return lambda path, mode: str(path) not in blocked


def by_name(checks, name) -> doctor.Check:
    return next(c for c in checks if c.name == name)


# ------------------------------------------------------------------ python

def test_old_python_is_fatal_and_says_the_version(tmp_path):
    check = doctor.check_python(ctx(tmp_path, python_version=(3, 9)))
    assert check.status == doctor.FAIL
    assert "3.9" in check.detail and "3.11" in check.detail
    assert check.fix


def test_current_python_passes(tmp_path):
    assert doctor.check_python(ctx(tmp_path)).status == doctor.OK


# -------------------------------------------------------------- media root

def test_missing_root_is_fatal(tmp_path):
    check = doctor.check_root(ctx(tmp_path, root=tmp_path / "nope"))
    assert check.status == doctor.FAIL
    assert "does not exist" in check.detail


def test_root_that_is_a_file_is_fatal(tmp_path):
    target = tmp_path / "afile"
    target.write_text("x")
    check = doctor.check_root(ctx(tmp_path, root=target))
    assert check.status == doctor.FAIL
    assert "not a directory" in check.detail


def test_unreadable_root_is_fatal_and_names_the_fix(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    check = doctor.check_root(ctx(tmp_path, root=locked, access=deny(locked)))
    assert check.status == doctor.FAIL
    assert "chmod" in check.fix


# ------------------------------------------------------------------ ledger

def test_ledger_on_a_read_only_parent_is_fatal_and_suggests_a_volume(tmp_path):
    """The signature failure of a read-only archive mount, which is the setup
    the project actively recommends."""
    archive = tmp_path / "archive"
    archive.mkdir()
    check = doctor.check_ledger(ctx(tmp_path, root=archive,
                                    state=archive / "state.json",
                                    access=deny(archive)))
    assert check.status == doctor.FAIL
    assert "--state" in check.fix


def test_existing_ledger_that_is_not_writable_is_fatal(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{}")
    check = doctor.check_ledger(ctx(tmp_path, state=state, access=deny(state)))
    assert check.status == doctor.FAIL
    assert "chmod +w" in check.fix


def test_ledger_that_does_not_exist_yet_is_fine(tmp_path):
    check = doctor.check_ledger(ctx(tmp_path))
    assert check.status == doctor.OK
    assert "will be created" in check.detail


def test_ledger_parent_missing_is_fatal_with_mkdir(tmp_path):
    check = doctor.check_ledger(ctx(tmp_path, state=tmp_path / "gone" / "state.json"))
    assert check.status == doctor.FAIL
    assert "mkdir" in check.fix


def test_existing_ledger_is_reported_as_existing(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{}")
    check = doctor.check_ledger(ctx(tmp_path, state=state))
    assert check.status == doctor.OK
    assert "exists" in check.detail


# -------------------------------------------------------------- write mode

def test_write_off_is_ok_and_says_so_plainly(tmp_path):
    check = doctor.check_write_mode(ctx(tmp_path, write=False))
    assert check.status == doctor.OK
    assert "off" in check.detail.lower()


def test_write_on_against_read_only_root_is_fatal(tmp_path):
    archive = tmp_path / "ro"
    archive.mkdir()
    check = doctor.check_write_mode(
        ctx(tmp_path, root=archive, write=True, access=deny(archive)))
    assert check.status == doctor.FAIL
    assert ":ro" in check.fix or "readOnly" in check.fix


def test_write_on_with_a_missing_root_warns_and_defers_to_the_root_check(tmp_path):
    check = doctor.check_write_mode(ctx(tmp_path, root=tmp_path / "nope", write=True))
    assert check.status == doctor.WARN
    assert "root check failed" in check.detail
    assert "media root" in check.fix


def test_write_on_against_writable_root_warns_rather_than_passing_silently(tmp_path):
    check = doctor.check_write_mode(ctx(tmp_path, write=True))
    assert check.status == doctor.WARN
    assert "archive" in check.detail


# ------------------------------------------------------------------ engines

def test_missing_engine_warns_and_names_what_it_would_have_handled(tmp_path):
    checks = doctor.check_engine(
        ctx(tmp_path, which=lambda name: None), "shutter-select")
    assert checks[0].status == doctor.WARN
    assert "video" in checks[0].detail
    assert "pip install" in checks[0].fix


def test_engine_on_path_but_not_runnable_is_fatal(tmp_path):
    """A broken wheel or a missing shared library passes `which` and fails on
    use, which is a completely different fix from not being installed."""
    checks = doctor.check_engine(
        ctx(tmp_path, version_of=lambda name: (127, "libGL.so.1: cannot open shared object file")),
        "shutter-cull")
    assert checks[0].status == doctor.FAIL
    assert "will not run" in checks[0].detail
    assert "libGL" in checks[0].detail


def test_engine_present_but_its_binary_missing_is_fatal(tmp_path):
    """shutter-select without ffmpeg fails on every video folder, so the farm
    should say that now rather than two hundred times tonight."""
    checks = doctor.check_engine(
        ctx(tmp_path, which=lambda name: None if name == "ffmpeg" else f"/usr/bin/{name}"),
        "shutter-select")
    ffmpeg = by_name(checks, "shutter-select needs ffmpeg")
    assert ffmpeg.status == doctor.FAIL
    assert "install" in ffmpeg.fix


def test_healthy_engine_reports_its_version_line(tmp_path):
    checks = doctor.check_engine(ctx(tmp_path), "shutter-cull")
    assert checks[0].status == doctor.OK
    assert "1.2.3" in checks[0].detail
    assert all(c.status == doctor.OK for c in checks)


def test_both_engines_missing_is_caught_even_though_each_is_only_a_warning(tmp_path):
    checks = doctor.run_checks(ctx(tmp_path, which=lambda name: None), discovered=3)
    assert not any(c.fatal for c in checks)
    assert doctor.both_engines_missing(checks)


def test_one_engine_present_is_not_both_missing(tmp_path):
    checks = doctor.run_checks(
        ctx(tmp_path, which=lambda n: None if n == "shutter-select" else f"/usr/bin/{n}"),
        discovered=3)
    assert not doctor.both_engines_missing(checks)


# ------------------------------------------------------------- disk, ports

def test_low_disk_is_fatal(tmp_path):
    check = doctor.check_disk(ctx(tmp_path, disk_free_gb=lambda p: 0.4))
    assert check.status == doctor.FAIL
    assert "0.4" in check.detail


def test_plenty_of_disk_passes(tmp_path):
    assert doctor.check_disk(ctx(tmp_path)).status == doctor.OK


def test_busy_metrics_port_is_fatal(tmp_path):
    check = doctor.check_metrics_port(
        ctx(tmp_path, metrics_port=9090, port_free=lambda p: False))
    assert check.status == doctor.FAIL
    assert "9090" in check.detail and "9090" in check.fix


def test_metrics_port_not_requested_is_not_checked(tmp_path):
    check = doctor.check_metrics_port(ctx(tmp_path, metrics_port=0))
    assert check.status == doctor.OK
    assert "Not requested" in check.detail


# ------------------------------------------------------------------- work

def test_no_work_found_explains_the_skip_rules_rather_than_just_saying_zero(tmp_path):
    check = doctor.check_jobs(ctx(tmp_path), discovered=0)
    assert check.status == doctor.WARN
    assert "underscore" in check.fix or "hidden" in check.fix


def test_work_found_is_counted(tmp_path):
    assert "7 folder" in doctor.check_jobs(ctx(tmp_path), discovered=7).detail


# ---------------------------------------------------------------- rendering

def test_every_non_passing_check_carries_a_fix(tmp_path):
    """A check that cannot tell the user what to type is not finished."""
    broken = ctx(tmp_path, root=tmp_path / "nope", python_version=(3, 9),
                 which=lambda n: None, disk_free_gb=lambda p: 0.1,
                 metrics_port=9090, port_free=lambda p: False,
                 state=tmp_path / "missing" / "state.json")
    for check in doctor.run_checks(broken, discovered=None):
        if check.status != doctor.OK:
            assert check.fix, f"{check.name} has no fix"


def test_render_is_readable_and_counts_the_blockers(tmp_path):
    checks = doctor.run_checks(ctx(tmp_path, disk_free_gb=lambda p: 0.1), discovered=2)
    out = doctor.render(checks)
    assert "[FAIL]" in out
    assert "1 blocking problem" in out
    assert "->" in out  # the fix line is shown, not just the failure


def test_the_summary_never_contradicts_the_exit_code(tmp_path):
    """The first version printed "A sweep will run" directly above the reason
    it would not, and then exited non-zero."""
    for broken in [
        dict(which=lambda n: None),                       # no engines
        dict(disk_free_gb=lambda p: 0.1),                 # fatal check
        dict(),                                           # healthy
    ]:
        checks = doctor.run_checks(ctx(tmp_path, **broken), discovered=2)
        workable, summary = doctor.verdict(checks)
        assert ("will run" in summary) == workable, summary
        assert summary in doctor.render(checks).replace("\n  ", " ") or \
            all(word in doctor.render(checks) for word in summary.split()[:4])


def test_no_engines_is_reported_as_blocking_in_the_summary(tmp_path):
    checks = doctor.run_checks(ctx(tmp_path, which=lambda n: None), discovered=2)
    workable, summary = doctor.verdict(checks)
    assert workable is False
    assert "Neither engine" in summary
    assert "will run" not in summary


def test_render_says_so_when_everything_is_fine(tmp_path):
    out = doctor.render(doctor.run_checks(ctx(tmp_path), discovered=2))
    assert "Everything checks out" in out
    assert "[FAIL]" not in out


def test_render_output_is_ascii(tmp_path):
    out = doctor.render(doctor.run_checks(ctx(tmp_path, which=lambda n: None),
                                          discovered=0))
    assert out.encode("ascii")


# ---------------------------------------------------------------------- cli

def test_cli_doctor_exits_one_when_the_root_is_missing(tmp_path, capsys):
    code = main(["doctor", "--root", str(tmp_path / "nope")])
    assert code == 1
    assert "does not exist" in capsys.readouterr().out


def test_cli_doctor_reports_every_problem_not_just_the_first(tmp_path, capsys):
    """Three round trips to fix three things is the support experience this
    command exists to avoid."""
    code = main(["doctor", "--root", str(tmp_path / "nope"),
                 "--state", str(tmp_path / "also-nope" / "s.json")])
    out = capsys.readouterr().out
    assert code == 1
    assert "media root" in out and "ledger" in out
    assert out.count("FAIL") >= 2


def test_cli_doctor_json_mode_emits_one_object_per_check(tmp_path, capsys):
    import json
    main(["doctor", "--root", str(tmp_path), "--json"])
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l.strip()]
    objects = [json.loads(l) for l in lines]

    checks = [o for o in objects if o["event"] == "doctor_check"]
    verdicts = [o for o in objects if o["event"] == "doctor_verdict"]
    assert checks and all({"check", "status", "detail"} <= set(o) for o in checks)
    assert len(verdicts) == 1, "a probe needs exactly one line to alert on"
    assert {"workable", "summary"} <= set(verdicts[0])
    assert set(objects[-1]) >= {"event"} and objects[-1]["event"] == "doctor_verdict"


def test_cli_doctor_runs_with_no_root_at_all(tmp_path, capsys):
    """Someone debugging an install has not necessarily mounted anything yet."""
    main(["doctor"])
    out = capsys.readouterr().out
    assert "tools and this machine only" in out
    assert "python" in out


def test_cli_doctor_counts_real_folders_under_a_real_root(tmp_path, capsys):
    shoot = tmp_path / "2026-04-canyon"
    shoot.mkdir()
    for i in range(3):
        (shoot / f"IMG_{i}.jpg").write_bytes(b"x")
    main(["doctor", "--root", str(tmp_path)])
    assert "1 folder(s) with media" in capsys.readouterr().out


# ------------------------------------------------- the real version probe

def test_version_probe_accepts_a_cli_that_has_no_version_flag(monkeypatch):
    """shutter-cull requires a subcommand and exits 2 on a bare --version.

    Calling that broken sends someone to reinstall a working install, which
    is the worst thing a diagnostic can do."""
    calls = []

    class Result:
        def __init__(self, code, out):
            self.returncode, self.stdout, self.stderr = code, out, ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd[1])
        if cmd[1] == "--version":
            return Result(2, "usage: shutter-cull [-h] {scan,cull} ...")
        return Result(0, "usage: shutter-cull [-h] {scan,cull} ...")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    code, line = doctor._real_version_of("shutter-cull")
    assert code == 0
    assert "does not report a version" in line
    assert calls == ["--version", "--help"]


def test_version_probe_reports_a_binary_that_cannot_execute_at_all(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise OSError("libGL.so.1: cannot open shared object file")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    code, line = doctor._real_version_of("shutter-select")
    assert code == 127
    assert "libGL" in line


def test_version_probe_prefers_a_real_version_line(monkeypatch):
    class Result:
        def __init__(self, code, out):
            self.returncode, self.stdout, self.stderr = code, out, ""

    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda cmd, **kw: Result(0, "shutter-select 0.1.0"))
    assert doctor._real_version_of("shutter-select") == (0, "shutter-select 0.1.0")
