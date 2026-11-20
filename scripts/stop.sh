#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-dropbox-mirror@dropbox-mirror.service}"
sudo systemctl stop "$SERVICE_NAME"
