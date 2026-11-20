"""Configuration parsing and validation."""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_INTERVAL_RE = re.compile(r"^([1-9][0-9]*)(s|m|h)$")
_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_ALLOWED_GENERAL = {"timezone", "credentials_file", "log_file", "lock_file"}
_ALLOWED_DROPBOX = {"app_key"}
_ALLOWED_JOB = {"source_type", "source", "destination", "schedule", "interval", "times", "exclude"}


class ConfigurationError(ValueError):
    """Raised when the INI file is invalid."""


@dataclass(frozen=True)
class Schedule:
    mode: str
    interval_seconds: int | None = None
    daily_times: tuple[time, ...] = ()


@dataclass(frozen=True)
class JobConfig:
    name: str
    source_type: str
    source: Path
    destination: str
    schedule: Schedule
    exclusions: tuple[str, ...]


@dataclass(frozen=True)
class AppConfig:
    timezone: ZoneInfo
    credentials_file: Path
    log_file: Path
    lock_file: Path
    app_key: str
    jobs: tuple[JobConfig, ...]


def _absolute_path(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{label} must be an absolute path: {value!r}")
    return path


def _remote_destination(value: str, *, is_file: bool) -> str:
    if not value.startswith("/") or value == "/":
        raise ConfigurationError("destination must be an absolute App Folder path other than '/'")
    if value.endswith("/") or "\\" in value or "//" in value:
        raise ConfigurationError(f"invalid Dropbox destination: {value!r}")
    parts = value[1:].split("/")
    invalid_remote_chars = set('<>:"|?*')
    if any(
        part in {"", ".", ".."}
        or any(ord(char) < 32 or char in invalid_remote_chars for char in part)
        or part.endswith((" ", "."))
        for part in parts
    ):
        raise ConfigurationError(f"invalid Dropbox destination: {value!r}")
    if is_file and not parts[-1]:
        raise ConfigurationError(f"file destination must name a file: {value!r}")
    return "/" + "/".join(parts)


def _destinations_overlap(first: str, second: str) -> bool:
    left = first.casefold().split("/")
    right = second.casefold().split("/")
    return left == right or left == right[:len(left)] or right == left[:len(right)]


def _parse_schedule(section: configparser.SectionProxy, job_name: str) -> Schedule:
    mode = section.get("schedule", "").strip().lower()
    if mode == "interval":
        if "times" in section:
            raise ConfigurationError(f"job {job_name!r}: interval schedule cannot define times")
        match = _INTERVAL_RE.fullmatch(section.get("interval", "").strip())
        if not match:
            raise ConfigurationError(
                f"job {job_name!r}: interval must be a positive integer followed by s, m, or h"
            )
        multiplier = {"s": 1, "m": 60, "h": 3600}[match.group(2)]
        return Schedule("interval", interval_seconds=int(match.group(1)) * multiplier)
    if mode == "daily":
        if "interval" in section:
            raise ConfigurationError(f"job {job_name!r}: daily schedule cannot define interval")
        raw_times = section.get("times", "").split(",")
        values = [item.strip() for item in raw_times]
        if not values or any(not _TIME_RE.fullmatch(value) for value in values):
            raise ConfigurationError(f"job {job_name!r}: times must be comma-separated HH:MM values")
        if len(set(values)) != len(values):
            raise ConfigurationError(f"job {job_name!r}: duplicate daily times are not allowed")
        return Schedule("daily", daily_times=tuple(time.fromisoformat(value) for value in values))
    raise ConfigurationError(f"job {job_name!r}: schedule must be 'interval' or 'daily'")


def load_config(path: Path) -> AppConfig:
    parser = configparser.ConfigParser(interpolation=None, strict=True, delimiters=("=",))
    parser.optionxform = str
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error, UnicodeError) as error:
        raise ConfigurationError(f"cannot read configuration {path}: {error}") from error

    if parser.defaults():
        raise ConfigurationError("[DEFAULT] settings are not supported")
    if not parser.has_section("general") or not parser.has_section("dropbox"):
        raise ConfigurationError("configuration must contain [general] and [dropbox] sections")
    unknown_general = set(parser["general"]) - _ALLOWED_GENERAL
    unknown_dropbox = set(parser["dropbox"]) - _ALLOWED_DROPBOX
    if unknown_general:
        raise ConfigurationError(f"unknown [general] setting(s): {', '.join(sorted(unknown_general))}")
    if unknown_dropbox:
        raise ConfigurationError(f"unknown [dropbox] setting(s): {', '.join(sorted(unknown_dropbox))}")
    for required in ("timezone", "credentials_file", "log_file", "lock_file"):
        if not parser["general"].get(required, "").strip():
            raise ConfigurationError(f"[general] requires {required}")
    app_key = parser["dropbox"].get("app_key", "").strip()
    if not app_key or app_key == "YOUR_DROPBOX_APP_KEY":
        raise ConfigurationError("[dropbox] requires a real app_key")
    try:
        timezone = ZoneInfo(parser["general"]["timezone"].strip())
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigurationError(f"invalid IANA time zone: {parser['general']['timezone']!r}") from error

    jobs: list[JobConfig] = []
    seen_names: set[str] = set()
    destinations: list[tuple[str, str]] = []
    for section_name in parser.sections():
        if section_name in {"general", "dropbox"}:
            continue
        if not section_name.startswith("job:") or len(section_name) == 4:
            raise ConfigurationError(f"unknown section: [{section_name}]")
        name = section_name[4:]
        folded_name = name.casefold()
        if folded_name in seen_names:
            raise ConfigurationError(f"duplicate job definition: {name!r}")
        seen_names.add(folded_name)
        section = parser[section_name]
        unknown = set(section) - _ALLOWED_JOB
        if unknown:
            raise ConfigurationError(f"job {name!r}: unknown setting(s): {', '.join(sorted(unknown))}")
        required = ("source_type", "source", "destination", "schedule")
        missing = [key for key in required if not section.get(key, "").strip()]
        if missing:
            raise ConfigurationError(f"job {name!r}: missing required setting(s): {', '.join(missing)}")
        source_type = section["source_type"].strip().lower()
        if source_type not in {"directory", "file", "glob"}:
            raise ConfigurationError(f"job {name!r}: source_type must be directory, file, or glob")
        source_text = section["source"].strip()
        source = _absolute_path(source_text, f"job {name!r} source")
        if source_type == "glob":
            if any(char in str(source.parent) for char in "*?[]") or "**" in source.name:
                raise ConfigurationError(f"job {name!r}: glob wildcards are allowed only in the final component")
            if any(char in source.name for char in "/\\") or not any(char in source.name for char in "*?["):
                raise ConfigurationError(f"job {name!r}: glob source must contain a filename pattern")
        elif any(char in source_text for char in "*?["):
            raise ConfigurationError(f"job {name!r}: wildcards require source_type = glob")
        destination = _remote_destination(section["destination"].strip(), is_file=source_type == "file")
        for previous_name, previous_destination in destinations:
            if _destinations_overlap(destination, previous_destination):
                raise ConfigurationError(
                    f"job {name!r} destination overlaps job {previous_name!r} "
                    f"(Dropbox paths are case-insensitive)"
                )
        destinations.append((name, destination))
        exclusions = tuple(item.strip() for item in section.get("exclude", "").splitlines() if item.strip())
        for pattern in exclusions:
            normalized = pattern.rstrip("/")
            wildcard = any(character in pattern for character in "*?[]")
            if (
                pattern.startswith("/")
                or "\\" in pattern
                or "\x00" in pattern
                or ".." in pattern.split("/")
                or "//" in pattern
                or ("/" in normalized and wildcard)
            ):
                raise ConfigurationError(f"job {name!r}: invalid exclusion pattern {pattern!r}")
        jobs.append(JobConfig(name, source_type, source, destination, _parse_schedule(section, name), exclusions))

    if not jobs:
        raise ConfigurationError("at least one [job:name] section is required")
    general = parser["general"]
    return AppConfig(
        timezone,
        _absolute_path(general["credentials_file"].strip(), "[general] credentials_file"),
        _absolute_path(general["log_file"].strip(), "[general] log_file"),
        _absolute_path(general["lock_file"].strip(), "[general] lock_file"),
        app_key,
        tuple(jobs),
    )
