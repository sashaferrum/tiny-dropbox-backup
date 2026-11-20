import hashlib
import io
import threading
import pytest

from conftest import FakeStore
from dropbox_mirror.mirror import (
    RemoteError,
    RemoteNotFound,
    RetryableRemoteError,
    _retry,
    dropbox_content_hash,
    mirror_job,
)
from dropbox_mirror.source import Inventory, SourceError, _add_file, inventory_source


def expected_hash(data):
    block_hashes = b"".join(
        hashlib.sha256(data[index:index + 4 * 1024 * 1024]).digest()
        for index in range(0, len(data), 4 * 1024 * 1024)
    )
    return hashlib.sha256(block_hashes).hexdigest()


@pytest.mark.parametrize(
    "content",
    [b"", b"small", b"x" * (4 * 1024 * 1024), b"x" * (4 * 1024 * 1024 + 17)],
    ids=["empty", "small", "block", "block-plus-one"],
)
def test_dropbox_content_hash_vectors(content):
    assert dropbox_content_hash(io.BytesIO(content)) == expected_hash(content)


def test_directory_mapping_updates_unchanged_files_and_deletes_remote_only(tmp_path, make_job):
    source = tmp_path / "documents"
    source.mkdir()
    (source / "notes.txt").write_bytes(b"new")
    (source / "sub").mkdir()
    (source / "sub" / "same.txt").write_bytes(b"same")
    store = FakeStore("/documents")
    store.add_folder("")
    store.add_file("sub/same.txt", b"same")
    store.add_file("old.txt", b"old")

    result = mirror_job(make_job(source, destination="/documents"), store)

    assert result.outcome == "success"
    assert result.files_uploaded == 1
    assert result.files_unchanged == 1
    assert result.files_deleted == 1
    assert store.contents["notes.txt"] == b"new"
    assert "old.txt" not in store.entries
    assert ("upload", "notes.txt") in store.operations
    assert not any(operation[0] == "delete" and operation[1].endswith("notes.txt")
                   for operation in store.operations)


def test_individual_file_maps_to_exact_remote_file_and_overwrites(tmp_path, make_job):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"updated")
    store = FakeStore("/reports/report.pdf")
    store.add_file("", b"manual remote change")

    result = mirror_job(make_job(source, source_type="file", destination="/reports/report.pdf"), store)

    assert result.files_uploaded == 1
    assert store.contents[""] == b"updated"
    assert [op[0] for op in store.operations].count("delete") == 0


def test_glob_files_map_by_basename(tmp_path, make_job):
    source = tmp_path / "reports"
    source.mkdir()
    (source / "one.pdf").write_bytes(b"one")
    (source / "two.pdf").write_bytes(b"two")
    store = FakeStore("/pdf-reports")
    job = make_job(source / "*.pdf", source_type="glob", destination="/pdf-reports")

    result = mirror_job(job, store)

    assert result.files_uploaded == 2
    assert store.contents == {"one.pdf": b"one", "two.pdf": b"two"}


def test_valid_empty_directory_clears_contents_but_preserves_destination(tmp_path, make_job):
    source = tmp_path / "empty"
    source.mkdir()
    store = FakeStore("/documents")
    store.add_folder("")
    store.add_folder("old")
    store.add_file("old/unused.txt", b"unused")
    store.add_file("stale.txt", b"stale")

    result = mirror_job(make_job(source, destination="/documents"), store)

    assert result.outcome == "success"
    assert result.files_deleted == 2
    assert "stale.txt" not in store.entries
    assert "old" not in store.entries
    assert "" in store.entries


def test_zero_match_glob_skips_without_listing_or_cleanup(tmp_path, make_job, caplog):
    source = tmp_path / "reports"
    source.mkdir()
    store = FakeStore("/pdf-reports")
    store.add_file("old.pdf", b"old")
    job = make_job(source / "*.pdf", source_type="glob", destination="/pdf-reports")

    result = mirror_job(job, store)

    assert result.outcome == "success"
    assert result.cleanup_skipped
    assert "old.pdf" in store.entries
    assert not any(operation[0] == "list" for operation in store.operations)
    assert "skipped" in caplog.text


def test_missing_source_preserves_remote_files(tmp_path, make_job):
    missing = tmp_path / "missing"
    store = FakeStore("/documents")
    store.add_file("important.txt", b"important")

    result = mirror_job(make_job(missing, destination="/documents"), store)

    assert result.outcome == "failed"
    assert result.cleanup_skipped
    assert "important.txt" in store.entries


def test_listing_failure_blocks_deletions(tmp_path, make_job):
    source = tmp_path / "source"
    source.mkdir()
    store = FakeStore("/documents")
    store.add_file("stale.txt", b"stale")
    store.list_error = RuntimeError("incomplete pagination")

    result = mirror_job(make_job(source, destination="/documents"), store)

    assert result.cleanup_skipped
    assert "stale.txt" in store.entries
    assert not any(operation[0] == "delete" for operation in store.operations)


