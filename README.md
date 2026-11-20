# Tiny Dropbox Backup

Tiny Dropbox Backup is a small, foreground Python utility that mirrors selected
local files and directories into a Dropbox App Folder on a schedule. It targets
Ubuntu Linux and Python 3.11 or newer.

This is a **one-way mirror**, not a bidirectional synchronizer and not an
independent, versioned backup system. Local changes, including deletions and
corruption, can propagate to Dropbox. Recovery depends on Dropbox file history,
the account plan, retention period, and Dropbox policies. The application does
not make snapshots or archives, and does not guarantee indefinite recovery.
Restore files through Dropbox; this application has no restore operation.

## What it does

- Supports `run` (foreground scheduler) and `auth` (initial Dropbox authorization).
- Mirrors directory, individual-file, and final-component glob sources.
- Compares content using Dropbox's 4 MiB block `content_hash` algorithm.
- Updates changed files with Dropbox overwrite semantics and does not autorename.
- Uses the official Dropbox Python SDK with scoped App Folder access.
- Uses an exclusive process lock, rotating logs, and a system-level systemd unit.

There are no status, validation-only, one-shot, dry-run, logout, web, notification,
database, or background-daemon commands. Configuration is read at startup;
restart the process after changing it.

## Safety and limitations

Each job follows an inventory, upload, cleanup sequence. Cleanup is permitted
only after complete local and paginated remote inventories, successful local
hashing, folder creation and uploads, and no structural or naming conflicts.
Any local enumeration/read/hash error, remote listing error, failed upload, or
requested shutdown blocks cleanup for that job. Failed uploads do not prevent
other files from being attempted, but they prevent all cleanup. Cleanup is
non-transactional: if a deletion fails, already completed deletions are not
rolled back and remaining deletions stop.

- A missing or unavailable source never triggers remote deletion. A missing
  single-file source leaves its remote file intact.
- A fully enumerated, valid empty directory is authoritative: eligible managed
  remote contents are removed, while the configured destination root remains.
- A glob with zero matching regular files is skipped with a warning and cannot
  clear its destination.
- A glob job owns its **entire dedicated destination directory**. Do not put
  unrelated remote files there. Directory and file jobs own only paths mapped
  from their source; a file job never owns its destination's siblings.
- Excluded, symlinked, and unsupported special filesystem objects are skipped
  and protected from deletion. Directories with protected descendants are not
  deleted. The application never uses Dropbox permanent-delete operations.
- Remote file/directory conflicts and local names that collide under
  Dropbox's case-insensitive path rules stop the job before cleanup. The app
  does not destructively remove a remote object to resolve a conflict.
- Dropbox's ordinary `delete_v2` operation recursively deletes a folder and
  its contents. The app calls it for a folder only after all known eligible
  descendants have been deleted, and never when a protected descendant remains.
  The configured destination root is never passed to deletion. Cleanup is not
  atomic; Dropbox or another client may change remote contents after inventory.
- Symlinks are never followed. Devices, sockets, FIFOs, and other special local
  objects are not uploaded.
- Files are read live. A file can change between hashing and upload, and the
  application does not create a snapshot or retry specifically for concurrent
  edits. Copying a live database does not guarantee a consistent backup.
- Uploads use sequential 8 MiB Dropbox upload-session requests. Dropbox's
  session file-size ceiling (2^41 - 2^22 bytes) is enforced before upload;
  larger files fail the job's upload phase and block cleanup. The API limit is
  documented by Dropbox and is not a recommended dataset size.
- Recursive Dropbox listing is paginated completely. If listing fails or
  Dropbox rejects a large recursive listing, the job does not clean up.
- File history is maintained by Dropbox, not encoded into the uploaded file.
  Recovery depends on Dropbox's current account policy and retention window.

The app disables the SDK's built-in retries and applies at most three attempts
per individual request: immediately, then after at least 5 seconds and at least
15 seconds. A Dropbox `Retry-After` delay is respected as a minimum. The SDK
uses a 30-second connect/read timeout for each request; it is not a hard
total-duration deadline if a peer continues sending data. Some upload-session
outcomes can remain ambiguous after a lost response; in that case the job fails
safely and skips cleanup rather than claiming success.

## Installation

Install Python 3.11+, its venv support, Git, `tzdata`, and systemd on Ubuntu.
Clone the project:

