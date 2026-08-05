"""Pre-flight checks, so a nightly batch fails at 9pm instead of at 3am.

Every scheduled job has the same first support ticket: it ran, it did nothing,
and the logs are technically complete and humanly useless. Almost always the
cause is environmental. A tool is not on PATH inside the container. The archive
is mounted read-only and the ledger cannot be written. `--write` is on against
a read-only volume, so every folder will fail identically forever. ffmpeg is
missing so video folders die and photo folders do not.

`shutter-farm doctor` asks those questions on purpose, before the schedule
does. Each check answers three things: what is true, whether that is fatal, and
the exact command that fixes it. A check that cannot tell the user what to type
is not finished.

The checks are pure functions over a Context. The environment is injected, so
the suite can build a machine that is broken in one specific way rather than
mutating the machine running the tests.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

OK = "ok"
WARN = "warn"
FAIL = "fail"

# The engines the farm dispatches to, and what each one needs on PATH beyond
# itself. The farm never imports them, so a missing engine is a PATH problem
# and not an install problem, which is a different fix.
ENGINES = {
    "shutter-cull": ("photo folders", ["exiftool"]),
    "shutter-select": ("video folders", ["ffmpeg", "ffprobe"]),
}

MIN_PYTHON = (3, 11)

# Enough headroom for one shoot's outputs. Below this a sweep can half-write a
# selects tree and leave a folder that looks done and is not.
LOW_DISK_GB = 2.0


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def fatal(self) -> bool:
        return self.status == FAIL


@dataclass
class Context:
    """Everything a check is allowed to look at.

    The callables are the seam. Real runs get the real ones, tests get a
    machine with exactly one thing wrong. Permissions go through an injected
    access() rather than os.access directly, because chmod-based tests skip
    themselves under a root user and CI containers usually run as root: a
    permission check that only runs on a laptop is not a tested check."""

    root: Path
    state: Path
    write: bool = False
    metrics_port: int = 0
    which: Callable[[str], str | None] = shutil.which
    access: Callable[[Path, int], bool] = os.access
    version_of: Callable[[str], tuple[int, str]] | None = None
    disk_free_gb: Callable[[Path], float] | None = None
    port_free: Callable[[int], bool] | None = None
    python_version: tuple[int, int] = field(default_factory=lambda: sys.version_info[:2])

    def __post_init__(self) -> None:
        if self.version_of is None:
            self.version_of = _real_version_of
        if self.disk_free_gb is None:
            self.disk_free_gb = _real_disk_free_gb
        if self.port_free is None:
            self.port_free = _real_port_free


def _real_version_of(binary: str) -> tuple[int, str]:
    """Answer one question: does this binary actually execute?

    Being on PATH is not the same as being runnable. A broken symlink, a wheel
    built for the wrong architecture and a container missing a shared library
    all pass a `which` check and fail on use.

    `--version` is the probe, but a non-zero exit from it does not mean the
    binary is broken. Plenty of CLIs, including one in this very toolchain,
    have a required subcommand and exit 2 on a bare `--version`. So a failing
    `--version` falls back to `--help`: if that runs, the binary runs, and the
    only honest thing to report is that it does not announce a version.
    Reporting "it will not run" for a tool that runs fine is the worst failure
    a diagnostic can have, because it sends someone to reinstall a working
    install."""
    def attempt(flag: str) -> tuple[int, str]:
        try:
            proc = subprocess.run([binary, flag], capture_output=True,
                                  text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, str(exc)
        text = (proc.stdout or proc.stderr).strip().splitlines()
        return proc.returncode, text[0] if text else ""

    code, line = attempt("--version")
    if code == 0:
        return 0, line
    help_code, help_line = attempt("--help")
    if help_code == 0:
        return 0, f"{binary}, installed, does not report a version"
    return help_code or code, help_line or line


def _real_disk_free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / (1024 ** 3)


def _real_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
            return True
        except OSError:
            return False


# ---------------------------------------------------------------- the checks

def check_python(ctx: Context) -> Check:
    have = ".".join(str(n) for n in ctx.python_version)
    want = ".".join(str(n) for n in MIN_PYTHON)
    if ctx.python_version >= MIN_PYTHON:
        return Check("python", OK, f"Python {have}")
    return Check("python", FAIL, f"Python {have}, but this needs {want} or newer",
                 fix=f"Install Python {want}+ and reinstall: pip install shutter-farm")


def check_root(ctx: Context) -> Check:
    if not ctx.root.exists():
        return Check("media root", FAIL, f"{ctx.root} does not exist",
                     fix="Check --root, or FARM_ROOT, or the volume mount path "
                         "in your manifest. In a container this is usually a "
                         "volume that did not mount.")
    if not ctx.root.is_dir():
        return Check("media root", FAIL, f"{ctx.root} is not a directory",
                     fix="Point --root at the folder that holds your shoots.")
    if not ctx.access(ctx.root, os.R_OK | os.X_OK):
        return Check("media root", FAIL, f"{ctx.root} is not readable",
                     fix=f"chmod +rx {ctx.root}, or run as a user that can read it. "
                         "In Kubernetes check fsGroup against the volume's owner.")
    return Check("media root", OK, f"{ctx.root} is readable")


def check_jobs(ctx: Context, discovered: int | None) -> Check:
    if discovered is None:
        return Check("work found", WARN, "Could not scan the root",
                     fix="Fix the media root above first.")
    if discovered == 0:
        return Check("work found", WARN,
                     "No folders with media in them under the root",
                     fix="Folders are skipped when they are hidden, start with an "
                         "underscore, or are named 'do not include' or 'do not use'. "
                         "An archive of archives needs --root pointed one level down.")
    return Check("work found", OK, f"{discovered} folder(s) with media")


def check_ledger(ctx: Context) -> Check:
    """The single most common way a scheduled sweep silently repeats itself.

    A read-only archive is good practice, and it means the ledger cannot live
    inside the archive. Without a ledger every sweep redoes everything, which
    looks like the tool ignoring its own idempotency."""
    parent = ctx.state.parent
    if ctx.state.exists():
        if ctx.access(ctx.state, os.W_OK):
            return Check("ledger", OK, f"{ctx.state} exists and is writable")
        return Check("ledger", FAIL, f"{ctx.state} exists but is not writable",
                     fix=f"chmod +w {ctx.state}, or move the ledger onto a writable "
                         "volume with --state or FARM_STATE.")
    if not parent.exists():
        return Check("ledger", FAIL, f"{parent} does not exist, so the ledger cannot be created",
                     fix=f"mkdir -p {parent}, or point --state somewhere that does exist.")
    if not ctx.access(parent, os.W_OK):
        return Check("ledger", FAIL, f"Cannot create {ctx.state}, {parent} is read-only",
                     fix="Put the ledger on its own writable volume: "
                         "--state /state/shutter-farm-state.json. This is the normal "
                         "setup when the archive is mounted read-only, which it should be.")
    return Check("ledger", OK, f"{ctx.state} will be created on the first sweep")


def check_write_mode(ctx: Context) -> Check:
    """--write against a read-only root fails every folder, identically, forever.

    Worth its own check because the failure is loud, repetitive and completely
    uninformative in the logs: two hundred folders, one error each, all the
    same errno."""
    if not ctx.write:
        return Check("write mode", OK,
                     "Writes are off. The tools will analyze and report without "
                     "touching the archive.")
    if not ctx.root.exists():
        return Check("write mode", WARN, "Writes are on but the root check failed",
                     fix="Fix the media root above first.")
    if not ctx.access(ctx.root, os.W_OK):
        return Check("write mode", FAIL,
                     f"--write is on but {ctx.root} is read-only, so every folder "
                     "will fail the same way",
                     fix="Either drop --write and FARM_WRITE, or mount the archive "
                         "read-write. In docker-compose remove the :ro suffix, in "
                         "Kubernetes set readOnly: false on the volumeMount.")
    return Check("write mode", WARN,
                 f"--write is on and {ctx.root} is writable. The tools will write "
                 "sidecars and selects trees into your archive.",
                 fix="Intended? Then nothing to do. Look at a dry sweep first if not.")


def check_engine(ctx: Context, engine: str) -> list[Check]:
    handles, needs = ENGINES[engine]
    path = ctx.which(engine)
    if path is None:
        return [Check(engine, WARN, f"Not on PATH, so {handles} will be skipped",
                      fix=f"pip install git+https://github.com/keivanmalhani/{engine}.git "
                          "  In the container, check that ENGINE_REF built and that "
                          "/opt/venv/bin is on PATH.")]

    code, line = ctx.version_of(engine)
    if code != 0:
        return [Check(engine, FAIL,
                      f"Found at {path} but it will not run: {line[:120]}",
                      fix="Reinstall it in this environment. Being on PATH is not "
                          "the same as being runnable, and this is what a broken "
                          "wheel or a missing shared library looks like.")]

    checks = [Check(engine, OK, f"{line or path}, handles {handles}")]
    for binary in needs:
        if ctx.which(binary) is None:
            checks.append(Check(f"{engine} needs {binary}", FAIL,
                                f"{binary} is not on PATH, so {engine} will fail on "
                                "every folder it picks up",
                                fix=f"brew install {binary}, or apt-get install -y "
                                    f"{binary}. The container image already has it, "
                                    "so seeing this means you are running outside it."))
        else:
            checks.append(Check(f"{engine} needs {binary}", OK, "present"))
    return checks


def check_disk(ctx: Context) -> Check:
    free = ctx.disk_free_gb(ctx.state.parent)
    if free < LOW_DISK_GB:
        return Check("disk space", FAIL,
                     f"{free:.1f} GB free where outputs and the ledger go",
                     fix="Free space before the next sweep. A sweep that runs out "
                         "part way leaves a folder that looks finished and is not.")
    return Check("disk space", OK, f"{free:.1f} GB free")


def check_metrics_port(ctx: Context) -> Check:
    if not ctx.metrics_port:
        return Check("metrics port", OK, "Not requested")
    if ctx.port_free(ctx.metrics_port):
        return Check("metrics port", OK, f"Port {ctx.metrics_port} is free")
    return Check("metrics port", FAIL, f"Port {ctx.metrics_port} is already in use",
                 fix=f"Pick another with --metrics-port, or stop whatever holds "
                     f"{ctx.metrics_port}. In compose, check the ports mapping.")


def run_checks(ctx: Context, discovered: int | None = None) -> list[Check]:
    checks = [check_python(ctx), check_root(ctx), check_jobs(ctx, discovered),
              check_ledger(ctx), check_write_mode(ctx)]
    for engine in ENGINES:
        checks.extend(check_engine(ctx, engine))
    checks.append(check_disk(ctx))
    checks.append(check_metrics_port(ctx))
    return checks


def both_engines_missing(checks: list[Check]) -> bool:
    """Neither engine present means a sweep can only ever do nothing, which is
    a failure even though each individual engine is only a warning."""
    return all(c.status == WARN for c in checks if c.name in ENGINES)


def verdict(checks: list[Check]) -> tuple[bool, str]:
    """The one place that decides whether a sweep will work.

    Both the printed summary and the exit code come from here, because the
    first version computed them separately and printed "a sweep will run"
    immediately above the reason it would not, then exited non-zero. A
    diagnostic that contradicts itself is worse than no diagnostic."""
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)

    if fails:
        return False, (f"{fails} blocking problem(s), {warns} warning(s). A sweep "
                       "will not work until the blocking ones are fixed.")
    if both_engines_missing(checks):
        return False, (f"Neither engine is installed, so a sweep would find work and "
                       f"then skip all of it. {warns} warning(s), and that one is "
                       "blocking in practice.")
    if warns:
        return True, f"No blocking problems, {warns} warning(s). A sweep will run."
    return True, "Everything checks out. A sweep will run."


def render(checks: list[Check]) -> str:
    """Human first. This is the output someone pastes into a support thread,
    so it has to be readable without a JSON parser."""
    marks = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    width = max(len(c.name) for c in checks)
    indent = " " * (width + 9)
    lines = []
    for check in checks:
        lines.append(f"  [{marks[check.status]}] {check.name.ljust(width)}  {check.detail}")
        if check.fix and check.status != OK:
            for i, part in enumerate(_wrap(check.fix, 68)):
                lines.append(f"{indent}{'-> ' if i == 0 else '   '}{part}")
    lines.append("")
    for part in _wrap(verdict(checks)[1], 74):
        lines.append(f"  {part}")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines
