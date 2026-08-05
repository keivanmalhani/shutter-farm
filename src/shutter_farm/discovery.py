"""Find the work. A folder of media plus the tool that should process it.

The farm does not analyze anything itself. It decides which folders are
jobs and which tool owns each one, then hands off. Everything about what
a good frame or a good take is lives in shutter-cull and shutter-select,
which is where it belongs and where it is tested.

A folder is a job when it directly contains media the farm recognizes.
Nesting is handled by treating each media-bearing folder as its own job
rather than by trying to guess a shoot's boundaries: a photographer's
folder structure is their business, and a runner that redraws it will be
wrong on somebody's archive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PHOTO_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".raf", ".dng", ".jpg", ".jpeg"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mxf", ".mts", ".m2ts", ".avi", ".mkv"}

# Skip rules ported from the family. Output trees start with "_", so the
# farm never treats its own or another tool's output as new work.
SKIP_MARKERS = ("do not include", "do not use")

MIN_MEDIA_FILES = 2


@dataclass(frozen=True)
class Job:
    """One folder and the tool that owns it."""

    folder: Path
    kind: str  # "photo" or "video"
    media_count: int

    @property
    def tool(self) -> str:
        return "shutter-cull" if self.kind == "photo" else "shutter-select"

    @property
    def name(self) -> str:
        return self.folder.name


def skip_dir(name: str) -> bool:
    lowered = name.casefold()
    if name.startswith(".") or name.startswith("_"):
        return True
    return any(marker in lowered for marker in SKIP_MARKERS)


def classify(paths: list[Path]) -> tuple[str | None, int]:
    """Decide a folder's kind by which media dominates it."""
    photos = sum(1 for p in paths if p.suffix.lower() in PHOTO_EXTS)
    videos = sum(1 for p in paths if p.suffix.lower() in VIDEO_EXTS)
    if photos == 0 and videos == 0:
        return None, 0
    # A folder with both is a video job: shutter-select ignores stills, and
    # running the photo culler over a folder of video would be a no-op that
    # still costs a full walk. Mixed folders are rare and this is the
    # cheaper way to be wrong.
    if videos >= photos:
        return "video", videos
    return "photo", photos


def discover(root: Path, min_media: int = MIN_MEDIA_FILES) -> list[Job]:
    """Walk root and return one Job per media-bearing folder.

    Symlinked directories are not followed, so a link inside the work root
    cannot pull an unrelated archive into a scheduled batch.
    """
    jobs: list[Job] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d))
        here = Path(dirpath)
        media = [
            here / f
            for f in filenames
            if not f.startswith(".")
            and (here / f).suffix.lower() in (PHOTO_EXTS | VIDEO_EXTS)
        ]
        if len(media) < min_media:
            continue
        kind, count = classify(media)
        if kind is None:
            continue
        jobs.append(Job(folder=here, kind=kind, media_count=count))
    return sorted(jobs, key=lambda j: str(j.folder))
