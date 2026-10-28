"""Verification and restore of a VM from a backup."""
import os
import re
import shutil
import uuid as uuidlib
import xml.etree.ElementTree as ET

from . import config, fsutil, virt, xmlutil
from .jobs import JobError, human

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def _machine_alias(machine):
    if not machine:
        return None
    if machine.startswith("pc-q35") or machine == "q35":
        return "q35"
    if machine.startswith("pc-i440fx") or machine == "pc":
        return "pc"
    if machine.startswith("virt"):
        return "virt"
    return None


def _host_arch_info(arch):
    caps = virt.capabilities()
    for g in caps.findall("guest"):
        a = g.find("arch")
        if a is None or a.get("name") != arch:
            continue
        machines = set()
        for m in a.findall("machine") + a.findall("domain/machine"):
            if m.text:
                machines.add(m.text.strip())
            if m.get("canonical"):
                machines.add(m.get("canonical"))
        return a.findtext("emulator"), machines
    return None, set()


def paths_in_use():
    """{path: VM name} for every disk file used by existing VMs (including chains and snapshots)."""
    used = {}
    for vm in virt.list_domains():
        try:
            roots = [d["path"] for d in xmlutil.disk_entries(ET.fromstring(virt.domain_xml(vm)))
                     if d["type"] == "file" and d["path"]]
            for sn in virt.snapshot_list(vm):
                r = ET.fromstring(virt.snapshot_xml(vm, sn))
                for el in xmlutil.snapshot_domains(r):
                    roots += [d["path"] for d in xmlutil.disk_entries(el) if d["type"] == "file" and d["path"]]
                roots += [s.get("file") for s in r.findall("disks/disk/source") if s.get("file")]
        except (virt.CmdError, ET.ParseError):
            continue
        for path in set(roots):
            used.setdefault(os.path.normpath(path), vm)
            if os.path.exists(path):
                try:
                    for ci in virt.qemu_img_chain(path):
                        if ci.get("filename"):
                            used.setdefault(os.path.normpath(ci["filename"]), vm)
                except virt.CmdError:
                    pass
    return used