```bash
sudo apt install python3.11 python3.11-venv git tzdata
git clone <repository-url> tiny-dropbox-backup
cd tiny-dropbox-backup
chmod +x install.sh
./install.sh
```

The installer automatically:
- Creates a Python virtual environment
- Installs the package and dependencies
- Creates the `~/.dropbox-mirror` credentials directory with secure permissions
- Makes wrapper scripts executable

### Manual installation (alternative)

If you prefer to install without the script:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
mkdir -p ~/.dropbox-mirror
chmod 700 ~/.dropbox-mirror
chmod +x scripts/start.sh scripts/stop.sh scripts/restart.sh
```

### Quick start after installation

After running `./install.sh`, configure and start the application:

```bash
# Activate the virtual environment (if not already active)
source .venv/bin/activate

# Copy the example configuration
cp config.example.ini ~/.dropbox-mirror/config.ini

# Edit the app key, source paths, and absolute credentials_file, log_file,
# and lock_file paths so they are writable by your user
nano ~/.dropbox-mirror/config.ini

# Authorize with Dropbox (first time only, interactive)
dropbox-mirror --config "$HOME/.dropbox-mirror/config.ini" auth

# Start the application in the foreground
dropbox-mirror --config "$HOME/.dropbox-mirror/config.ini" run
```

The example's `/var/lib` and `/var/log` paths normally require system setup;
for a foreground run, replace them with writable absolute paths under your home
directory. The CLI defaults to `/etc/dropbox-mirror/config.ini` when `--config`
is omitted. To run as a service, follow the separate systemd setup below; the
wrapper scripts only control an already installed service.

For development and tests:

```bash
python -m pip install -e ".[test]"
pytest
```

The runtime dependency is the official Dropbox SDK (`dropbox>=12.2.4,<13`).
Tests use pytest and fake Dropbox stores; they do not need credentials or
network access.

## Dropbox app and headless authorization

1. In the [Dropbox App Console](https://www.dropbox.com/developers/apps), create
   an app with **Scoped access** and **App folder** access. Do not select Full
   Dropbox.
2. In the app's **Permissions** tab, enable only:
   - `files.metadata.read` to list remote paths and read Dropbox content hashes.
   - `files.content.write` to upload, create folders, and perform ordinary
     recoverable deletion.
3. Apply the permission changes. Copy the app key from the app's Settings tab
   into `[dropbox] app_key` in your configuration. No client secret or redirect
   URI is needed for this PKCE no-redirect flow.
4. Create your installation-specific INI file outside the repository. Start
   from [`config.example.ini`](config.example.ini) and update all local paths,
   the app key, time zone, and jobs.
5. From an SSH terminal on the server, run:

   ```bash
   .venv/bin/dropbox-mirror --config "$HOME/.dropbox-mirror/config.ini" auth
   ```

   Open the printed URL in a browser on any computer, approve the app, and
   paste the authorization code shown by Dropbox into the SSH prompt. The
   supported SDK no-redirect OAuth flow uses PKCE and requests offline access;
   the server does not host a callback and does not need a GUI browser.
6. Credentials are written automatically as JSON to the configured
   `credentials_file`, outside the repository, using an atomic replacement,
   a protected parent directory, and owner-only file permissions (0600 on
   Linux). Keep the file private and do not commit it. The SDK refreshes the
   access token automatically using the refresh token. If approval is revoked,
   run `auth` again as the service user.

With App Folder access, every configured Dropbox path is relative to the app's
own folder under `/apps` in the user's Dropbox. For example, destination
`/documents` maps to the `documents` folder inside that app's folder, not to
the user's general Dropbox root. The exact displayed app-folder name is set by
Dropbox when the app is created.

The app never logs access tokens, refresh tokens, authorization codes, or PKCE
verifiers.

## Configuration

Use an INI file with exactly one `[general]` section, one `[dropbox]` section,
and one or more `[job:<name>]` sections. Section and option names are
case-sensitive. Unknown sections/options, repeated definitions, malformed
paths, bad schedules, and overlapping destinations are rejected at startup.
INI interpolation is disabled, so percent signs are literal.

```ini
[general]
timezone = Europe/Moscow
credentials_file = /var/lib/dropbox-mirror/credentials.json
log_file = /var/log/dropbox-mirror/mirror.log
lock_file = /var/lib/dropbox-mirror/process.lock

