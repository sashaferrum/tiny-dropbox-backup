"""Command-line entry point."""

from __future__ import annotations

import argparse
import getpass
import logging
import logging.handlers
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from dropbox import DropboxOAuth2FlowNoRedirect

from .config import AppConfig, ConfigurationError, load_config
from .credentials import CredentialError, load_credentials, save_credentials
from .dropbox_store import APP_SCOPES, DropboxStore, create_client
from .locking import LockError, ProcessLock
from .mirror import JobResult, mirror_job
from .scheduler import run_scheduler


class ZonedFormatter(logging.Formatter):
    def __init__(self, timezone):
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self.timezone = timezone

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self.timezone).isoformat(timespec="seconds")


def _configure_logging(config: AppConfig) -> None:
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(ZonedFormatter(config.timezone))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


def _log_result(result: JobResult) -> None:
    logging.getLogger(__name__).info(
        "Job summary: name=%s outcome=%s files_examined=%d files_uploaded=%d "
        "files_unchanged=%d files_deleted=%d directories_ensured=%d "
        "directories_deleted=%d errors=%d duration_seconds=%.3f cleanup_skipped=%s",
        result.job_name,
        result.outcome,
        result.files_examined,
        result.files_uploaded,
        result.files_unchanged,
        result.files_deleted,
        result.directories_created,
        result.directories_deleted,
        result.errors,
        result.duration_seconds,
        str(result.cleanup_skipped).lower(),
    )


def _run(config: AppConfig) -> int:
    shutdown = threading.Event()
    previous_handlers = {}

    def request_shutdown(signum, frame) -> None:
        shutdown.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, request_shutdown)

    store = None
    try:
        refresh_token = load_credentials(config.credentials_file)
        client = create_client(config.app_key, refresh_token)
        store = DropboxStore(client)
        logging.getLogger(__name__).info(
            "Application started with %d job(s) in time zone %s", len(config.jobs), config.timezone.key
        )

        def execute(job):
            result = mirror_job(job, store, shutdown)
            _log_result(result)
            return result

        run_scheduler(config.jobs, config.timezone, execute, shutdown)
        logging.getLogger(__name__).info("Application stopped")
        return 0
    finally:
        if store is not None:
            store.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def _authorize(config: AppConfig) -> int:
    flow = DropboxOAuth2FlowNoRedirect(
        config.app_key,
        token_access_type="offline",
        scope=list(APP_SCOPES),
        use_pkce=True,
        timeout=30,
    )
    try:
        url = flow.start()
        print("Open this URL in a browser on any computer and authorize the app:\n")
        print(url)
        print("\nAfter approval, paste the authorization code shown by Dropbox.")
        code = getpass.getpass("Authorization code: ").strip()
        if not code:
            print("Authorization code cannot be empty.", file=sys.stderr)
            return 2
        result = flow.finish(code)
        save_credentials(
            config.credentials_file,
            {
                "refresh_token": result.refresh_token,
                "account_id": result.account_id,
                "scope": result.scope,
                "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            },
        )
    except CredentialError as error:
        logging.getLogger(__name__).error("Dropbox credentials could not be saved: %s", error)
        print("Authorization was not completed; no credentials were saved.", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("Authorization was not completed; no credentials were saved.", file=sys.stderr)
        return 2
    except Exception as error:
        # OAuth SDK errors may include response details; never echo codes or token material.
        logging.getLogger(__name__).error("Dropbox authorization failed: %s", type(error).__name__)
        print(
            "Dropbox authorization failed. Check the app key and authorization code, then run auth again.",
            file=sys.stderr,
        )
        return 2
    logging.getLogger(__name__).info("Dropbox authorization completed; credentials saved")
    print(f"Credentials saved securely to {config.credentials_file}")
    return 0


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dropbox-mirror")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/dropbox-mirror/config.ini"),
        help="absolute path to the INI configuration file",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run scheduled mirroring in the foreground")
    commands.add_parser("auth", help="authorize this installation with Dropbox")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        _configure_logging(config)
    except (ConfigurationError, OSError) as error:
        print(f"Startup error: {error}", file=sys.stderr)
        return 2

    logger = logging.getLogger(__name__)
    try:
        with ProcessLock(config.lock_file):
            if args.command == "auth":
                return _authorize(config)
            return _run(config)
    except (LockError, CredentialError, OSError) as error:
        logger.error("Application startup failed: %s", error)
        print(f"Startup error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        logger.error("Application stopped after an unexpected error (%s)", type(error).__name__)
        print("Application failed. See the configured log file for details.", file=sys.stderr)
        return 1
