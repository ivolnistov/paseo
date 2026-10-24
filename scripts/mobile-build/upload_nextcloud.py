"""Copy one built APK and its provenance into a local Nextcloud sync folder. See scripts/upload-nextcloud.sh.

Writes three artifact-specific files into <root>/<subdir>/: <apk>, <apk>.build-manifest.json (byte copy of the
manifest) and <apk>.sha256. It never replaces a file: identical files are left alone, a differing one stops the
whole upload before anything is written; to stage a different receipt, choose a distinct destination.

Concurrency and paths:
- One upload per root at a time: the root directory is opened once and held with a non-blocking flock for the whole
  run, including the final re-check; a second uploader exits 3. The OS releases the lock when the process ends.
- Components below the root are opened one by one with O_NOFOLLOW and created if missing. All reads, temp files,
  links and unlinks go through the opened directory's descriptor, so a component swapped for a symlink after it was
  opened cannot redirect a write. Trust boundary: the root is the caller's own folder; its path is resolved once,
  when it is opened, and replacing the root itself is outside this check.

Each file goes to a temp name and is hard-linked into place, never over an existing file. The run records each temp
and target with its inode before linking. On an error or an interrupt (Ctrl-C) it removes only files that still
carry this run's inodes, so a file that another process put in place is never removed, and then the interrupt is
re-raised. SIGKILL or power loss cannot be caught and may leave a temp file. After writing, the whole set is
checked again; a mismatch exits 3.

The manifest's source commit is metadata the build recorded; it is checked against the manifest, not recovered from
the APK. This is a local copy only; the Nextcloud client syncs it, and a successful copy does not prove remote sync.

Exit codes: 0 ok, 2 usage, 3 precondition or write failure, 4 verification failure, 5 refused overwrite.
"""
import argparse
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
import sys

DEFAULT_ROOT = "/Users/ivolnistov/Library/CloudStorage/Nextcloud-cloud.volnistov.ru-IlyaVolnistov"
DEFAULT_SUBDIR = "Paseo/mobile"
SCHEMA = "paseo-mobile-build-manifest-v1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Stop(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def check_manifest(apk, manifest_bytes):
    try:
        m = json.loads(manifest_bytes)
    except ValueError as e:
        raise Stop(4, f"manifest is not JSON: {e}")
    if not isinstance(m, dict):
        raise Stop(4, "manifest rejected: the document is not a JSON object")
    art = m.get("artifact") or {}
    src = m.get("source") or {}
    if not isinstance(art, dict) or not isinstance(src, dict):
        raise Stop(4, "manifest rejected: artifact and source must be JSON objects")
    data = apk.read_bytes()
    problems = []
    if m.get("schema") != SCHEMA:
        problems.append(f"schema is not {SCHEMA}")
    if art.get("file") != apk.name:
        problems.append("artifact.file does not name this APK")
    if not HEX64.match(str(art.get("sha256", ""))) or art.get("sha256") != sha256_bytes(data):
        problems.append("artifact.sha256 does not match the APK")
    if art.get("size") != len(data):
        problems.append("artifact.size does not match the APK")
    if not HEX40.match(str(src.get("sha", ""))):
        problems.append("source.sha missing or not a full commit SHA")
    for key in ("version", "variant", "package"):
        if not m.get(key):
            problems.append(f"{key} missing")
    if problems:
        raise Stop(4, "manifest rejected: " + "; ".join(problems))
    return m, data


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY


def parse_subdir(subdir):
    rel = pathlib.PurePosixPath(subdir)
    if not subdir or rel.is_absolute() or any(part in ("..", ".", "") for part in rel.parts):
        raise Stop(2, f"--subdir must be a relative path inside the root: {subdir!r}")
    return rel


