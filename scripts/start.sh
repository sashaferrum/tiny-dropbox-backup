#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$(realpath "$0")")/common.sh"
sudo systemctl start "$SERVICE_NAME"
