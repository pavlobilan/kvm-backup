import json
import os

CONFIG_DIR = os.environ.get("KBP_CONFIG_DIR", "/etc/kvm-backup")

DEFAULTS = {
    "backup_root": "/var/lib/kvm-backup",
    "transfer_group": "kvmbackup",
    "libvirt_uri": "qemu:///system",
    "shutdown_timeout": 180,
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


