"""Local source inventory and exclusion matching."""

from __future__ import annotations

import fnmatch
import logging
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

from .config import JobConfig

logger = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """Raised when a source cannot be completely inventoried."""


class NoGlobMatches(SourceError):
    """Raised when a glob has no regular files to mirror."""

    def __init__(self, message: str, examined: int):
        super().__init__(message)
        self.examined = examined


@dataclass
class Inventory:
    files: dict[str, Path] = field(default_factory=dict)
    directories: set[str] = field(default_factory=set)
    protected: set[str] = field(default_factory=set)
    examined: int = 0


def is_excluded(relative: str, is_directory: bool, patterns: tuple[str, ...]) -> bool:
    components = relative.split("/") if relative else []
    for raw_pattern in patterns:
        directory_only = raw_pattern.endswith("/")
        pattern = raw_pattern.rstrip("/")
        if not pattern:
            continue
        if "/" not in pattern:
            for index, component in enumerate(components):
                if fnmatch.fnmatchcase(component, pattern):
                    if not directory_only or index < len(components) - 1 or is_directory:
                        return True
        elif relative == pattern or relative.startswith(pattern + "/"):
            return True
    return False


def _relative_key(relative: str) -> str:
    return relative.casefold()


def _validate_remote_components(relative: str) -> None:
    invalid = set('<>:"\\|?*')
    if any(
        not component
        or component in {".", ".."}
        or component.endswith((" ", "."))
        or any(ord(char) < 32 or char in invalid for char in component)
        for component in relative.split("/")
    ):
        raise SourceError(f"local path cannot be represented safely in Dropbox: {relative!r}")


def _add_file(inventory: Inventory, relative: str, path: Path) -> None:
    _validate_remote_components(relative)
    key = _relative_key(relative)
    for existing in (*inventory.files, *inventory.directories):
        if _relative_key(existing) == key:
            raise SourceError(f"local paths map to conflicting Dropbox names: {existing!r} and {relative!r}")
    inventory.files[relative] = path


def _add_directory(inventory: Inventory, relative: str) -> None:
    _validate_remote_components(relative)
    key = _relative_key(relative)
    for existing in (*inventory.files, *inventory.directories):
        if existing and _relative_key(existing) == key and existing != relative:
            raise SourceError(f"local paths map to conflicting Dropbox names: {existing!r} and {relative!r}")
    inventory.directories.add(relative)


@contextmanager
def open_regular_file(path: Path) -> Iterator[BinaryIO]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"source is no longer a regular file: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_directory(directory: Path) -> list[tuple[str, Path, os.stat_result]]:
    if os.name == "nt":
        with os.scandir(directory) as entries:
            return sorted(
                [(entry.name, Path(directory) / entry.name, entry.stat(follow_symlinks=False))
                 for entry in entries],
                key=lambda item: item[0],
            )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory, flags)
    try:
        with os.scandir(descriptor) as entries:
            return sorted(
                [(entry.name, directory / entry.name, entry.stat(follow_symlinks=False))
                 for entry in entries],
                key=lambda item: item[0],
            )
    finally:
        os.close(descriptor)


def _walk_directory(job: JobConfig, inventory: Inventory) -> None:
    root = job.source
    try:
        root_stat = root.lstat()
    except OSError as error:
        raise SourceError(f"cannot inspect source directory {root}: {error}") from error
    if stat.S_ISLNK(root_stat.st_mode):
        logger.warning("Job %s source is a symbolic link; refusing to follow it", job.name)
        raise SourceError(f"source directory is a symbolic link: {root}")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SourceError(f"source is not a directory: {root}")
    inventory.directories.add("")

    def visit(directory: Path, relative_parent: str) -> None:
        try:
            children = _read_directory(directory)
        except OSError as error:
            raise SourceError(f"cannot enumerate source directory {directory}: {error}") from error
        for name, child_path, info in children:
            relative = f"{relative_parent}/{name}" if relative_parent else name
            inventory.examined += 1
            if is_excluded(relative, stat.S_ISDIR(info.st_mode), job.exclusions):
                inventory.protected.add(relative)
                continue
            if stat.S_ISLNK(info.st_mode):
                logger.warning("Job %s: skipping symbolic link %s", job.name, child_path)
                inventory.protected.add(relative)
            elif stat.S_ISDIR(info.st_mode):
                _add_directory(inventory, relative)
                visit(child_path, relative)
            elif stat.S_ISREG(info.st_mode):
                _add_file(inventory, relative, child_path)
            else:
                logger.warning("Job %s: skipping special filesystem object %s", job.name, child_path)
                inventory.protected.add(relative)

    visit(root, "")


