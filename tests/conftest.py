"""Fixtures. A fake media root and a fake tool runner.

No real tools are invoked. What the farm is made of is discovery,
idempotency, failure isolation and observability, and all four are
testable without OpenCV, ffmpeg or a single real photo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shutter_farm.runner import RunOutcome
from shutter_farm.state import Ledger


def make_media(folder: Path, names: list[str], size: int = 2048) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        (folder / name).write_bytes(bytes([i % 256]) * size)


@pytest.fixture()
def archive(tmp_path) -> Path:
    """A realistic media root: two photo shoots, one video shoot, plus noise."""
    root = tmp_path / "archive"
    make_media(root / "2026-04-canyon", ["DSC0001.ARW", "DSC0002.ARW", "DSC0003.ARW"])
    make_media(root / "2026-05-studio", ["DSC1001.ARW", "DSC1002.ARW"])
    make_media(root / "2026-06-interview", ["A001.MOV", "A002.MOV"])
    # Noise that must never become a job:
    make_media(root / "_phone-ready", ["out.MOV", "out2.MOV"])       # an output tree
    make_media(root / "DO NOT INCLUDE THESE", ["client.ARW", "c2.ARW"])
    make_media(root / "docs", ["notes.txt", "invoice.pdf"])          # no media
    make_media(root / "2026-07-single", ["DSC9999.ARW"])             # below the floor
    return root


@pytest.fixture()
def ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "state.json")


class FakeRun:
    """Stands in for runner.run_job. Scriptable per folder name."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.fail: dict[str, str] = {}
        self.timeout: set[str] = set()
        self.raise_missing: set[str] = set()
        self.duration = 0.25

    def __call__(self, job, *, write=False, timeout=3600.0, extra=None):
        from shutter_farm.runner import ToolMissing

        self.calls.append((job.folder.name, write))
        if job.folder.name in self.raise_missing:
            raise ToolMissing(f"{job.tool} is not on PATH in this environment.")
        if job.folder.name in self.timeout:
            return RunOutcome(False, job.tool, None, self.duration, "hung", timed_out=True)
        if job.folder.name in self.fail:
            return RunOutcome(False, job.tool, 1, self.duration,
                              self.fail[job.folder.name])
        return RunOutcome(True, job.tool, 0, self.duration, "ok")


@pytest.fixture()
def fake_run() -> FakeRun:
    return FakeRun()
