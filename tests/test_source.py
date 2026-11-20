import os

import pytest

from dropbox_mirror.source import (
    NoGlobMatches,
    inventory_source,
    is_excluded,
    protected_remote_path,
)


def test_recursive_inventory_includes_hidden_and_empty_directories(tmp_path, make_job):
    root = tmp_path / "source"
    (root / ".hidden").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / ".hidden" / "secret.txt").write_text("private", encoding="utf-8")
    (root / "visible.txt").write_text("visible", encoding="utf-8")

    inventory = inventory_source(make_job(root))

    assert set(inventory.files) == {".hidden/secret.txt", "visible.txt"}
    assert "empty" in inventory.directories
    assert inventory.examined == 4


def test_exclusion_grammar_matches_names_and_protects_subtrees(tmp_path, make_job):
    root = tmp_path / "source"
    (root / "data" / "cache").mkdir(parents=True)
    (root / "data" / "cache" / "old.tmp").write_text("ignored", encoding="utf-8")
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    job = make_job(root, exclusions=("*.tmp", "data/cache/"))

    inventory = inventory_source(job)

    assert set(inventory.files) == {"keep.txt"}
    assert is_excluded("nested/x.tmp", False, ("*.tmp",))
    assert is_excluded("x/cache/item", False, ("cache/",))
    assert is_excluded("data/cache/item", False, ("data/cache/",))
    assert protected_remote_path("data", True, inventory, job.exclusions)
    assert protected_remote_path("data/cache/old.tmp", False, inventory, job.exclusions)


def test_symlink_is_skipped_and_protects_remote_path(tmp_path, make_job, caplog):
    root = tmp_path / "source"
    root.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("outside", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is not available on this platform")
    job = make_job(root)

    inventory = inventory_source(job)

    assert "link.txt" not in inventory.files
    assert "link.txt" in inventory.protected
    assert protected_remote_path("link.txt", False, inventory, job.exclusions)
    assert "symbolic link" in caplog.text


def test_glob_matches_only_regular_files_and_zero_matches_skip(tmp_path, make_job):
    root = tmp_path / "reports"
    root.mkdir()
    (root / "a.pdf").write_text("pdf", encoding="utf-8")
    (root / "b.txt").write_text("text", encoding="utf-8")
    job = make_job(root / "*.pdf", source_type="glob", destination="/reports")

    inventory = inventory_source(job)
    assert set(inventory.files) == {"a.pdf"}

    no_match = make_job(root / "*.csv", source_type="glob", destination="/csv")
    with pytest.raises(NoGlobMatches):
        inventory_source(no_match)


@pytest.mark.skipif(os.name == "nt", reason="FIFO objects are not available on Windows")
def test_special_files_are_skipped_and_protected(tmp_path, make_job):
    root = tmp_path / "source"
    root.mkdir()
    fifo = root / "pipe"
    os.mkfifo(fifo)
    inventory = inventory_source(make_job(root))
    assert "pipe" in inventory.protected
    assert "pipe" not in inventory.files