def inventory_source(job: JobConfig) -> Inventory:
    inventory = Inventory()
    if job.source_type == "directory":
        _walk_directory(job, inventory)
    elif job.source_type == "file":
        try:
            info = job.source.lstat()
        except OSError as error:
            raise SourceError(f"cannot inspect source file {job.source}: {error}") from error
        if stat.S_ISLNK(info.st_mode):
            logger.warning("Job %s source is a symbolic link; refusing to follow it", job.name)
            raise SourceError(f"source file is a symbolic link: {job.source}")
        if not stat.S_ISREG(info.st_mode):
            raise SourceError(f"source is not a regular file: {job.source}")
        inventory.examined = 1
        if is_excluded(job.source.name, False, job.exclusions):
            inventory.protected.add("")
        else:
            inventory.files[""] = job.source
    else:
        parent = job.source.parent
        try:
            parent_stat = parent.lstat()
            if stat.S_ISLNK(parent_stat.st_mode):
                raise SourceError(f"glob parent is a symbolic link: {parent}")
            if not stat.S_ISDIR(parent_stat.st_mode):
                raise SourceError(f"glob parent is not a directory: {parent}")
            with os.scandir(parent) as entries:
                matches = sorted((Path(entry.path) for entry in entries if fnmatch.fnmatchcase(
                    entry.name, job.source.name)), key=lambda path: path.name)
        except SourceError:
            raise
        except OSError as error:
            raise SourceError(f"cannot enumerate glob parent {parent}: {error}") from error
        for path in matches:
            inventory.examined += 1
            relative = path.name
            try:
                info = path.lstat()
            except OSError as error:
                raise SourceError(f"cannot inspect glob match {path}: {error}") from error
            if is_excluded(relative, stat.S_ISDIR(info.st_mode), job.exclusions):
                inventory.protected.add(relative)
                continue
            if stat.S_ISLNK(info.st_mode):
                logger.warning("Job %s: skipping symbolic link %s", job.name, path)
                inventory.protected.add(relative)
            elif stat.S_ISREG(info.st_mode):
                _add_file(inventory, relative, path)
            elif stat.S_ISDIR(info.st_mode):
                logger.warning("Job %s: skipping directory matched by glob %s", job.name, path)
                inventory.protected.add(relative)
            else:
                logger.warning("Job %s: skipping special filesystem object %s", job.name, path)
                inventory.protected.add(relative)
        if not inventory.files:
            raise NoGlobMatches(
                f"glob source has no matching regular files: {job.source}",
                examined=inventory.examined,
            )
    return inventory


def protected_remote_path(relative: str, remote_is_directory: bool, inventory: Inventory,
                          exclusions: tuple[str, ...]) -> bool:
    if relative == "":
        return False
    if is_excluded(relative, remote_is_directory, exclusions):
        return True
    for protected in inventory.protected:
        protected_folded = protected.casefold()
        relative_folded = relative.casefold()
        if (
            protected_folded == relative_folded
            or protected_folded.startswith(relative_folded + "/")
            or relative_folded.startswith(protected_folded + "/")
        ):
            return True
    if remote_is_directory:
        for excluded in exclusions:
            normalized = excluded.rstrip("/")
            if "/" in normalized and (normalized.startswith(relative + "/") or relative == normalized):
                return True
    return False
