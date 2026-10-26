"""Wrappers around virsh and qemu-img."""
import json
import os
import subprocess
import xml.etree.ElementTree as ET

from . import config

ENV = dict(os.environ, LC_ALL="C", LANG="C")


class CmdError(Exception):
    pass


def run(cmd, timeout=3600, check=True):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)
    except FileNotFoundError:
        raise CmdError(f"Program not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise CmdError(f"Command timed out: {' '.join(cmd)}")
    if check and p.returncode != 0:
        msg = (p.stderr or p.stdout).strip()
        raise CmdError(f"{' '.join(cmd[:4])}…: {msg}" if len(cmd) > 4 else f"{' '.join(cmd)}: {msg}")
    return p


def virsh(*args, check=True, timeout=600):
    return run(["virsh", "-q", "-c", config.get()["libvirt_uri"], *args], timeout=timeout, check=check)


def _lines(text):
    return [l.strip() for l in text.splitlines() if l.strip()]


# --- Domains ----------------------------------------------------------------

def list_domains():
    return sorted(_lines(virsh("list", "--all", "--name").stdout))


def domain_exists(name):
    return virsh("domuuid", name, check=False).returncode == 0


def domain_uuids():
    return set(_lines(virsh("list", "--all", "--uuid").stdout))


def domain_state(name):
    return virsh("domstate", name).stdout.strip()


def domain_xml(name):
    return virsh("dumpxml", "--inactive", "--security-info", name).stdout


def autostart(name):
    for line in virsh("dominfo", name).stdout.splitlines():
        if line.startswith("Autostart:"):
            return "enable" in line
    return False


# --- Snapshots --------------------------------------------------------------

def snapshot_list(name):
    p = virsh("snapshot-list", name, "--name", "--topological", check=False)
    if p.returncode != 0:
        p = virsh("snapshot-list", name, "--name")
    return _lines(p.stdout)


def snapshot_xml(name, snap):
    return virsh("snapshot-dumpxml", name, snap, "--security-info").stdout


def snapshot_current(name):
    p = virsh("snapshot-current", "--name", name, check=False)
    out = p.stdout.strip()
    return out if p.returncode == 0 and out else None


# --- Networks and pools-----------------------------------------------------

def networks():
    return set(_lines(virsh("net-list", "--all", "--name").stdout))


def network_uuids():
    return set(_lines(virsh("net-list", "--all", "--uuid").stdout))


def net_xml(net):
    return virsh("net-dumpxml", "--inactive", net).stdout


def net_autostart(net):
    for line in virsh("net-info", net).stdout.splitlines():
        if line.startswith("Autostart:"):
            return "yes" in line
    return False


def pools():
    """{name: directory path} for dir-type pools."""
    res = {}
    for name in _lines(virsh("pool-list", "--all", "--name").stdout):
        try:
            root = ET.fromstring(virsh("pool-dumpxml", name).stdout)
        except (CmdError, ET.ParseError):
            continue
        path = root.findtext("target/path")
        if path:
            res[name] = os.path.normpath(path)
    return res


def vol_path(pool, vol):
    return virsh("vol-path", "--pool", pool, vol).stdout.strip()


def capabilities():
    return ET.fromstring(virsh("capabilities").stdout)


def versions():
    out = {}
    p = virsh("version", check=False)
    lib = hyp = ""
    for line in p.stdout.splitlines():
        if line.startswith("Using library:"):
            lib = line.split(":", 1)[1].strip()
        elif line.startswith("Running hypervisor:"):
            hyp = line.split(":", 1)[1].strip()
    out["libvirt"] = ", ".join(x for x in (lib, hyp) if x)
    p = run(["qemu-img", "--version"], check=False)
    out["qemu_img"] = (p.stdout.splitlines() or [""])[0]
    return out


# --- qemu-img ---------------------------------------------------------------

def qemu_img_chain(path):
    p = run(["qemu-img", "info", "--backing-chain", "--output=json", "-U", path])
    data = json.loads(p.stdout)
    return data if isinstance(data, list) else [data]


def qemu_img_rebase_unsafe(path, backing, backing_fmt):
    cmd = ["qemu-img", "rebase", "-u", "-b", backing]
    if backing_fmt:
        cmd += ["-F", backing_fmt]
    run(cmd + [path])
