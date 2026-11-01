"""Filesystem capabilities shared by the deterministic engine.

Virtual paths follow the target's conservative Windows policy even on macOS.
Managed operations walk directory descriptors, never links, rather than trusting
an earlier string containment check.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"
}


def validate_relative_path(path: str) -> str:
    """Return a slash-separated safe virtual path, or raise ValueError.

    Unicode spelling is preserved: normalization equivalence is not presumed.
    Case collisions need a collection-level check, not Unicode rewriting.
    """
    if not isinstance(path, str) or not path or len(path) > 32760:
        raise ValueError("path must be a nonempty bounded relative string")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("control characters are forbidden in paths")
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized):
        raise ValueError("absolute, device and drive paths are forbidden")
    parts = normalized.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError("empty components and traversal are forbidden")
        if part.endswith((" ", ".")) or any(char in '<>:"|?*' for char in part):
            raise ValueError("Windows aliases, streams and wildcard names are forbidden")
        if part.split(".", 1)[0].upper() in _DEVICES:
            raise ValueError("reserved Windows device name")
    return "/".join(parts)


def check_path_collisions(paths: list[str]) -> None:
    seen: dict[str, str] = {}
    for raw in paths:
        path = validate_relative_path(raw)
        key = path.casefold()
        if key in seen:
            raise ValueError(f"colliding target paths: {seen[key]!r}, {raw!r}")
        seen[key] = raw


def validate_source_relative_path(path: str) -> str:
    """Validate host source traversal without applying a Windows output policy.

    A macOS source can contain Windows-reserved names. Preserve that spelling;
    managed writes still use validate_relative_path and collision checks.
    """
    if os.name == "nt":
        return validate_relative_path(path)
    if (not isinstance(path, str) or not path or len(path) > 32760 or path.startswith("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("invalid host-relative source path")
    return path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def paths_overlap(first: Path, second: Path) -> bool:
    """Compare containment using host identities, including case aliases."""
    first, second = Path(first).absolute(), Path(second).absolute()
    if _inside(first, second) or _inside(second, first):
        return True
    for path, root in ((first, second), (second, first)):
        if not root.exists():
            continue
        for parent in (path, *path.parents):
            if parent.exists() and parent.samefile(root):
                return True
    return False


def reject_link_components(path: Path) -> Path:
    """Reject links/reparse points before a managed root gains write authority."""
    absolute = Path(path).absolute()
    for component in reversed((absolute, *absolute.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"managed path contains a link/reparse point: {component}")
    return absolute


class ManagedFS:
    """Bounded writes within one managed tree and explicit source snapshots."""

    def __init__(self, root: Path, sources: list[Path] | None = None,
                 max_bytes: int = 32 * 1024 * 1024):
        supplied = reject_link_components(root)
        if supplied.is_symlink():
            raise ValueError("managed root cannot be a symlink")
        self.root = supplied.resolve()
        self.sources = [Path(source).resolve() for source in (sources or [])]
        if any(_inside(self.root, source) or _inside(source, self.root)
               for source in self.sources):
            raise ValueError("managed and source roots cannot overlap")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError("managed root must be a directory")
        info = self.root.stat()
        self._identity = (info.st_dev, info.st_ino)
        self.max_bytes = max_bytes

    def resolve(self, path: str) -> Path:
        normalized = validate_relative_path(path)
        info = self.root.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or
                (info.st_dev, info.st_ino) != self._identity):
            raise ValueError("managed root changed or became a link/reparse point")
        target = self.root.joinpath(*normalized.split("/"))
        current = self.root
        for part in normalized.split("/"):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("links/reparse points are forbidden in managed paths")
        return target

    @contextmanager
    def _parent(self, path: str, create: bool = False) -> Iterator[tuple[int, str]]:
        normalized = validate_relative_path(path)
        parts = normalized.split("/")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        # Windows lacks dir_fd operations. A private, link-free managed tree is
        # required there; native junction qualification remains a release gate.
        if os.name == "nt":
            self._windows_parent(normalized, create)
            yield -1, normalized
            return
        fd = os.open(self.root, flags)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self._identity:
                raise ValueError("managed root changed")
            for part in parts[:-1]:
                self._alias_check(fd, part)
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            self._alias_check(fd, parts[-1])
            yield fd, parts[-1]
        except FileExistsError:
            raise
        except OSError as exc:
            raise ValueError(f"unsafe or unavailable managed path: {exc.strerror}") from exc
        finally:
            os.close(fd)

    @staticmethod
    def _alias_check(fd: int, name: str) -> None:
        for existing in os.listdir(fd):
            if existing != name and existing.casefold() == name.casefold():
                raise ValueError("case-aliased managed path")

    def _windows_parent(self, normalized: str, create: bool) -> None:
        self.resolve(normalized)
        current = self.root
        parts = normalized.split("/")
        for index, part in enumerate(parts):
            if current.exists():
                for entry in current.iterdir():
                    if entry.name != part and entry.name.casefold() == part.casefold():
                        raise ValueError("case-aliased managed path")
            current = current / part
            if index == len(parts) - 1:
                break
            if current.exists():
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Windows reparse points are forbidden")
            elif create:
                current.mkdir(mode=0o700)
        self.resolve(normalized)

    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes:
        limit = self.max_bytes if max_bytes is None else min(max_bytes, self.max_bytes)
        with self._parent(path) as (fd, name):
            target = self.root / name if fd == -1 else name
            handle = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
                             **({} if fd == -1 else {"dir_fd": fd}))
            try:
                info = os.fstat(handle)
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise ValueError("only bounded regular files may be read")
                with os.fdopen(handle, "rb", closefd=False) as stream:
                    data = _read_bounded(stream, limit)
                return data
            finally:
                os.close(handle)

    def write_bytes(self, path: str, data: bytes, *, replace: bool = True) -> Path:
        if not isinstance(data, bytes) or len(data) > self.max_bytes:
            raise ValueError("write must contain bounded bytes")
        with self._parent(path, create=True) as (fd, name):
            if fd == -1:
                target = self.resolve(name)
                if target.exists() and not replace:
                    raise FileExistsError(target)
                temporary = target.with_name(f".tmp-{uuid.uuid4().hex}")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if target.is_symlink():
                        raise ValueError("destination became a link")
                    if not replace and target.exists():
                        raise FileExistsError(target)
                    if replace:
                        os.replace(temporary, target)
                    else:
                        # Windows rename refuses an existing target atomically.
                        os.rename(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                return target
            try:
                existing = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(existing.st_mode):
                    raise ValueError("destination is not a regular file")
                if not replace:
                    raise FileExistsError(name)
            except FileNotFoundError:
                pass
            temporary = f".tmp-{uuid.uuid4().hex}"
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=fd)
            try:
                with os.fdopen(handle, "wb", closefd=False) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if replace:
                    os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                else:
                    os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd,
                            follow_symlinks=False)
                    os.unlink(temporary, dir_fd=fd)
                os.fsync(fd)
            finally:
                os.close(handle)
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
        return self.resolve(path)

    def snapshot(self, source: Path, destination: str) -> dict:
        source = Path(source).absolute()
        matched = next((root for root in self.sources if _inside(source, root)), None)
        if matched is None:
            raise ValueError("source is outside configured read roots")
        relative = source.relative_to(matched).as_posix()
        # A read-only view reuses descriptor containment without granting writes
        # to callers. This helper is the sole operation performed on this view.
        reader = ManagedFS.__new__(ManagedFS)
        reader.root, reader.sources, reader.max_bytes = matched, [], self.max_bytes
        info = matched.stat()
        reader._identity = (info.st_dev, info.st_ino)
        first = reader.read_bytes(relative)
        second = reader.read_bytes(relative)
        if first != second:
            raise ValueError("source changed during snapshot")
        self.write_bytes(destination, first, replace=False)
        return {"path": destination, "sha256": sha256_bytes(first), "size": len(first)}

    def unlink(self, path: str) -> None:
        with self._parent(path) as (fd, name):
            target = self.root / name if fd == -1 else name
            options = {} if fd == -1 else {"dir_fd": fd}
            info = os.stat(target, follow_symlinks=False, **options)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("only a managed regular file can be removed")
            os.unlink(target, **options)
            if fd != -1:
                os.fsync(fd)


def _read_bounded(stream, limit: int) -> bytes:
    # A byte ceiling can be terabytes for streamed backups. Never ask the CRT
    # to allocate that ceiling just to read a tiny manifest or journal.
    chunks, size = [], 0
    while chunk := stream.read(min(1024 * 1024, limit - size + 1)):
        size += len(chunk)
        if size > limit:
            raise ValueError("read budget exceeded")
        chunks.append(chunk)
    return b"".join(chunks)


def read_source_bytes(root: Path, relative: str, max_bytes: int = 32 * 1024 * 1024,
                      *, native_paths: bool = False) -> bytes:
    """Read one source capability without creating or changing source content."""
    root = Path(root).absolute()
    if native_paths:
        with open_source_stream(root, relative, native_paths=True) as stream:
            if os.fstat(stream.fileno()).st_size > max_bytes:
                raise ValueError("source exceeds bounded regular-file policy")
            return _read_bounded(stream, max_bytes)
    relative = validate_relative_path(relative)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source root must be an existing non-link directory")
    if getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("Windows source root cannot be a reparse point")
    if os.name == "nt":
        return _read_windows_source(root, relative, max_bytes)
    reader = ManagedFS.__new__(ManagedFS)
    reader.root, reader.sources, reader.max_bytes = root, [], max_bytes
    info = root.stat()
    reader._identity = (info.st_dev, info.st_ino)
    return reader.read_bytes(relative)


def _read_windows_source(root: Path, relative: str, max_bytes: int) -> bytes:
    descriptor = _windows_source_descriptor(root, relative)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("source exceeds bounded regular-file policy")
        return _read_bounded(stream, max_bytes)


def _windows_source_descriptor(root: Path, relative: str) -> int:
    """Verify final opened-handle identity before reading; never trust path checks."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    attribute_info = kernel.GetFileInformationByHandleEx
    attribute_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    attribute_info.restype = wintypes.BOOL

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    invalid = ctypes.c_void_p(-1).value

    def opened(path: Path, directory: bool = False):
        handle = create(str(path), 0x80000000, 0x7, None, 3,
                        0x00200000 | (0x02000000 if directory else 0), None)
        if handle == invalid:
            raise ValueError("cannot safely open Windows source")
        attributes = FileAttributeTagInfo()
        # FileAttributeTagInfo (9) describes the opened object. A final link
        # opened with OPEN_REPARSE_POINT can retain the expected path spelling.
        if not attribute_info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            close(handle)
            raise ValueError("cannot verify opened Windows source attributes")
        if attributes.FileAttributes & 0x400:
            close(handle)
            raise ValueError("Windows source links/reparse points are forbidden")
        buffer = ctypes.create_unicode_buffer(32768)
        count = final_path(handle, buffer, len(buffer), 0)
        if not count or count >= len(buffer):
            close(handle)
            raise ValueError("cannot establish opened Windows path containment")
        return handle, buffer.value.rstrip("\\")

    root_handle, root_name = opened(root, True)
    file_handle = None
    try:
        file_handle, actual = opened(root.joinpath(*relative.split("/")))
        expected = root_name + "\\" + relative.replace("/", "\\")
        if actual.casefold() != expected.casefold():
            raise ValueError("opened Windows source escaped or followed a reparse path")
        descriptor = msvcrt.open_osfhandle(file_handle, os.O_RDONLY | os.O_BINARY)
        file_handle = None  # descriptor now owns the native handle
        return descriptor
    finally:
        close(root_handle)
        if file_handle is not None:
            close(file_handle)


