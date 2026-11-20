#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-dropbox-mirror@dropbox-mirror.service}"
sudo systemctl restart "$SERVICE_NAME"
