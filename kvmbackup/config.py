import json
import os

CONFIG_DIR = os.environ.get("KBP_CONFIG_DIR", "/etc/kvm-backup")

DEFAULTS = {
    # Inside the default libvirt storage folder, so copied disk files are on the same
    # disk as the VMs and can be moved into place without extra space
    "backup_root": "/var/lib/libvirt/images/kvm-backup",
    # Folders used by earlier versions; archives found there are still offered for restore
    "legacy_roots": ["/var/lib/kvm-backup"],
    "transfer_group": "kvmbackup",
    "libvirt_uri": "qemu:///system",
    "nvram_dir": "/var/lib/libvirt/qemu/nvram",
    "swtpm_dir": "/var/lib/libvirt/swtpm",
}

_cfg = None


def get():
    global _cfg
    if _cfg is None:
        cfg = dict(DEFAULTS)
        path = os.path.join(CONFIG_DIR, "config.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        _cfg = cfg
    return _cfg


