# kvm-backup

Full backup and migration of KVM/libvirt virtual machines between Debian servers: configuration, disks and the whole snapshot tree, including branches. Runs in the terminal and depends only on the Python 3 standard library, `virsh` and `qemu-img` (no Cockpit, no Debian Python packages).

## Install (on both servers)

```
curl -fsSL https://raw.githubusercontent.com/pavlobilan/kvm-backup/main/install.sh | sudo bash
```

The installer then starts an interactive menu: **Back up a VM** or **Restore a VM**. Every stage is shown as a numbered step and asks only when a choice is needed. Later the menu is started with `sudo kvm-backup`.

The user who runs `sudo` is added to the `kvmbackup` group, which lets WinSCP read and write the needed files without root (takes effect after the next login).

## Back up (old server)

1. Choose a virtual machine.
2. Review it (state, snapshot tree, disk files) and confirm; a running VM is shut down.
3. The configuration is saved to a single archive: `/var/lib/kvm-backup/<VM>_<date>.tar.gz`.
4. The tool prints the install command for the new server, every file to copy with its source directory, the target directory on the new server (`/var/lib/kvm-backup/`) and what to run there. Copy the files with WinSCP, then press Enter: temporary read access is removed and the VM is started again if it was running.

## Restore (new server)

1. Choose a backup archive from `/var/lib/kvm-backup/` (the tool waits if nothing has been copied yet).
2. Every file listed in the archive is checked for presence and size; missing files are listed and the check can be repeated after copying them.
3. After confirmation, checksums are verified, disks are moved to their original paths and the VM is recreated with all snapshots.
4. The result is shown: disks, snapshot tree, current snapshot, VM state. The VM appears in Cockpit, ready to start.

## Screenshots

Backup on the old server:

![Backup](docs/backup.png)

Restore on the new server:

![Restore](docs/restore.png)

Restore stopped because a copied file is damaged:

![Damaged file](docs/restore-damaged-file.png)

## Limitations

- Only file-based disks (qcow2, raw) are migrated; block devices (LVM) are reported and skipped.
- Network bridges (`br0` etc.) must exist on the new server.
- Snapshots with saved memory may fail to revert on a host with a different CPU or a much older QEMU.

## Uninstall

```
sudo /opt/kvm-backup/uninstall.sh
```
