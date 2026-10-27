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
        self._started = False

    def phase(self, n, title):
        """Numbered step; from the second one on, wait for Enter before starting it."""
        if self._started and self.confirm and not pause():
            raise JobError("Cancelled. All changes made by this restore were rolled back.")
        self._started = True
        step(self.mode, n, self.total, title)

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
            a = input(f"Number [1-{len(items)}, 0 to quit]: ").strip()
        except EOFError:
            return None
        if a == "0":
            return None
        if a.isdigit() and 1 <= int(a) <= len(items):
            return items[int(a) - 1]


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


def cmd_backup(vm, assume_yes, checksums):
    vms = virt.list_domains()
    if not vms:
        raise JobError("There are no virtual machines on this server.")

    step("Backup", 1, 4, "Choose a virtual machine")
    if not vm:
        states = {n: virt.domain_state(n) for n in vms}
        vm = choose("Virtual machines on this server:", vms, lambda n: f"{n:<24} {st(states[n])}")
        if not vm:
            return 0
    elif vm not in vms:
        raise JobError(f"VM '{vm}' not found. Available: {', '.join(vms)}")
    print(f"  Selected: {bold(vm)}")

    step("Backup", 2, 4, f"Review VM '{vm}'")
    a = backup.analyse(vm)
    print(f"  State: {st(a['state'])} · {a['vcpu']} vCPU · {human(int(a['memory_kib'] or 0) * 1024)}"
          f" · {'UEFI' if a['nvram'] else 'BIOS'}{' · vTPM' if a['tpm'] else ''}")
    print(f"  Snapshots: {len(a['snapshots'])}")
    if a["snapshots"]:
        snap_tree(a["snapshots"], a["current"], "    ")
    print(f"  Disk and snapshot files: {len(a['files'])}, total {human(a['total_size'])}")
    for w in a["warnings"]:
        print("  " + warn("⚠ " + w))

    opts = {"checksums": checksums}
    print()
    # kvm-backup never shuts the VM down itself: wait until the user does it
    while True:
        state = virt.domain_state(vm)
        if state == "shut off":
            break
        print(warn(f"  ⚠ VM '{vm}' is {state}. It must be shut off for a consistent copy of its disks."))
        print("    Shut it down from the guest OS or in Cockpit (Shut down), then check again.")
        if assume_yes or not wait_enter("    Press Enter to check again, or 0 to quit: "):
            print("Cancelled.")
            return 1
        print()
    print(ok(f"  ✔ VM '{vm}' is shut off."))
    if not ask(f"  Create a backup of '{vm}'?", True, assume_yes):
        print("Cancelled.")
        return 1

    step("Backup", 3, 4, "Create the backup")
    job = TermJob()
    backup.run_backup(job, vm, opts)
    res = job.result
    m, archive = res["manifest"], res["archive"]
    print(ok("  Configuration saved: ") + bold(archive) + f" ({human(os.path.getsize(archive))})")

    acl = []
    if not assume_yes:
        acl, err = fsutil.grant_read_acl([e["original_path"] for e in m["files"]])
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
            print(f"  The archive is kept: {archive}")
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
    print(f"  In its menu choose {bold('2) Restore a VM')}; it waits until the files are copied.")
    print()
    print(f"  Copy these files with WinSCP to {bold(dest + '/')} on the new server:")
    print()
    print(f"     from {bold(os.path.dirname(archive) + '/')}")
    print(f"       {os.path.basename(archive):<40} {human(os.path.getsize(archive)):>10}   configuration archive")
    for d, files in group_by_dir(m["files"]).items():
        print(f"     from {bold(d + '/')}")
        for e in sorted(files, key=lambda x: x["original_path"]):
            rename = f"   → save as {e['backup_name']}" if e["backup_name"] != os.path.basename(e["original_path"]) else ""
            opt = "   (ISO, optional)" if not e["required"] else ""
            print(f"       {os.path.basename(e['original_path']):<40} {human(e['size']):>10}{opt}{rename}")
    print(f"     Total: {len(m['files']) + 1} files, {human(m['total_size'] + os.path.getsize(archive))}")
    print()
    print(bad(f"  ⚠ Do NOT start VM '{vm}' until all files are copied."))
    print(bad("    Starting it changes the disk files, and the copy on the new server will not match."))
    print()
    if not confirm("Press Enter when all files are copied:"):
        return 1
    for e in fsutil.revoke_acl(acl):
        print("  " + warn("⚠ " + e))
    if virt.domain_state(vm) != "shut off":
        print(bad(f"  ✖ VM '{vm}' was started before copying finished."))
        print(bad("    If it was running while files were copied, the copy may be inconsistent:"))
        print(bad("    the restore will report a checksum mismatch. In that case shut the VM down"))
        print(bad("    and run the backup again."))
        print(warn("  Backup finished with a warning."))
        return 1
    print(ok(f"  ✔ Files copied. VM '{vm}' stayed shut off; temporary WinSCP access removed."))
    print()
    print(f"  Continue on the new server: press {bold('Enter')} in the waiting menu, "
          f"or run {bold('sudo kvm-backup restore')}.")
    print(f"  VM '{vm}' on this server can be started again: Cockpit → Virtual machines, or: "
          f"{bold(f'sudo virsh start {vm}')}")
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


