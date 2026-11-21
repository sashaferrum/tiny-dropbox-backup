#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$(realpath "$0")")/common.sh"
exec "$PROJECT_DIR/.venv/bin/dropbox-mirror" --config "$CONFIG_FILE" auth
