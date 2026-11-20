import io
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

import dropbox_mirror.dropbox_store as adapter
from dropbox import DropboxOAuth2FlowNoRedirect
from dropbox.exceptions import ApiError, RateLimitError
from dropbox.files import DeleteError, GetMetadataError, ListFolderError, LookupError, WriteMode
from dropbox_mirror.dropbox_store import DropboxStore, create_client
from dropbox_mirror.mirror import RemoteError, RemoteNotFound, RetryableRemoteError, mirror_job


class FakeFile:
    def __init__(self, path, content_hash="hash"):
        self.path_display = path
        self.path_lower = path.lower()
        self.content_hash = content_hash


class FakeFolder:
    pass


class FakePagedClient:
    def __init__(self):
        self.continues = 0

    def files_get_metadata(self, path):
        return FakeFolder()

    def files_list_folder(self, path, **kwargs):
        return SimpleNamespace(entries=[FakeFile(path + "/one")], cursor="next", has_more=True)

    def files_list_folder_continue(self, cursor):
        self.continues += 1
        return SimpleNamespace(entries=[FakeFile("/dest/two")], cursor="end", has_more=False)


def test_missing_destination_is_created_and_uploaded(tmp_path, make_job, monkeypatch):
    monkeypatch.setattr(adapter, "FolderMetadata", FakeFolder)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.txt").write_bytes(b"notes")

    class Client:
        def __init__(self):
            self.folders = set()
            self.uploads = {}

        def files_get_metadata(self, path):
            if path not in self.folders:
                raise ApiError("request", GetMetadataError.path(LookupError.not_found), None, None)
            return FakeFolder()

        def files_create_folder_v2(self, path):
            self.folders.add(path)

        def files_upload(self, content, path, **kwargs):
            self.uploads[path] = content

    client = Client()
    result = mirror_job(make_job(source, destination="/documents"), DropboxStore(client))

    assert result.outcome == "success"
    assert client.folders == {"/documents"}
    assert client.uploads == {"/documents/notes.txt": b"notes"}


def test_metadata_other_lookup_error_is_not_treated_as_missing():
    class Client:
        def files_get_metadata(self, path):
            raise ApiError("request", GetMetadataError.path(LookupError.not_folder), None, None)

    with pytest.raises(RemoteError) as error:
        DropboxStore(Client()).list_tree("/documents")
    assert not isinstance(error.value, RemoteNotFound)


def test_folder_listing_failure_does_not_become_empty_inventory(monkeypatch):
    monkeypatch.setattr(adapter, "FolderMetadata", FakeFolder)

    class Client:
        def files_get_metadata(self, path):
            return FakeFolder()

        def files_list_folder(self, path, **kwargs):
            raise ApiError("request", ListFolderError.path(LookupError.not_found), None, None)

    with pytest.raises(RemoteError):
        DropboxStore(Client()).list_tree("/documents")


def test_delete_missing_path_uses_sdk_lookup_error():
    class Client:
        def files_delete_v2(self, path):
            raise ApiError("request", DeleteError.path_lookup(LookupError.not_found), None, None)

    with pytest.raises(RemoteNotFound):
        DropboxStore(Client()).delete("/documents/old.txt")


def test_list_tree_reads_all_pages(monkeypatch):
    monkeypatch.setattr(adapter, "FileMetadata", FakeFile)
    monkeypatch.setattr(adapter, "FolderMetadata", FakeFolder)
    client = FakePagedClient()

    result = DropboxStore(client).list_tree("/dest")

    assert set(result) == {"", "one", "two"}
    assert result["two"].content_hash == "hash"
    assert client.continues == 1


def test_list_tree_paginates_until_complete_even_for_more_than_one_page(monkeypatch):
    monkeypatch.setattr(adapter, "FileMetadata", FakeFile)
    monkeypatch.setattr(adapter, "FolderMetadata", FakeFolder)

    class MultiPageClient(FakePagedClient):
        def __init__(self):
            super().__init__()
            self.pages = 0

        def files_list_folder(self, path, **kwargs):
            self.pages += 1
            return SimpleNamespace(entries=[FakeFile(path + "/one")], cursor="next-1", has_more=True)

        def files_list_folder_continue(self, cursor):
            self.continues += 1
            if self.continues == 1:
                return SimpleNamespace(entries=[FakeFile("/dest/two")], cursor="next-2", has_more=True)
            return SimpleNamespace(entries=[FakeFile("/dest/three")], cursor="end", has_more=False)

    client = MultiPageClient()
    result = DropboxStore(client).list_tree("/dest")

    assert set(result) == {"", "one", "two", "three"}
    assert client.continues == 2