def test_incomplete_local_enumeration_blocks_deletions(tmp_path, make_job, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    store = FakeStore("/documents")
    store.add_file("stale.txt", b"stale")

    def permission_error(_):
        raise PermissionError("cannot enumerate")

    monkeypatch.setattr("dropbox_mirror.source._read_directory", permission_error)
    result = mirror_job(make_job(source, destination="/documents"), store)

    assert result.outcome == "failed"
    assert result.cleanup_skipped
    assert "stale.txt" in store.entries
    assert not any(operation[0] == "list" for operation in store.operations)


def test_one_failed_upload_continues_other_files_and_blocks_all_cleanup(tmp_path, make_job):
    source = tmp_path / "source"
    source.mkdir()
    (source / "bad.txt").write_bytes(b"bad")
    (source / "good.txt").write_bytes(b"good")
    store = FakeStore("/documents")
    store.fail_uploads.add("bad.txt")
    store.add_file("stale.txt", b"stale")

    result = mirror_job(make_job(source, destination="/documents"), store)

    assert result.outcome == "partial"
    assert result.cleanup_skipped
    assert result.files_uploaded == 1
    assert store.contents["good.txt"] == b"good"
    assert "stale.txt" in store.entries
    assert not any(operation[0] == "delete" for operation in store.operations)


def test_excluded_absent_path_and_its_ancestor_are_protected(tmp_path, make_job):
    source = tmp_path / "source"
    source.mkdir()
    store = FakeStore("/documents")
    store.add_folder("data")
    store.add_folder("data/cache")
    store.add_file("data/cache/keep.tmp", b"keep")
    job = make_job(source, destination="/documents", exclusions=("data/cache/",))

    result = mirror_job(job, store)

    assert result.outcome == "success"
    assert "data" in store.entries
    assert "data/cache" in store.entries
    assert "data/cache/keep.tmp" in store.entries
    assert not any(op[0] == "delete" for op in store.operations)


def test_absent_basename_exclusion_protects_remote_file_and_ancestor(tmp_path, make_job):
    source = tmp_path / "source"
    source.mkdir()
    store = FakeStore("/documents")
    store.add_folder("archive")
    store.add_file("archive/keep.tmp", b"excluded")
    store.add_file("archive/remove.txt", b"managed")
    job = make_job(source, destination="/documents", exclusions=("*.tmp",))

    result = mirror_job(job, store)

    assert result.outcome == "success"
    assert "archive" in store.entries
    assert "archive/keep.tmp" in store.entries
    assert "archive/remove.txt" not in store.entries


def test_symlink_protects_remote_path_and_parent_directory(tmp_path, make_job):
    source = tmp_path / "source"
    (source / "folder").mkdir(parents=True)
    try:
        (source / "folder" / "link").symlink_to(tmp_path / "missing-target")
    except OSError:
        pytest.skip("symlink creation is not available on this platform")
    store = FakeStore("/documents")
    store.add_folder("folder")
    store.add_file("folder/link", b"preserve")

    result = mirror_job(make_job(source, destination="/documents"), store)

    assert result.outcome == "success"
    assert "folder" in store.entries
    assert "folder/link" in store.entries


def test_shutdown_finishes_current_file_but_starts_no_next_file_or_cleanup(tmp_path, make_job):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_bytes(b"first")
    (source / "b.txt").write_bytes(b"second")
    store = FakeStore("/documents")
    store.add_file("stale.txt", b"stale")
    shutdown = threading.Event()
    original_upload = store.upload

    def request_shutdown_after_current_file(stream, path, size):
        original_upload(stream, path, size)
        shutdown.set()

    store.upload = request_shutdown_after_current_file
    result = mirror_job(make_job(source, destination="/documents"), store, shutdown)

    assert result.outcome == "interrupted"
    assert result.cleanup_skipped
    assert result.files_uploaded == 1
    assert "b.txt" not in store.contents
    assert "stale.txt" in store.entries


def test_remote_structural_conflict_stops_before_upload_and_cleanup(tmp_path, make_job):
    source = tmp_path / "source"
    source.mkdir()
    (source / "report").write_bytes(b"local file")
    store = FakeStore("/documents")
    store.add_folder("report")
    store.add_file("stale.txt", b"stale")

    result = mirror_job(make_job(source, destination="/documents"), store)

    assert result.outcome == "failed"
    assert result.cleanup_skipped
    assert not any(op[0] in {"upload", "delete"} for op in store.operations)


def test_retry_budget_and_minimum_delays(monkeypatch):
    calls = []
    delays = []
    monkeypatch.setattr("dropbox_mirror.mirror.time.sleep", delays.append)

    def operation():
        calls.append(None)
        if len(calls) < 3:
            raise RetryableRemoteError("temporary")
        return "ok"

    assert _retry(operation) == "ok"
    assert len(calls) == 3
    assert delays == [5, 15]


def test_retry_after_is_a_minimum_delay(monkeypatch):
    delays = []
    monkeypatch.setattr("dropbox_mirror.mirror.time.sleep", delays.append)
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableRemoteError("rate limited", retry_after=20)
        return None

    _retry(operation)
    assert delays == [20]


def test_permanent_remote_error_is_not_retried(monkeypatch):
    delays = []
    monkeypatch.setattr("dropbox_mirror.mirror.time.sleep", delays.append)
    calls = []

    def operation():
        calls.append(None)
        raise RemoteError("permanent")

    with pytest.raises(RemoteError):
        _retry(operation)
    assert len(calls) == 1
    assert delays == []


def test_already_deleted_object_counts_as_success(tmp_path, make_job):
    class AmbiguousStore(FakeStore):
        def delete(self, path):
            self.operations.append(("delete", path))
            self.entries.clear()
            raise RemoteNotFound("the previous delete succeeded")

    source = tmp_path / "empty"
    source.mkdir()
    store = AmbiguousStore("/documents")
    store.add_file("stale.txt", b"stale")
    result = mirror_job(make_job(source, destination="/documents"), store)

    assert result.outcome == "success"
    assert result.files_deleted == 1


def test_local_name_collision_detection_is_case_insensitive(tmp_path):
    inventory = Inventory()
    _add_file(inventory, "Report.txt", tmp_path / "one")
    with pytest.raises(SourceError, match="conflicting Dropbox names"):
        _add_file(inventory, "report.txt", tmp_path / "two")
