import hashlib
import io
from pathlib import Path

import pytest

from dropbox_mirror.config import JobConfig, Schedule
from dropbox_mirror.mirror import RemoteEntry, dropbox_content_hash


class FakeStore:
    def __init__(self, destination="/remote"):
        self.destination = destination
        self.entries = {}
        self.contents = {}
        self.operations = []
        self.fail_uploads = set()
        self.list_error = None
        self.delete_error = None

    def add_file(self, relative, content):
        path = self.destination if not relative else f"{self.destination}/{relative}"
        self.entries[relative] = RemoteEntry(path, "file", dropbox_content_hash(io.BytesIO(content)))
        self.contents[relative] = content

    def add_folder(self, relative):
        path = self.destination if not relative else f"{self.destination}/{relative}"
        self.entries[relative] = RemoteEntry(path, "folder")

    def list_tree(self, path):
        self.operations.append(("list", path))
        if self.list_error:
            raise self.list_error
        if path != self.destination:
            raise AssertionError(f"unexpected list path {path}")
        return dict(self.entries)

    def ensure_folder(self, path):
        self.operations.append(("ensure", path))
        if path == self.destination:
            self.add_folder("")
        elif path.startswith(self.destination + "/"):
            self.add_folder(path[len(self.destination) + 1:])
        else:
            self.operations.append(("ensure-parent", path))

    def upload(self, stream, path, size):
        relative = "" if path == self.destination else path[len(self.destination) + 1:]
        self.operations.append(("upload", relative))
        if relative in self.fail_uploads:
            raise OSError("fake upload failure")
        content = stream.read()
        assert len(content) == size
        self.contents[relative] = content
        self.entries[relative] = RemoteEntry(
            path, "file", dropbox_content_hash(io.BytesIO(content))
        )

    def delete(self, path):
        self.operations.append(("delete", path))
        if self.delete_error:
            raise self.delete_error
        relative = "" if path == self.destination else path[len(self.destination) + 1:]
        if relative not in self.entries:
            from dropbox_mirror.mirror import RemoteNotFound

            raise RemoteNotFound("already absent")
        if self.entries[relative].kind == "folder":
            prefix = relative + "/"
            if any(name.startswith(prefix) for name in self.entries if name != relative):
                raise OSError("folder is not empty")
        self.entries.pop(relative)
        self.contents.pop(relative, None)


@pytest.fixture
def make_job():
    def build(source, source_type="directory", destination="/remote", exclusions=(), mode="interval"):
        schedule = Schedule("interval", interval_seconds=3600) if mode == "interval" else Schedule("daily")
        return JobConfig("job", source_type, Path(source), destination, schedule, tuple(exclusions))

    return build