[dropbox]
app_key = YOUR_DROPBOX_APP_KEY

[job:documents]
source_type = directory
source = /home/alex/documents
destination = /documents
schedule = interval
interval = 2h
exclude =
    .git/
    __pycache__/
    *.tmp
    data/cache/

[job:report]
source_type = file
source = /home/alex/reports/report.pdf
destination = /reports/report.pdf
schedule = daily
times = 10:00, 23:30

[job:pdf_reports]
source_type = glob
source = /home/alex/reports/*.pdf
destination = /pdf-reports
schedule = daily
times = 23:45
```

### Global settings

- `timezone`: required IANA time-zone name, such as `Europe/Moscow` or `UTC`.
- `credentials_file`: absolute path to the private credential JSON file.
- `log_file`: absolute path for the rotating log.
- `lock_file`: absolute path for the process-wide exclusive lock.
- `[dropbox] app_key`: the app key from the Dropbox App Console.

### Jobs and destination mapping

Every job has exactly one `source_type`, `source`, `destination`, and
`schedule`.

- `directory`: recursively mirrors the **contents** into the destination,
  without adding the source directory's basename. Hidden files and empty
  directories are included.
- `file`: mirrors one regular file to the exact destination file path. Its
  destination is not treated as a directory; siblings are not managed.
- `glob`: matches files in one parent directory using wildcards only in the
  final filename component. `*`, `?`, and bracket character classes are
  supported. Parent-component wildcards and recursive `**` are not supported;
  directories matched by the pattern are skipped. Matched files are placed
  directly in the dedicated destination directory by basename.

Sources must be absolute local paths. Destinations must be absolute Dropbox
App Folder paths beginning with `/`, but `/` itself is forbidden. Empty
components, `.`/`..`, trailing slash, backslash, control characters, and
ambiguous/unsupported path characters are rejected. Job destinations cannot
be equal or ancestor/descendant paths of another job, ignoring case.

### Exclusions

Exclusions apply to source-relative names and use a small case-sensitive
pattern grammar, not the complete `.gitignore` language:

- A basename pattern without `/`, such as `*.tmp`, matches object names at any
  depth and supports `*`, `?`, and bracket character classes.
- A pattern ending in `/`, such as `cache/`, matches directories with that
  name at any depth and their entire subtrees.
- A pattern with `/`, such as `data/cache/`, matches that relative path and
  everything below it. These path-specific patterns are literal (wildcards
  are rejected); the trailing slash is optional.

Excluded objects are never uploaded or deleted remotely, including when they
are absent locally. Their ancestor directories are kept if deleting them
could remove protected descendants. Exclusions also protect matching remote
cleanup candidates.

### Schedules

Each job uses exactly one schedule mode:

- `schedule = interval` with a positive integer `interval` and one unit:
  `s` (seconds), `m` (minutes), or `h` (hours), e.g. `30m`.
- `schedule = daily` with one or more distinct comma-separated 24-hour
  `HH:MM` values in `times`, e.g. `08:00, 23:45`.

All interval jobs run immediately at startup. Their interval grid is anchored
to scheduler startup and measured with a monotonic clock, not job completion.
Missed interval boundaries do not accumulate. Daily jobs start at the next
future occurrence and are not run immediately at startup. A daily time skipped
while the process was stopped is not caught up. During a fall-back clock
change, a configured local time runs at most once per local date in the
process; nonexistent spring-forward times are skipped. Jobs execute
sequentially in INI section order when due. Each job can have only one pending
run; further triggers while it is running or pending are dropped.

## systemd service

Create a dedicated ordinary user and writable state/log directories. For
example, replace `dropbox-mirror` with your chosen service account and adjust
all installation paths to match your clone:

```bash
sudo useradd --system --create-home --home-dir /var/lib/dropbox-mirror \
  --shell /usr/sbin/nologin dropbox-mirror
sudo install -d -o dropbox-mirror -g dropbox-mirror -m 0700 /var/lib/dropbox-mirror
sudo install -d -o dropbox-mirror -g dropbox-mirror -m 0750 /var/log/dropbox-mirror
sudo install -d -o root -g dropbox-mirror -m 0750 /etc/dropbox-mirror
sudo install -o root -g dropbox-mirror -m 0640 \
  "$HOME/.dropbox-mirror/config.ini" /etc/dropbox-mirror/config.ini
```

Before installing the configuration, adjust its credentials, log, lock, and
source paths for the service user. The credentials used by the service must be
created by authorizing as that user, as shown below.

The service template is [`systemd/dropbox-mirror@.service`](systemd/dropbox-mirror@.service).
Replace `/opt/tiny-dropbox-backup` and `/etc/dropbox-mirror/config.ini` in it
with absolute installation paths, then install and authorize as the service
user:

```bash
sudo install -m 0644 systemd/dropbox-mirror@.service \
  /etc/systemd/system/dropbox-mirror@.service
sudo -u dropbox-mirror /opt/tiny-dropbox-backup/.venv/bin/dropbox-mirror \
  --config /etc/dropbox-mirror/config.ini auth
sudo systemctl daemon-reload
sudo systemctl enable --now dropbox-mirror@dropbox-mirror.service
```

The unit runs directly from the virtual environment as the named non-root
account, starts after network-online, restarts on failure with a delay and
systemd start-rate limit, and handles SIGTERM. `TimeoutStopSec=infinity` is
intentional: systemd will not kill the process after an arbitrary short stop
timeout while it finishes its current file and bounded request retries. The
trade-off is that stopping can take a long time for a large file or a slow
network; each request uses a finite 30-second connect/read timeout, not a hard
overall deadline. A host administrator can still force termination.

The wrappers [`scripts/start.sh`](scripts/start.sh),
[`scripts/stop.sh`](scripts/stop.sh), and
[`scripts/restart.sh`](scripts/restart.sh) call `sudo systemctl`. Their default
unit name is `dropbox-mirror@dropbox-mirror.service`; override it by setting
`SERVICE_NAME`. No status wrapper is provided.

If needed, make the wrappers executable with
`chmod +x scripts/start.sh scripts/stop.sh scripts/restart.sh`.

## Logs and troubleshooting

Logs go to `[general] log_file`, rotate at 5 MiB, and keep three backups.
Timestamps include the configured time-zone offset. Successful files are not
logged individually. Each executed job has one summary with:

- `files_examined`: local entries visited (files and directories for directory
  sources; matched entries for globs; one for a single-file source).
- `files_uploaded`, `files_unchanged`, `files_deleted`: file counts.
- `directories_ensured`: destination directories checked/created during that
  run, including existing directories; it is not a count of new folders only.
- `directories_deleted`: remote directory entries removed.
- `errors`, `duration_seconds`, `cleanup_skipped`, and the outcome.

Common checks:

- Verify the INI path and that its source paths exist and are readable by the
  service user.
- For authorization failures, run `auth` again as the service user; verify the
  app key, App Folder access, and required scopes in the App Console.
- Check that the credential parent, log directory, and lock directory are
  writable by the service user.
- A job with an incomplete source inventory, API listing, hashing, or upload
  intentionally reports an error and skips cleanup. Correct the cause and let
  the next scheduled run reconcile it.
- A file/directory conflict is intentionally not fixed by deleting either
  object. Resolve it manually in Dropbox or in the local source, then let a
  later run mirror the resolved structure.
- Logs never include credential contents. Protect the log and config files as
  operational data.

## Development

```bash
python -m pip install -e ".[test]"
pytest
```

The tests use temporary local directories, a fake remote store, and injected
clocks/sleep functions. They do not authenticate with Dropbox or access a real
account.

## License

MIT. Replace the obvious copyright placeholders in [`LICENSE`](LICENSE) with
the actual year and rights holder before distribution.

## Official Dropbox references

- [OAuth authorization and PKCE](https://docs.dropboxapi.com/dropbox-api/docs/get-started/authorization.md)
- [Dropbox OAuth scopes](https://docs.dropboxapi.com/dropbox-api/docs/oauth.md)
- [Files list_folder](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/list-folder.md)
- [Files upload and WriteMode](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/upload.md)
- [Upload session endpoints](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/upload-session-start.md)
- [Dropbox content_hash](https://docs.dropboxapi.com/dropbox-api/docs/technical-reference/content-hash.md)
- [Dropbox error handling](https://docs.dropboxapi.com/dropbox-api/docs/error-handling.md)