def cmd_restore(archive, assume_yes, verify):
    root = config.get()["backup_root"]

    step("Restore", 1, 7, "Choose a backup archive")
    if not archive:
        while True:
            found = find_archives(root)
            if found:
                break
            print(f"  No backup archives in {bold(root + '/')} yet.")
            print("  Copy the archive and the disk files there with WinSCP.")
            if assume_yes or not wait_enter("  Press Enter to look again, or 0 to quit: "):
                return 1
        sel = choose("Backup archives:", found,
                     lambda x: f"{x[1]['vm']['name']:<20} {when(x[1]['created'])}  "
                               f"from {x[1]['source_host']}  ({os.path.basename(x[0])})")
        if not sel:
            return 0
        archive = sel[0]
    archive = os.path.abspath(archive)
    if not os.path.isfile(archive):
        raise JobError(f"File {archive} not found.")

    work = os.path.join(root, ".work", f"restore-{os.getpid()}")
    os.makedirs(work, exist_ok=True)
    try:
        m, bdir = read_archive(archive, work)
        vm = m["vm"]
        print(f"  Selected: {bold(os.path.basename(archive))}")
        print(f"  VM {bold(vm['name'])} · created {when(m['created'])} on {m['source_host']}")
        print(f"  {vm['vcpu']} vCPU · {human(int(vm['memory_kib'] or 0) * 1024)} · {vm['machine']}"
              f"{' · UEFI' if m.get('nvram') else ''}{' · vTPM' if m.get('tpm') else ''}")
        print(f"  Snapshots: {len(m['snapshots'])}")
        if m["snapshots"]:
            snap_tree(m["snapshots"], m.get("current_snapshot"), "    ")

        step("Restore", 2, 7, "Check files")
        import_dir = os.path.dirname(archive)
        while True:
            p = restore.plan(m, bdir, {"import_dir": import_dir})
            moves = show_check(p)
            if not p["errors"]:
                break
            print()
            print(bad("  Restore is not possible yet:"))
            for e in p["errors"]:
                print(bad(f"   • {e}"))
            print(f"\n  Disk files must be in {bold(import_dir + '/')} (next to the archive) "
                  f"or already at their original paths.")
            if assume_yes or not wait_enter("  Fix this and press Enter to check again, or 0 to quit: "):
                return 1
            print()
        print(ok("  All files present."))
        for (src, dst), n in moves.items():
            print(f"  {n} file(s) will be moved: {src}/ → {bold(dst + '/')}")

        print()
        if not ask(f"  Restore VM '{p['name']}' now?", True, assume_yes):
            print("Cancelled.")
            return 1
        job = TermJob("Restore", 7, confirm=not assume_yes)
        restore.run_restore(job, m, bdir, p, {"verify": verify})
        r = job.result
    finally:
        shutil.rmtree(work, ignore_errors=True)

    name = r["vm"]
    if not assume_yes:
        pause()
    step("Restore", 7, 7, "Result")
    print(ok(f"  VM '{name}' restored."))
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
    print()
    print(ok("  The VM is ready to start: Cockpit → Virtual machines, or: ") + bold(f"sudo virsh start {name}"))
    leftover = [f for f in os.listdir(import_dir) if f.endswith(".part")]
    if leftover:
        print(warn(f"  Unfinished files left in {import_dir}: {', '.join(leftover)}"))
    return 0


# ─────────────────────────── entry point ───────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(prog="kvm-backup", description="Full backup and migration of KVM virtual machines.")
    ap.add_argument("-V", "--version", action="version", version=f"kvm-backup {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("backup", help="create a VM backup")
    b.add_argument("vm", nargs="?")
    b.add_argument("-y", "--yes", action="store_true", help="do not ask questions")
    b.add_argument("--no-checksum", action="store_true", help="skip SHA-256 checksums of disks")
    r = sub.add_parser("restore", help="restore a VM from an archive")
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
                            lambda x: {"backup": "Back up a VM",
                                       "restore": "Restore a VM"}[x])
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
