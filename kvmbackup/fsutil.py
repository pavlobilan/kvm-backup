"""File operations: hashing, copying with progress, access rights."""
import grp
import hashlib
import os
import pwd
import shutil

from . import config
from .virt import run

CHUNK = 8 << 20


def sha256_file(path, cb=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            if cb:
                cb(len(b))
    return h.hexdigest()


def copy_sparse(src, dst, cb=None):
    """Copy with progress; zero blocks are skipped so the file stays sparse."""
    zero = bytes(CHUNK)
    size = os.path.getsize(src)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            if b == zero[:len(b)]:
                fo.seek(len(b), os.SEEK_CUR)
            else:
                fo.write(b)
            if cb:
                cb(len(b))
        fo.truncate(size)
    shutil.copystat(src, dst)


def existing_dir(p):
    """Nearest existing directory for a path that may not exist yet."""
    while not os.path.exists(p):
        p = os.path.dirname(p)
    return p


def same_fs(a, b):
    return os.stat(existing_dir(a)).st_dev == os.stat(existing_dir(b)).st_dev


def free_bytes(path):
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return shutil.disk_usage(path).free


def owner_names(path):
    st = os.stat(path)
    try:
        u = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        u = str(st.st_uid)
    try:
        g = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        g = str(st.st_gid)
    return u, g


def chown_names(path, user, group, recursive=False):
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        uid = -1
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        gid = -1
    if uid == -1 and gid == -1:
        return False
    paths = [path]
    if recursive and os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            paths += [os.path.join(root, x) for x in dirs + files]
    for p in paths:
        os.lchown(p, uid, gid)
    return True


def transfer_gid():
    try:
        return grp.getgrnam(config.get()["transfer_group"]).gr_gid
    except KeyError:
        return None


def grant_read_acl(files):
    """Give the transfer group read access to disk files and traverse access to their directories. Returns the changed paths."""
    group = config.get()["transfer_group"]
    if transfer_gid() is None:
        return [], f"Group {group} does not exist; WinSCP access was not granted."
    changed, dirs = [], set()
    for f in files:
        d = os.path.dirname(f)
        while d and d != "/":
            dirs.add(d)
            d = os.path.dirname(d)
    # Add an ACL only where "others" lack the needed permission.
    for d in sorted(dirs):
        if not os.stat(d).st_mode & 0o001:
            run(["setfacl", "-m", f"g:{group}:x", d])
            changed.append(d)
    for f in files:
        if not os.stat(f).st_mode & 0o004:
            run(["setfacl", "-m", f"g:{group}:r", f])
            changed.append(f)
    return changed, None


def revoke_acl(paths):
    group = config.get()["transfer_group"]
    errors = []
    for p in paths:
        if os.path.exists(p):
            r = run(["setfacl", "-x", f"g:{group}", p], check=False)
            if r.returncode != 0:
                errors.append(f"{p}: {r.stderr.strip()}")
    return errors
