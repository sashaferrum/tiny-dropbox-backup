import pytest

from dropbox_mirror.config import ConfigurationError, load_config


def write_config(tmp_path, job, general=""):
    job = job.replace("/tmp", str(tmp_path))
    path = tmp_path / "mirror.ini"
    path.write_text(
        "[general]\n"
        f"timezone = UTC\n"
        f"credentials_file = {tmp_path / 'credentials.json'}\n"
        f"log_file = {tmp_path / 'mirror.log'}\n"
        f"lock_file = {tmp_path / 'process.lock'}\n"
        f"{general}"
        "[dropbox]\n"
        "app_key = sample-key\n"
        f"{job}",
        encoding="utf-8",
    )
    return path


VALID_JOB = (
    "[job:documents]\n"
    "source_type = directory\n"
    "source = /tmp/documents\n"
    "destination = /documents\n"
    "schedule = interval\n"
    "interval = 2h\n"
)


def test_loads_valid_config_and_literal_percent_sign(tmp_path):
    source = tmp_path / "100% reports"
    source.mkdir()
    job = VALID_JOB.replace("/tmp/documents", str(source))
    config = load_config(write_config(tmp_path, job))
    assert config.jobs[0].source == source
    assert config.timezone.key == "UTC"
    assert config.jobs[0].schedule.interval_seconds == 7200


@pytest.mark.parametrize(
    "job, message",
    [
        (VALID_JOB.replace("interval = 2h", "interval = 0h"), "interval"),
        (VALID_JOB.replace("schedule = interval", "schedule = monthly"), "schedule"),
        (VALID_JOB.replace("source_type = directory", "source_type = inferred"), "source_type"),
        (VALID_JOB.replace("interval = 2h", "interval = 2h\ntimes = 10:00"), "cannot define times"),
    ],
)
def test_rejects_invalid_schedules_and_source_type(tmp_path, job, message):
    path = write_config(tmp_path, job)
    with pytest.raises(ConfigurationError, match=message):
        load_config(path)


def test_rejects_invalid_timezone(tmp_path):
    path = write_config(tmp_path, VALID_JOB)
    path.write_text(path.read_text(encoding="utf-8").replace("timezone = UTC", "timezone = No/Such_Zone"),
                    encoding="utf-8")
    with pytest.raises(ConfigurationError, match="time zone"):
        load_config(path)


def test_rejects_duplicate_job_definitions_case_insensitively(tmp_path):
    job = VALID_JOB + (
        "[job:DOCUMENTS]\n"
        "source_type = file\n"
        "source = /tmp/file.txt\n"
        "destination = /report.txt\n"
        "schedule = daily\n"
        "times = 10:00\n"
    )
    with pytest.raises(ConfigurationError, match="duplicate job"):
        load_config(write_config(tmp_path, job))


@pytest.mark.parametrize(
    "first, second",
    [
        ("/docs", "/docs/sub"),
        ("/Docs", "/docs"),
    ],
)
def test_rejects_overlapping_destinations(tmp_path, first, second):
    job = VALID_JOB.replace("destination = /documents", f"destination = {first}")
    job += (
        "[job:second]\n"
        "source_type = directory\n"
        "source = /tmp/other\n"
        f"destination = {second}\n"
        "schedule = interval\n"
        "interval = 5m\n"
    )
    with pytest.raises(ConfigurationError, match="overlaps"):
        load_config(write_config(tmp_path, job))


def test_rejects_root_destination_and_glob_parent_wildcards(tmp_path):
    with pytest.raises(ConfigurationError, match="other than '/'"):
        load_config(write_config(tmp_path, VALID_JOB.replace("destination = /documents", "destination = /")))
    glob_job = (
        "[job:glob]\n"
        "source_type = glob\n"
        "source = /tmp/*/report*.pdf\n"
        "destination = /reports\n"
        "schedule = daily\n"
        "times = 10:00\n"
    )
    with pytest.raises(ConfigurationError, match="final component"):
        load_config(write_config(tmp_path, glob_job))


def test_rejects_duplicate_ini_section(tmp_path):
    job = VALID_JOB + VALID_JOB
    with pytest.raises(ConfigurationError, match="cannot read"):
        load_config(write_config(tmp_path, job))
