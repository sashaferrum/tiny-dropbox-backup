"""Atomic, owner-only credential storage."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


class CredentialError(RuntimeError):
    """Raised when credentials cannot be safely read or written."""


def _is_inside_git_repository(path: Path) -> bool:
    resolved = path.resolve()
    return any((parent / ".git").exists() for parent in resolved.parents)


def load_credentials(path: Path) -> str:
    if _is_inside_git_repository(path):
        raise CredentialError(f"credential file must be outside a Git repository: {path}")
    try:
        if os.name != "nt":
            file_info = path.lstat()
            parent_info = path.parent.stat()
            if not stat.S_ISREG(file_info.st_mode) or stat.S_IMODE(file_info.st_mode) & 0o077:
                raise CredentialError(f"credential file {path} must be a regular owner-only file")
            if stat.S_IMODE(parent_info.st_mode) & 0o077:
                raise CredentialError(f"credential directory {path.parent} must be owner-only")
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except CredentialError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise CredentialError(f"cannot read credentials at {path}: {error}") from error
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("refresh_token"), str)
        or not value["refresh_token"]
    ):
        raise CredentialError(f"credential file {path} does not contain a refresh token")
    return value["refresh_token"]


def save_credentials(path: Path, credentials: dict[str, object]) -> None:
    if _is_inside_git_repository(path):
        raise CredentialError(f"credential file must be outside a Git repository: {path}")
    if not isinstance(credentials.get("refresh_token"), str) or not credentials["refresh_token"]:
        raise CredentialError("authorization did not return a refresh token")
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
        temporary_path = Path(temporary_name)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(credentials, stream, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError as error:
        raise CredentialError(f"cannot securely save credentials to {path}: {error}") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
