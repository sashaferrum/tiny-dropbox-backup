#!/usr/bin/env bash

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
USER_ID=$(id -u)
STATE_DIR="${HOME}/.dropbox-mirror"
CONFIG_FILE="${STATE_DIR}/config.ini"
SERVICE_NAME="tiny-dropbox-backup-${USER_ID}.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"

if [[ "$PROJECT_DIR" == *$'\n'* || "$HOME" == *$'\n'* || "$HOME" != /* ]]; then
    echo "The installation directory and HOME must be absolute paths without newlines." >&2
    exit 1
fi

check_unit_ownership() {
    if [[ -L "$UNIT_PATH" ]] || { [[ -e "$UNIT_PATH" ]] &&
        ! grep -Fxq -- "# Installed by tiny-dropbox-backup: $PROJECT_DIR" "$UNIT_PATH"; }; then
        echo "Refusing to overwrite or remove a service owned by another installation: $UNIT_PATH" >&2
        exit 1
    fi
}

unit_escape() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    if [[ $2 == yes ]]; then
        value=${value//\$/\$\$}
    fi
    printf '%s' "$value"
}
