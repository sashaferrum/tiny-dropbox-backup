#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$(realpath "$0")")/scripts/common.sh"

if [[ "$OSTYPE" != linux-gnu* ]] || [[ "$USER_ID" == 0 ]]; then
    echo "Run this installer as an ordinary user on Linux, not as root." >&2
    exit 1
fi
for command in git systemctl sudo; do
    if ! command -v "$command" >/dev/null; then
        echo "Required command not found: $command" >&2
        exit 1
    fi
done
check_unit_ownership

PYTHON_CMD=""
for candidate in python3.11 python3.12 python3.13 python3.14 python3; do
    if command -v "$candidate" >/dev/null &&
       "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        PYTHON_CMD="$candidate"
        break
    fi
done
if [[ -z "$PYTHON_CMD" ]]; then
    echo "Python 3.11 or newer is required." >&2
    exit 1
fi

cd "$PROJECT_DIR"
if [[ ! -e .venv && ! -L .venv ]]; then
    "$PYTHON_CMD" -m venv .venv
    printf '%s\n' "$PROJECT_DIR" > .venv/.tiny-dropbox-backup-created
elif [[ ! -x .venv/bin/python ]]; then
    echo "Existing .venv is not a usable virtual environment." >&2
    exit 1
fi

.venv/bin/python -m pip install --quiet --upgrade pip setuptools
.venv/bin/python -m pip install --quiet .
printf '%s\n' "$PROJECT_DIR" > .venv/.tiny-dropbox-backup-installed
.venv/bin/dropbox-mirror --help >/dev/null

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
if [[ ! -e "$CONFIG_FILE" && ! -L "$CONFIG_FILE" ]]; then
    (umask 077; cp config.example.ini "$CONFIG_FILE")
fi
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Configuration is not a regular file: $CONFIG_FILE" >&2
    exit 1
fi

chmod +x scripts/auth.sh scripts/start.sh scripts/stop.sh scripts/restart.sh uninstall.sh

unit_file=$(mktemp)
trap 'rm -f -- "$unit_file"' EXIT
{
    printf '# Installed by tiny-dropbox-backup: %s\n' "$PROJECT_DIR"
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            'User=@USER_ID@') printf 'User=%s\n' "$USER_ID" ;;
            'ExecStart="@EXECUTABLE@" --config "@CONFIG_FILE@" run')
                printf 'ExecStart="%s" --config "%s" run\n' \
                    "$(unit_escape "$PROJECT_DIR/.venv/bin/dropbox-mirror" no)" \
                    "$(unit_escape "$CONFIG_FILE" yes)" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < systemd/dropbox-mirror@.service
} > "$unit_file"
sudo install -m 0644 "$unit_file" "$UNIT_PATH"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "Installation complete. Edit $CONFIG_FILE, then run:"
echo "  ./scripts/auth.sh"
echo "  ./scripts/start.sh"
