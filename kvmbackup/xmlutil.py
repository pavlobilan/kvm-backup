"""Parsing and rewriting of libvirt domain and snapshot XML."""
import os
import random
import xml.etree.ElementTree as ET

for _p, _u in {
    "qemu": "http://libvirt.org/schemas/domain/qemu/1.0",
    "libosinfo": "http://libosinfo.org/xmlns/libvirt/domain/1.0",
    "cockpit_machines": "https://github.com/cockpit-project/cockpit-machines",
}.items():
    ET.register_namespace(_p, _u)


def tostring(el):
    return ET.tostring(el, encoding="unicode")


# --- Parsing -----------------------------------------------------------------

def disk_entries(dom):
    res = []
    devices = dom.find("devices")
    if devices is None:
        return res
    for d in devices.findall("disk"):
        dtype = d.get("type", "file")
        src = d.find("source")
        tgt = d.find("target")
        drv = d.find("driver")
        e = {
            "type": dtype,
            "device": d.get("device", "disk"),
            "target": tgt.get("dev") if tgt is not None else None,
            "format": drv.get("type") if drv is not None else None,
            "path": None, "pool": None, "volume": None,
        }
        if src is not None:
            if dtype == "file":
                e["path"] = src.get("file")
            elif dtype == "block":
                e["path"] = src.get("dev")
            elif dtype == "volume":
                e["pool"], e["volume"] = src.get("pool"), src.get("volume")
        res.append(e)
    return res


def nvram_path(dom):
    n = dom.find("os/nvram")
    if n is None:
        return None
    s = n.find("source")
    if s is not None and s.get("file"):
        return s.get("file")
    return (n.text or "").strip() or None


def set_nvram_path(dom, path):
    n = dom.find("os/nvram")
    if n is None:
        return
    s = n.find("source")
    if s is not None and s.get("file"):
        s.set("file", path)
    else:
        n.text = path


def has_tpm_emulator(dom):
    return any(b.get("type") == "emulator" for b in dom.findall("devices/tpm/backend"))


def interface_networks(dom):
    out = []
    for i in dom.findall("devices/interface"):
        s = i.find("source")
        if i.get("type") == "network" and s is not None and s.get("network"):
            out.append(s.get("network"))
    return out


def interface_bridges(dom):
    out = []
    for i in dom.findall("devices/interface"):
        s = i.find("source")
        if i.get("type") == "bridge" and s is not None and s.get("bridge"):
            out.append(s.get("bridge"))
    return out


def interface_macs(dom):
    return [m.get("address").lower() for m in dom.findall("devices/interface/mac") if m.get("address")]


def snapshot_domains(root):
    return [el for el in (root.find("domain"), root.find("inactiveDomain")) if el is not None]


def parse_snapshot(xml_text):
    root = ET.fromstring(xml_text)
    mem = root.find("memory")
    try:
        created = int(root.findtext("creationTime") or 0)
    except ValueError:
        created = 0
    return {
        "name": root.findtext("name"),
        "parent": root.findtext("parent/name"),
        "state": root.findtext("state"),
        "description": root.findtext("description") or "",
        "created": created,
        "memory": mem.get("snapshot") if mem is not None else None,
        "memory_file": mem.get("file") if mem is not None else None,
        "disks": [
            {"name": d.get("name"), "snapshot": d.get("snapshot"),
             "file": d.find("source").get("file") if d.find("source") is not None else None}
            for d in root.findall("disks/disk")
        ],
    }


def topo_sort(snaps):
    """Parents before children; branches ordered by creation time."""
    by = {s["name"]: s for s in snaps}
    out, seen = [], set()

    def visit(s):
        if s["name"] in seen:
            return
        p = s.get("parent")
        if p and p in by and p not in seen:
            visit(by[p])
        seen.add(s["name"])
        out.append(s)

    for s in sorted(snaps, key=lambda s: (s.get("created") or 0, s["name"])):
        visit(s)
    return out


# --- Rewriting ----------------------------------------------------------------

def random_mac():
    return "52:54:00:%02x:%02x:%02x" % tuple(random.randint(0, 255) for _ in range(3))


def rewrite_domain(dom, ctx, warnings):
    """ctx: path_map, volume_map, name, uuid, mac_map, nvram, emulator, machine."""
    pm = ctx.get("path_map", {})
    if ctx.get("name"):
        el = dom.find("name")
        if el is not None:
            el.text = ctx["name"]
    if ctx.get("uuid"):
        el = dom.find("uuid")
        if el is None:
            el = ET.SubElement(dom, "uuid")
        el.text = ctx["uuid"]
    if ctx.get("machine"):
        t = dom.find("os/type")
        if t is not None:
            t.set("machine", ctx["machine"])
    if ctx.get("nvram"):
        set_nvram_path(dom, ctx["nvram"])
    devices = dom.find("devices")
    if devices is None:
        return
    if ctx.get("emulator"):
        em = devices.find("emulator")
        if em is not None:
            em.text = ctx["emulator"]
    for d in devices.findall("disk"):
        dtype = d.get("type", "file")
        src = d.find("source")
        if src is None:
            continue
        if dtype == "volume":
            orig = ctx.get("volume_map", {}).get(f"{src.get('pool')}/{src.get('volume')}")
            if orig:
                d.set("type", "file")
                for a in ("pool", "volume", "mode"):
                    src.attrib.pop(a, None)
                src.set("file", pm.get(orig, orig))
                dtype = "file"
        if dtype == "file" and src.get("file"):
            f = src.get("file")
            if f in pm:
                src.set("file", pm[f])
            elif d.get("device") in ("cdrom", "floppy") and not os.path.exists(f):
                d.remove(src)
                msg = f"Image {f} was not transferred; the {d.get('device')} drive will be empty."
                if msg not in warnings:
                    warnings.append(msg)
        for bs in d.findall(".//backingStore/source"):
            if bs.get("file") in pm:
                bs.set("file", pm[bs.get("file")])
    mm = ctx.get("mac_map") or {}
    for m in devices.findall("interface/mac"):
        a = (m.get("address") or "").lower()
        if a in mm:
            m.set("address", mm[a])


def rewrite_snapshot(xml_text, ctx, warnings):
    root = ET.fromstring(xml_text)
    pm = ctx.get("path_map", {})
    mem = root.find("memory")
    if mem is not None and mem.get("file") in pm:
        mem.set("file", pm[mem.get("file")])
    for s in root.findall("disks/disk/source"):
        if s.get("file") in pm:
            s.set("file", pm[s.get("file")])
    for el in snapshot_domains(root):
        rewrite_domain(el, ctx, warnings)
    return tostring(root)
