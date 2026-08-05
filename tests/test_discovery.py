from __future__ import annotations

from shutter_farm.discovery import Job, classify, discover, skip_dir
from tests.conftest import make_media


def names(jobs: list[Job]) -> list[str]:
    return [j.folder.name for j in jobs]


def test_discovery_finds_the_real_shoots_and_nothing_else(archive):
    jobs = discover(archive)
    assert set(names(jobs)) == {"2026-04-canyon", "2026-05-studio", "2026-06-interview"}
    assert names(jobs) == sorted(names(jobs))  # stable order for stable logs


def test_output_trees_are_never_work(archive):
    # An underscore folder is an output tree by family convention. If the
    # farm treated one as a job it would process its own results forever.
    assert "_phone-ready" not in names(discover(archive))


def test_do_not_include_folders_are_respected(archive):
    assert "DO NOT INCLUDE THESE" not in names(discover(archive))


def test_folders_without_media_are_not_jobs(archive):
    assert "docs" not in names(discover(archive))


def test_a_single_file_is_below_the_floor(archive):
    assert "2026-07-single" not in names(discover(archive))
    # ...but the floor is configurable for someone who wants it.
    assert "2026-07-single" in names(discover(archive, min_media=1))


def test_kinds_and_tools_are_assigned(archive):
    by_name = {j.folder.name: j for j in discover(archive)}
    assert by_name["2026-04-canyon"].kind == "photo"
    assert by_name["2026-04-canyon"].tool == "shutter-cull"
    assert by_name["2026-06-interview"].kind == "video"
    assert by_name["2026-06-interview"].tool == "shutter-select"


def test_a_mixed_folder_is_a_video_job(tmp_path):
    folder = tmp_path / "mixed"
    make_media(folder, ["a.MOV", "b.MOV", "c.ARW"])
    kind, count = classify(sorted(folder.iterdir()))
    assert kind == "video" and count == 2


def test_nested_shoots_are_separate_jobs(tmp_path):
    root = tmp_path / "root"
    make_media(root / "trip" / "day1", ["a.ARW", "b.ARW"])
    make_media(root / "trip" / "day2", ["c.ARW", "d.ARW"])
    assert set(names(discover(root))) == {"day1", "day2"}


def test_symlinked_directories_are_not_followed(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "someone_elses_archive"
    make_media(root / "mine", ["a.ARW", "b.ARW"])
    make_media(outside / "theirs", ["x.ARW", "y.ARW"])
    (root / "link").symlink_to(outside)
    assert set(names(discover(root))) == {"mine"}


def test_skip_dir_rules():
    assert skip_dir("_selects") and skip_dir(".git")
    assert skip_dir("DO NOT INCLUDE THESE (CLAUDE)")
    assert skip_dir("please do not use")
    assert not skip_dir("2026-04-canyon")
