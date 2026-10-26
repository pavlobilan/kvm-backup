# kvm-backup

Full backup and migration of KVM/libvirt virtual machines between Debian servers: configuration, disks and the whole snapshot tree, including branches. Runs in the terminal and depends only on the Python 3 standard library, `virsh` and `qemu-img` (no Cockpit, no Debian Python packages).

## Install (on both servers)

```
curl -fsSL https://raw.githubusercontent.com/pavlobilan/kvm-backup/main/install.sh | sudo bash
```

When the installation finishes, the interactive menu opens automatically: choose **1) Back up a VM** or **2) Restore a VM**. No separate command is needed after installing. Every stage is shown as a numbered step and asks only when a choice is needed.

## Run again (already installed)

```
sudo kvm-backup
```

This opens the same menu. To skip the menu:

```
sudo kvm-backup backup
sudo kvm-backup restore
```

Running the install command again updates kvm-backup to the latest version and also opens the menu.

The user who runs `sudo` is added to the `kvmbackup` group, which lets WinSCP read and write the needed files without root (takes effect after the next login).

## Back up (old server)

1. Choose a virtual machine.
2. Review it (state, snapshot tree, disk files). A running VM is never shut down by the tool: it asks you to shut it down and checks again.
3. A SHA-256 checksum of every disk and snapshot file is calculated, and the configuration is saved to a single archive: `/var/lib/kvm-backup/<VM>_<date>.tar.gz`.
4. The tool prints the install command for the new server, every file to copy with its source directory, the target directory on the new server (`/var/lib/kvm-backup/`) and what to run there. Copy the files with WinSCP while the VM stays shut off, then press Enter: temporary read access is removed and the tool reports whether the VM stayed off.

## Restore (new server)

1. Choose a backup archive from `/var/lib/kvm-backup/` (the tool waits if nothing has been copied yet).
2. Every file listed in the archive is checked for presence and size; missing files are listed and the check can be repeated after copying them.
3. After confirmation, checksums are verified, disks are moved to their original paths and the VM is recreated with all snapshots.
4. The result is shown: disks, snapshot tree, current snapshot, VM state. The VM appears in Cockpit, ready to start.

## Checksums

- During backup, the SHA-256 checksum and size of every disk and snapshot file are stored in `manifest.json` inside the archive.
- During restore, every file is checked for presence and size, then its SHA-256 is compared with the archive. On a mismatch the restore stops before anything is changed on the server.
- Not covered by a separate checksum: ISO images (optional), the configuration archive itself (protected only by the gzip integrity check) and the result of copying disks between different file systems on the new server.

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
