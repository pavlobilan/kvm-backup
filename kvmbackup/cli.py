"""kvm-backup: full backup and migration of KVM virtual machines from the terminal.

  sudo kvm-backup                          interactive menu
  sudo kvm-backup backup [VM]              create a backup
  sudo kvm-backup restore [archive.tar.gz] restore a VM
"""
import argparse
import json
import os
import shutil
import signal
import sys
import tarfile
import time
import xml.etree.ElementTree as ET

from . import __version__, backup, config, fsutil, restore, virt
from .jobs import JobError, human

TTY = sys.stdout.isatty()


def c(code, text):
    return f"\033[{code}m{text}\033[0m" if TTY else text


def ok(t):
    return c("32", t)


def bad(t):
    return c("31", t)


def warn(t):
    return c("33", t)


def bold(t):
    return c("1", t)


def header(t):
    print()
    print(bold(f"━━━ {t} ━━━"))


class Bar:
    """Progress bar. Turns green with ✔ on success, red with ✖ on failure."""

    def __init__(self, total, label):
        self.total, self.label, self.done, self._last, self._closed = max(total, 1), label, 0, 0, False

    def _draw(self, color=None, mark=""):
        frac = min(self.done / self.total, 1)
        cols = shutil.get_terminal_size((100, 24)).columns
        tail = f" {frac * 100:5.1f}%  {human(self.done)} / {human(self.total)}"
        width = max(10, min(30, cols - len(self.label) - len(tail) - len(mark) - 8))
        bar = "█" * int(frac * width) + "░" * (width - int(frac * width))
        if color:
            bar = c(color, bar)
        line = f"  {self.label} {bar}{tail}"
        if mark:
            line += "  " + c(color, mark) if color else "  " + mark
        sys.stdout.write(f"\r\033[K{line}")
        sys.stdout.flush()

    def __call__(self, n, sub=""):
        self.done += n
        now = time.time()
        if not TTY or self._closed or (now - self._last < 0.3 and self.done < self.total):
            return
        self._last = now
        self._draw()

    def _close(self, color, mark, plain):
        if self._closed:
            return
        self._closed = True
        if TTY:
            self._draw(color, mark)
            sys.stdout.write("\n")
        else:
            print(f"  {self.label}: {plain} ({human(self.done)})")

    def finish(self, text="done"):
        self.done = max(self.done, self.total)
        self._close("32", f"✔ {text}", text)

    def fail(self, text="failed"):
        self._close("31", f"✖ {text}", text)


class TermJob:
    """Progress reporter used by backup/restore: step/log/bytes_progress/result."""

    def __init__(self, mode="", total=0, confirm=True):
        self.result = None
        self.user = os.environ.get("SUDO_USER") or "root"
        self.created = time.time()
        self.step_text = ""
        self.progress = 0
        self.mode, self.total, self.confirm = mode, total, confirm
        self.label = ""
        self._started = False

    def phase(self, n, title):
        """Numbered step; from the second one on, wait for Enter before starting it."""
        if self._started and self.confirm and not pause():
            raise JobError("Cancelled. All changes made by this restore were rolled back.")
        self._started = True
        step(self.mode, n, self.total, f"{title} · {self.label}" if self.label else title)

    def done(self, text):
        print(ok("  " + text))

    def step(self, text, progress=None):
        print(f"  • {text}")

    def log(self, msg):
        if msg.startswith("⚠"):
            print("    " + warn(msg))
        else:
            print(f"    {msg}")

    def bytes_progress(self, total, start, end, label):
        return Bar(total, label)


def ask(prompt, default=True, assume=False):
    if assume:
        return True
    hint = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"{prompt} {hint}: ").strip().lower()
    except EOFError:
        return default
    if not a:
        return default
    return a in ("y", "yes")


def choose(title, items, render):
    if not items:
        return None
    print(bold(title))
    for i, it in enumerate(items, 1):
        print(f"  {i}) {render(it)}")
    while True:
        try:
            n = len(items)
            choices = "1" if n == 1 else "1 or 2" if n == 2 else f"1-{n}"
            a = input(f"Number [{choices}, 0 to quit]: ").strip()
        except EOFError:
            return None
        if a == "0":
            return None
        if a.isdigit() and 1 <= int(a) <= len(items):
            return items[int(a) - 1]


