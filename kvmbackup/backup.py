"""VM analysis and backup creation (metadata archived separately from disk files)."""
import datetime
import json
import os
import re
import shutil
import socket
import tarfile
import xml.etree.ElementTree as ET

from . import config, fsutil, virt, xmlutil
from .jobs import JobError, human

FORMAT = "kvm-backup"
VERSION = 1


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._") or "vm"


def analyse(vm):
    """Collect everything about the VM without changing anything on disk."""
    if not virt.domain_exists(vm):
        raise JobError(f"VM '{vm}' not found.")
    warnings = []

    def warn(msg):
        if msg not in warnings:
            warnings.append(msg)

    dom_xml = virt.domain_xml(vm)
    dom = ET.fromstring(dom_xml)

    snaps = []
    for n in virt.snapshot_list(vm):
        x = virt.snapshot_xml(vm, n)
        info = xmlutil.parse_snapshot(x)
        info["name"] = n
        info["xml"] = x
        snaps.append(info)
    snaps = xmlutil.topo_sort(snaps)
    current = virt.snapshot_current(vm)

    volume_map, roots = {}, []

    def add_domain_disks(el, origin):
        for e in xmlutil.disk_entries(el):
            path = e["path"]
            if e["type"] == "volume":
                key = f"{e['pool']}/{e['volume']}"
                if key not in volume_map:
                    try:
                        volume_map[key] = virt.vol_path(e["pool"], e["volume"])
                    except virt.CmdError as ex:
                        warn(f"Could not resolve volume {key} ({origin}): {ex}")
                        continue
                path = volume_map[key]
            elif e["type"] == "block":
                warn(f"Disk {e['target']} ({path}) is a block device. Only file-based disks are migrated; "
                     f"move this device separately.")
                continue
            elif e["type"] != "file":
                if e["device"] == "disk":
                    warn(f"Disk {e['target']} has type '{e['type']}', which is not migrated.")
                continue
            if path:
                roots.append((path, e["device"], origin))

    add_domain_disks(dom, "VM")
    for s in snaps:
        root = ET.fromstring(s["xml"])
        origin = f"snapshot '{s['name']}'"
        for el in xmlutil.snapshot_domains(root):
            add_domain_disks(el, origin)
        for d in s["disks"]:
            if d["file"]:
                roots.append((d["file"], "overlay", origin))
        if s["memory_file"]:
            roots.append((s["memory_file"], "memory", origin))

    files = {}
    dangling, missing = [], set()  # overlay paths libvirt deleted when reverting to a snapshot

    def file_entry(path, role, fmt=None, required=True):
        st = os.stat(path)
        u, g = fsutil.owner_names(path)
        return {"original_path": path, "role": role, "format": fmt, "required": required,
                "size": st.st_size, "allocated": st.st_blocks * 512, "owner": u, "group": g,
                "mode": oct(st.st_mode & 0o7777), "backing_raw": None, "backing_original": None,
                "backing_format": None, "virtual_size": None, "internal_snapshots": [], "sha256": None}

    for path, device, origin in roots:
        if path in files:
            continue
        if not os.path.exists(path):
            if device == "overlay":
                if path not in dangling:
                    dangling.append(path)
                continue
            if path in missing:
                continue
            missing.add(path)
            if device in ("cdrom", "floppy"):
                warn(f"Image {path} not found; the drive will be empty.")
            else:
                warn(f"File {path} ({origin}) not found; it will not be in the backup.")
            continue
        if device in ("cdrom", "floppy"):
            files[path] = file_entry(path, "iso", "raw", required=False)
            continue
        if device == "overlay":
            device = "disk"
        if device == "memory":
            files[path] = file_entry(path, "memory")
            continue
        try:
            chain = virt.qemu_img_chain(path)
        except virt.CmdError as ex:
            warn(f"qemu-img could not read {path}: {ex}")
            files[path] = file_entry(path, "disk")
            continue
        for i, ci in enumerate(chain):
            fn = ci.get("filename")
            if not fn or fn.startswith("json:") or fn in files:
                continue
            if not os.path.exists(fn):
                warn(f"Backing chain file {fn} not found.")
                continue
            e = file_entry(fn, "disk" if i == 0 else "backing", ci.get("format"))
            e["virtual_size"] = ci.get("virtual-size")
            e["internal_snapshots"] = [x.get("name") for x in ci.get("snapshots", []) or []]
            if ci.get("backing-filename"):
                e["backing_raw"] = ci.get("backing-filename")
                nxt = chain[i + 1]["filename"] if i + 1 < len(chain) else None
                e["backing_original"] = nxt or ci.get("full-backing-filename")
                e["backing_format"] = ci.get("backing-filename-format") or (
                    chain[i + 1].get("format") if i + 1 < len(chain) else None)
            files[fn] = e

    # Unique file names within the backup
    used = set()
    for e in files.values():
        base = os.path.basename(e["original_path"])
        name, n = base, 1
        while name in used:
            n += 1
            name = f"{n}_{base}"
        used.add(name)
        e["backup_name"] = name

    # Internal snapshots missing from qcow2 files
    internal_present = {x for e in files.values() for x in e["internal_snapshots"]}
    for s in snaps:
        if any(d["snapshot"] == "internal" for d in s["disks"]) and s["name"] not in internal_present:
            warn(f"Internal snapshot '{s['name']}' not found in any qcow2 file.")

    nv = xmlutil.nvram_path(dom)
    nvram = None
    if nv:
        if os.path.exists(nv):
            nvram = {"original_path": nv, "file": os.path.basename(nv)}
        else:
            warn(f"NVRAM file {nv} not found (it will be recreated from the template).")

    uuid = dom.findtext("uuid")
    tpm = None
    if xmlutil.has_tpm_emulator(dom):
        tdir = os.path.join(config.get()["swtpm_dir"], uuid or "")
        if uuid and os.path.isdir(tdir):
            u, g = fsutil.owner_names(tdir)
            tpm = {"original_path": tdir, "owner": u, "group": g}
        else:
            warn("Virtual TPM state not found; the TPM will be recreated.")

    net_names, bridges = [], []
    for el in [dom] + [d for s in snaps for d in xmlutil.snapshot_domains(ET.fromstring(s["xml"]))]:
        for n in xmlutil.interface_networks(el):
            if n not in net_names:
                net_names.append(n)
        for b in xmlutil.interface_bridges(el):
            if b not in bridges:
                bridges.append(b)
    existing_nets = virt.networks()
    networks = []
    for n in net_names:
        if n in existing_nets:
            networks.append({"name": n, "xml": virt.net_xml(n), "autostart": virt.net_autostart(n)})
        else:
            warn(f"libvirt network '{n}' not found on this host.")

    ostype = dom.find("os/type")
    return {
        "vm": vm, "uuid": uuid, "state": virt.domain_state(vm), "autostart": virt.autostart(vm),
        "arch": ostype.get("arch") if ostype is not None else None,
        "machine": ostype.get("machine") if ostype is not None else None,
        "memory_kib": dom.findtext("memory"), "vcpu": dom.findtext("vcpu"),
        "domain_xml": dom_xml, "disks": [
            {k: v for k, v in e.items()} for e in xmlutil.disk_entries(dom)],
        "snapshots": snaps, "current": current, "files": list(files.values()),
        "volume_map": volume_map, "dangling": dangling, "nvram": nvram, "tpm": tpm,
        "networks": networks, "bridges": bridges, "warnings": warnings,
        "total_size": sum(e["size"] for e in files.values()),
        "required_size": sum(e["size"] for e in files.values() if e["required"]),
    }


