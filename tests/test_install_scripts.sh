#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
export HOME="$tmp/home %\$ &"
export TEST_UNIT_DIR="$tmp/units" TEST_LOG="$tmp/commands.log"
mkdir -p "$HOME" "$TEST_UNIT_DIR" "$tmp/bin"
project="$tmp/project %\$ &"
mkdir -p "$project"
cp -R "$root/install.sh" "$root/uninstall.sh" "$root/scripts" "$root/systemd" \
    "$root/config.example.ini" "$project/"
sed -i 's|^UNIT_PATH=.*|UNIT_PATH="${TEST_UNIT_DIR}/${SERVICE_NAME}"|' "$project/scripts/common.sh"

cat > "$tmp/bin/id" <<'EOF'
#!/usr/bin/env bash
echo 987654
EOF
cat > "$tmp/bin/sudo" <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF
cat > "$tmp/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$TEST_LOG"
EOF
cat > "$tmp/bin/python3.11" <<'EOF'
#!/usr/bin/env bash
if [[ $1 == -c ]]; then exit 0; fi
if [[ $1 != -m || $2 != venv ]]; then exit 1; fi
mkdir -p "$3/bin"
cat > "$3/bin/python" <<'INNER'
#!/usr/bin/env bash
printf 'python %s\n' "$*" >> "$TEST_LOG"
INNER
cat > "$3/bin/dropbox-mirror" <<'INNER'
#!/usr/bin/env bash
printf 'dropbox-mirror' >> "$TEST_LOG"
printf ' <%s>' "$@" >> "$TEST_LOG"
printf '\n' >> "$TEST_LOG"
INNER
chmod +x "$3/bin/python" "$3/bin/dropbox-mirror"
EOF
chmod +x "$tmp/bin/"*
export PATH="$tmp/bin:$PATH"
unit="$TEST_UNIT_DIR/tiny-dropbox-backup-987654.service"

cd "$project"
bash install.sh
test -f "$unit"
test -f "$HOME/.dropbox-mirror/config.ini"
grep -Fxq 'systemctl enable tiny-dropbox-backup-987654.service' "$TEST_LOG"
if grep -q 'systemctl start' "$TEST_LOG"; then exit 1; fi
grep -Fq 'project %%$ &' "$unit"
grep -Fq 'home %%$$ &' "$unit"
if command -v systemd-analyze >/dev/null; then
    systemd-analyze verify "$unit"
fi
printf 'user configuration\n' > "$HOME/.dropbox-mirror/config.ini"
bash install.sh
grep -Fxq 'user configuration' "$HOME/.dropbox-mirror/config.ini"
./scripts/auth.sh
grep -Fxq "dropbox-mirror <--config> <$HOME/.dropbox-mirror/config.ini> <auth>" "$TEST_LOG"
./scripts/start.sh
./scripts/stop.sh
./scripts/restart.sh
grep -Fxq 'systemctl start tiny-dropbox-backup-987654.service' "$TEST_LOG"
grep -Fxq 'systemctl stop tiny-dropbox-backup-987654.service' "$TEST_LOG"
grep -Fxq 'systemctl restart tiny-dropbox-backup-987654.service' "$TEST_LOG"
./uninstall.sh
test ! -e "$unit"
test ! -e .venv
test -f "$HOME/.dropbox-mirror/config.ini"
grep -Fxq 'systemctl disable --now tiny-dropbox-backup-987654.service' "$TEST_LOG"

# An existing environment is reused and kept on uninstall.
python3.11 -m venv .venv
touch .venv/keep
bash install.sh
./uninstall.sh
test -f .venv/keep
grep -Fxq 'python -m pip uninstall -y tiny-dropbox-backup' "$TEST_LOG"

# Never overwrite or remove another installation's service.
printf '# foreign unit\n' > "$unit"
if bash install.sh; then exit 1; fi
if ./uninstall.sh; then exit 1; fi
grep -Fxq '# foreign unit' "$unit"
echo "Installer integration tests passed."
