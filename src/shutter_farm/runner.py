"""Dispatch. Run the right tool over a folder as a subprocess, safely.

The farm shells out rather than importing the tools, for the same reason
shutter-clip shells out to shutter-select: the tools keep their own
dependency stacks and release cadences, and a batch runner that imports
three heavy packages is a batch runner that breaks when any one of them
does.

Everything here is about surviving a long unattended run:

- Every invocation has a timeout. A hung ffmpeg on one corrupt file must
  not stall a nightly batch forever.
- Output is captured and truncated, because a tool that goes haywire can
  emit gigabytes and the ledger is not a log sink.
- Commands are built as argument lists, never shell strings. A folder
  named with a quote in it is a normal thing on a photographer's drive
  and must not be an injection.
- The process group is killed on timeout, not just the child, so ffmpeg
  subprocesses do not survive their parent.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from shutter_farm.discovery import Job

DEFAULT_TIMEOUT_SECONDS = 3600.0
MAX_CAPTURED_OUTPUT = 8000


class ToolMissing(Exception):
    """The tool a job needs is not installed in this image."""


@dataclass
class RunOutcome:
    ok: bool
    tool: str
    exit_code: int | None
    duration: float
    output: str
    timed_out: bool = False

    @property
    def error(self) -> str:
        if self.timed_out:
            return f"{self.tool} timed out"
        if not self.ok:
            tail = self.output.strip().splitlines()[-3:]
            return f"{self.tool} exited {self.exit_code}: " + " / ".join(tail)
        return ""


def command_for(job: Job, *, write: bool, extra: list[str] | None = None) -> list[str]:
    """The exact argv for a job. Both tools default to not touching disk.

    shutter-cull needs --write before it writes sidecars, and
    shutter-select writes only under its own _selects output tree. The
    farm passes write through rather than deciding on the operator's
    behalf: a scheduled batch that starts rating a client archive because
    a default flipped is exactly the failure this design avoids.
    """
    if job.kind == "photo":
        cmd = ["shutter-cull", "cull", str(job.folder)]
        if write:
            cmd.append("--write")
    else:
        cmd = ["shutter-select", "run" if write else "analyze", str(job.folder)]
    if extra:
        cmd.extend(extra)
    return cmd


def tool_available(job: Job) -> bool:
    return shutil.which(job.tool) is not None


def run_job(
    job: Job,
    *,
    write: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    extra: list[str] | None = None,
    runner=subprocess.Popen,
) -> RunOutcome:
    """Run one job. Never raises for a tool failure: that is data, not an error."""
    if not tool_available(job):
        raise ToolMissing(
            f"{job.tool} is not on PATH in this environment. The farm "
            "dispatches to the tools, it does not contain them; install "
            f"{job.tool} in the image or on the host."
        )

    cmd = command_for(job, write=write, extra=extra)
    started = time.monotonic()
    # start_new_session puts the child in its own process group so a
    # timeout can take down whatever it spawned, not just the child.
    proc = runner(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            output, _ = proc.communicate(timeout=30)
        except Exception:
            output = ""
    duration = time.monotonic() - started

    output = (output or "")[-MAX_CAPTURED_OUTPUT:]
    code = proc.returncode
    return RunOutcome(
        ok=(not timed_out and code == 0),
        tool=job.tool,
        exit_code=code,
        duration=duration,
        output=output,
        timed_out=timed_out,
    )


def _kill_group(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass
    time.sleep(2.0)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass


def outputs_of(job: Job) -> list[str]:
    """Where a finished job's results live. Reported, never parsed."""
    if job.kind == "photo":
        return [str(job.folder) + "/*.xmp"]
    return [str(job.folder / "_selects")]