def summary(a):
    """Analysis summary without XML."""
    out = {k: v for k, v in a.items() if k not in ("domain_xml", "snapshots", "networks")}
    out["snapshots"] = [{k: s[k] for k in ("name", "parent", "state", "created", "description", "memory")}
                        for s in a["snapshots"]]
    out["networks"] = [n["name"] for n in a["networks"]]
    return out


def run_backup(job, vm, opts):
    job.step("Checking VM state", 1)
    state = virt.domain_state(vm)
    if state != "shut off":
        raise JobError(f"VM '{vm}' is {state}. Shut it down first; kvm-backup does not shut VMs down.")
    was_running = False

    job.step("Collecting configuration, disks and snapshots", 10)
    a = analyse(vm)
    for w in a["warnings"]:
        job.log("⚠ " + w)
    job.log(f"Snapshots: {len(a['snapshots'])}, disk files: {len(a['files'])}, size: {human(a['total_size'])}")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bid = f"{safe_name(vm)}_{stamp}"
    root = config.get()["backup_root"]
    work = os.path.join(root, ".work", bid)
    dest = os.path.join(work, bid)
    os.makedirs(os.path.join(dest, "snapshots"))
    archive = os.path.join(root, bid + ".tar.gz")
    try:
        with open(os.path.join(dest, "domain.xml"), "w", encoding="utf-8") as f:
            f.write(a["domain_xml"])
        snap_meta = []
        for i, s in enumerate(a["snapshots"], 1):
            fn = f"snapshots/{i:03d}_{safe_name(s['name'])}.xml"
            with open(os.path.join(dest, fn), "w", encoding="utf-8") as f:
                f.write(s["xml"])
            snap_meta.append({k: s[k] for k in ("name", "parent", "state", "created", "description", "memory")}
                             | {"file": fn})
        if a["nvram"]:
            os.makedirs(os.path.join(dest, "nvram"))
            shutil.copy2(a["nvram"]["original_path"], os.path.join(dest, "nvram", a["nvram"]["file"]))
        if a["tpm"]:
            shutil.copytree(a["tpm"]["original_path"], os.path.join(dest, "swtpm"), symlinks=True)
        net_meta = []
        if a["networks"]:
            os.makedirs(os.path.join(dest, "networks"))
            for n in a["networks"]:
                fn = f"networks/{safe_name(n['name'])}.xml"
                with open(os.path.join(dest, fn), "w", encoding="utf-8") as f:
                    f.write(n["xml"])
                net_meta.append({"name": n["name"], "file": fn, "autostart": n["autostart"]})

        if opts.get("checksums", True):
            todo = [e for e in a["files"] if e["role"] != "iso"]
            prog = job.bytes_progress(sum(e["size"] for e in todo), 15, 95, "SHA-256 checksums")
            for e in todo:
                e["sha256"] = fsutil.sha256_file(
                    e["original_path"], lambda n, b=e["backup_name"]: prog(n, b))
            prog.finish("calculated")

        job.step("Creating archive", 96)
        manifest = {
            "format": FORMAT, "version": VERSION,
            "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_host": socket.getfqdn(), "versions": virt.versions(),
            "vm": {"name": vm, "uuid": a["uuid"], "autostart": a["autostart"], "arch": a["arch"],
                   "machine": a["machine"], "memory_kib": a["memory_kib"], "vcpu": a["vcpu"],
                   "state_before": state},
            "domain_xml": "domain.xml", "snapshots": snap_meta, "current_snapshot": a["current"],
            "files": a["files"], "volume_map": a["volume_map"], "dangling_paths": a["dangling"], "nvram": a["nvram"],
            "tpm": ({"dir": "swtpm", "owner": a["tpm"]["owner"], "group": a["tpm"]["group"],
                     "original_path": a["tpm"]["original_path"]} if a["tpm"] else None),
            "networks": net_meta, "bridges": a["bridges"], "warnings": a["warnings"],
            "total_size": a["total_size"],
        }
        with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        with open(os.path.join(dest, "FILES.txt"), "w", encoding="utf-8") as f:
            f.write(files_list(manifest))
        tmp_archive = archive + ".part"
        with tarfile.open(tmp_archive, "w:gz") as tar:
            tar.add(dest, arcname=bid)
        os.rename(tmp_archive, archive)
        gid = fsutil.transfer_gid()
        if gid is not None:
            os.chown(archive, -1, gid)
        os.chmod(archive, 0o640)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    job.result = {"archive": archive, "manifest": manifest, "was_running": was_running}


def files_list(m):
    lines = [f"VM '{m['vm']['name']}', backup of {m['created']} (host {m['source_host']})", "",
             "Disk and snapshot files (all must be on the new server before restoring):", ""]
    for e in m["files"]:
        opt = "" if e["required"] else "  (optional, ISO)"
        lines.append(f"  {e['original_path']}  [{human(e['size'])}]{opt}")
    lines.append("")
    return "\n".join(lines)