def plan(m, bdir, opts):
    """Check whether the restore is possible and build a plan. Changes nothing.
    m: manifest, bdir: extracted archive, opts["import_dir"]: directory with the copied disk files."""
    cfg = config.get()
    errors, warnings = [], list(m.get("warnings") or [])
    import_dir = opts.get("import_dir") or cfg["backup_root"]
    use_original = bool(opts.get("use_original", True))
    target_dir = os.path.normpath(opts.get("target_dir") or cfg["backup_root"])
    if not os.path.isabs(target_dir):
        errors.append("The disk directory must be an absolute path.")
    name = (opts.get("name") or m["vm"]["name"]).strip()
    if not NAME_RE.match(name):
        errors.append("VM name may contain Latin letters, digits and . _ + -")
    if virt.domain_exists(name):
        errors.append(f"A VM named '{name}' already exists on this host.")

    old_uuid = m["vm"]["uuid"]
    new_ids = bool(opts.get("new_ids"))
    if not new_ids and old_uuid in virt.domain_uuids():
        new_ids = True
        warnings.append("A VM with the same UUID already exists; a new UUID and new MAC addresses will be generated.")
    renamed = name != m["vm"]["name"]

    files, copy_bytes = [], 0
    dests = set()
    in_use = paths_in_use()
    for e in m["files"]:
        dst = e["original_path"] if use_original else os.path.join(target_dir, e["backup_name"])
        cands = []
        for c in (os.path.join(import_dir, e["backup_name"]), os.path.join(import_dir, os.path.basename(e["original_path"])),
                  dst, e["original_path"]):
            if c not in cands:
                cands.append(c)
        found, mismatch = None, []
        for c in cands:
            if os.path.normpath(c) in in_use:
                continue  # file belongs to another VM; never use it as a source
            if os.path.isfile(c):
                if e.get("size") is None or os.path.getsize(c) == e["size"]:
                    found = c
                    break
                mismatch.append(f"{c} ({human(os.path.getsize(c))})")
        status = "ok" if found else ("size" if mismatch else "missing")
        item = {"name": e["backup_name"], "role": e["role"], "required": e["required"], "size": e["size"],
                "source": found, "dest": dst, "status": status, "action": None,
                "original_path": e["original_path"], "has_checksum": bool(e.get("sha256"))}
        busy = in_use.get(os.path.normpath(dst))
        if busy:
            errors.append(f"{e['backup_name']}: destination {dst} is already used by VM '{busy}'.")
        elif not found:
            where = f"{import_dir}/ or {e['original_path']}"
            if mismatch:
                msg = f"{e['backup_name']}: size differs from the original ({human(e['size'])}): {'; '.join(mismatch)}"
            else:
                msg = f"{e['backup_name']}: file not found (looked in: {where})"
            (errors if e["required"] else warnings).append(msg + ("" if e["required"] else "; the drive will be empty."))
        else:
            if os.path.abspath(found) == os.path.abspath(dst):
                item["action"] = "on-place"
            elif os.path.exists(dst):
                errors.append(f"Destination file {dst} already exists.")
            elif dst in dests:
                errors.append(f"Two files share the same destination {dst}.")
            else:
                if opts.get("mode", "move") == "move" and fsutil.same_fs(found, dst):
                    item["action"] = "rename"
                else:
                    item["action"] = "copy"
                    copy_bytes += e["size"]
        dests.add(dst)
        files.append(item)

    # Free space is checked on the file system each disk is copied to
    need = {}
    for f in files:
        if f["action"] == "copy":
            d = fsutil.existing_dir(os.path.dirname(f["dest"]))
            need.setdefault(os.stat(d).st_dev, [d, 0])[1] += f["size"]
    for d, size in need.values():
        free = fsutil.free_bytes(d)
        if free < size:
            errors.append(f"Not enough space in {d}: {human(size)} needed, {human(free)} free.")

    nvram_dst = None
    if m.get("nvram"):
        orig = m["nvram"]["original_path"]
        nvram_dst = orig if not renamed else os.path.join(cfg["nvram_dir"], f"{name}_VARS.fd")
        if os.path.exists(nvram_dst):
            alt = os.path.join(cfg["nvram_dir"], f"{name}_VARS.fd")
            if alt != nvram_dst and not os.path.exists(alt):
                nvram_dst = alt
            else:
                errors.append(f"NVRAM file {nvram_dst} already exists.")
        if not os.path.isfile(os.path.join(bdir, "nvram", m["nvram"]["file"])):
            errors.append("The NVRAM file is missing from the backup.")

    final_uuid = str(uuidlib.uuid4()) if new_ids else old_uuid
    if m.get("tpm"):
        tdir = os.path.join(cfg["swtpm_dir"], final_uuid)
        if os.path.exists(tdir):
            errors.append(f"TPM state directory {tdir} already exists.")

    nets = []
    existing = virt.networks()
    for n in m.get("networks", []):
        nets.append({"name": n["name"], "exists": n["name"] in existing})
    for b in m.get("bridges", []):
        if not os.path.exists(f"/sys/class/net/{b}"):
            warnings.append(f"Network bridge {b} does not exist on this host. Create it, "
                            f"otherwise the VM will not start (or change the VM network in Cockpit).")

    emulator, machines = _host_arch_info(m["vm"].get("arch") or "x86_64")
    dom = ET.parse(os.path.join(bdir, m["domain_xml"])).getroot()
    cur_em = dom.findtext("devices/emulator")
    set_emulator = None
    if cur_em and not os.path.exists(cur_em):
        if emulator:
            set_emulator = emulator
            warnings.append(f"Emulator {cur_em} is missing; {emulator} will be used.")
        else:
            errors.append(f"No emulator for architecture {m['vm'].get('arch')} on this host.")
    set_machine = None
    mach = m["vm"].get("machine")
    if mach and machines and mach not in machines:
        alias = _machine_alias(mach)
        if opts.get("adapt_machine", True) and alias:
            set_machine = alias
            warnings.append(f"Machine type {mach} is not supported by this host's QEMU; '{alias}' (latest version) will be used. "
                            f"Snapshots with saved memory may fail to revert.")
        else:
            errors.append(f"Machine type {mach} is not supported by this host's QEMU.")

    pools = virt.pools()
    pool_dirs = sorted({os.path.dirname(f["dest"]) for f in files if f["source"]})
    new_pools = [d for d in pool_dirs if d not in pools.values()] if opts.get("create_pool", True) else []

    return {
        "vm": m["vm"], "name": name, "renamed": renamed, "new_ids": new_ids,
        "uuid": final_uuid, "target_dir": target_dir, "use_original": use_original,
        "files": files, "copy_bytes": copy_bytes, "nvram_dest": nvram_dst, "networks": nets,
        "new_pools": new_pools, "emulator": set_emulator, "machine": set_machine,
        "snapshots": len(m["snapshots"]), "current_snapshot": m.get("current_snapshot"),
        "errors": errors, "warnings": warnings, "ok": not errors,
    }


