#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$(realpath "$0")")/scripts/common.sh"

if [[ "$OSTYPE" != linux-gnu* ]] || [[ "$USER_ID" == 0 ]]; then
    echo "Run this uninstaller as the user who installed the application, not as root." >&2
    exit 1
fi
check_unit_ownership

if [[ -e "$UNIT_PATH" ]]; then
    sudo systemctl disable --now "$SERVICE_NAME"
    sudo rm -- "$UNIT_PATH"
    sudo systemctl daemon-reload
fi

if [[ -d "$PROJECT_DIR/.venv" && ! -L "$PROJECT_DIR/.venv" &&
      -f "$PROJECT_DIR/.venv/.tiny-dropbox-backup-created" ]] &&
   [[ $(< "$PROJECT_DIR/.venv/.tiny-dropbox-backup-created") == "$PROJECT_DIR" ]]; then
    rm -rf -- "$PROJECT_DIR/.venv"
elif [[ -x "$PROJECT_DIR/.venv/bin/python" &&
        -f "$PROJECT_DIR/.venv/.tiny-dropbox-backup-installed" ]] &&
     [[ $(< "$PROJECT_DIR/.venv/.tiny-dropbox-backup-installed") == "$PROJECT_DIR" ]]; then
    "$PROJECT_DIR/.venv/bin/python" -m pip uninstall -y tiny-dropbox-backup
    rm -- "$PROJECT_DIR/.venv/.tiny-dropbox-backup-installed"
    echo "Preserved the pre-existing virtual environment and its dependencies."
fi

echo "Uninstalled. Configuration, credentials, logs, source files, and Dropbox data were preserved."