def parse_numbers(text, n):
    """'2', '1 3', '1,3', '1-3' -> list of numbers in 1..n (order kept, no duplicates)."""
    out = []
    for tok in text.replace(",", " ").split():
        if "-" in tok:
            a, b = (int(x) for x in tok.split("-", 1))
            nums = range(a, b + 1)
            if a > b:
                raise ValueError
        else:
            nums = [int(tok)]
        for k in nums:
            if not 1 <= k <= n:
                raise ValueError
            if k not in out:
                out.append(k)
    if not out:
        raise ValueError
    return out


def choose_many(title, columns, rows, question, noun):
    """Table with numbered rows; returns the selected items (list) or None to quit.
    rows: list of (item, [cell, ...]); noun: e.g. "VM" for the examples."""
    widths = [max(len(str(c)), *(len(str(r[1][i])) for r in rows)) for i, c in enumerate(columns)]
    line = lambda cells: "  ".join(f"{str(x):<{w}}" for x, w in zip(cells, widths)).rstrip()
    print(f"  {title}")
    print()
    print(bold(f"    No.  {line(columns)}"))
    for i, (_, cells) in enumerate(rows, 1):
        print(f"    {i:<3}  {line(cells)}")
    print()
    n = len(rows)
    if n == 1:
        prompt = "  Enter 1 to select it, or 0 to quit: "
        print(f"  {question}")
    else:
        print(f"  {question} Enter one or more numbers:")
        print(f"    {'2':<6} only {noun} 2")
        if n == 2:
            print(f"    {'1 2':<6} {noun}s 1 and 2")
        else:
            print(f"    {'1 3':<6} {noun}s 1 and 3")
            print(f"    {'1-3':<6} {noun}s 1, 2 and 3 (a range)")
        prompt = "  Numbers, or 0 to quit: "
    while True:
        try:
            a = input(prompt).strip()
        except EOFError:
            return None
        if a == "0":
            return None
        try:
            return [rows[k - 1][0] for k in parse_numbers(a, n)]
        except ValueError:
            print(bad(f"  Enter numbers from 1 to {n}, for example: 2, 1 3 or 1-3."))


def snap_tree(snaps, current, indent="  "):
    names = {s["name"] for s in snaps}
    kids = {}
    for s in sorted(snaps, key=lambda s: (s.get("created") or 0, s["name"])):
        kids.setdefault(s["parent"] if s.get("parent") in names else None, []).append(s)

    def walk(parent, prefix):
        items = kids.get(parent, [])
        for i, s in enumerate(items):
            last = i == len(items) - 1
            mark = "└─ " if last else "├─ "
            cur = ok("  ← current") if s["name"] == current else ""
            print(f"{indent}{prefix}{mark}{s['name']}{cur}")
            walk(s["name"], prefix + ("   " if last else "│  "))
    walk(None, "")


STATES = {"pmsuspended": "suspended"}


def st(state):
    return STATES.get(state, state)


def when(iso):
    try:
        import datetime
        return datetime.datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso


def group_by_dir(files):
    out = {}
    for e in files:
        out.setdefault(os.path.dirname(e["original_path"]), []).append(e)
    return out


# ─────────────────────────── backup ───────────────────────────

def step(mode, n, total, title):
    print()
    print(bold(f"━━━ {mode} · Step {n} of {total} · {title} ━━━"))


