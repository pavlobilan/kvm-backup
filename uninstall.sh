#!/usr/bin/env bash
# kvm-backup: uninstall. Archives in /var/lib/kvm-backup are kept.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
rm -f /usr/local/bin/kvm-backup
rm -rf /opt/kvm-backup
echo "kvm-backup removed. Archives kept in /var/lib/kvm-backup."
