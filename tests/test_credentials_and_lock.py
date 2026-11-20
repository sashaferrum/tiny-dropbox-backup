import json
import logging
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from dropbox_mirror import cli
from dropbox_mirror.config import AppConfig
from dropbox_mirror.credentials import CredentialError, load_credentials, save_credentials
from dropbox_mirror.locking import LockError, ProcessLock


def test_credentials_are_atomically_saved_with_private_permissions(tmp_path):
    path = tmp_path / "private" / "credentials.json"
    save_credentials(path, {"refresh_token": "do-not-log", "account_id": "id"})
    assert load_credentials(path) == "do-not-log"
    assert not list(path.parent.glob(".credentials-*"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_credentials_reject_missing_refresh_token(tmp_path):
    parent = tmp_path / "secure"
    parent.mkdir()
    if os.name != "nt":
        parent.chmod(0o700)
    path = parent / "credentials.json"
    path.write_text(json.dumps({"access_token": "not-enough"}), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(CredentialError, match="refresh token"):
        load_credentials(path)


def test_credentials_inside_git_repository_are_rejected(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(CredentialError, match="outside a Git repository"):
        save_credentials(tmp_path / "credentials.json", {"refresh_token": "secret"})


def test_authorization_secrets_are_not_written_to_logs_or_terminal(tmp_path, monkeypatch, caplog, capsys):
    authorization_code = "authorization-code-secret"
    refresh_token = "refresh-token-secret"

    class FakeFlow:
        def __init__(self, *args, **kwargs):
            assert kwargs["use_pkce"] is True
            assert kwargs["token_access_type"] == "offline"

        def start(self):
            return "https://dropbox.example/authorize"

        def finish(self, code):
            assert code == authorization_code
            return SimpleNamespace(
                refresh_token=refresh_token,
                account_id="account",
                scope=["files.content.write"],
                expires_at=None,
            )

    monkeypatch.setattr(cli, "DropboxOAuth2FlowNoRedirect", FakeFlow)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: authorization_code)
    credentials = tmp_path / "private" / "credentials.json"
    config = AppConfig(
        ZoneInfo("UTC"),
        credentials,
        tmp_path / "mirror.log",
        tmp_path / "process.lock",
        "app-key",
        (),
    )

    with caplog.at_level(logging.INFO):
        assert cli._authorize(config) == 0
    captured = capsys.readouterr()

    assert refresh_token not in caplog.text
    assert authorization_code not in caplog.text
    assert refresh_token not in captured.out
    assert authorization_code not in captured.out
    assert load_credentials(credentials) == refresh_token


def test_lock_prevents_concurrent_instances_and_releases(tmp_path):
    path = tmp_path / "process.lock"
    first = ProcessLock(path)
    second = ProcessLock(path)
    first.acquire()
    try:
        with pytest.raises(LockError):
            second.acquire()
    finally:
        first.release()
    with ProcessLock(path):
        pass