class Rollback:
    def __init__(self, job):
        self.job, self.actions = job, []

    def add(self, desc, fn):
        self.actions.append((desc, fn))

    def run(self):
        for desc, fn in reversed(self.actions):
            try:
                fn()
                self.job.log(f"↩ Rolled back: {desc}")
            except Exception as e:  # noqa: BLE001
                self.job.log(f"⚠ Could not roll back '{desc}': {e}")


def run_restore(job, m, bdir, p, opts):
    """p: result of plan() with no errors."""
    mf = {e["backup_name"]: e for e in m["files"]}
    files = [f for f in p["files"] if f["source"]]
    name = p["name"]
    rb = Rollback(job)
    tmp = os.path.join(bdir, "restore-tmp")
    to_delete_after = []

    try:
        # 1. Checksums
        _phase(job, 3, "Verify checksums")
        verified = False
        if not opts.get("verify", True) or not any(mf[f["name"]].get("sha256") for f in files):
            job.log("⚠ Checksums are not verified (skipped or not stored in the archive).")
        if opts.get("verify", True):
            todo = [f for f in files if mf[f["name"]].get("sha256")]
            if todo:
                prog = job.bytes_progress(sum(f["size"] for f in todo), 3, 35, "Verifying checksums")
                for f in todo:
                    h = fsutil.sha256_file(f["source"], lambda n, b=f["name"]: prog(n, b))
                    if h != mf[f["name"]]["sha256"]:
                        if hasattr(prog, "fail"):
                            prog.fail(f"mismatch: {f['name']}")
                        raise JobError(f"checksum mismatch for {f['name']}.\n"
                                       f"  The file is damaged or was changed after the backup was created.\n"
                                       f"  Nothing was changed on this server.\n"
                                       f"  Copy the file again from the old server and run: sudo kvm-backup restore")
                if hasattr(prog, "finish"):
                    prog.finish("all files match")
                verified = True

        # 2. Place files
        # A successful check goes straight on; a mismatch has already stopped the restore above
        _phase(job, 4, "Place disk files", ask_first=not verified)
        path_map = {}
        placed = {}
        copy_total = sum(f["size"] for f in files if f["action"] == "copy")
        prog = job.bytes_progress(copy_total, 35, 70, "Copying disks")
        for f in files:
            src, dst = f["source"], f["dest"]
            path_map[f["original_path"]] = dst
            if f["action"] == "on-place":
                placed.setdefault(("already in place in", os.path.dirname(dst)), []).append(f["name"])
                continue
            _mkdirs(os.path.dirname(dst))
            if f["action"] == "rename":
                os.rename(src, dst)
                rb.add(f"move {f['name']}", lambda s=src, d=dst: os.rename(d, s))
                placed.setdefault(("moved to", os.path.dirname(dst)), []).append(f["name"])
            else:
                part = dst + ".part"
                rb.add(f"copy {f['name']}", lambda d=dst, pt=part: [os.remove(x) for x in (d, pt) if os.path.exists(x)])
                fsutil.copy_sparse(src, part, lambda n, b=f["name"]: prog(n, b))
                os.rename(part, dst)
                placed.setdefault(("copied to", os.path.dirname(dst)), []).append(f["name"])
                if opts.get("mode", "move") == "move":
                    to_delete_after.append(src)
            os.chown(dst, 0, 0)
            os.chmod(dst, 0o600)
            if mf[f["name"]].get("format") == "raw" and f["role"] != "iso" and opts.get("sparsify", True):
                virt.run(["fallocate", "--dig-holes", dst], check=False)

        for dp in m.get("dangling_paths") or []:
            if dp not in path_map and not p["use_original"]:
                path_map[dp] = os.path.join(p["target_dir"], os.path.basename(dp))

        if copy_total and hasattr(prog, "finish"):
            prog.finish("copied")
        for (how, d), names in placed.items():
            _done(job, f"✓ {len(names)} file(s) {how} {d}/")

        # 3. Backing file chains
        _phase(job, 5, "Create the VM")
        job.step("Updating links between disk files", 71)
        for f in files:
            e = mf[f["name"]]
            if not e.get("backing_original"):
                continue
            new_backing = path_map.get(e["backing_original"])
            if not new_backing:
                raise JobError(f"Backing file {e['backing_original']} for {f['name']} is missing.")
            raw = e["backing_raw"]
            resolved = raw if os.path.isabs(raw) else os.path.normpath(os.path.join(os.path.dirname(f["dest"]), raw))
            if os.path.normpath(resolved) != os.path.normpath(new_backing):
                virt.qemu_img_rebase_unsafe(f["dest"], new_backing, e.get("backing_format"))
                rb.add(f"rebase {f['name']}", lambda d=f["dest"], r=raw, fm=e.get("backing_format"):
                       virt.qemu_img_rebase_unsafe(d, r, fm))

        # 4. NVRAM and TPM
        ctx = {"path_map": path_map, "volume_map": m.get("volume_map") or {},
               "name": name if p["renamed"] else None,
               "uuid": p["uuid"] if p["new_ids"] else None,
               "emulator": p["emulator"], "machine": p["machine"], "mac_map": {}}
        if m.get("nvram"):
            job.step("Restoring NVRAM (UEFI)", 74)
            dst = p["nvram_dest"]
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(bdir, "nvram", m["nvram"]["file"]), dst)
            rb.add("NVRAM", lambda d=dst: os.remove(d))
            ctx["nvram"] = dst
        if m.get("tpm"):
            job.step("Restoring TPM state", 75)
            tdst = os.path.join(config.get()["swtpm_dir"], p["uuid"])
            shutil.copytree(os.path.join(bdir, m["tpm"]["dir"]), tdst, symlinks=True)
            rb.add("TPM state", lambda d=tdst: shutil.rmtree(d))
            if not fsutil.chown_names(tdst, m["tpm"]["owner"], m["tpm"]["group"], recursive=True):
                job.log("⚠ TPM state owner not found; libvirt will set permissions on start.")

        os.makedirs(tmp, exist_ok=True)
        dom = ET.parse(os.path.join(bdir, m["domain_xml"])).getroot()
        snap_xml = []
        for s in m["snapshots"]:
            with open(os.path.join(bdir, s["file"]), encoding="utf-8") as fh:
                snap_xml.append((s, fh.read()))
        if p["new_ids"]:
            macs = set(xmlutil.interface_macs(dom))
            for _, x in snap_xml:
                for el in xmlutil.snapshot_domains(ET.fromstring(x)):
                    macs |= set(xmlutil.interface_macs(el))
            ctx["mac_map"] = {mac: xmlutil.random_mac() for mac in macs}

        # 5. Networks
        job.step("Checking libvirt networks", 77)
        existing = virt.networks()
        for n in m.get("networks", []):
            if n["name"] in existing:
                continue
            nroot = ET.parse(os.path.join(bdir, n["file"])).getroot()
            u = nroot.find("uuid")
            if u is not None and u.text in virt.network_uuids():
                nroot.remove(u)
            br = nroot.find("bridge")
            if br is not None and br.get("name") and os.path.exists(f"/sys/class/net/{br.get('name')}"):
                del br.attrib["name"]
            mac = nroot.find("mac")
            if mac is not None:
                nroot.remove(mac)
            nf = os.path.join(tmp, f"net-{n['name']}.xml")
            with open(nf, "w", encoding="utf-8") as fh:
                fh.write(xmlutil.tostring(nroot))
            virt.virsh("net-define", nf)
            rb.add(f"network {n['name']}", lambda nn=n["name"]: (virt.virsh("net-destroy", nn, check=False),
                                                                 virt.virsh("net-undefine", nn)))
            r = virt.virsh("net-start", n["name"], check=False)
            if r.returncode != 0:
                job.log(f"⚠ Network {n['name']} defined but not started: {r.stderr.strip()}")
            if n.get("autostart"):
                virt.virsh("net-autostart", n["name"], check=False)
            job.log(f"Network {n['name']} created")

        # 6. Storage pools (so the disks show up in Cockpit → Storage)
        if p["new_pools"]:
            job.step("Creating storage pools", 80)
            existing_pools = virt.pools()
            for d in p["new_pools"]:
                base = re.sub(r"[^A-Za-z0-9_-]+", "-", os.path.basename(d) or "storage").strip("-") or "storage"
                pname, i = base, 1
                while pname in existing_pools:
                    i += 1
                    pname = f"{base}-{i}"
                virt.virsh("pool-define-as", pname, "dir", "--target", d)
                rb.add(f"pool {pname}", lambda pn=pname: (virt.virsh("pool-destroy", pn, check=False),
                                                         virt.virsh("pool-undefine", pn)))
                virt.virsh("pool-start", pname, check=False)
                virt.virsh("pool-autostart", pname, check=False)
                existing_pools[pname] = d
                job.log(f"Storage pool {pname} created ({d})")

        # 7. Define the VM
        job.step("Defining the VM in libvirt", 84)
        warns = []
        xmlutil.rewrite_domain(dom, ctx, warns)
        dfile = os.path.join(tmp, "domain.xml")
        with open(dfile, "w", encoding="utf-8") as fh:
            fh.write(xmlutil.tostring(dom))
        virt.virsh("define", dfile)
        rb.add(f"VM {name}", lambda: _undefine(name))
        _done(job, f"✓ VM '{name}' defined in libvirt")

        # 8. Snapshots (parents before children, which preserves branches)
        _phase(job, 6, "Restore snapshots")
        if not snap_xml:
            job.log("The VM has no snapshots.")
        total = max(len(snap_xml), 1)
        for i, (s, x) in enumerate(snap_xml):
            job.step(f"Restoring snapshot '{s['name']}' ({i + 1}/{len(snap_xml)})", 86 + 11 * i / total)
            sx = xmlutil.rewrite_snapshot(x, ctx, warns)
            sf = os.path.join(tmp, f"snap-{i:03d}.xml")
            with open(sf, "w", encoding="utf-8") as fh:
                fh.write(sx)
            virt.virsh("snapshot-create", name, "--xmlfile", sf, "--redefine")
        if m.get("current_snapshot"):
            virt.virsh("snapshot-current", name, m["current_snapshot"])
            _done(job, f"✓ {len(snap_xml)} snapshot(s) restored, current: {m['current_snapshot']}")
        elif snap_xml:
            _done(job, f"✓ {len(snap_xml)} snapshot(s) restored")

        if opts.get("autostart", True) and m["vm"].get("autostart"):
            virt.virsh("autostart", name, check=False)
            job.log("VM autostart enabled")
        for pn, d in virt.pools().items():
            if d in {os.path.dirname(f["dest"]) for f in files}:
                virt.virsh("pool-refresh", pn, check=False)
    except BaseException:
        if rb.actions:
            job.log("Error, rolling back changes…")
            rb.run()
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for src in to_delete_after:
        try:
            os.remove(src)
        except OSError as e:
            job.log(f"⚠ Could not delete {src}: {e}")
    job.result = {"vm": name, "files": files, "snapshots": len(snap_xml),
                  "current": m.get("current_snapshot"), "nvram": ctx.get("nvram"), "warnings": warns}


def _phase(job, n, title, ask_first=True):
    """Start a numbered restore step (the terminal UI may ask to continue first)."""
    if hasattr(job, "phase"):
        job.phase(n, title, ask_first)
    else:
        job.step(title)


def _done(job, text):
    if hasattr(job, "done"):
        job.done(text)
    else:
        job.log(text)


def _mkdirs(path):
    """Create directories with mode 0755 so QEMU (libvirt-qemu) can reach them."""
    missing = []
    while path and not os.path.exists(path):
        missing.append(path)
        path = os.path.dirname(path)
    for d in reversed(missing):
        os.mkdir(d)
        os.chmod(d, 0o755)


def _undefine(name):
    r = virt.virsh("undefine", name, "--snapshots-metadata", "--keep-nvram", "--keep-tpm", check=False)
    if r.returncode != 0:
        virt.virsh("undefine", name, "--snapshots-metadata", "--keep-nvram")