class DestDir:
    """The destination directory, held by descriptor. fd is None when a dry run found it missing."""

    def __init__(self, fd, path):
        self.fd = fd
        self.path = path

    def read(self, name):
        """Bytes of an existing regular file, or None when it does not exist. Never follows a symlink."""
        if self.fd is None:
            return None
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise Stop(5, f"{self.path}/{name} is a symlink; not replaced")
            raise
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise Stop(5, f"{self.path}/{name} exists and is not a regular file; not replaced")
            return f.read()

    def create_temp(self, name, data):
        """Write data to a new temp file next to name; returns (temp name, inode). Removes it if writing fails."""
        temp = f".{name}.tmp-{os.getpid()}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as f:
                ino = os.fstat(f.fileno()).st_ino
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            os.unlink(temp, dir_fd=self.fd)
            raise
        return temp, ino

    def link(self, temp, name):
        os.link(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)  # fails if name exists: never replaces

    def unlink_if_inode(self, name, ino):
        try:
            st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if st.st_ino == ino and stat.S_ISREG(st.st_mode):
            os.unlink(name, dir_fd=self.fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def open_destination(root, rel, create):
    """Open <root>/<rel> component by component without following symlinks; root is a directory fd or a path.

    create=False (dry run) returns a DestDir with fd None when a component is missing, and creates nothing.
    """
    try:
        fd = os.dup(root) if isinstance(root, int) else os.open(root, DIR_FLAGS)
    except OSError as e:
        raise Stop(3, f"cannot open root {root}: {e}")
    path = "<root>" if isinstance(root, int) else str(root)
    try:
        for part in rel.parts:
            path = f"{path}/{part}"
            try:
                child = os.open(part, DIR_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return DestDir(None, path)
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass  # created concurrently; accepted below only as a real directory
                except OSError as e:
                    raise Stop(3, f"cannot create {path}: {e}")
                try:
                    child = os.open(part, DIR_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as e:
                    raise Stop(3, f"{path} is not a real directory under the root: {e}")
            except OSError as e:
                raise Stop(3, f"{path} is not a real directory under the root: {e}")
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return DestDir(fd, path)


def lock_root(root):
    """Open the root once and hold an exclusive flock on it for the run; returns the descriptor."""
    try:
        fd = os.open(root, DIR_FLAGS)
    except OSError as e:
        raise Stop(3, f"root {root} does not exist or is not a directory; not created ({e})")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Stop(3, f"another upload into {root} is running; try again after it finishes")
    return fd


def planned_set(apk_name, manifest_bytes, data):
    sidecar = f"{sha256_bytes(data)}  {apk_name}\n".encode()
    return [(apk_name, data), (f"{apk_name}.build-manifest.json", manifest_bytes), (f"{apk_name}.sha256", sidecar)]


def plan_files(dest, files):
    """The (name, bytes) pairs still to write. An identical file is skipped; a differing one stops everything."""
    todo = []
    for name, content in files:
        existing = dest.read(name)
        if existing is None:
            todo.append((name, content))
        elif existing != content:
            raise Stop(5, f"{dest.path}/{name} exists with different content; not replaced (nothing written)")
    return todo


def write_all(todo, dest):
    """Place every (name, bytes) pair through dest. On any failure remove only files carrying this run's inodes."""
    created = []
    try:
        for name, content in todo:
            temp, ino = dest.create_temp(name, content)
            created.append((temp, ino))
            created.append((name, ino))  # recorded before the link, so an interrupt right after it is cleaned up
            dest.link(temp, name)
            dest.unlink_if_inode(temp, ino)
            created.remove((temp, ino))
            if dest.read(name) != content:
                raise Stop(3, f"{dest.path}/{name} does not match after placing")
    except BaseException as e:  # includes KeyboardInterrupt; SIGKILL never reaches here
        for name, ino in reversed(created):
            try:
                dest.unlink_if_inode(name, ino)
            except OSError:
                pass
        if isinstance(e, Stop):
            raise
        if isinstance(e, OSError):
            raise Stop(3, f"write failed; this run's files were removed: {e}")
        raise
    return [name for name, _ in todo]


def main(argv, *, destination=open_destination):
    p = argparse.ArgumentParser(prog="upload-nextcloud.sh", description=__doc__.split("\n\n")[0])
    p.add_argument("--apk", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--subdir", default=DEFAULT_SUBDIR)
    p.add_argument("--dry-run", action="store_true")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    root_fd = None
    dest = None
    try:
        apk = pathlib.Path(args.apk)
        if not apk.name.lower().endswith(".apk"):
            raise Stop(4, f"only APK artifacts are uploaded; {apk.name} is not an .apk file")
        if not apk.is_file():
            raise Stop(3, f"APK {apk} not found")
        try:
            manifest_bytes = pathlib.Path(args.manifest).read_bytes()
        except OSError as e:
            raise Stop(3, f"manifest unreadable: {e}")
        m, data = check_manifest(apk, manifest_bytes)
        rel = parse_subdir(args.subdir)
        root = os.path.abspath(args.root)
        root_fd = lock_root(root)
        dest = destination(root_fd, rel, not args.dry_run)
        files = planned_set(apk.name, manifest_bytes, data)
        todo = plan_files(dest, files)
        print(f"artifact {apk.name} sha256 {sha256_bytes(data)} source {m['source']['sha']} "
              f"version {m['version']} variant {m['variant']}")
        print(f"destination {root}/{rel}")
        for name, _ in todo:
            print(f"{'would copy' if args.dry_run else 'copy'} {name}")
        if len(todo) < len(files):
            print(f"{len(files) - len(todo)} file(s) already present with identical content")
        if args.dry_run:
            print("DRY RUN: nothing written")
            return 0
        write_all(todo, dest)
        if plan_files(dest, files):
            raise Stop(3, "the destination changed during the upload; the set is incomplete")
        print("Copied into the local Nextcloud folder. This does not prove the server has synced it; "
              "check the Nextcloud client or the web UI for remote sync.")
        return 0
    except Stop as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return e.code
    except OSError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 3
    finally:
        if dest is not None:
            dest.close()
        if root_fd is not None:
            os.close(root_fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
