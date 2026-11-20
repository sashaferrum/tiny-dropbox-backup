"""Dropbox SDK adapter with bounded request retries."""

from __future__ import annotations

from typing import BinaryIO, Callable, TypeVar

import dropbox
from dropbox import Dropbox
from dropbox.exceptions import ApiError, AuthError, InternalServerError, RateLimitError
from dropbox.files import CommitInfo, FileMetadata, FolderMetadata, UploadSessionCursor, WriteMode
from requests.exceptions import ConnectionError as RequestConnectionError
from requests.exceptions import Timeout as RequestTimeout

from .mirror import (
    UPLOAD_CHUNK_SIZE,
    RemoteEntry,
    RemoteError,
    RemoteNotFound,
    RetryableRemoteError,
    _retry,
)

T = TypeVar("T")
APP_SCOPES = ("files.metadata.read", "files.content.write")
NETWORK_TIMEOUT_SECONDS = 30
MAX_FILE_SIZE = 2**41 - 2**22


def create_client(app_key: str, refresh_token: str) -> Dropbox:
    return Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=app_key,
        scope=list(APP_SCOPES),
        max_retries_on_error=0,
        max_retries_on_rate_limit=0,
        timeout=NETWORK_TIMEOUT_SECONDS,
    )


def _is_not_found(error: ApiError) -> bool:
    summary = getattr(error, "error_summary", "") or ""
    return summary.startswith(("path/not_found", "path_lookup/not_found"))


def _request(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except RateLimitError as error:
        raise RetryableRemoteError("Dropbox rate limit reached", float(error.backoff or 0)) from error
    except InternalServerError as error:
        raise RetryableRemoteError("Dropbox temporary server error") from error
    except (RequestConnectionError, RequestTimeout) as error:
        raise RetryableRemoteError(f"Dropbox network request failed: {error}") from error
    except ApiError as error:
        if _is_not_found(error):
            raise RemoteNotFound("Dropbox path no longer exists") from error
        summary = getattr(error, "error_summary", "") or ""
        if any(tag.startswith("too_many_write_operations") for tag in summary.split("/")):
            raise RetryableRemoteError("Dropbox write operation limit reached") from error
        raise RemoteError(f"Dropbox API rejected the operation: {error}") from error
    except AuthError as error:
        raise RemoteError(f"Dropbox authorization failed; run 'dropbox-mirror auth': {error}") from error
    except dropbox.exceptions.HttpError as error:
        status = getattr(error, "status_code", None)
        if status == 408 or (isinstance(status, int) and status >= 500):
            raise RetryableRemoteError(f"Dropbox temporary HTTP error ({status})") from error
        raise RemoteError(f"Dropbox HTTP request failed: {error}") from error


def _request_with_retry(operation: Callable[[], T]) -> T:
    return _retry(lambda: _request(operation))


class DropboxStore:
    def __init__(self, client: Dropbox):
        self.client = client

    def close(self) -> None:
        self.client.close()

    def list_tree(self, path: str) -> dict[str, RemoteEntry]:
        try:
            root = _request_with_retry(lambda: self.client.files_get_metadata(path))
        except RemoteNotFound:
            return {}
        if isinstance(root, FileMetadata):
            return {"": RemoteEntry(path, "file", root.content_hash)}
        if not isinstance(root, FolderMetadata):
            raise RemoteError(f"unexpected Dropbox metadata at {path}")

        entries: dict[str, RemoteEntry] = {"": RemoteEntry(path, "folder")}
        result = _request_with_retry(
            lambda: self.client.files_list_folder(path, recursive=True, include_deleted=False)
        )
        while True:
            for item in result.entries:
                display_path = item.path_display or item.path_lower
                relative = _relative_path(path, display_path)
                if isinstance(item, FileMetadata):
                    entry = RemoteEntry(display_path, "file", item.content_hash)
                elif isinstance(item, FolderMetadata):
                    entry = RemoteEntry(display_path, "folder")
                else:
                    continue
                folded = relative.casefold()
                existing = next((key for key in entries if key and key.casefold() == folded), None)
                if existing and existing != relative:
                    raise RemoteError(f"Dropbox returned case-colliding paths: {existing!r}, {relative!r}")
                entries[relative] = entry
            if not result.has_more:
                break
            result = _request_with_retry(lambda: self.client.files_list_folder_continue(result.cursor))
        return entries

    def ensure_folder(self, path: str) -> None:
        if path in {"", "/"}:
            return
        components = path.strip("/").split("/")
        current = ""
        for component in components:
            current += "/" + component
            try:
                metadata = _request_with_retry(lambda current=current: self.client.files_get_metadata(current))
            except RemoteNotFound:
                try:
                    _request_with_retry(lambda current=current: self.client.files_create_folder_v2(current))
                except RemoteError:
                    # A concurrent creator is harmless only if the resulting object is a folder.
                    metadata = _request_with_retry(
                        lambda current=current: self.client.files_get_metadata(current)
                    )
                    if not isinstance(metadata, FolderMetadata):
                        raise
                continue
            if not isinstance(metadata, FolderMetadata):
                raise RemoteError(f"Dropbox path is a file, not a folder: {current}")

    def upload(self, stream: BinaryIO, path: str, size: int) -> None:
        if size > MAX_FILE_SIZE:
            raise RemoteError(
                f"file exceeds Dropbox's supported upload-session size limit ({MAX_FILE_SIZE} bytes): {path}"
            )
        if size <= UPLOAD_CHUNK_SIZE:
            payload = stream.read(size + 1)
            if len(payload) != size:
                raise RemoteError(f"source size changed while reading {path}")
            _request_with_retry(
                lambda: self.client.files_upload(
                    payload, path, mode=WriteMode.overwrite, autorename=False
                )
            )
            return

        first = stream.read(UPLOAD_CHUNK_SIZE)
        if len(first) != UPLOAD_CHUNK_SIZE:
            raise RemoteError(f"source size changed while reading {path}")
        session = _request_with_retry(
            lambda: self.client.files_upload_session_start(first, close=False)
        )
        offset = len(first)
        cursor = UploadSessionCursor(session.session_id, offset)
        while size - offset > UPLOAD_CHUNK_SIZE:
            chunk = stream.read(UPLOAD_CHUNK_SIZE)
            if len(chunk) != UPLOAD_CHUNK_SIZE:
                raise RemoteError(f"source size changed while reading {path}")
            _request_with_retry(
                lambda chunk=chunk, cursor=cursor: self.client.files_upload_session_append_v2(
                    chunk, cursor
                )
            )
            offset += len(chunk)
            cursor = UploadSessionCursor(session.session_id, offset)
        final_size = size - offset
        final_chunk = stream.read(final_size + 1)
        if len(final_chunk) != final_size:
            raise RemoteError(f"source size changed while reading {path}")
        commit = CommitInfo(path, mode=WriteMode.overwrite, autorename=False)
        _request_with_retry(
            lambda: self.client.files_upload_session_finish(final_chunk, cursor, commit)
        )

    def delete(self, path: str) -> None:
        try:
            _request_with_retry(lambda: self.client.files_delete_v2(path))
        except RemoteNotFound:
            raise


def _relative_path(root: str, full_path: str) -> str:
    root_parts = root.strip("/").split("/")
    full_parts = full_path.strip("/").split("/")
    if len(full_parts) > len(root_parts) and all(
        left.casefold() == right.casefold()
        for left, right in zip(root_parts, full_parts)
    ):
        return "/".join(full_parts[len(root_parts):])
    raise RemoteError(f"Dropbox returned an entry outside the listed destination: {full_path}")