def _posix_source_descriptor(root: Path, relative: str) -> int:
    """Anchor each source component without case folding or following links."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent = os.open(root.anchor, flags)
    try:
        parts = [*root.parts[1:], *relative.split("/")]
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


@contextmanager
def open_source_stream(root: Path, relative: str, *, native_paths: bool = False):
    """Open an anchored regular source without following directory/file links."""
    root = Path(root).absolute()
    relative = validate_source_relative_path(relative) if native_paths else validate_relative_path(relative)
    if not root.is_dir() or root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("source root must be a real non-reparse directory")
    if os.name == "nt":
        descriptor = _windows_source_descriptor(root, relative)
    elif native_paths:
        descriptor = _posix_source_descriptor(root, relative)
    else:
        reader = ManagedFS.__new__(ManagedFS)
        reader.root, reader.sources, reader.max_bytes = root, [], 2 ** 42
        info = root.stat()
        reader._identity = (info.st_dev, info.st_ino)
        with reader._parent(relative) as (fd, name):
            descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("source is not a regular file")
        yield stream


def hash_source_file(root: Path, relative: str, *, max_bytes: int = 2 ** 42,
                     native_paths: bool = False) -> dict:
    """Stream an exact stable source hash without loading large game assets."""
    digest, length = hashlib.sha256(), 0
    for chunk in iter_source_file(root, relative, max_bytes=max_bytes, native_paths=native_paths):
        digest.update(chunk)
        length += len(chunk)
    validated = validate_source_relative_path(relative) if native_paths else validate_relative_path(relative)
    return {"path": validated, "sha256": digest.hexdigest(), "size": length}


def iter_source_file(root: Path, relative: str, *, chunk_size: int = 1024 * 1024,
                     max_bytes: int = 2 ** 42, native_paths: bool = False) -> Iterator[bytes]:
    """Bounded chunks from one stable source, including pathname replacement checks."""
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or not 1 <= chunk_size <= 8 * 1024 * 1024:
        raise ValueError("source chunk size is outside bounds")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("source byte budget must be nonnegative")
    with open_source_stream(root, relative, native_paths=native_paths) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError("hash input is not a bounded regular source file")
        length = 0
        while chunk := stream.read(chunk_size):
            length += len(chunk)
            if length > max_bytes:
                raise ValueError("source hash byte budget exceeded")
            yield chunk
        after = os.fstat(stream.fileno())
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or length != before.st_size:
            raise ValueError("source changed while hashing")
        # Workshop updates can replace a filename while the original open
        # handle remains stable. The final name must still identify that file.
        with open_source_stream(root, relative, native_paths=native_paths) as current:
            if identity(before) != identity(os.fstat(current.fileno())):
                raise ValueError("source path changed during capture")


class ManagedLock:
    """OS-backed cross-process lock; lock ownership survives no crash."""

    def __init__(self, path: Path, timeout: float = 0):
        self.path, self.timeout, self._stream = Path(path), timeout, None

    def __enter__(self):
        reject_link_components(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
        self._stream = os.fdopen(descriptor, "r+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._stream.seek(0)
                    if self.path.stat().st_size == 0:
                        self._stream.write(b"0")
                        self._stream.flush()
                    self._stream.seek(0)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise ValueError("managed target is locked by another operation")
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))

    def __exit__(self, *args):
        if self._stream is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._stream.seek(0)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            finally:
                self._stream.close()
                self._stream = None