def install_command():
    """Install command for the new server, built from the repository this copy was installed from."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "REPO")) as f:
            repo, branch = (f.read().split() + ["main"])[:2]
    except (OSError, ValueError):
        return None
    if repo == "local":
        return "local"
    if not repo.startswith("https://github.com/"):
        return None
    path = repo[len("https://github.com/"):].rstrip("/")
    return f"curl -fsSL https://raw.githubusercontent.com/{path}/{branch}/install.sh | sudo bash"


def names(items):
    return ", ".join(f"'{x}'" for x in items)


def cmd_backup(vm, assume_yes, checksums):
    vms = virt.list_domains()
    if not vms:
        raise JobError("There are no virtual machines on this server.")

    step("Backup", 1, 4, "Choose virtual machines")
    if vm:
        if vm not in vms:
            raise JobError(f"VM '{vm}' not found. Available: {', '.join(vms)}")
        selected = [vm]
    else:
        rows = [(n, [n, st(virt.domain_state(n)), len(virt.snapshot_list(n))]) for n in vms]
        selected = choose_many("Virtual machines on this server:", ["Name", "State", "Snapshots"], rows,
                               "Which VMs do you want to back up?", "VM")
        if not selected:
            return 0
    print(ok(f"  ✔ Selected: {', '.join(selected)}"))

    step("Backup", 2, 4, "Review the VMs" if len(selected) > 1 else f"Review VM '{selected[0]}'")
    for name in selected:
        a = backup.analyse(name)
        if len(selected) > 1:
            print()
            print(bold(f"  {name}"))
        print(f"  State: {st(a['state'])} · {a['vcpu']} vCPU · {human(int(a['memory_kib'] or 0) * 1024)}"
              f" · {'UEFI' if a['nvram'] else 'BIOS'}{' · vTPM' if a['tpm'] else ''}")
        print(f"  Snapshots: {len(a['snapshots'])}")
        if a["snapshots"]:
            snap_tree(a["snapshots"], a["current"], "    ")
        print(f"  Disk and snapshot files: {len(a['files'])}, total {human(a['total_size'])}")
        for w in a["warnings"]:
            print("  " + warn("⚠ " + w))

    print()
    # kvm-backup never shuts a VM down itself: wait until the user does it
    while True:
        running = [(n, virt.domain_state(n)) for n in selected]
        running = [(n, state) for n, state in running if state != "shut off"]
        if not running:
            break
        for n, state in running:
            print(warn(f"  ⚠ VM '{n}' is {state}. It must be shut off for a consistent copy of its disks."))
        print("    Shut it down from the guest OS or in Cockpit (Shut down), then check again.")
        if assume_yes or not wait_enter("    Press Enter to check again, or 0 to quit: "):
            print("Cancelled.")
            return 1
        print()
    print(ok(f"  ✔ Shut off: {', '.join(selected)}"))
    if not ask(f"  Create a backup of {names(selected)}?", True, assume_yes):
        print("Cancelled.")
        return 1

    step("Backup", 3, 4, "Create the backups" if len(selected) > 1 else "Create the backup")
    done, taken = [], set()
    for name in selected:
        if len(selected) > 1:
            print()
            print(bold(f"  {name}"))
        job = TermJob()
        backup.run_backup(job, name, {"checksums": checksums, "taken": taken})
        m, archive = job.result["manifest"], job.result["archive"]
        print(ok("  Configuration saved: ") + bold(archive) + f" ({human(os.path.getsize(archive))})")
        done.append((name, m, archive))

    acl = []
    if not assume_yes:
        acl, err = fsutil.grant_read_acl([e["original_path"] for _, m, _ in done for e in m["files"]])
        if err:
            print("  " + warn("⚠ " + err))

    dest = config.get()["backup_root"]
    cmd = install_command()
    local = cmd in (None, "local")
    install = "sudo bash ~/kvm-backup/install.sh" if local else cmd

    def confirm(prompt):
        """Wait for Enter; on Ctrl+C or end of input remove the temporary access and stop."""
        if assume_yes:
            return True
        try:
            input(f"  {prompt} ")
            return True
        except (KeyboardInterrupt, EOFError):
            print()
            for e in fsutil.revoke_acl(acl):
                print("  " + warn("⚠ " + e))
            print(warn("  Interrupted. Temporary WinSCP access was removed."))
            print(f"  The archives are kept in {dest}/")
            return False

    # Step 4: copy the files (the old server only serves files from here on)
    step("Backup", 4, 4, "Copy the files to the new server")
    print("  On the new server, install kvm-backup first; it creates the target directory:")
    if local:
        print("    copy the kvm-backup folder there (for example to your home directory) and run:")
        print(f"    {bold(install)}")
    else:
        print(f"    {bold('sudo apt install -y curl')}")
        print(f"    {bold(install)}")
    print(f"  In its menu choose {bold('2) Restore VMs')}; it waits until the files are copied.")
    print()
    print(f"  Copy these files with WinSCP to {bold(dest + '/')} on the new server:")
    count = size = 0
    for name, m, archive in done:
        print()
        if len(done) > 1:
            print(bold(f"   {name}"))
        print(f"     from {bold(os.path.dirname(archive) + '/')}")
        print(f"       {os.path.basename(archive):<40} {human(os.path.getsize(archive)):>10}   configuration archive")
        for d, files in group_by_dir(m["files"]).items():
            print(f"     from {bold(d + '/')}")
            for e in sorted(files, key=lambda x: x["original_path"]):
                rename = f"   → save as {e['backup_name']}" if e["backup_name"] != os.path.basename(e["original_path"]) else ""
                opt = "   (ISO, optional)" if not e["required"] else ""
                print(f"       {os.path.basename(e['original_path']):<40} {human(e['size']):>10}{opt}{rename}")
        count += len(m["files"]) + 1
        size += m["total_size"] + os.path.getsize(archive)
    print()
    print(f"     Total: {count} files, {human(size)}")
    print()
    vm_word = "VM" if len(selected) == 1 else "VMs"
    print(bad(f"  ⚠ Do NOT start {vm_word} {names(selected)} until all files are copied."))
    print(bad("    Starting a VM changes its disk files, and the copy on the new server will not match."))
    print()
    if not confirm("Press Enter when all files are copied:"):
        return 1
    for e in fsutil.revoke_acl(acl):
        print("  " + warn("⚠ " + e))
    started = [n for n in selected if virt.domain_state(n) != "shut off"]
    if started:
        print(bad(f"  ✖ Started before copying finished: {names(started)}."))
        print(bad("    If a VM was running while its files were copied, the copy may be inconsistent:"))
        print(bad("    the restore will report a checksum mismatch. In that case shut the VM down"))
        print(bad("    and run the backup again."))
        print(warn("  Backup finished with a warning."))
        return 1
    print(ok(f"  ✔ Files copied. {vm_word} stayed shut off; temporary WinSCP access removed."))
    print()
    print(f"  Continue on the new server: press {bold('Enter')} in the waiting menu, "
          f"or run {bold('sudo kvm-backup restore')}.")
    print(f"  The {vm_word} on this server can be started again in Cockpit → Virtual machines, or:")
    for n in selected:
        print(f"    {bold(f'sudo virsh start {n}')}")
    print(ok("  Backup finished."))
    return 0


# ─────────────────────────── restore ───────────────────────────

def read_archive(path, work):
    try:
        tar = tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError) as e:
        raise JobError(f"Cannot open archive {path}: {e}")
    with tar:
        members = tar.getmembers()
        tops = {x.name.split("/", 1)[0] for x in members}
        if len(tops) != 1:
            raise JobError("Damaged archive: expected a single top-level folder.")
        top = tops.pop()
        for x in members:
            if x.name.startswith("/") or ".." in x.name.split("/") or not (x.isfile() or x.isdir()):
                raise JobError(f"Unsafe archive entry: {x.name}")
        try:
            tar.extractall(work, filter="data")
        except TypeError:
            tar.extractall(work)
    bdir = os.path.join(work, top)
    try:
        with open(os.path.join(bdir, "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        raise JobError("The archive has no valid manifest.json; it is not a kvm-backup archive.")
    if m.get("format") != backup.FORMAT:
        raise JobError("The archive was not created by kvm-backup.")
    return m, bdir


def peek_manifest(path):
    try:
        with tarfile.open(path, "r:gz") as tar:
            for x in tar:
                if x.name.endswith("/manifest.json") and x.name.count("/") == 1:
                    return json.load(tar.extractfile(x))
    except (tarfile.TarError, OSError, ValueError):
        return None
    return None


def find_archives(root):
    if not os.path.isdir(root):
        return []
    paths = sorted((os.path.join(root, f) for f in os.listdir(root) if f.endswith(".tar.gz")),
                   key=os.path.getmtime, reverse=True)
    return [(p, m) for p, m in ((p, peek_manifest(p)) for p in paths) if m]


def pause():
    """Wait for Enter after a finished step. False when the user enters 0."""
    print()
    try:
        return input("  Press Enter to continue, or 0 to cancel: ").strip() != "0"
    except EOFError:
        return False


def wait_enter(prompt):
    """True to retry, False to quit."""
    try:
        return input(prompt).strip() != "0"
    except EOFError:
        return False


def show_check(p):
    moves = {}
    for f in p["files"]:
        name = f"{f['name']:<40} {human(f['size']):>10}"
        if f["source"] and f["action"]:
            if f["action"] == "on-place":
                print(f"  {ok('✔')} {name}  already in place")
            else:
                key = (os.path.dirname(f["source"]), os.path.dirname(f["dest"]))
                moves[key] = moves.get(key, 0) + 1
                print(f"  {ok('✔')} {name}")
        elif not f["required"]:
            print(f"  {warn('–')} {name}  {warn('missing (ISO, optional)')}")
        else:
            why = "size mismatch" if f["status"] == "size" else "not found"
            print(f"  {bad('✖')} {name}  {bad(why)}")
    for w in p["warnings"]:
        print("  " + warn("⚠ " + w))
    return moves


def show_result(name, m, r):
    dom = ET.fromstring(virt.domain_xml(name))
    print("  Disks:")
    for d in dom.findall("devices/disk"):
        src = d.find("source")
        tgt = d.find("target")
        path = src.get("file") if src is not None else "(empty)"
        print(f"    {tgt.get('dev') if tgt is not None else '?'}  {path}")
    snaps = virt.snapshot_list(name)
    cur = virt.snapshot_current(name)
    mark = ok("✔") if len(snaps) == len(m["snapshots"]) else bad("✖")
    print(f"  Snapshots: {len(snaps)} of {len(m['snapshots'])} {mark}")
    if snaps:
        info = []
        for s in snaps:
            sx = ET.fromstring(virt.snapshot_xml(name, s))
            info.append({"name": s, "parent": sx.findtext("parent/name"),
                         "created": int(sx.findtext("creationTime") or 0)})
        snap_tree(info, cur, "    ")
    if r.get("nvram"):
        print(f"  NVRAM: {r['nvram']}")
    for w in r.get("warnings") or []:
        print("  " + warn("⚠ " + w))
    print(f"  State: {st(virt.domain_state(name))} · autostart: {'yes' if virt.autostart(name) else 'no'}")


def cmd_restore(archive, assume_yes, verify):
    root = config.get()["backup_root"]

    step("Restore", 1, 7, "Choose backup archives")
    if archive:
        archives = [os.path.abspath(archive)]
        if not os.path.isfile(archives[0]):
            raise JobError(f"File {archives[0]} not found.")
    else:
        while True:
            found = find_archives(root)
            if found:
                break
            group = config.get()["transfer_group"]
            print(warn("  No backup archives found yet."))
            print()
            print("  Copy with WinSCP from the old server to this server:")
            print()
            print(f"    {bold('Configuration archive')}  <VM>_<date>.tar.gz")
            print(f"      from (old server):  {root}/")
            print(f"      to (this server):   {bold(root + '/')}")
            print()
            print(f"    {bold('Disk image and snapshot files')}  (.qcow2, .img and others)")
            print("      from (old server):  usually /var/lib/libvirt/images/")
            print("                          (the exact list was shown at the end of the backup)")
            print(f"      to (this server):   {bold(root + '/')}  (the same folder as the archive)")
            print("      A file marked \"→ save as <name>\" in that list must be saved under that name.")
            print()
            print(f"  The restore then moves the disk files from {root}/ to their original location")
            print("  (for example /var/lib/libvirt/images/). There is no need to do it yourself.")
            print()
            print("  Tip: copy the archive first. Then step 2 shows every disk file that is still missing.")
            print(f"  WinSCP must log in as root or as a member of the group {bold(group)}.")
            print()
            if assume_yes or not wait_enter("  Press Enter to look again, or 0 to quit: "):
                return 1
        rows = [(p, [m["vm"]["name"], when(m["created"]), m["source_host"], os.path.basename(p)]) for p, m in found]
        while True:
            archives = choose_many(f"Backup archives in {root}/:", ["VM", "Created", "From", "Archive"], rows,
                                   "Which backups do you want to restore?", "backup")
            if not archives:
                return 0
            vms = [dict(found)[a]["vm"]["name"] for a in archives]
            dup = sorted({v for v in vms if vms.count(v) > 1})
            if not dup:
                break
            print(bad(f"  Several backups of the same VM selected ({', '.join(dup)}); choose one backup per VM."))
            print()

    work = os.path.join(root, ".work", f"restore-{os.getpid()}")
    os.makedirs(work, exist_ok=True)
    items, restored = [], []
    try:
        for i, a in enumerate(archives):
            m, bdir = read_archive(a, os.path.join(work, str(i)))
            items.append({"archive": a, "m": m, "bdir": bdir, "import_dir": os.path.dirname(a)})
        for it in items:
            m, vm = it["m"], it["m"]["vm"]
            print()
            print(f"  {bold(vm['name'])} · {os.path.basename(it['archive'])} · created {when(m['created'])} "
                  f"on {m['source_host']}")
            print(f"  {vm['vcpu']} vCPU · {human(int(vm['memory_kib'] or 0) * 1024)} · {vm['machine']}"
                  f"{' · UEFI' if m.get('nvram') else ''}{' · vTPM' if m.get('tpm') else ''}")
            print(f"  Snapshots: {len(m['snapshots'])}")
            if m["snapshots"]:
                snap_tree(m["snapshots"], m.get("current_snapshot"), "    ")

        step("Restore", 2, 7, "Check files")
        while True:
            errors, dests = [], {}
            for it in items:
                if len(items) > 1:
                    print(bold(f"  {it['m']['vm']['name']}"))
                it["plan"] = p = restore.plan(it["m"], it["bdir"], {"import_dir": it["import_dir"]})
                it["moves"] = show_check(p)
                errors += [f"{it['m']['vm']['name']}: {e}" if len(items) > 1 else e for e in p["errors"]]
                for f in p["files"]:
                    other = dests.setdefault(f["dest"], p["name"])
                    if other != p["name"]:
                        errors.append(f"VMs '{other}' and '{p['name']}' use the same disk path {f['dest']}.")
                if len(items) > 1:
                    print()
            if not errors:
                break
            print()
            print(bad("  Restore is not possible yet:"))
            for e in errors:
                print(bad(f"   • {e}"))
            print(f"\n  Disk files must be in {bold(root + '/')} (next to the archive) "
                  f"or already at their original paths.")
            if assume_yes or not wait_enter("  Fix this and press Enter to check again, or 0 to quit: "):
                return 1
            print()
        print(ok("  All files present."))
        moves = {}
        for it in items:
            for k, n in it["moves"].items():
                moves[k] = moves.get(k, 0) + n
        for (src, dst), n in moves.items():
            print(f"  {n} file(s) will be moved: {src}/ → {bold(dst + '/')}")

        print()
        vm_names = [it["plan"]["name"] for it in items]
        if not ask(f"  Restore {'VM' if len(items) == 1 else 'VMs'} {names(vm_names)} now?", True, assume_yes):
            print("Cancelled.")
            return 1
        job = TermJob("Restore", 7, confirm=not assume_yes)
        for it in items:
            job.label = it["plan"]["name"] if len(items) > 1 else ""
            try:
                restore.run_restore(job, it["m"], it["bdir"], it["plan"], {"verify": verify})
            except BaseException:
                if restored:
                    print()
                    print(warn(f"  Already restored: {names([n for n, _, _ in restored])}. "
                               f"Not restored: {names(vm_names[len(restored):])}."))
                raise
            restored.append((job.result["vm"], it["m"], job.result))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if not assume_yes:
        pause()
    step("Restore", 7, 7, "Result")
    for name, m, r in restored:
        print(ok(f"  ✔ VM '{name}' restored."))
        show_result(name, m, r)
        print()
    print(ok("  Ready to start: Cockpit → Virtual machines, or:"))
    for name, _, _ in restored:
        print(f"    {bold(f'sudo virsh start {name}')}")
    for d in sorted({it["import_dir"] for it in items}):
        leftover = [f for f in os.listdir(d) if f.endswith(".part")]
        if leftover:
            print(warn(f"  Unfinished files left in {d}: {', '.join(leftover)}"))
    return 0


# ─────────────────────────── entry point ───────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(prog="kvm-backup", description="Full backup and migration of KVM virtual machines.")
    ap.add_argument("-V", "--version", action="version", version=f"kvm-backup {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("backup", help="back up VMs")
    b.add_argument("vm", nargs="?")
    b.add_argument("-y", "--yes", action="store_true", help="do not ask questions")
    b.add_argument("--no-checksum", action="store_true", help="skip SHA-256 checksums of disks")
    r = sub.add_parser("restore", help="restore VMs from archives")
    r.add_argument("archive", nargs="?")
    r.add_argument("-y", "--yes", action="store_true", help="do not ask questions")
    r.add_argument("--no-verify", action="store_true", help="skip checksum verification")
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        print(bad("Run as root: sudo kvm-backup"), file=sys.stderr)
        return 1
    signal.signal(signal.SIGHUP, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    os.makedirs(config.get()["backup_root"], exist_ok=True)

    cmd = args.cmd
    try:
        if not cmd:
            print(bold(f"kvm-backup {__version__}: backup and migration of KVM virtual machines"))
            print()
            choice = choose("What do you want to do?", ["backup", "restore"],
                            lambda x: {"backup": "Back up VMs",
                                       "restore": "Restore VMs"}[x])
            if not choice:
                return 0
            return cmd_backup(None, False, True) if choice == "backup" else cmd_restore(None, False, True)
        if cmd == "backup":
            return cmd_backup(args.vm, args.yes, not args.no_checksum)
        return cmd_restore(args.archive, args.yes, not args.no_verify)
    except JobError as e:
        print()
        print(bad(f"Error: {e}"), file=sys.stderr)
        return 1
    except virt.CmdError as e:
        print()
        print(bad(f"Command failed: {e}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
