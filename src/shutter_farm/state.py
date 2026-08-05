"""The ledger. What makes a scheduled batch safe to run every hour.

A cron job that reprocesses everything every time is not a pipeline, it is
a space heater. The farm has to answer one question cheaply and correctly:
has this folder already been done, in the state it is in right now?

Timestamps alone are the obvious answer and the wrong one. A folder's
mtime changes when anything inside it is touched, including by the tools
the farm itself just ran. Content is the right key: a fingerprint over
every media file's name, size and mtime. Add a photo and the fingerprint
changes, so the folder is work again. Copy the folder somewhere else and
it is a different job, correctly. Run the tool and write outputs into a
subfolder the discovery skips, and the fingerprint does not move, so the
next scheduled run does nothing.

Failures are recorded per folder, never per run. One unreadable card does
not fail a nightly batch of two hundred shoots. A folder that fails is
retried with exponential backoff, and after enough attempts it is
quarantined with its last error, so it stops burning cycles and starts
being visible instead.

The ledger is a single JSON file written atomically. It is state, not a
database: if it is lost the farm reprocesses, which is wasteful but never
wrong, and that is the correct failure direction for something that runs
unattended.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shutter_farm.discovery import PHOTO_EXTS, VIDEO_EXTS, Job

SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 300.0


@dataclass
class Record:
    """One folder's history."""

    fingerprint: str
    status: str  # "done" | "failed" | "quarantined"
    tool: str
    attempts: int = 0
    last_error: str = ""
    last_run_at: float = 0.0
    duration_seconds: float = 0.0
    outputs: list[str] = field(default_factory=list)


def fingerprint(job: Job) -> str:
    """Content-address a folder over its media files' names, sizes and mtimes.

    Only media is hashed. Outputs the tools write, sidecars, timelines,
    reports, live beside or under the folder and must not make the folder
    look like new work, or every scheduled run would redo the last one.
    """
    digest = hashlib.sha256()
    digest.update(str(job.folder).encode("utf-8"))
    entries = []
    try:
        for child in job.folder.iterdir():
            if not child.is_file() or child.name.startswith("."):
                continue
            if child.suffix.lower() not in (PHOTO_EXTS | VIDEO_EXTS):
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(f"{child.name}|{stat.st_size}|{stat.st_mtime:.3f}")
    except OSError:
        return "unreadable"
    for entry in sorted(entries):
        digest.update(b"\0")
        digest.update(entry.encode("utf-8"))
    return digest.hexdigest()[:24]


class Ledger:
    """Load, query, and atomically persist the farm's record of work."""

    def __init__(self, path: Path, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self.path = path
        self.max_attempts = max_attempts
        self._records: dict[str, Record] = {}
        self._load()

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # A missing or corrupt ledger means redo, never wrong.
        if raw.get("schema_version") != SCHEMA_VERSION:
            return
        for key, value in (raw.get("records") or {}).items():
            try:
                self._records[key] = Record(**value)
            except TypeError:
                continue

    def save(self) -> None:
        """Atomic write. A crash mid-save must not leave a corrupt ledger."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "records": {k: asdict(v) for k, v in self._records.items()},
        }
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".part")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=1, ensure_ascii=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- queries

    @staticmethod
    def key(job: Job) -> str:
        return str(job.folder)

    def get(self, job: Job) -> Record | None:
        return self._records.get(self.key(job))

    def should_run(self, job: Job, now: float) -> tuple[bool, str]:
        """Decide whether to process a job now, and say why either way."""
        record = self.get(job)
        current = fingerprint(job)

        if record is None:
            return True, "never processed"
        if record.fingerprint != current:
            return True, "contents changed since the last run"
        if record.status == "done":
            return False, "already done and unchanged"
        if record.status == "quarantined":
            return False, (
                f"quarantined after {record.attempts} attempts: {record.last_error}"
            )
        # failed: back off exponentially so a broken folder does not eat
        # every scheduled run, but never give up silently.
        wait = BACKOFF_BASE_SECONDS * (2 ** max(0, record.attempts - 1))
        if now - record.last_run_at < wait:
            remaining = int(wait - (now - record.last_run_at))
            return False, f"failed, backing off another {remaining}s"
        return True, f"retrying after {record.attempts} failed attempts"

    # ---------------------------------------------------------------- updates

    def record_success(
        self, job: Job, *, duration: float, now: float, outputs: list[str]
    ) -> None:
        self._records[self.key(job)] = Record(
            fingerprint=fingerprint(job),
            status="done",
            tool=job.tool,
            attempts=0,
            last_error="",
            last_run_at=now,
            duration_seconds=round(duration, 2),
            outputs=outputs,
        )

    def record_failure(self, job: Job, *, error: str, now: float) -> Record:
        previous = self.get(job)
        attempts = (previous.attempts + 1) if previous else 1
        status = "quarantined" if attempts >= self.max_attempts else "failed"
        record = Record(
            fingerprint=fingerprint(job),
            status=status,
            tool=job.tool,
            attempts=attempts,
            last_error=error[:500],
            last_run_at=now,
        )
        self._records[self.key(job)] = record
        return record

    # ---------------------------------------------------------------- summary

    def counts(self) -> dict[str, int]:
        out = {"done": 0, "failed": 0, "quarantined": 0}
        for record in self._records.values():
            out[record.status] = out.get(record.status, 0) + 1
        return out

    def quarantined(self) -> list[tuple[str, Record]]:
        return [
            (key, rec)
            for key, rec in sorted(self._records.items())
            if rec.status == "quarantined"
        ]

    def forget(self, folder: str) -> bool:
        """Clear one folder's record so it will be reprocessed. For operators."""
        return self._records.pop(folder, None) is not None
