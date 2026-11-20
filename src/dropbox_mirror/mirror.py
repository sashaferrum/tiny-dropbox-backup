"""Inventory, upload, and safety-first remote cleanup."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Protocol, TypeVar

from .config import JobConfig
from .source import NoGlobMatches, SourceError, inventory_source, open_regular_file, protected_remote_path

logger = logging.getLogger(__name__)
T = TypeVar("T")
HASH_BLOCK_SIZE = 4 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
MAX_REQUEST_ATTEMPTS = 3


@dataclass(frozen=True)
class RemoteEntry:
    path: str
    kind: str
    content_hash: str | None = None


class RemoteStore(Protocol):
    def list_tree(self, path: str) -> dict[str, RemoteEntry]: ...
    def ensure_folder(self, path: str) -> None: ...
    def upload(self, stream: BinaryIO, path: str, size: int) -> None: ...
    def delete(self, path: str) -> None: ...


class RemoteError(RuntimeError):
    """Base class for remote operation errors."""


class RetryableRemoteError(RemoteError):
    """A temporary remote failure that may be retried."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RemoteNotFound(RemoteError):
    """A remote object disappeared before its deletion completed."""


@dataclass
class JobResult:
    job_name: str
    outcome: str = "success"
    files_examined: int = 0
    files_uploaded: int = 0
    files_unchanged: int = 0
    files_deleted: int = 0
    directories_created: int = 0
    directories_deleted: int = 0
    errors: int = 0
    cleanup_skipped: bool = False
    duration_seconds: float = 0.0
    error_messages: list[str] = field(default_factory=list)


def dropbox_content_hash(stream: BinaryIO) -> str:
    """Compute Dropbox's SHA-256-of-block-hashes content hash."""
    whole_hash = hashlib.sha256()
    while True:
        block = stream.read(HASH_BLOCK_SIZE)
        if not block:
            break
        whole_hash.update(hashlib.sha256(block).digest())
    return whole_hash.hexdigest()


def _remote_path(destination: str, relative: str) -> str:
    return destination if not relative else f"{destination.rstrip('/')}/{relative}"


def _retry(operation: Callable[[], T]) -> T:
    delays = (5, 15)
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        try:
            return operation()
        except RetryableRemoteError as error:
            if attempt == MAX_REQUEST_ATTEMPTS - 1:
                raise
            delay = max(delays[attempt], error.retry_after or 0)
            logger.warning("Temporary Dropbox error; retrying attempt %s/3 in %s seconds: %s",
                           attempt + 2, delay, error)
            time.sleep(delay)


def _metadata_conflicts(job: JobConfig, inventory: Inventory, remote: dict[str, RemoteEntry]) -> list[str]:
    conflicts: list[str] = []
    expected: dict[str, str] = {path: "file" for path in inventory.files}
    expected.update({path: "folder" for path in inventory.directories if path})
    if job.source_type in {"directory", "glob"}:
        expected[""] = "folder"
    remote_by_folded = {path.casefold(): entry for path, entry in remote.items()}
    for relative, kind in expected.items():
        current = remote_by_folded.get(relative.casefold())
        if current and current.kind != kind:
            conflicts.append(relative)
    return conflicts


def _remote_collision(paths: list[str]) -> str | None:
    by_folded: dict[str, str] = {}
    for path in paths:
        folded = path.casefold()
        previous = by_folded.get(folded)
        if previous is not None and previous != path:
            return f"{previous!r} and {path!r} map to the same Dropbox path"
        by_folded[folded] = path
    return None


