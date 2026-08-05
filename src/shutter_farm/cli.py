"""shutter-farm CLI.

    shutter-farm run   --root /mnt/archive          one sweep, then exit
    shutter-farm serve --root /mnt/archive          sweep on an interval
    shutter-farm status --root /mnt/archive         what the ledger knows
    shutter-farm retry  --root /mnt/archive FOLDER  un-quarantine one folder
    shutter-farm doctor --root /mnt/archive         will a sweep actually work

run is the shape a Kubernetes Job or a Cloud Run Job wants: do the work,
exit zero, let the platform own the schedule. serve is for a plain host or
a long-lived container, and is the only mode that keeps the metrics port
open across sweeps.

doctor is the one to run first, and the one to ask for when someone reports
that a scheduled sweep did nothing. It reports every problem it finds in one
pass with the command that fixes each.

Exit codes: 0 all good, 1 a configuration error, 2 the sweep ran but some
folders failed. A scheduler can tell "I could not start" from "I ran and
some of the work is broken", which are different pages at 3am.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from shutter_farm import __version__, doctor
from shutter_farm.discovery import discover
from shutter_farm.farm import Farm
from shutter_farm.observability import excepthook_to_log, log, serve_metrics
from shutter_farm.state import Ledger

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_JOB_FAILURES = 2

DEFAULT_LEDGER_NAME = "shutter-farm-state.json"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shutter-farm",
        description=(
            "Batch runner for the shutter toolchain. Watches a media root, "
            "dispatches folders to shutter-cull or shutter-select, and keeps "
            "a ledger so scheduled runs are idempotent and resumable."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=_env("FARM_ROOT", ""),
                       help="Media root to sweep. Env: FARM_ROOT")
        p.add_argument("--state", default=_env("FARM_STATE", ""),
                       help=f"Ledger path. Default <root>/.{DEFAULT_LEDGER_NAME}. "
                            "Env: FARM_STATE")

    def run_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--write", action="store_true",
                       default=_env("FARM_WRITE", "").lower() in ("1", "true", "yes"),
                       help="Let the tools write their outputs. Off by default: a "
                            "scheduled batch that starts rating an archive because "
                            "a default flipped is the failure this avoids. "
                            "Env: FARM_WRITE")
        p.add_argument("--timeout", type=float,
                       default=float(_env("FARM_TIMEOUT", "3600")),
                       help="Seconds before one folder's tool is killed. Env: FARM_TIMEOUT")
        p.add_argument("--max-jobs", type=int,
                       default=int(_env("FARM_MAX_JOBS", "0")),
                       help="Cap folders processed per sweep, 0 for no cap. Env: FARM_MAX_JOBS")
        p.add_argument("--max-attempts", type=int,
                       default=int(_env("FARM_MAX_ATTEMPTS", "3")),
                       help="Failures before a folder is quarantined. Env: FARM_MAX_ATTEMPTS")
        p.add_argument("--metrics-port", type=int,
                       default=int(_env("FARM_METRICS_PORT", "0")),
                       help="Serve /metrics, /healthz, /readyz on this port. "
                            "0 disables. Env: FARM_METRICS_PORT")

    p_run = sub.add_parser("run", help="one sweep, then exit")
    common(p_run); run_flags(p_run)

    p_serve = sub.add_parser("serve", help="sweep forever on an interval")
    common(p_serve); run_flags(p_serve)
    p_serve.add_argument("--interval", type=float,
                         default=float(_env("FARM_INTERVAL", "900")),
                         help="Seconds between sweeps. Env: FARM_INTERVAL")

    p_status = sub.add_parser("status", help="what the ledger knows")
    common(p_status)

    p_retry = sub.add_parser("retry", help="clear a folder's record so it runs again")
    common(p_retry)
    p_retry.add_argument("folder", help="Folder path as it appears in status")

    p_doctor = sub.add_parser(
        "doctor", help="check this machine before trusting it with a schedule")
    common(p_doctor)
    p_doctor.add_argument("--write", action="store_true",
                          default=_env("FARM_WRITE", "").lower() in ("1", "true", "yes"),
                          help="Check the environment as it would be with writes on. "
                               "Env: FARM_WRITE")
    p_doctor.add_argument("--metrics-port", type=int,
                          default=int(_env("FARM_METRICS_PORT", "0")),
                          help="Also check this port is bindable. Env: FARM_METRICS_PORT")
    p_doctor.add_argument("--json", action="store_true",
                          help="One JSON object per check, for a probe rather than a person")

    return parser


def _resolve(args) -> tuple[Path, Path] | None:
    if not args.root:
        log("config_error", level="error",
            error="No media root. Pass --root or set FARM_ROOT.")
        return None
    root = Path(args.root).expanduser()
    if not root.is_dir():
        log("config_error", level="error", error=f"Not a directory: {root}")
        return None
    root = root.resolve()
    state = Path(args.state).expanduser() if args.state \
        else root / f".{DEFAULT_LEDGER_NAME}"
    return root, state


def cmd_run(args) -> int:
    resolved = _resolve(args)
    if resolved is None:
        return EXIT_CONFIG
    root, state = resolved
    ledger = Ledger(state, max_attempts=args.max_attempts)
    farm = Farm(root, ledger, write=args.write, timeout=args.timeout,
                max_jobs=args.max_jobs)
    farm.install_signal_handlers()
    if args.metrics_port:
        serve_metrics(args.metrics_port)
    result = farm.sweep()
    return EXIT_JOB_FAILURES if result.failed else EXIT_OK


def cmd_serve(args) -> int:
    resolved = _resolve(args)
    if resolved is None:
        return EXIT_CONFIG
    root, state = resolved
    ledger = Ledger(state, max_attempts=args.max_attempts)
    farm = Farm(root, ledger, write=args.write, timeout=args.timeout,
                max_jobs=args.max_jobs)
    farm.install_signal_handlers()
    if args.metrics_port:
        # Ready once a sweep has completed: before that the farm is alive
        # but has nothing to say, and a load balancer should know that.
        serve_metrics(args.metrics_port, ready_check=lambda: farm.last_sweep_at > 0)
    log("serve_started", root=str(root), interval_seconds=args.interval)
    while not farm._stopping:
        farm.sweep()
        deadline = time.monotonic() + args.interval
        while time.monotonic() < deadline and not farm._stopping:
            time.sleep(min(1.0, deadline - time.monotonic()))
    log("serve_stopped")
    return EXIT_OK


def cmd_status(args) -> int:
    resolved = _resolve(args)
    if resolved is None:
        return EXIT_CONFIG
    root, state = resolved
    ledger = Ledger(state)
    counts = ledger.counts()
    log("status", root=str(root), state_file=str(state), **counts)
    quarantined = ledger.quarantined()
    for path, record in quarantined:
        log("quarantined_folder", level="warn", folder=path,
            attempts=record.attempts, tool=record.tool, error=record.last_error)
    if not any(counts.values()):
        log("status_note", note="The ledger is empty. Nothing has been processed yet.")
    return EXIT_OK


def cmd_retry(args) -> int:
    resolved = _resolve(args)
    if resolved is None:
        return EXIT_CONFIG
    _root, state = resolved
    ledger = Ledger(state)
    target = str(Path(args.folder).expanduser().resolve()) \
        if Path(args.folder).exists() else args.folder
    if ledger.forget(target):
        ledger.save()
        log("record_cleared", folder=target,
            note="It will be processed on the next sweep.")
        return EXIT_OK
    log("record_not_found", level="warn", folder=target,
        note="Run status to see the folders the ledger knows about.")
    return EXIT_CONFIG


def cmd_doctor(args) -> int:
    """Diagnose before scheduling.

    Deliberately does not bail out when the root is wrong. A person running
    doctor wants every problem at once, not the first one: three round trips
    to fix three things is exactly the support experience this avoids."""
    root = Path(args.root).expanduser() if args.root else Path(".")
    if args.root:
        root = root.resolve() if root.exists() else root
    state = Path(args.state).expanduser() if args.state \
        else root / f".{DEFAULT_LEDGER_NAME}"

    discovered: int | None = None
    if root.is_dir() and os.access(root, os.R_OK | os.X_OK):
        try:
            discovered = len(discover(root))
        except OSError:
            discovered = None

    ctx = doctor.Context(root=root, state=state, write=args.write,
                         metrics_port=args.metrics_port)
    checks = doctor.run_checks(ctx, discovered)

    if args.json:
        for check in checks:
            log("doctor_check",
                level={"ok": "info", "warn": "warn", "fail": "error"}[check.status],
                check=check.name, status=check.status, detail=check.detail,
                fix=check.fix)
    else:
        if not args.root:
            print("No --root given, checking the tools and this machine only.\n")
        print(doctor.render(checks))

    workable, summary = doctor.verdict(checks)
    if args.json:
        log("doctor_verdict", level="info" if workable else "error",
            workable=workable, summary=summary)
    return EXIT_OK if workable else EXIT_CONFIG


def main(argv: list[str] | None = None) -> int:
    excepthook_to_log()
    args = build_parser().parse_args(argv)
    handlers = {
        "run": cmd_run, "serve": cmd_serve,
        "status": cmd_status, "retry": cmd_retry,
        "doctor": cmd_doctor,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        log("interrupted", level="warn")
        return 130


if __name__ == "__main__":
    sys.exit(main())
