#!/usr/bin/env bash
# kvm-backup: install or update.
#   curl -fsSL https://raw.githubusercontent.com/pavlobilan/kvm-backup/main/install.sh | sudo bash
set -euo pipefail

REPO="${KVMB_REPO:-https://github.com/pavlobilan/kvm-backup}"
BRANCH="${KVMB_BRANCH:-main}"

PREFIX=/opt/kvm-backup
DATA_DIR=/var/lib/kvm-backup
GROUP=kvmbackup

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo."
[ -f /etc/debian_version ] || die "only Debian is supported."

export DEBIAN_FRONTEND=noninteractive
PKGS="python3 qemu-utils libvirt-clients acl curl ca-certificates util-linux"
if ! command -v virsh >/dev/null 2>&1 || { [ ! -x /usr/sbin/libvirtd ] && [ ! -x /usr/sbin/virtqemud ]; }; then
  say "libvirt/KVM not found, installing it"
  PKGS="$PKGS libvirt-daemon-system qemu-system-x86 ovmf swtpm swtpm-tools"
fi
say "Installing dependencies"
apt-get update -qq </dev/null
# shellcheck disable=SC2086
apt-get install -y -qq --no-install-recommends $PKGS </dev/null >/dev/null

say "Downloading kvm-backup"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/kvmbackup/cli.py" ]; then
  cp -a "$SRC_DIR/." "$TMP/src"
  SOURCE=local
else
  curl -fsSL "$REPO/archive/refs/heads/$BRANCH.tar.gz" -o "$TMP/src.tar.gz" || die "failed to download $REPO"
  mkdir "$TMP/src"
  tar -xzf "$TMP/src.tar.gz" -C "$TMP/src" --strip-components=1
fi
[ -f "$TMP/src/kvmbackup/cli.py" ] || die "the downloaded archive does not contain kvm-backup."

rm -rf "$PREFIX"
mkdir -p "$PREFIX"
cp -a "$TMP/src/kvmbackup" "$TMP/src/uninstall.sh" "$PREFIX/"
rm -rf "$PREFIX/kvmbackup/__pycache__"
# Remember the source so kvm-backup can print the install command for the new server
if [ "${SOURCE:-}" = local ]; then
  echo "local" > "$PREFIX/REPO"
else
  echo "$REPO $BRANCH" > "$PREFIX/REPO"
fi
chown -R root:root "$PREFIX"
chmod -R go-w "$PREFIX"

cat > /usr/local/bin/kvm-backup <<EOF
#!/bin/sh
exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, "$PREFIX"); from kvmbackup.cli import main; sys.exit(main())' "\$@"
EOF
chmod 755 /usr/local/bin/kvm-backup

# Group allowed to read disks (old server) and write to $DATA_DIR (new server) via WinSCP
getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  usermod -aG "$GROUP" "$SUDO_USER"
  say "User $SUDO_USER added to group $GROUP (takes effect after the next SSH/WinSCP login)"
fi
mkdir -p "$DATA_DIR"
chown root:"$GROUP" "$DATA_DIR"
chmod 2775 "$DATA_DIR"

say "kvm-backup is installed. Start it again any time with: sudo kvm-backup"

# Start the interactive menu; the keyboard is read from /dev/tty because stdin is the curl pipe
if [ -z "${KVMB_NO_MENU:-}" ] && { : </dev/tty; } 2>/dev/null; then
  echo
  /usr/local/bin/kvm-backup </dev/tty || true
fi