def test_dropbox_sdk_retries_are_disabled_and_timeout_is_finite():
    client = create_client("app-key", "refresh-token")
    assert client._max_retries_on_error == 0
    assert client._max_retries_on_rate_limit == 0
    assert client._timeout == 30
    client.close()


def test_headless_oauth_requests_offline_pkce_without_a_secret():
    flow = DropboxOAuth2FlowNoRedirect(
        "app-key",
        token_access_type="offline",
        scope=list(adapter.APP_SCOPES),
        use_pkce=True,
    )
    parameters = parse_qs(urlparse(flow.start()).query)
    assert parameters["token_access_type"] == ["offline"]
    assert parameters["code_challenge_method"] == ["S256"]
    assert set(parameters["scope"][0].split()) == set(adapter.APP_SCOPES)
    assert "code_challenge" in parameters


class FakeUploadClient:
    def __init__(self):
        self.calls = []
        self.session_id = "session"

    def files_upload_session_start(self, data, close=False):
        self.calls.append(("start", len(data), close))
        return SimpleNamespace(session_id=self.session_id)

    def files_upload_session_append_v2(self, data, cursor):
        self.calls.append(("append", len(data), cursor.offset))

    def files_upload_session_finish(self, data, cursor, commit):
        self.calls.append(("finish", len(data), cursor.offset, commit.mode, commit.autorename))


def test_large_file_uses_small_sequential_upload_session_requests():
    content = b"x" * (16 * 1024 * 1024 + 3)
    client = FakeUploadClient()
    DropboxStore(client).upload(io.BytesIO(content), "/dest/large.bin", len(content))

    assert client.calls[0] == ("start", 8 * 1024 * 1024, False)
    assert client.calls[-1] == ("finish", 3, 16 * 1024 * 1024, WriteMode.overwrite, False)
    assert all(call[1] <= 8 * 1024 * 1024 for call in client.calls)


def test_oversized_file_is_rejected_before_upload():
    client = FakeUploadClient()
    with pytest.raises(adapter.RemoteError, match="size limit"):
        DropboxStore(client).upload(io.BytesIO(b""), "/dest/huge", adapter.MAX_FILE_SIZE + 1)
    assert client.calls == []


class AmbiguousDeleteClient:
    def __init__(self):
        self.calls = 0

    def files_delete_v2(self, path):
        self.calls += 1
        if self.calls == 1:
            raise RetryableRemoteError("response lost after delete")
        raise RemoteNotFound("delete already took effect")


def test_ambiguous_delete_retry_treats_already_missing_as_success(monkeypatch):
    monkeypatch.setattr("dropbox_mirror.mirror.time.sleep", lambda _: None)
    client = AmbiguousDeleteClient()
    with pytest.raises(RemoteNotFound):
        DropboxStore(client).delete("/dest/stale")
    assert client.calls == 2


def test_delete_uses_only_recoverable_endpoint():
    class DeleteClient:
        def __init__(self):
            self.called = []

        def files_delete_v2(self, path):
            self.called.append(path)

    client = DeleteClient()
    DropboxStore(client).delete("/dest/file")
    assert client.called == ["/dest/file"]


def test_rate_limit_has_one_retry_budget(monkeypatch):
    monkeypatch.setattr("dropbox_mirror.mirror.time.sleep", lambda _: None)
    client = FakePagedClient()
    attempts = []

    def limited(cursor):
        attempts.append(cursor)
        if len(attempts) < 3:
            raise RateLimitError("request", error=None, backoff=0)
        return SimpleNamespace(entries=[], cursor="end", has_more=False)

    client.files_list_folder_continue = limited
    monkeypatch.setattr(adapter, "FileMetadata", FakeFile)
    monkeypatch.setattr(adapter, "FolderMetadata", FakeFolder)
    DropboxStore(client).list_tree("/dest")
    assert len(attempts) == 3


def test_temporary_write_operation_error_is_retryable_by_api_tag(monkeypatch):
    class FakeApiError(Exception):
        error_summary = "delete/too_many_write_operations..."

    monkeypatch.setattr(adapter, "ApiError", FakeApiError)

    def reject():
        raise FakeApiError("busy")

    with pytest.raises(RetryableRemoteError):
        adapter._request(reject)


def test_permanent_api_error_is_not_classified_as_retryable(monkeypatch):
    class FakeApiError(Exception):
        error_summary = "upload/path/invalid_path/..."

    monkeypatch.setattr(adapter, "ApiError", FakeApiError)

    def reject():
        raise FakeApiError("invalid")

    with pytest.raises(RemoteError):
        adapter._request(reject)