def mirror_job(job: JobConfig, store: RemoteStore, shutdown: threading.Event | None = None) -> JobResult:
    started = time.monotonic()
    result = JobResult(job.name)

    def finish(value: JobResult) -> JobResult:
        value.duration_seconds = time.monotonic() - started
        return value

    def fail(message: str, *, interrupted: bool = False) -> JobResult:
        result.errors += 1
        result.error_messages.append(message)
        result.outcome = "interrupted" if interrupted else ("partial" if result.files_uploaded else "failed")
        result.cleanup_skipped = True
        logger.error("Job %s: %s", job.name, message)
        return result

    def stopped() -> bool:
        return shutdown is not None and shutdown.is_set()

    try:
        inventory = inventory_source(job)
        result.files_examined = inventory.examined
    except NoGlobMatches as error:
        result.files_examined = error.examined
        logger.warning("Job %s skipped: %s", job.name, error)
        result.cleanup_skipped = True
        return finish(result)
    except SourceError as error:
        result.errors += 1
        result.outcome = "failed"
        result.cleanup_skipped = True
        result.error_messages.append(str(error))
        logger.error("Job %s: %s", job.name, error)
        result.duration_seconds = time.monotonic() - started
        return result

    local_hashes: dict[str, str] = {}
    hash_failed = False
    for relative, path in inventory.files.items():
        if stopped():
            return finish(fail("shutdown requested before local inventory completed", interrupted=True))
        try:
            with open_regular_file(path) as stream:
                local_hashes[relative] = dropbox_content_hash(stream)
        except OSError as error:
            hash_failed = True
            result.errors += 1
            result.error_messages.append(f"cannot read or hash {path}: {error}")
            logger.error("Job %s: cannot read or hash %s: %s", job.name, path, error)

    if stopped():
        return finish(fail("shutdown requested before Dropbox inventory", interrupted=True))

    try:
        remote = store.list_tree(job.destination)
    except Exception as error:
        return finish(fail(f"cannot obtain complete Dropbox inventory: {error}"))

    conflict = _remote_collision(list(inventory.files) + list(inventory.directories))
    if conflict:
        return finish(fail(f"local path collision: {conflict}"))
    conflicts = _metadata_conflicts(job, inventory, remote)
    if conflicts:
        return finish(fail("remote file/directory type conflict at " + ", ".join(sorted(conflicts))))

    folders_to_create = sorted(
        [directory for directory in inventory.directories if directory]
        + ([ "" ] if job.source_type != "file" else []),
        key=lambda item: (item.count("/"), item.casefold()),
    )
    if job.source_type == "file":
        parent_relative = job.destination.rsplit("/", 1)[0]
        folders_to_create = [parent_relative]
    try:
        for relative in folders_to_create:
            if stopped():
                return finish(fail("shutdown requested before folder creation completed", interrupted=True))
            folder_path = _remote_path(job.destination, relative) if job.source_type != "file" else relative
            store.ensure_folder(folder_path)
            result.directories_created += 1
    except Exception as error:
        return finish(fail(f"cannot create Dropbox destination folder: {error}"))

    upload_failed = False
    remote_by_folded = {path.casefold(): entry for path, entry in remote.items()}
    for relative, path in inventory.files.items():
        if stopped():
            return finish(fail("shutdown requested before the next file", interrupted=True))
        if relative not in local_hashes:
            upload_failed = True
            continue
        remote_entry = remote_by_folded.get(relative.casefold())
        if remote_entry and remote_entry.kind == "file" and remote_entry.content_hash == local_hashes[relative]:
            result.files_unchanged += 1
            continue
        try:
            size = path.stat().st_size
            with open_regular_file(path) as stream:
                store.upload(stream, _remote_path(job.destination, relative), size)
            result.files_uploaded += 1
        except Exception as error:
            upload_failed = True
            result.errors += 1
            result.error_messages.append(f"upload failed for {path}: {error}")
            logger.error("Job %s: upload failed for %s: %s", job.name, path, error)
    if upload_failed or hash_failed:
        result.outcome = "partial" if result.files_uploaded or result.files_unchanged else "failed"
        result.cleanup_skipped = True
        return finish(result)
    if stopped():
        return finish(fail("shutdown requested; cleanup was not started", interrupted=True))

    keep_directories = set(inventory.directories)
    local_files_folded = {path.casefold() for path in inventory.files}
    local_directories_folded = {path.casefold() for path in keep_directories}
    # Never remove the configured destination root, even for an empty source.
    deletions: list[tuple[str, RemoteEntry]] = []
    for relative, entry in remote.items():
        if not relative:
            continue
        if protected_remote_path(relative, entry.kind == "folder", inventory, job.exclusions):
            continue
        expected_kind = "file" if relative.casefold() in local_files_folded else (
            "folder" if relative.casefold() in local_directories_folded and relative else None
        )
        if expected_kind is None:
            deletions.append((relative, entry))

    # Remove files before folders, then delete only folders emptied by this cleanup.
    deletions.sort(
        key=lambda item: (
            item[1].kind == "folder",
            -item[1].path.count("/"),
            item[1].path.casefold(),
        )
    )
    for relative, entry in deletions:
        if stopped():
            return finish(fail("shutdown requested during cleanup", interrupted=True))
        # A parent directory is removable only when no protected descendant remains.
        if entry.kind == "folder" and any(
            protected_remote_path(other_relative, other.kind == "folder", inventory, job.exclusions)
            and other_relative.casefold().startswith(relative.casefold() + "/")
            for other_relative, other in remote.items()
        ):
            continue
        if entry.kind == "folder" and any(
            other_relative.casefold().startswith(relative.casefold() + "/")
            for other_relative in remote
        ):
            result.errors += 1
            result.error_messages.append(f"refusing to delete non-empty Dropbox folder {entry.path}")
            result.outcome = "partial"
            result.cleanup_skipped = False
            logger.error("Job %s: refusing to delete non-empty Dropbox folder %s",
                         job.name, entry.path)
            break
        try:
            store.delete(entry.path)
            remote.pop(relative, None)
            if entry.kind == "file":
                result.files_deleted += 1
            else:
                result.directories_deleted += 1
        except RemoteNotFound:
            # A prior ambiguous delete may have succeeded; absence is the desired state.
            remote.pop(relative, None)
            if entry.kind == "file":
                result.files_deleted += 1
            else:
                result.directories_deleted += 1
            continue
        except Exception as error:
            result.errors += 1
            result.error_messages.append(f"cleanup failed for {entry.path}: {error}")
            result.outcome = "partial"
            result.cleanup_skipped = False
            logger.error("Job %s: cleanup stopped at %s: %s", job.name, entry.path, error)
            break
    if result.errors and result.outcome == "success":
        result.outcome = "partial"
    return finish(result)

